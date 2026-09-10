"""Wallet operations: hold, debit, credit, release, expiry, freeze."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.exceptions import ForbiddenError, PaymentRequiredError
from app.db.base import as_utc, utcnow
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType
from app.models.ledger import LedgerEntry
from app.services.billing import wallet_repo, wallet_service
from tests.conftest import fund, make_user

CREDIT = 1_000_000  # one credit, in micros


async def _ledger_total(db, wallet_id, bucket: LedgerBucket) -> int:
    return (
        await db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(
                LedgerEntry.wallet_id == wallet_id, LedgerEntry.bucket == bucket
            )
        )
    ).scalar_one()


async def assert_consistent(db, wallet_id) -> None:
    """The invariant, spelled out: every counter equals its ledger."""
    snapshot = await wallet_repo.snapshot_by_id(db, wallet_id)
    assert snapshot.paid_micros == await _ledger_total(db, wallet_id, LedgerBucket.PAID)
    assert snapshot.bonus_micros == await _ledger_total(db, wallet_id, LedgerBucket.BONUS)
    assert snapshot.reserved_micros == await _ledger_total(db, wallet_id, LedgerBucket.RESERVED)
    assert snapshot.paid_micros >= 0
    assert snapshot.bonus_micros >= 0
    assert snapshot.reserved_micros >= 0


# --- wallet creation -------------------------------------------------------


async def test_a_new_account_gets_an_empty_wallet_on_first_sight(session):
    user = await make_user(session)

    snapshot = await wallet_service.ensure_wallet(session, user.id)

    assert snapshot.available_micros == 0
    assert not snapshot.is_frozen


async def test_ensuring_a_wallet_twice_returns_the_same_one(session):
    user = await make_user(session)

    first = await wallet_service.ensure_wallet(session, user.id)
    second = await wallet_service.ensure_wallet(session, user.id)

    assert first.wallet_id == second.wallet_id


async def test_the_signup_bonus_is_granted_at_most_once_ever(session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "billing_signup_bonus_micros", 5 * CREDIT)
    monkeypatch.setattr(settings, "billing_signup_bonus_days", 30)
    user = await make_user(session)

    first = await wallet_service.grant_signup_bonus(session, user.id)
    second = await wallet_service.grant_signup_bonus(session, user.id)

    assert first.bonus_micros == 5 * CREDIT
    assert second.bonus_micros == 5 * CREDIT
    await assert_consistent(session, first.wallet_id)


# --- debit -----------------------------------------------------------------


async def test_a_debit_takes_bonus_before_paid_credit(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT, bonus=3 * CREDIT)

    movement = await wallet_repo.debit(
        session,
        wallet_id=snapshot.wallet_id,
        amount_micros=5 * CREDIT,
        idempotency_key="d1",
        ref_type=LedgerRefType.USAGE_EVENT,
    )

    assert movement.bonus_delta_micros == -3 * CREDIT
    assert movement.paid_delta_micros == -2 * CREDIT
    assert movement.charged_micros == 5 * CREDIT
    assert movement.bonus_micros == 0
    assert movement.paid_micros == 8 * CREDIT
    await assert_consistent(session, snapshot.wallet_id)


async def test_a_split_debit_writes_one_group_of_two_entries(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT, bonus=3 * CREDIT)

    movement = await wallet_repo.debit(
        session,
        wallet_id=snapshot.wallet_id,
        amount_micros=5 * CREDIT,
        idempotency_key="d1",
        ref_type=LedgerRefType.USAGE_EVENT,
    )

    rows = list(
        (
            await session.execute(
                select(LedgerEntry).where(LedgerEntry.group_id == movement.group_id)
            )
        ).scalars()
    )
    assert len(rows) == 2
    assert {r.bucket for r in rows} == {LedgerBucket.PAID, LedgerBucket.BONUS}
    # Both halves came from one wallet UPDATE, so they describe the same
    # resulting state. A statement replay has to group, not walk.
    assert {r.wallet_version for r in rows} == {movement.version}
    assert {r.balance_after_paid_micros for r in rows} == {movement.paid_micros}


async def test_a_debit_beyond_the_balance_is_refused_with_the_shortfall(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    with pytest.raises(PaymentRequiredError) as excinfo:
        await wallet_repo.debit(
            session,
            wallet_id=snapshot.wallet_id,
            amount_micros=3 * CREDIT,
            idempotency_key="d1",
            ref_type=LedgerRefType.USAGE_EVENT,
        )

    error = excinfo.value
    assert error.status_code == 402
    assert error.code == "insufficient_balance"
    assert error.extra == {
        "requiredMicros": 3 * CREDIT,
        "availableMicros": CREDIT,
        "shortfallMicros": 2 * CREDIT,
    }
    await assert_consistent(session, snapshot.wallet_id)


async def test_a_refused_debit_moves_nothing(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    with pytest.raises(PaymentRequiredError):
        await wallet_repo.debit(
            session,
            wallet_id=snapshot.wallet_id,
            amount_micros=3 * CREDIT,
            idempotency_key="d1",
            ref_type=LedgerRefType.USAGE_EVENT,
        )

    after = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert after.paid_micros == CREDIT
    # And the key is still free, so a retry after a top-up works.
    assert await wallet_repo._replay(session, "d1") is None


async def test_a_replayed_debit_charges_once_and_says_so(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)

    first = await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT,
        idempotency_key="same", ref_type=LedgerRefType.USAGE_EVENT,
    )
    second = await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT,
        idempotency_key="same", ref_type=LedgerRefType.USAGE_EVENT,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.charged_micros == first.charged_micros
    assert second.group_id == first.group_id
    after = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert after.paid_micros == 6 * CREDIT, "charged once, not twice"
    await assert_consistent(session, snapshot.wallet_id)


async def test_a_debit_can_release_its_hold_in_the_same_step(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=6 * CREDIT, idempotency_key="h1"
    )

    movement = await wallet_repo.debit(
        session,
        wallet_id=snapshot.wallet_id,
        amount_micros=2 * CREDIT,
        idempotency_key="d1",
        ref_type=LedgerRefType.USAGE_EVENT,
        release_reserved_micros=6 * CREDIT,
    )

    assert movement.reserved_micros == 0
    assert movement.available_micros == 8 * CREDIT
    await assert_consistent(session, snapshot.wallet_id)


# --- grace and write-off ---------------------------------------------------


async def test_grace_charges_what_is_there_and_writes_off_the_rest(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    movement = await wallet_repo.debit(
        session,
        wallet_id=snapshot.wallet_id,
        amount_micros=3 * CREDIT,
        idempotency_key="d1",
        ref_type=LedgerRefType.USAGE_EVENT,
        max_writeoff_micros=5 * CREDIT,
    )

    assert movement.charged_micros == CREDIT
    assert movement.writeoff_micros == 2 * CREDIT
    assert movement.paid_micros == 0, "never negative"
    after = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert after.paid_micros == 0
    await assert_consistent(session, snapshot.wallet_id)


async def test_a_write_off_writes_no_ledger_entry_because_no_money_moved(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=3 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
        max_writeoff_micros=5 * CREDIT,
    )

    # The counter records it; the ledger does not. This is why the reconciler
    # deliberately does not check `lifetime_writeoff_micros`.
    total = await _ledger_total(session, snapshot.wallet_id, LedgerBucket.PAID)
    assert total == 0
    await assert_consistent(session, snapshot.wallet_id)


async def test_grace_beyond_the_cap_is_still_refused(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    with pytest.raises(PaymentRequiredError):
        await wallet_repo.debit(
            session, wallet_id=snapshot.wallet_id, amount_micros=10 * CREDIT,
            idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
            max_writeoff_micros=CREDIT,
        )


# --- holds -----------------------------------------------------------------


async def test_a_hold_reduces_what_is_available_without_spending_it(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)

    movement = await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )

    assert movement.paid_micros == 10 * CREDIT, "nothing was spent"
    assert movement.reserved_micros == 4 * CREDIT
    assert movement.available_micros == 6 * CREDIT
    await assert_consistent(session, snapshot.wallet_id)


async def test_two_holds_cannot_together_exceed_the_balance(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)

    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=7 * CREDIT, idempotency_key="h1"
    )
    with pytest.raises(PaymentRequiredError):
        await wallet_repo.place_hold(
            session, wallet_id=snapshot.wallet_id, amount_micros=7 * CREDIT, idempotency_key="h2"
        )

    await assert_consistent(session, snapshot.wallet_id)


async def test_a_zero_hold_touches_nothing(session):
    """One-shot calls take no hold: nothing happens between check and charge."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)

    movement = await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=0, idempotency_key="h0"
    )

    assert movement.version == snapshot.version, "no write at all"
    assert movement.reserved_micros == 0


