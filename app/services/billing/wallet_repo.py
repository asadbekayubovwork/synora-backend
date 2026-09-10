"""The only module that mutates a wallet balance.

Everything that moves credit — a session hold, a usage debit, a top-up, an
admin grant, a bonus expiry — comes through here, so that

    wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)

holds because the wallet UPDATE and its ledger INSERTs are written in one
transaction by one piece of code, rather than because everyone remembered to.
`tests/test_billing_invariants.py::test_only_wallet_repo_mutates_balances`
walks the source tree to keep the "one piece of code" half true.

Three things in here are load-bearing and easy to undo by accident:

1. **Balances are read as column tuples, never as `select(Wallet)`.** A loaded
   ORM instance enters the identity map, and because the sessionmaker is
   configured `expire_on_commit=False` it would keep its pre-UPDATE numbers for
   the rest of the request — a stale balance sitting there waiting to be read
   by mistake. A column tuple puts nothing in the map, so there is nothing to
   go stale.

2. **Debits are compare-and-swap, not `SELECT ... FOR UPDATE`.** Partly because
   `for_update()` is a silent no-op on SQLite, so the locking would be untested
   in CI and unexercised in the current single-worker deploy. But mainly
   because the bonus/paid split has to be known to write the ledger, and
   `UPDATE ... RETURNING <expr>` evaluates the expression against the *new*
   row: `RETURNING CASE WHEN bonus_micros < :amt THEN ...` silently returns the
   split computed from the already-decremented bonus. Postgres 14 has no
   `RETURNING OLD.*`, and the CTE form that does work there is rejected by
   SQLite. CAS runs identically on both, so the statement CI exercises is the
   statement production runs.

3. **Every bulk UPDATE sets `updated_at` by hand.** `Base.updated_at` uses
   `onupdate=utcnow`, which SQLAlchemy applies in Python and therefore does not
   apply to `update(...).values(...)`. Forget it and the column silently
   freezes at row-creation time.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PaymentRequiredError,
)
from app.db.base import as_utc, utcnow
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType
from app.models.ledger import LedgerEntry
from app.models.wallet import Wallet

logger = logging.getLogger("synora.billing")

# Each miss costs one extra read plus one extra write. Five is far more than a
# single wallet's real concurrency — a user has a handful of sessions, not
# hundreds — so exceeding it means something is wrong rather than busy.
MAX_CAS_ATTEMPTS = 5

# How much room a ledger idempotency key has, read off the mapped column rather
# than written down as a number — the same discipline, and for the same reason,
# as `session_service.IDEMPOTENCY_KEY_MAX_LENGTH`.
#
# Every key this module writes is composed: `hold:{uuid}`, `usage:{uuid}`,
# `admin:{whatever the caller sent}`. The internal ones are bounded by their own
# shape and cannot outgrow the column, but a caller-supplied one is bounded only
# by whatever the route that accepts it publishes, and that number has to be
# derived from this column or it is a copy waiting to drift from it. It drifted
# once already on `ai_sessions.idempotency_key`, and no test running on SQLite
# can ever be the thing that notices: Postgres answers an over-long value with
# 22001 and a 500, SQLite ignores the declared width and stores the whole
# string, so the suite stays green while the deployed engine refuses the write.
LEDGER_IDEMPOTENCY_KEY_MAX_LENGTH: int = LedgerEntry.__table__.c.idempotency_key.type.length

# Which bucket each kind moves, so a caller cannot pair `HOLD` with `paid`.
_KIND_BUCKETS: dict[LedgerEntryKind, frozenset[LedgerBucket]] = {
    LedgerEntryKind.TOPUP: frozenset({LedgerBucket.PAID}),
    LedgerEntryKind.BONUS_GRANT: frozenset({LedgerBucket.BONUS}),
    LedgerEntryKind.DEBIT: frozenset({LedgerBucket.PAID, LedgerBucket.BONUS}),
    LedgerEntryKind.REFUND: frozenset({LedgerBucket.PAID, LedgerBucket.BONUS}),
    LedgerEntryKind.REVERSAL: frozenset({LedgerBucket.PAID, LedgerBucket.BONUS}),
    LedgerEntryKind.ADJUSTMENT: frozenset({LedgerBucket.PAID, LedgerBucket.BONUS}),
    LedgerEntryKind.EXPIRY: frozenset({LedgerBucket.BONUS}),
    LedgerEntryKind.HOLD: frozenset({LedgerBucket.RESERVED}),
    LedgerEntryKind.RELEASE: frozenset({LedgerBucket.RESERVED}),
}


# --- value objects ---------------------------------------------------------


@dataclass(frozen=True)
class WalletSnapshot:
    """A wallet's numbers at one instant, with no ORM identity behind them."""

    wallet_id: uuid.UUID
    user_id: uuid.UUID
    paid_micros: int
    bonus_micros: int
    bonus_expires_at: datetime | None
    reserved_micros: int
    version: int
    frozen_at: datetime | None
    low_balance_threshold_micros: int

    @property
    def is_frozen(self) -> bool:
        return self.frozen_at is not None

    @property
    def effective_bonus_micros(self) -> int:
        """Bonus that still counts.

        Expired bonus reads as zero from the instant it expires, not from
        whenever a sweeper next runs. `debit` then writes the `expiry` ledger
        entry lazily, which is what keeps the wallet matching its ledger
        without depending on a background job having fired.
        """
        if self.bonus_micros <= 0:
            return 0
        if self.bonus_expires_at is None:
            return self.bonus_micros
        return self.bonus_micros if as_utc(self.bonus_expires_at) > utcnow() else 0

    @property
    def bonus_has_expired(self) -> bool:
        return self.bonus_micros > 0 and self.effective_bonus_micros == 0

    @property
    def available_micros(self) -> int:
        return self.paid_micros + self.effective_bonus_micros - self.reserved_micros


