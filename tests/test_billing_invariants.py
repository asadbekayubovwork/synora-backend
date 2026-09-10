"""The invariant, and the guards that keep it true.

    wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)

Three angles on it: a property test that hammers a wallet with random
operations, a source-tree check that nothing outside `wallet_repo` can move a
balance in the first place, and a localising replay that names the operation
where a divergence started.

Then a fourth kind of guard, at the bottom: the checks that no behavioural test
can make. The suite runs on SQLite, and SQLite does not enforce a declared
VARCHAR width — it stores whatever it is given — so a value too wide for its
column passes every test here and answers 22001 on Postgres. Nothing catches
that class of bug except an assertion about the schema itself, written against
the mapped column rather than against a number somebody copied out of it. The
same section pins the two invariants of the reaper that cost money when they
break: a session still delivering audio is not billed at zero, and a batch's
session belongs to the batch lifecycle.
"""

from __future__ import annotations

import random
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.api.v1 import admin as admin_routes
from app.core.config import settings
from app.core.exceptions import PaymentRequiredError
from app.db.base import utcnow
from app.main import app
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    LedgerBucket,
    LedgerEntryKind,
    LedgerRefType,
    TtsBatchJobState,
    UsageMetric,
)
from app.models.ledger import LedgerEntry
from app.models.tts_job import TtsBatchJob
from app.services.billing import (
    reconcile_service,
    session_service,
    wallet_repo,
    wallet_service,
)
from tests.conftest import fund, make_user, requires_postgres

CREDIT = 1_000_000
APP_ROOT = Path(__file__).resolve().parent.parent / "app"

# The longest surface name we would plausibly give a key scope. `speech` and
# `batch` are the two that exist; sixteen is the headroom the column has to
# leave so that adding `voice-agent-live` later is a naming decision rather
# than a migration.
LONGEST_PLAUSIBLE_SCOPE = 16


# --- the property test -----------------------------------------------------


async def _bucket_total(db, wallet_id, bucket: LedgerBucket) -> int:
    return (
        await db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(
                LedgerEntry.wallet_id == wallet_id, LedgerEntry.bucket == bucket
            )
        )
    ).scalar_one()


async def test_the_ledger_always_adds_up_to_the_balance(session):
    """Two hundred random operations, then check every counter.

    Seeded rather than property-based: a failure has to be reproducible from
    the test name alone, and `requirements-dev.txt` stays at four packages.
    """
    rng = random.Random(1337)
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    wallet_id = snapshot.wallet_id
    declined = 0

    for step in range(200):
        choice = rng.choice(["topup", "bonus", "hold", "release", "debit", "debit", "settle"])
        key = f"prop-{step}"
        try:
            if choice == "topup":
                await wallet_repo.credit(
                    session, wallet_id=wallet_id, paid_micros=rng.randint(1, 5) * CREDIT,
                    kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP,
                    idempotency_key=key,
                )
            elif choice == "bonus":
                await wallet_repo.credit(
                    session, wallet_id=wallet_id, bonus_micros=rng.randint(1, 3) * CREDIT,
                    bonus_expires_at=utcnow() + timedelta(days=rng.randint(1, 60)),
                    kind=LedgerEntryKind.BONUS_GRANT, ref_type=LedgerRefType.ADMIN_GRANT,
                    idempotency_key=key,
                )
            elif choice == "hold":
                await wallet_repo.place_hold(
                    session, wallet_id=wallet_id, amount_micros=rng.randint(1, 4) * CREDIT,
                    idempotency_key=key,
                )
            elif choice == "release":
                await wallet_repo.release_hold(
                    session, wallet_id=wallet_id, amount_micros=rng.randint(1, 4) * CREDIT,
                    idempotency_key=key,
                )
            elif choice == "settle":
                await wallet_repo.debit(
                    session, wallet_id=wallet_id, amount_micros=rng.randint(1, 2) * CREDIT,
                    idempotency_key=key, ref_type=LedgerRefType.USAGE_EVENT,
                    release_reserved_micros=rng.randint(1, 3) * CREDIT,
                )
            else:
                await wallet_repo.debit(
                    session, wallet_id=wallet_id, amount_micros=rng.randint(1, 3) * CREDIT,
                    idempotency_key=key, ref_type=LedgerRefType.USAGE_EVENT,
                    max_writeoff_micros=rng.choice([0, CREDIT]),
                )
        except PaymentRequiredError:
            declined += 1

    final = await wallet_repo.snapshot_by_id(session, wallet_id)
    assert final.paid_micros == await _bucket_total(session, wallet_id, LedgerBucket.PAID)
    assert final.bonus_micros == await _bucket_total(session, wallet_id, LedgerBucket.BONUS)
    assert final.reserved_micros == await _bucket_total(session, wallet_id, LedgerBucket.RESERVED)
    assert final.paid_micros >= 0
    assert final.bonus_micros >= 0
    assert final.reserved_micros >= 0
    # If nothing was ever declined the test is not reaching the interesting
    # paths, and if everything was it is not exercising the happy ones.
    assert 0 < declined < 200