async def test_releasing_more_than_is_held_clamps_instead_of_going_negative(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=2 * CREDIT, idempotency_key="h1"
    )

    movement = await wallet_repo.release_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=9 * CREDIT, idempotency_key="r1"
    )

    assert movement.reserved_micros == 0
    await assert_consistent(session, snapshot.wallet_id)


async def test_a_replayed_release_does_not_release_twice(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=3 * CREDIT, idempotency_key="h2"
    )

    await wallet_repo.release_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="r1"
    )
    replay = await wallet_repo.release_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="r1"
    )

    assert replay.replayed is True
    after = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert after.reserved_micros == 3 * CREDIT, "the second hold is untouched"
    await assert_consistent(session, snapshot.wallet_id)


# --- bonus expiry ----------------------------------------------------------


async def test_an_expired_bonus_stops_counting_immediately(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT, bonus=5 * CREDIT, bonus_days=1)
    # Move the expiry into the past without touching anything else.
    from sqlalchemy import update

    from app.models.wallet import Wallet

    await session.execute(
        update(Wallet)
        .where(Wallet.id == snapshot.wallet_id)
        .values(bonus_expires_at=utcnow() - timedelta(minutes=1))
    )

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.bonus_micros == 5 * CREDIT, "the column still holds it"
    assert fresh.effective_bonus_micros == 0, "but it is not spendable"
    assert fresh.available_micros == CREDIT