@dataclass(frozen=True)
class Split:
    from_bonus: int
    from_paid: int

    @property
    def total(self) -> int:
        return self.from_bonus + self.from_paid


@dataclass(frozen=True)
class WalletMovement:
    """What one call to this module did, and where the wallet ended up."""

    wallet_id: uuid.UUID
    user_id: uuid.UUID
    group_id: uuid.UUID
    kind: LedgerEntryKind
    # Deltas applied to each bucket, with the same sign rule the ledger uses:
    # negative took credit out, positive put it in. So a debit has negative
    # paid/bonus deltas and a top-up has a positive one.
    paid_delta_micros: int
    bonus_delta_micros: int
    reserved_delta_micros: int
    writeoff_micros: int
    paid_micros: int
    bonus_micros: int
    reserved_micros: int
    available_micros: int
    version: int
    replayed: bool
    cas_attempts: int

    @property
    def charged_micros(self) -> int:
        """What the user actually paid. Zero or negative for a credit."""
        return -(self.paid_delta_micros + self.bonus_delta_micros)


def split_bonus_first(amount_micros: int, effective_bonus_micros: int) -> Split:
    """Bonus goes first, because it is the part that can expire.

    Pure, so the boundaries — no bonus, exactly enough bonus, more bonus than
    needed — are tested without a database anywhere near them.
    """
    if amount_micros < 0:
        raise ValueError("amount_micros must not be negative")
    from_bonus = min(amount_micros, max(0, effective_bonus_micros))
    return Split(from_bonus=from_bonus, from_paid=amount_micros - from_bonus)


# --- reads -----------------------------------------------------------------

_SNAPSHOT_COLUMNS = (
    Wallet.id,
    Wallet.user_id,
    Wallet.paid_micros,
    Wallet.bonus_micros,
    Wallet.bonus_expires_at,
    Wallet.reserved_micros,
    Wallet.version,
    Wallet.frozen_at,
    Wallet.low_balance_threshold_micros,
)


async def snapshot_by_id(session: AsyncSession, wallet_id: uuid.UUID) -> WalletSnapshot:
    row = (
        await session.execute(select(*_SNAPSHOT_COLUMNS).where(Wallet.id == wallet_id))
    ).one_or_none()
    if row is None:
        raise NotFoundError("This wallet does not exist.", code="wallet_not_found")
    return WalletSnapshot(*row)


async def snapshot_by_user(session: AsyncSession, user_id: uuid.UUID) -> WalletSnapshot:
    row = (
        await session.execute(select(*_SNAPSHOT_COLUMNS).where(Wallet.user_id == user_id))
    ).one_or_none()
    if row is None:
        raise NotFoundError("This account has no wallet.", code="wallet_not_found")
    return WalletSnapshot(*row)