async def test_lifetime_totals_match_their_ledger_kinds(session):
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    wallet_id = snapshot.wallet_id

    await wallet_repo.credit(
        session, wallet_id=wallet_id, paid_micros=10 * CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await wallet_repo.debit(
        session, wallet_id=wallet_id, amount_micros=4 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
    )

    from app.models.wallet import Wallet

    wallet = (
        await session.execute(select(Wallet).where(Wallet.id == wallet_id))
    ).scalar_one()
    assert wallet.lifetime_topup_micros == 10 * CREDIT
    assert wallet.lifetime_spend_micros == 4 * CREDIT


async def test_a_divergence_can_be_traced_to_one_operation(session):
    """Replaying by group must reproduce each recorded `balance_after_*`.

    Grouping matters: a split debit writes two rows that share one
    `wallet_version` and one recorded balance, so a naive row-by-row running
    sum diverges on the first one.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    wallet_id = snapshot.wallet_id

    await wallet_repo.credit(
        session, wallet_id=wallet_id, paid_micros=10 * CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await wallet_repo.credit(
        session, wallet_id=wallet_id, bonus_micros=2 * CREDIT,
        kind=LedgerEntryKind.BONUS_GRANT, ref_type=LedgerRefType.ADMIN_GRANT,
        idempotency_key="b1",
    )
    await wallet_repo.place_hold(
        session, wallet_id=wallet_id, amount_micros=3 * CREDIT, idempotency_key="h1"
    )
    # Spans both buckets, so it writes a two-row group.
    await wallet_repo.debit(
        session, wallet_id=wallet_id, amount_micros=5 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
        release_reserved_micros=3 * CREDIT,
    )

    rows = list(
        (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.wallet_id == wallet_id)
                .order_by(LedgerEntry.wallet_version, LedgerEntry.bucket)
            )
        ).scalars()
    )
    groups: dict[uuid.UUID, list[LedgerEntry]] = {}
    for row in rows:
        groups.setdefault(row.group_id, []).append(row)

    running = {LedgerBucket.PAID: 0, LedgerBucket.BONUS: 0, LedgerBucket.RESERVED: 0}
    for group in sorted(groups.values(), key=lambda g: g[0].wallet_version):
        for entry in group:
            running[entry.bucket] += entry.amount_micros
        head = group[0]
        assert running[LedgerBucket.PAID] == head.balance_after_paid_micros, (
            f"diverged at version {head.wallet_version} ({head.kind.value})"
        )
        assert running[LedgerBucket.BONUS] == head.balance_after_bonus_micros
        assert running[LedgerBucket.RESERVED] == head.balance_after_reserved_micros

    final = await wallet_repo.snapshot_by_id(session, wallet_id)
    assert running[LedgerBucket.PAID] == final.paid_micros
    assert running[LedgerBucket.BONUS] == final.bonus_micros
    assert running[LedgerBucket.RESERVED] == final.reserved_micros


# --- the static guard ------------------------------------------------------

# Anything that would move a balance outside the one module allowed to.
_FORBIDDEN = (
    re.compile(r"update\(\s*Wallet\s*\)"),
    re.compile(r"\bWallet\.paid_micros\s*=[^=]"),
    re.compile(r"\bWallet\.bonus_micros\s*=[^=]"),
    re.compile(r"\bWallet\.reserved_micros\s*=[^=]"),
    re.compile(r"\.paid_micros\s*\+=|\.paid_micros\s*-="),
    re.compile(r"\.bonus_micros\s*\+=|\.bonus_micros\s*-="),
    re.compile(r"\.reserved_micros\s*\+=|\.reserved_micros\s*-="),
)

_ALLOWED = {"services/billing/wallet_repo.py"}


def test_only_wallet_repo_mutates_balances():
    """The cheapest possible defence against the likeliest regression.

    Six months from now someone will want to nudge a balance from a route.
    The invariant survives because this fails when they do.
    """
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative in _ALLOWED:
            continue
        source = path.read_text()
        for pattern in _FORBIDDEN:
            for match in pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"app/{relative}:{line} -> {match.group(0)!r}")

    assert not offenders, (
        "balances may only be moved by app/services/billing/wallet_repo.py:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_would_actually_catch_something():
    """A test that only ever passes is not a test."""
    sample = "await session.execute(update(Wallet).values(paid_micros=0))"
    assert any(pattern.search(sample) for pattern in _FORBIDDEN)


# --- idempotency -----------------------------------------------------------


async def test_a_replayed_credit_grants_once(session):
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    for _ in range(5):
        await wallet_repo.credit(
            session, wallet_id=snapshot.wallet_id, paid_micros=3 * CREDIT,
            kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP,
            idempotency_key="only-once",
        )

    final = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert final.paid_micros == 3 * CREDIT
    count = (
        await session.execute(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.idempotency_key == "only-once")
        )
    ).scalar_one()
    assert count == 1


async def test_the_two_halves_of_a_split_debit_share_one_key(session):
    """`(idempotency_key, bucket)` is unique, not the key alone.

    Both halves have to insert under one key, while a replay of either still
    collides. That is why the constraint covers the pair.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)
    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=5 * CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )
    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, bonus_micros=2 * CREDIT,
        kind=LedgerEntryKind.BONUS_GRANT, ref_type=LedgerRefType.ADMIN_GRANT,
        idempotency_key="b1",
    )

    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT,
        idempotency_key="split", ref_type=LedgerRefType.USAGE_EVENT,
    )

    buckets = list(
        (
            await session.execute(
                select(LedgerEntry.bucket).where(LedgerEntry.idempotency_key == "split")
            )
        ).scalars()
    )
    assert sorted(b.value for b in buckets) == ["bonus", "paid"]