async def test_spending_after_an_expiry_writes_the_expiry_entry_first(session):
    """The wallet must keep matching its ledger without waiting for a sweeper."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=3 * CREDIT, bonus=5 * CREDIT, bonus_days=1)
    from sqlalchemy import update

    from app.models.wallet import Wallet

    await session.execute(
        update(Wallet)
        .where(Wallet.id == snapshot.wallet_id)
        .values(bonus_expires_at=utcnow() - timedelta(minutes=1))
    )

    movement = await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
    )

    assert movement.bonus_delta_micros == 0, "the lapsed bonus was not spent"
    assert movement.paid_delta_micros == -CREDIT
    assert movement.bonus_micros == 0, "and the column was zeroed"
    kinds = list(
        (
            await session.execute(
                select(LedgerEntry.kind).where(LedgerEntry.wallet_id == snapshot.wallet_id)
            )
        ).scalars()
    )
    assert LedgerEntryKind.EXPIRY in kinds
    await assert_consistent(session, snapshot.wallet_id)


async def test_the_sweeper_can_expire_a_bonus_nobody_is_spending(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, bonus=5 * CREDIT, bonus_days=1)
    from sqlalchemy import update

    from app.models.wallet import Wallet

    await session.execute(
        update(Wallet)
        .where(Wallet.id == snapshot.wallet_id)
        .values(bonus_expires_at=utcnow() - timedelta(minutes=1))
    )

    movement = await wallet_repo.expire_bonus(session, wallet_id=snapshot.wallet_id)

    assert movement is not None
    assert movement.bonus_micros == 0
    assert await wallet_repo.expire_bonus(session, wallet_id=snapshot.wallet_id) is None
    await assert_consistent(session, snapshot.wallet_id)


async def test_the_first_bonus_grant_keeps_its_expiry(session):
    """A null expiry on an empty bucket means "no bonus", not "never expires".

    Conflating the two silently made every welcome bonus permanent.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    expires = utcnow() + timedelta(days=30)

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=CREDIT,
        bonus_expires_at=expires, kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.SIGNUP_BONUS, idempotency_key="b1",
    )

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.bonus_expires_at is not None
    assert abs((as_utc(fresh.bonus_expires_at) - expires).total_seconds()) < 1