def require_unfrozen(snapshot: WalletSnapshot) -> None:
    if snapshot.is_frozen:
        raise ForbiddenError(
            "This wallet is on hold. Please contact support.",
            code="wallet_frozen",
        )


# --- the atomic write ------------------------------------------------------


class _Unset:
    """Distinguishes "leave the expiry alone" from "clear the expiry"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


async def _cas(
    session: AsyncSession,
    snapshot: WalletSnapshot,
    *,
    paid_delta: int = 0,
    bonus_delta: int = 0,
    reserved_delta: int = 0,
    spend_delta: int = 0,
    topup_delta: int = 0,
    writeoff_delta: int = 0,
    bonus_expires_at: datetime | None | _Unset = _UNSET,
    now: datetime,
) -> tuple[int, int, int, int] | None:
    """One atomic wallet mutation. Returns the new numbers, or None on a miss.

    A miss means someone else moved the row between our read and our write, so
    the caller has to re-read and recompute — the bonus/paid split may now fall
    differently, and a blind retry of the same deltas would be wrong.

    Pass `bonus_expires_at` to also set the expiry (a fresh bonus grant);
    leaving it as the sentinel leaves the column alone, which is different from
    passing `None` to clear it.
    """
    values: dict[str, object] = {
        "paid_micros": Wallet.paid_micros + paid_delta,
        "bonus_micros": Wallet.bonus_micros + bonus_delta,
        "reserved_micros": Wallet.reserved_micros + reserved_delta,
        "lifetime_spend_micros": Wallet.lifetime_spend_micros + spend_delta,
        "lifetime_topup_micros": Wallet.lifetime_topup_micros + topup_delta,
        "lifetime_writeoff_micros": Wallet.lifetime_writeoff_micros + writeoff_delta,
        "version": Wallet.version + 1,
        # See the module docstring: `onupdate` does not fire on a bulk update.
        "updated_at": now,
    }
    if bonus_expires_at is not _UNSET:
        values["bonus_expires_at"] = bonus_expires_at

    stmt = (
        update(Wallet)
        .where(
            Wallet.id == snapshot.wallet_id,
            # The compare half of compare-and-swap. On its own this is enough
            # for correctness.
            Wallet.version == snapshot.version,
            # Belt and braces. If a future code path ever computes a delta
            # wrongly, or forgets to bump the version, these turn a silent
            # overdraw into a clean rowcount-0 decline instead of relying on
            # the CHECK constraints to raise.
            Wallet.paid_micros + paid_delta >= 0,
            Wallet.bonus_micros + bonus_delta >= 0,
            Wallet.reserved_micros + reserved_delta >= 0,
        )
        .values(**values)
        .returning(
            Wallet.paid_micros,
            Wallet.bonus_micros,
            Wallet.reserved_micros,
            Wallet.version,
        )
        # Nothing of this wallet is in the identity map, so there is nothing to
        # synchronise and no extra SELECT to pay for.
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    return None if row is None else (row[0], row[1], row[2], row[3])


# --- ledger writing --------------------------------------------------------


def _ledger_rows(
    *,
    snapshot: WalletSnapshot,
    group_id: uuid.UUID,
    kind: LedgerEntryKind,
    amounts: dict[LedgerBucket, int],
    after: tuple[int, int, int],
    version: int,
    ref_type: LedgerRefType,
    topup_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    ai_session_id: uuid.UUID | None = None,
    usage_event_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    note: str | None = None,
) -> list[LedgerEntry]:
    """One row per bucket that actually moved.

    Zero-amount rows are skipped rather than written, because
    `ck_ledger_entries_amount_nonzero` refuses them — a movement of nothing is
    not a movement, and letting them in would make every SUM query need a
    filter.

    All rows share `group_id`, `wallet_version` and `balance_after_*`: they
    were produced by a single wallet UPDATE, so they describe the same
    resulting state. A statement replay therefore has to group by `group_id`
    and apply the group's total, not walk row by row.
    """
    allowed = _KIND_BUCKETS[kind]
    paid_after, bonus_after, reserved_after = after

    rows: list[LedgerEntry] = []
    for bucket, amount in amounts.items():
        if amount == 0:
            continue
        if bucket not in allowed:
            raise ValueError(f"{kind.value} may not move the {bucket.value} bucket")
        rows.append(
            LedgerEntry(
                wallet_id=snapshot.wallet_id,
                user_id=snapshot.user_id,
                kind=kind,
                bucket=bucket,
                amount_micros=amount,
                balance_after_paid_micros=paid_after,
                balance_after_bonus_micros=bonus_after,
                balance_after_reserved_micros=reserved_after,
                wallet_version=version,
                group_id=group_id,
                ref_type=ref_type,
                topup_id=topup_id,
                payment_id=payment_id,
                ai_session_id=ai_session_id,
                usage_event_id=usage_event_id,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                note=note,
            )
        )
    return rows


async def _flush_ledger(session: AsyncSession, rows: list[LedgerEntry]) -> bool:
    """Insert the rows now. False means the idempotency key already existed.

    The flush is deliberate. The sessionmaker is `autoflush=False`, so without
    it nothing would reach the database until commit — where a unique violation
    is an unhandleable 500 rather than something we can recognise as a replay
    and answer properly.
    """
    session.add_all(rows)
    try:
        await session.flush()
    except IntegrityError:
        # Rolls back the wallet UPDATE too, which is the point: the losing
        # racer's whole movement is undone rather than compensated.
        await session.rollback()
        return False
    return True


async def _replay(session: AsyncSession, idempotency_key: str) -> WalletMovement | None:
    """Rebuild the answer we gave the first time this key arrived.

    Reconstructed from the ledger rather than cached, because the ledger is the
    thing that cannot lie. `available_micros` is read fresh, though: a client
    asking again wants the current balance, not a snapshot from four minutes
    ago.
    """
    rows = list(
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.idempotency_key == idempotency_key)
                .order_by(LedgerEntry.wallet_version, LedgerEntry.bucket)
            )
        ).scalars()
    )
    if not rows:
        return None

    first = rows[0]
    by_bucket = {row.bucket: row.amount_micros for row in rows}
    current = await snapshot_by_id(session, first.wallet_id)

    return WalletMovement(
        wallet_id=first.wallet_id,
        user_id=first.user_id,
        group_id=first.group_id,
        kind=first.kind,
        paid_delta_micros=by_bucket.get(LedgerBucket.PAID, 0),
        bonus_delta_micros=by_bucket.get(LedgerBucket.BONUS, 0),
        reserved_delta_micros=by_bucket.get(LedgerBucket.RESERVED, 0),
        # Write-offs move no money and so leave no ledger row. A replayed
        # answer cannot report one; the `usage_events` row is where that number
        # lives, and the caller reads it from there.
        writeoff_micros=0,
        paid_micros=first.balance_after_paid_micros,
        bonus_micros=first.balance_after_bonus_micros,
        reserved_micros=first.balance_after_reserved_micros,
        available_micros=current.available_micros,
        version=first.wallet_version,
        replayed=True,
        cas_attempts=0,
    )


# --- operations ------------------------------------------------------------


def _movement(
    snapshot: WalletSnapshot,
    *,
    group_id: uuid.UUID,
    kind: LedgerEntryKind,
    after: tuple[int, int, int, int],
    paid_delta: int = 0,
    bonus_delta: int = 0,
    reserved_delta: int = 0,
    writeoff: int = 0,
    attempts: int,
) -> WalletMovement:
    paid, bonus, reserved, version = after
    # The expiry column is unchanged by every operation except a bonus grant,
    # which passes a fresh snapshot in, so reusing it here is safe.
    effective_bonus = replace(snapshot, bonus_micros=bonus).effective_bonus_micros
    return WalletMovement(
        wallet_id=snapshot.wallet_id,
        user_id=snapshot.user_id,
        group_id=group_id,
        kind=kind,
        paid_delta_micros=paid_delta,
        bonus_delta_micros=bonus_delta,
        reserved_delta_micros=reserved_delta,
        writeoff_micros=writeoff,
        paid_micros=paid,
        bonus_micros=bonus,
        reserved_micros=reserved,
        available_micros=paid + effective_bonus - reserved,
        version=version,
        replayed=False,
        cas_attempts=attempts,
    )


async def _expire_bonus_now(
    session: AsyncSession, snapshot: WalletSnapshot, *, now: datetime
) -> bool:
    """Zero an expired bonus and record it. False if the row moved under us.

    Called from the debit path rather than only from a sweeper, so the wallet
    keeps matching its ledger the moment the bonus lapses instead of during
    whatever window the sweeper has not run in yet.
    """
    amount = snapshot.bonus_micros
    after = await _cas(
        session,
        snapshot,
        bonus_delta=-amount,
        bonus_expires_at=None,
        now=now,
    )
    if after is None:
        return False

    group_id = uuid.uuid4()
    rows = _ledger_rows(
        snapshot=snapshot,
        group_id=group_id,
        kind=LedgerEntryKind.EXPIRY,
        amounts={LedgerBucket.BONUS: -amount},
        after=after[:3],
        version=after[3],
        ref_type=LedgerRefType.BONUS_EXPIRY,
        note=f"Bonus of {amount} micros expired",
    )
    # No idempotency key, so this cannot collide; the CAS is the guard.
    session.add_all(rows)
    await session.flush()
    logger.info("Expired %s bonus micros on wallet %s", amount, snapshot.wallet_id)
    return True


async def debit(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount_micros: int,
    idempotency_key: str,
    ref_type: LedgerRefType,
    ai_session_id: uuid.UUID | None = None,
    usage_event_id: uuid.UUID | None = None,
    release_reserved_micros: int = 0,
    max_writeoff_micros: int = 0,
    note: str | None = None,
) -> WalletMovement:
    """Take `amount_micros` from the wallet, bonus bucket first.

    `release_reserved_micros` lets a settlement give back the part of its hold
    it no longer needs in the same atomic step as the charge, so there is no
    instant at which the money has moved but the hold has not shrunk.

    `max_writeoff_micros` above zero is the grace path: charge what is there,
    write off up to that much of the shortfall, and never drive a bucket
    negative. A write-off is not a debt — no money moved, so it gets no ledger
    entry, only a counter. That is why `wallets.lifetime_writeoff_micros` is
    the one denormalised total the reconciler does not check against the
    ledger.
    """
    if amount_micros < 0:
        raise BadRequestError("A debit cannot be negative.", code="billing_invalid_amount")

    replayed = await _replay(session, idempotency_key)
    if replayed is not None:
        return replayed

    now = utcnow()
    for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        snapshot = await snapshot_by_id(session, wallet_id)

        if snapshot.bonus_has_expired:
            await _expire_bonus_now(session, snapshot, now=now)
            continue

        chargeable = min(amount_micros, max(0, snapshot.available_micros))
        shortfall = amount_micros - chargeable
        if shortfall > max_writeoff_micros:
            raise PaymentRequiredError(
                "There is not enough credit for this.",
                code="insufficient_balance",
                required_micros=amount_micros,
                available_micros=snapshot.available_micros,
                shortfall_micros=shortfall,
            )

        split = split_bonus_first(chargeable, snapshot.effective_bonus_micros)
        release = min(release_reserved_micros, snapshot.reserved_micros)

        after = await _cas(
            session,
            snapshot,
            paid_delta=-split.from_paid,
            bonus_delta=-split.from_bonus,
            reserved_delta=-release,
            spend_delta=chargeable,
            writeoff_delta=shortfall,
            now=now,
        )
        if after is None:
            continue  # somebody moved it; the split may fall differently now

        group_id = uuid.uuid4()
        amounts = {
            LedgerBucket.BONUS: -split.from_bonus,
            LedgerBucket.PAID: -split.from_paid,
        }
        rows = _ledger_rows(
            snapshot=snapshot,
            group_id=group_id,
            kind=LedgerEntryKind.DEBIT,
            amounts=amounts,
            after=after[:3],
            version=after[3],
            ref_type=ref_type,
            ai_session_id=ai_session_id if usage_event_id is None else None,
            usage_event_id=usage_event_id,
            idempotency_key=idempotency_key,
            note=note,
        )
        if release:
            rows += _ledger_rows(
                snapshot=snapshot,
                group_id=group_id,
                kind=LedgerEntryKind.RELEASE,
                amounts={LedgerBucket.RESERVED: -release},
                after=after[:3],
                version=after[3],
                ref_type=ref_type,
                ai_session_id=ai_session_id,
                # Shares the debit's key. It does not collide with the rows
                # above because the unique constraint is on
                # (idempotency_key, bucket) and this one moves `reserved` —
                # which is precisely why that constraint covers the pair.
                idempotency_key=idempotency_key,
            )

        if not await _flush_ledger(session, rows):
            # The key was taken between our replay check and our insert, i.e.
            # a concurrent caller won the race. Their answer is the answer.
            existing = await _replay(session, idempotency_key)
            if existing is None:  # pragma: no cover - would mean another constraint failed
                raise ConflictError(
                    "This charge could not be recorded. Please retry.",
                    code="billing_write_conflict",
                )
            return existing

        return _movement(
            snapshot,
            group_id=group_id,
            kind=LedgerEntryKind.DEBIT,
            after=after,
            paid_delta=-split.from_paid,
            bonus_delta=-split.from_bonus,
            reserved_delta=-release,
            writeoff=shortfall,
            attempts=attempt,
        )

    raise ConflictError(
        "This wallet is being updated too often. Please retry.",
        code="wallet_busy",
    )


async def credit(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    paid_micros: int = 0,
    bonus_micros: int = 0,
    bonus_expires_at: datetime | None = None,
    kind: LedgerEntryKind,
    ref_type: LedgerRefType,
    idempotency_key: str,
    topup_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    note: str | None = None,
) -> WalletMovement:
    """Put credit in: a top-up, a bonus grant, a refund, an admin adjustment.

    A bonus grant replaces the expiry rather than merging with an existing one.
    Two bonuses with different expiry dates cannot both be represented by one
    column and one date, so the later date wins and the whole bonus balance
    rides on it. That is generous rather than stingy, and it is the trade the
    single-column design makes; per-grant expiry would need a `credit_grants`
    table and FIFO allocation on every debit.
    """
    if paid_micros < 0 or bonus_micros < 0:
        raise BadRequestError("A credit cannot be negative.", code="billing_invalid_amount")
    if paid_micros == 0 and bonus_micros == 0:
        raise BadRequestError("A credit must move something.", code="billing_invalid_amount")

    replayed = await _replay(session, idempotency_key)
    if replayed is not None:
        return replayed

    now = utcnow()
    for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        snapshot = await snapshot_by_id(session, wallet_id)

        expiry: datetime | None | _Unset = _UNSET
        if bonus_micros:
            if snapshot.bonus_has_expired:
                # Do not stack a new grant on top of a lapsed balance — clear
                # the old one first so the ledger stays truthful about which
                # micros expired and which were granted.
                await _expire_bonus_now(session, snapshot, now=now)
                continue
            existing = snapshot.bonus_expires_at
            if snapshot.bonus_micros == 0:
                # Nothing to merge with. A null expiry on an empty bonus
                # bucket means "no bonus", not "a bonus that never expires" —
                # conflating the two is how the first grant on a fresh wallet
                # ends up with no expiry at all.
                expiry = bonus_expires_at
            elif bonus_expires_at is None or existing is None:
                # One of the two never expires, and a single column cannot say
                # "half of this lapses on Tuesday". Never-expires wins, which
                # is the generous reading and the only safe one: expiring
                # credit the user was told was permanent is worse than the
                # reverse.
                expiry = None
            else:
                expiry = max(as_utc(existing), as_utc(bonus_expires_at))

        after = await _cas(
            session,
            snapshot,
            paid_delta=paid_micros,
            bonus_delta=bonus_micros,
            topup_delta=paid_micros + bonus_micros,
            bonus_expires_at=expiry,
            now=now,
        )
        if after is None:
            continue

        group_id = uuid.uuid4()
        rows = _ledger_rows(
            snapshot=snapshot,
            group_id=group_id,
            kind=kind,
            amounts={LedgerBucket.PAID: paid_micros, LedgerBucket.BONUS: bonus_micros},
            after=after[:3],
            version=after[3],
            ref_type=ref_type,
            topup_id=topup_id,
            payment_id=payment_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            note=note,
        )
        if not await _flush_ledger(session, rows):
            existing_movement = await _replay(session, idempotency_key)
            if existing_movement is None:  # pragma: no cover
                raise ConflictError(
                    "This credit could not be recorded. Please retry.",
                    code="billing_write_conflict",
                )
            return existing_movement

        return _movement(
            replace(snapshot, bonus_expires_at=expiry if expiry is not _UNSET else snapshot.bonus_expires_at),
            group_id=group_id,
            kind=kind,
            after=after,
            paid_delta=paid_micros,
            bonus_delta=bonus_micros,
            attempts=attempt,
        )

    raise ConflictError(
        "This wallet is being updated too often. Please retry.",
        code="wallet_busy",
    )


async def place_hold(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount_micros: int,
    idempotency_key: str,
    ai_session_id: uuid.UUID | None = None,
    note: str | None = None,
) -> WalletMovement:
    """Commit credit to a session that has not settled yet.

    A hold only touches `reserved_micros`, so there is no bonus/paid split to
    recover and the whole check-and-increment collapses into one statement:
    either the wallet had the headroom and the row moved, or it did not and
    `rowcount` is zero. No read, no CAS loop, no lock, one round trip — and
    correct on both dialects.
    """
    if amount_micros < 0:
        raise BadRequestError("A hold cannot be negative.", code="billing_invalid_amount")

    replayed = await _replay(session, idempotency_key)
    if replayed is not None:
        return replayed

    snapshot = await snapshot_by_id(session, wallet_id)
    require_unfrozen(snapshot)

    if amount_micros == 0:
        # A one-shot call needs no hold: nothing happens between the check and
        # the charge. Report the current position without touching the row.
        return _movement(
            snapshot,
            group_id=uuid.uuid4(),
            kind=LedgerEntryKind.HOLD,
            after=(
                snapshot.paid_micros,
                snapshot.bonus_micros,
                snapshot.reserved_micros,
                snapshot.version,
            ),
            attempts=0,
        )

    now = utcnow()
    if snapshot.bonus_has_expired:
        # Must happen first: the predicate below reads the raw bonus column,
        # so a lapsed bonus would otherwise count as spendable headroom.
        # Expressing "unexpired" in SQL portably would mean passing `now` into
        # the statement and comparing there, which buys nothing — the debit
        # path already has to handle expiry, so it lives in one place.
        await _expire_bonus_now(session, snapshot, now=now)
        snapshot = await snapshot_by_id(session, wallet_id)

    stmt = (
        update(Wallet)
        .where(
            Wallet.id == wallet_id,
            Wallet.frozen_at.is_(None),
            # The whole concurrency story for holds, in one predicate: either
            # the wallet had the headroom and the row moved, or it did not and
            # rowcount is zero. No read-then-write window to lose a race in.
            Wallet.paid_micros + Wallet.bonus_micros - Wallet.reserved_micros >= amount_micros,
        )
        .values(
            reserved_micros=Wallet.reserved_micros + amount_micros,
            version=Wallet.version + 1,
            updated_at=now,
        )
        .returning(
            Wallet.paid_micros,
            Wallet.bonus_micros,
            Wallet.reserved_micros,
            Wallet.version,
        )
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise PaymentRequiredError(
            "There is not enough credit to start this.",
            code="insufficient_balance",
            required_micros=amount_micros,
            available_micros=snapshot.available_micros,
            shortfall_micros=max(0, amount_micros - snapshot.available_micros),
        )
    after = (row[0], row[1], row[2], row[3])

    group_id = uuid.uuid4()
    rows = _ledger_rows(
        snapshot=snapshot,
        group_id=group_id,
        kind=LedgerEntryKind.HOLD,
        amounts={LedgerBucket.RESERVED: amount_micros},
        after=after[:3],
        version=after[3],
        ref_type=LedgerRefType.AI_SESSION,
        ai_session_id=ai_session_id,
        idempotency_key=idempotency_key,
        note=note,
    )
    if not await _flush_ledger(session, rows):
        existing = await _replay(session, idempotency_key)
        if existing is None:  # pragma: no cover
            raise ConflictError(
                "This hold could not be recorded. Please retry.", code="billing_write_conflict"
            )
        return existing

    return _movement(
        snapshot,
        group_id=group_id,
        kind=LedgerEntryKind.HOLD,
        after=after,
        reserved_delta=amount_micros,
        attempts=1,
    )


async def release_hold(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount_micros: int,
    idempotency_key: str,
    ai_session_id: uuid.UUID | None = None,
    note: str | None = None,
) -> WalletMovement:
    """Give back credit a session no longer needs.

    Clamped to what is actually held, because releasing more than was held
    would drive `reserved_micros` negative and permanently overstate what the
    user can spend. The session's own `hold_released_at IS NULL` guard is what
    stops a double release from getting this far; this is the second line.
    """
    if amount_micros < 0:
        raise BadRequestError("A release cannot be negative.", code="billing_invalid_amount")

    replayed = await _replay(session, idempotency_key)
    if replayed is not None:
        return replayed

    now = utcnow()
    for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        snapshot = await snapshot_by_id(session, wallet_id)
        release = min(amount_micros, snapshot.reserved_micros)
        if release == 0:
            return _movement(
                snapshot,
                group_id=uuid.uuid4(),
                kind=LedgerEntryKind.RELEASE,
                after=(
                    snapshot.paid_micros,
                    snapshot.bonus_micros,
                    snapshot.reserved_micros,
                    snapshot.version,
                ),
                attempts=attempt,
            )

        after = await _cas(session, snapshot, reserved_delta=-release, now=now)
        if after is None:
            continue

        group_id = uuid.uuid4()
        rows = _ledger_rows(
            snapshot=snapshot,
            group_id=group_id,
            kind=LedgerEntryKind.RELEASE,
            amounts={LedgerBucket.RESERVED: -release},
            after=after[:3],
            version=after[3],
            ref_type=LedgerRefType.AI_SESSION,
            ai_session_id=ai_session_id,
            idempotency_key=idempotency_key,
            note=note,
        )
        if not await _flush_ledger(session, rows):
            existing = await _replay(session, idempotency_key)
            if existing is None:  # pragma: no cover
                raise ConflictError(
                    "This release could not be recorded. Please retry.",
                    code="billing_write_conflict",
                )
            return existing

        return _movement(
            snapshot,
            group_id=group_id,
            kind=LedgerEntryKind.RELEASE,
            after=after,
            reserved_delta=-release,
            attempts=attempt,
        )

    raise ConflictError(
        "This wallet is being updated too often. Please retry.", code="wallet_busy"
    )


async def expire_bonus(session: AsyncSession, *, wallet_id: uuid.UUID) -> WalletMovement | None:
    """Zero a lapsed bonus, for the sweeper. None if there was nothing to do.

    The debit and hold paths already do this lazily, so this exists to keep
    the *column* honest for wallets nobody is spending from — otherwise a
    dormant account would report a bonus in `GET /v1/wallet` that
    `available_micros` correctly refuses to spend, which reads as a bug.
    """
    now = utcnow()
    for _ in range(MAX_CAS_ATTEMPTS):
        snapshot = await snapshot_by_id(session, wallet_id)
        if not snapshot.bonus_has_expired:
            return None
        if await _expire_bonus_now(session, snapshot, now=now):
            after = await snapshot_by_id(session, wallet_id)
            return _movement(
                after,
                group_id=uuid.uuid4(),
                kind=LedgerEntryKind.EXPIRY,
                after=(
                    after.paid_micros,
                    after.bonus_micros,
                    after.reserved_micros,
                    after.version,
                ),
                bonus_delta=-snapshot.bonus_micros,
                attempts=1,
            )
    raise ConflictError(
        "This wallet is being updated too often. Please retry.", code="wallet_busy"
    )


async def set_frozen(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    frozen: bool,
    reason: str | None = None,
) -> WalletSnapshot:
    """Freeze or unfreeze. Idempotent, and never touches a balance.

    Freezing is not a ledger event — no credit moved — so it bumps `version`
    but writes no entry. The audit trail for *why* is the admin route's
    required note, which rides on the ledger entry of whatever adjustment
    accompanied it.
    """
    now = utcnow()
    await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet_id)
        .values(
            frozen_at=now if frozen else None,
            frozen_reason=reason if frozen else None,
            version=Wallet.version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    logger.info("Wallet %s %s (%s)", wallet_id, "frozen" if frozen else "unfrozen", reason or "-")
    return await snapshot_by_id(session, wallet_id)


async def mark_low_balance_notified(
    session: AsyncSession, *, wallet_id: uuid.UUID, notified: bool
) -> None:
    """Latch, or clear, the once-per-crossing low-balance warning.

    Without this the warning fires on every request for as long as the balance
    stays low, which is the most common way this feature turns into spam.
    """
    now = utcnow()
    await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet_id)
        .values(low_balance_notified_at=now if notified else None, updated_at=now)
        .execution_options(synchronize_session=False)
    )