async def test_a_stale_version_loses_the_compare_and_swap(session):
    """The lost-update sentinel.

    If this ever returns a row, the CAS has stopped comparing and every
    concurrency guarantee in the module is gone.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    await wallet_repo.credit(
        session, wallet_id=snapshot.wallet_id, paid_micros=CREDIT,
        kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP, idempotency_key="t1",
    )

    # `snapshot` was read before that credit, so its version is now stale.
    result = await wallet_repo._cas(session, snapshot, paid_delta=CREDIT, now=utcnow())
    assert result is None


async def test_a_fresh_version_wins_the_compare_and_swap(session):
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    result = await wallet_repo._cas(session, snapshot, paid_delta=CREDIT, now=utcnow())

    assert result is not None
    assert result[0] == CREDIT
    assert result[3] == snapshot.version + 1


async def test_the_belt_and_braces_guard_refuses_to_go_negative(session):
    """Even with a valid version, a delta that would underflow is declined.

    This is what turns a future arithmetic bug into a clean rowcount-0 rather
    than a constraint violation halfway through a transaction.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    result = await wallet_repo._cas(session, snapshot, paid_delta=-CREDIT, now=utcnow())

    assert result is None


# --- what SQLite cannot fail on --------------------------------------------


def test_a_stored_idempotency_key_cannot_outgrow_its_column():
    """The guard for a bug no behavioural test in this suite can reach.

    `open_oneshot` stores `{user_id}:{scope}:{client key}`, and the client's
    share has a published ceiling that both the request schema and the
    `Idempotency-Key` header advertise in OpenAPI. For one release the column
    was 128 wide while the widest legal value was 172, so Postgres answered a
    documented-length key with 22001 and a 500 while SQLite stored it happily
    and every test passed. The arithmetic is asserted here because there is
    nowhere else it can be: the suite runs on SQLite, and SQLite has no opinion
    about a declared VARCHAR width.
    """
    column_length = AiSession.__table__.c.idempotency_key.type.length
    assert column_length is not None, (
        "ai_sessions.idempotency_key must stay a bounded String — an unbounded "
        "one has no ceiling for open_oneshot to check against"
    )
    assert session_service.IDEMPOTENCY_KEY_MAX_LENGTH == column_length, (
        "the enforced ceiling is read off the column; if these disagree the "
        "constant has been written down again"
    )

    # The widest value the function can build: a full-length client key under
    # the longest surface name we would plausibly invent.
    widest = (
        f"{uuid.uuid4()}:{'s' * LONGEST_PLAUSIBLE_SCOPE}:"
        f"{'k' * session_service.MAX_CLIENT_IDEMPOTENCY_KEY}"
    )
    assert len(widest) <= column_length, (
        f"a published {session_service.MAX_CLIENT_IDEMPOTENCY_KEY}-character key "
        f"stores as {len(widest)} characters into VARCHAR({column_length})"
    )

    # And the minted form, for a caller that sent no key at all.
    minted = f"{uuid.uuid4()}:{'s' * LONGEST_PLAUSIBLE_SCOPE}:auto:{uuid.uuid4().hex}"
    assert len(minted) <= column_length