async def test_a_bonus_that_never_expires_stays_that_way(session):
    """Merging a dated grant into an undated balance must not create a deadline."""
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=CREDIT,
        bonus_expires_at=None, kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.ADMIN_GRANT, idempotency_key="b1",
    )
    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=CREDIT,
        bonus_expires_at=utcnow() + timedelta(days=1), kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.ADMIN_GRANT, idempotency_key="b2",
    )

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.bonus_expires_at is None
    assert fresh.effective_bonus_micros == 2 * CREDIT


async def test_a_bonus_grant_takes_the_later_of_the_two_expiries(session):
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    soon = utcnow() + timedelta(days=1)
    later = utcnow() + timedelta(days=30)

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=CREDIT,
        bonus_expires_at=soon, kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.ADMIN_GRANT, idempotency_key="b1",
    )
    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=CREDIT,
        bonus_expires_at=later, kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.ADMIN_GRANT, idempotency_key="b2",
    )

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.bonus_micros == 2 * CREDIT
    assert fresh.bonus_expires_at is not None
    assert abs((as_utc(fresh.bonus_expires_at) - later).total_seconds()) < 1


# --- freezing --------------------------------------------------------------


async def test_a_frozen_wallet_cannot_take_a_hold(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.set_frozen(
        session, wallet_id=snapshot.wallet_id, frozen=True, reason="reversal"
    )

    with pytest.raises(ForbiddenError) as excinfo:
        await wallet_repo.place_hold(
            session, wallet_id=snapshot.wallet_id, amount_micros=CREDIT, idempotency_key="h1"
        )

    assert excinfo.value.code == "wallet_frozen"


async def test_unfreezing_restores_the_wallet(session):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.set_frozen(session, wallet_id=snapshot.wallet_id, frozen=True, reason="x")

    after = await wallet_repo.set_frozen(session, wallet_id=snapshot.wallet_id, frozen=False)

    assert not after.is_frozen
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=CREDIT, idempotency_key="h1"
    )


# --- the append-only guard -------------------------------------------------


async def test_the_ledger_cannot_be_edited_even_from_sql(session):
    """Enforced by a trigger, not by review.

    A correction is a new entry; history never moves.
    """
    from sqlalchemy import text
    # `DBAPIError`, not `DatabaseError`: SQLite turns `RAISE(ABORT, ...)` into
    # an IntegrityError, but a plpgsql `RAISE EXCEPTION` reaches asyncpg
    # without a SQLSTATE that SQLAlchemy maps to a specific class, so it
    # arrives as the generic wrapper. `DBAPIError` is the common parent.
    from sqlalchemy.exc import DBAPIError

    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)
    await session.commit()

    # No WHERE clause: `Uuid()` renders as dash-less hex on SQLite and as a
    # native uuid on Postgres, so binding an id here would match nothing on
    # one of them and the row-level trigger would never fire.
    with pytest.raises(DBAPIError) as excinfo:
        await session.execute(text("UPDATE ledger_entries SET amount_micros = 999"))
    assert "append-only" in str(excinfo.value)
    await session.rollback()

    with pytest.raises(DBAPIError) as excinfo:
        await session.execute(text("DELETE FROM ledger_entries"))
    assert "append-only" in str(excinfo.value)
    await session.rollback()

    assert await _ledger_total(session, snapshot.wallet_id, LedgerBucket.PAID) == CREDIT


# --- missing wallet --------------------------------------------------------


async def test_an_unknown_wallet_is_a_clean_not_found(session):
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError) as excinfo:
        await wallet_repo.snapshot_by_id(session, uuid.uuid4())

    assert excinfo.value.code == "wallet_not_found"