async def test_the_longest_published_key_survives_a_real_open(session, price_book):
    """The same arithmetic, through the function that does it for real.

    The check above can only ever be as right as its model of what
    `open_oneshot` builds. This one asks `open_oneshot`.
    """
    user = await make_user(session)
    await fund(session, user.id, paid=10 * CREDIT)

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: 100},
        scope="speech",
        idempotency_key="k" * session_service.MAX_CLIENT_IDEMPOTENCY_KEY,
        request_digest=session_service.request_digest_for("hello", None, "standard"),
    )

    row = (
        await session.execute(select(AiSession).where(AiSession.id == ticket.ai_session_id))
    ).scalar_one()
    column_length = AiSession.__table__.c.idempotency_key.type.length
    assert len(row.idempotency_key) <= column_length
    assert row.request_digest is not None


@pytest.mark.pg
@requires_postgres
async def test_a_maximum_length_key_reaches_a_column_that_can_hold_it(session, price_book):
    """The one test in this file that can actually *fail* on the width.

    Everything above reasons about `AiSession.__table__`, which is our own
    model and agrees with itself by construction. What no assertion about the
    mapping can see is the column the server actually created — and that is the
    half that broke: the model said 128, the value written was 172, and the
    only engine that objects is the one CI was not running. SQLite ignores a
    declared VARCHAR width entirely, so on the default suite this test would
    pass while storing 172 characters in a column declared 128 and prove
    exactly nothing; it is marked `pg` and skipped instead.

    Two inserts, because they fail differently. The first is the widest value
    `open_oneshot` can build from a key at the published ceiling, which is the
    500 the bug produced on `POST /tts/speech`. The second is a value at the
    ceiling the service enforces, which is what pins the enforced number to the
    column rather than to a comment about it. Postgres answers either one with
    22001 `StringDataRightTruncation` if the width is wrong — an unhandled
    `DataError`, not a `BadRequestError`, which is why this is a schema test
    and not a behavioural one.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: 100},
        scope="speech",
        idempotency_key="k" * session_service.MAX_CLIENT_IDEMPOTENCY_KEY,
        request_digest=session_service.request_digest_for("hello", None, "standard"),
    )
    # `open_oneshot` commits its own transaction, so the INSERT has already
    # reached the server by here — there is no unit of work left holding it.
    stored = (
        await session.execute(
            select(AiSession.idempotency_key).where(AiSession.id == ticket.ai_session_id)
        )
    ).scalar_one()
    assert stored.endswith("k" * session_service.MAX_CLIENT_IDEMPOTENCY_KEY), (
        "stored whole, not truncated — a key silently cut short is two "
        "different requests sharing one key"
    )

    widest = AiSession(
        user_id=user.id,
        wallet_id=snapshot.wallet_id,
        service=BillingService.TTS,
        kind=AiSessionKind.ONESHOT,
        status=AiSessionStatus.PENDING,
        model_key=settings.tts_model_key,
        price_book_version_id=price_book.id,
        authorize_jti=uuid.uuid4().hex,
        idempotency_key="x" * session_service.IDEMPOTENCY_KEY_MAX_LENGTH,
    )
    session.add(widest)
    await session.commit()

    assert (
        len(
            (
                await session.execute(
                    select(AiSession.idempotency_key).where(AiSession.id == widest.id)
                )
            ).scalar_one()
        )
        == session_service.IDEMPOTENCY_KEY_MAX_LENGTH
    )


def test_a_request_digest_fits_its_column_and_says_what_a_price_cannot():
    """The replacement for the price comparison, and why it had to be one.

    The TTS price book charges per thousand characters with CEIL rounding, so
    a one-character text and a thousand-character text quote the same number:
    a replay guard comparing prices waves through a thousand different
    requests, which is how one paid character bought five free syntheses. A
    digest has no buckets.
    """
    column_length = AiSession.__table__.c.request_digest.type.length
    assert len(session_service.request_digest_for("x")) == column_length

    one_character = session_service.request_digest_for("a", None, "standard", "mp3", 24000)
    one_thousand = session_service.request_digest_for(
        "a" * 1000, None, "standard", "mp3", 24000
    )
    assert one_character != one_thousand, "the two the price book cannot tell apart"

    # A separator that can appear inside a field is not a separator: without
    # one, shifting a character across a boundary is a free collision.
    assert session_service.request_digest_for("ab", "c") != session_service.request_digest_for(
        "a", "bc"
    )
    # "no voice, use the default" is not "the voice named empty string".
    assert session_service.request_digest_for("a", None) != session_service.request_digest_for(
        "a", ""
    )
    # And the same request twice is the same request.
    assert session_service.request_digest_for("a", "b", 1) == session_service.request_digest_for(
        "a", "b", 1
    )


def test_an_admin_grant_key_fits_the_ledger_column():
    """The same arithmetic on the other caller-supplied key in the schema.

    `ai_sessions.idempotency_key` was not the only column reached by a value
    the client chooses the length of. `POST /admin/wallets/{user_id}/credits`
    stores `admin:` plus its `Idempotency-Key` header into
    `ledger_entries.idempotency_key`, a VARCHAR(128) — and for a release that
    header published no ceiling at all, describing itself as "any unique
    string". Anything past 122 characters was 22001 and a 500 on a route that
    grants real money, and green on SQLite, which has no opinion about a
    declared width.
    """
    column_length = LedgerEntry.__table__.c.idempotency_key.type.length
    assert column_length is not None, (
        "ledger_entries.idempotency_key must stay a bounded String — an "
        "unbounded one has no ceiling for the admin route to publish"
    )
    assert wallet_repo.LEDGER_IDEMPOTENCY_KEY_MAX_LENGTH == column_length, (
        "the ledger ceiling is read off the column; if these disagree the "
        "constant has been written down again"
    )

    widest = f"{admin_routes.IDEMPOTENCY_KEY_PREFIX}{'k' * admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY}"
    assert len(widest) == column_length, (
        f"a key at the published {admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY} "
        f"characters stores as {len(widest)} into VARCHAR({column_length})"
    )

    # The published number has to be the enforced one, or a client that reads
    # the OpenAPI page and generates a key at exactly that length is refused by
    # a limit the contract never mentioned.
    published = _idempotency_header_max_length()
    assert published == admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY, (
        "the Idempotency-Key header must publish the ceiling it enforces"
    )


def _idempotency_header_max_length() -> int | None:
    """What OpenAPI says the admin `Idempotency-Key` header may be."""
    schema = app.openapi()
    parameters = schema["paths"]["/api/v1/admin/wallets/{user_id}/credits"]["post"][
        "parameters"
    ]
    header = next(p for p in parameters if p["name"] == "Idempotency-Key")
    # Optional, so the schema is an anyOf of the string and a null.
    for option in header["schema"].get("anyOf", [header["schema"]]):
        if option.get("type") == "string":
            return option.get("maxLength")
    return None


@pytest.mark.pg
@requires_postgres
async def test_a_maximum_length_admin_key_reaches_a_column_that_can_hold_it(session):
    """The half of the above that only Postgres can fail.

    Everything in the test above reasons about `LedgerEntry.__table__`, which
    agrees with itself by construction. This one puts the widest key the admin
    route can build into the column the server actually created, where an
    over-long value is `StringDataRightTruncation` rather than an assertion —
    an unhandled `DataError` and a 500, which is what the bug was.
    """
    user = await make_user(session)
    snapshot = await wallet_service.ensure_wallet(session, user.id)

    key = f"{admin_routes.IDEMPOTENCY_KEY_PREFIX}{'k' * admin_routes.MAX_ADMIN_IDEMPOTENCY_KEY}"
    await wallet_repo.credit(
        session,
        wallet_id=snapshot.wallet_id,
        paid_micros=CREDIT,
        kind=LedgerEntryKind.ADJUSTMENT,
        ref_type=LedgerRefType.ADMIN_GRANT,
        idempotency_key=key,
        actor_user_id=user.id,
        note="widest admin key",
    )
    await session.commit()

    stored = (
        await session.execute(
            select(LedgerEntry.idempotency_key).where(
                LedgerEntry.idempotency_key == key
            )
        )
    ).scalar_one()
    assert stored == key, (
        "stored whole, not truncated — a key silently cut short is two "
        "different grants sharing one key"
    )


# --- what the reaper must not close ----------------------------------------


async def _held_session(
    db,
    snapshot,
    price_book,
    *,
    micros: int,
    expires_at,
    last_heartbeat_at=None,
):
    """A live one-shot with a real hold on the wallet behind it.

    Keyed the way `open_oneshot` keys its hold, so a release written by the
    reaper is the same release a healthy settlement would have written.
    """
    row = AiSession(
        user_id=snapshot.user_id,
        wallet_id=snapshot.wallet_id,
        service=BillingService.TTS,
        kind=AiSessionKind.ONESHOT,
        status=AiSessionStatus.ACTIVE,
        model_key=settings.tts_model_key,
        price_book_version_id=price_book.id,
        reserved_micros=micros,
        authorize_jti=uuid.uuid4().hex,
        claimed_at=utcnow(),
        expires_at=expires_at,
        last_heartbeat_at=last_heartbeat_at,
    )
    db.add(row)
    await db.flush()
    await wallet_repo.place_hold(
        db,
        wallet_id=snapshot.wallet_id,
        amount_micros=micros,
        idempotency_key=f"hold:{row.id}",
        ai_session_id=row.id,
    )
    return row


async def test_a_session_still_delivering_audio_is_not_reaped(session, price_book):
    """A deadline says when a call started, not whether it is coming back.

    A `/tts/speech` body is client-paced — the upstream read happens only when
    the generator is pulled — so a slow client legitimately holds a session
    open past the ten-minute TTL. Reaped, it settles into a terminal session,
    rebuilds a zero, and the whole synthesis is delivered free with nothing on
    the row to say it should have been billed. The reaper has to ask about
    progress, not only about the deadline.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    live = await _held_session(
        session,
        snapshot,
        price_book,
        micros=CREDIT,
        expires_at=utcnow() - timedelta(minutes=5),
        last_heartbeat_at=utcnow(),
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 0

    await session.refresh(live)
    assert live.status is AiSessionStatus.ACTIVE
    assert live.reserved_micros == CREDIT
    assert live.hold_released_at is None


async def test_a_session_that_went_quiet_is_still_reaped(session, price_book):
    """The control. A freshness test that never fires is not a freshness test.

    Same row as above, with progress that stopped longer ago than the grace
    period — which is what a stream whose process died actually looks like.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    stalled = await _held_session(
        session,
        snapshot,
        price_book,
        micros=CREDIT,
        expires_at=utcnow() - timedelta(minutes=5),
        last_heartbeat_at=utcnow()
        - timedelta(seconds=reconcile_service.PROGRESS_GRACE_SECONDS + 60),
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 1

    await session.refresh(stalled)
    assert stalled.status is AiSessionStatus.FAILED
    assert stalled.reserved_micros == 0
    assert stalled.hold_released_at is not None


async def _batch_job(db, session_row, *, state: TtsBatchJobState) -> TtsBatchJob:
    job = TtsBatchJob(
        user_id=session_row.user_id,
        wallet_id=session_row.wallet_id,
        ai_session_id=session_row.id,
        state=state,
        idempotency_key=f"{session_row.user_id}:{uuid.uuid4().hex}",
        audio_format="mp3",
        quality="standard",
        sample_rate=24000,
    )
    db.add(job)
    await db.flush()
    return job


async def test_a_live_batch_job_keeps_the_reaper_off_its_session(session, price_book):
    """The batch lifecycle owns its own deadline, and owns it exclusively.

    A batch's session deadline is measured from the moment it was opened and
    the job's from the moment upstream accepted it, which is always later — by
    the broker hop, by the retry backoff, by hours when a refused submit is
    resubmitted. In that window the reaper terminated the session of a job
    that was perfectly healthy; the job kept polling, upstream rendered the
    whole corpus, and the settlement found the session already terminal and
    stamped the job succeeded with nothing charged. `expire_job` has to be the
    only thing that ends a batch's session.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    backing = await _held_session(
        session,
        snapshot,
        price_book,
        micros=CREDIT,
        # Past its deadline and silent: a batch session never heartbeats, so
        # nothing but the job itself stands between it and the reaper.
        expires_at=utcnow() - timedelta(hours=2),
    )
    job = await _batch_job(session, backing, state=TtsBatchJobState.RUNNING)
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 0

    await session.refresh(backing)
    assert backing.status is AiSessionStatus.ACTIVE
    assert backing.reserved_micros == CREDIT

    # Once the job is over, the shield goes with it: a terminal job's session
    # is an ordinary stranded hold and the reaper is the backstop for it.
    await session.execute(
        update(TtsBatchJob)
        .where(TtsBatchJob.id == job.id)
        .values(state=TtsBatchJobState.FAILED)
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 1
    await session.refresh(backing)
    assert backing.hold_released_at is not None
