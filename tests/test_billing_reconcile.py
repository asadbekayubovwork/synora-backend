"""Reconciliation: does the audit notice, and does healing repair the right thing.

Three passes, and the tests keep them apart because they answer three different
questions. The reaper reads `AiSession.expires_at` and is the only pass that
can see a call that died without settling — a live session's hold is *supposed*
to be held, so the other two look straight past it. `heal_reserved` repairs
reserved credit no session claims. The ledger replay reports and repairs
nothing, because a balance that disagrees with its own ledger is evidence.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    LedgerEntryKind,
    LedgerRefType,
    SessionEndReason,
)
from app.models.usage import UsageEvent
from app.models.user import User
from app.models.wallet import Wallet
from app.services.billing import reconcile_service, wallet_repo, wallet_service
from tests.conftest import auth, fund, make_user, register_and_verify

CREDIT = 1_000_000


async def _session_row(
    db,
    snapshot,
    price_book,
    *,
    reserved: int,
    status: AiSessionStatus,
    expires_at: datetime | None = None,
    claimed_at: datetime | None = None,
    estimated: int = 0,
):
    row = AiSession(
        user_id=snapshot.user_id,
        wallet_id=snapshot.wallet_id,
        service=BillingService.VOICE_AGENT,
        kind=AiSessionKind.REALTIME,
        status=status,
        model_key="synora-voice-1",
        price_book_version_id=price_book.id,
        reserved_micros=reserved,
        # What the call was quoted at when its hold was placed, and the number
        # the reaper bills a claimed session at. Zero by default because most
        # of the tests below only care about the hold coming back, and a zero
        # estimate is what routes them through `abandon_oneshot`.
        estimated_micros=estimated,
        authorize_jti=uuid.uuid4().hex,
        expires_at=expires_at,
        claimed_at=claimed_at,
    )
    db.add(row)
    await db.flush()
    return row


async def _stranded_hold(
    db,
    snapshot,
    price_book,
    *,
    micros: int,
    expires_at: datetime,
    claimed: bool = True,
    estimated: int = 0,
):
    """A metered call that placed its hold and never came back.

    Every way a one-shot can die without settling ends in this one row shape:
    non-terminal, `reserved_micros` at the full price, `hold_released_at` null,
    and a real hold on the wallet behind it. A client that disconnected before
    Starlette started the response body; a settlement that failed on a dead
    connection; a batch upstream never accepted; a worker killed between the
    hold and the charge. The hold is placed through `wallet_repo` and keyed the
    way `open_oneshot` keys it, so the release the reaper writes is the same
    release a healthy settlement would have written.
    """
    row = await _session_row(
        db,
        snapshot,
        price_book,
        reserved=micros,
        status=AiSessionStatus.ACTIVE,
        expires_at=expires_at,
        claimed_at=utcnow() if claimed else None,
        estimated=estimated,
    )
    await wallet_repo.place_hold(
        db,
        wallet_id=snapshot.wallet_id,
        amount_micros=micros,
        idempotency_key=f"hold:{row.id}",
        ai_session_id=row.id,
    )
    return row


async def _reload(row_id: uuid.UUID) -> AiSession:
    """The session row as another connection sees it, after a route committed."""
    async with SessionLocal() as db:
        return (
            await db.execute(select(AiSession).where(AiSession.id == row_id))
        ).scalar_one()


async def test_a_healthy_wallet_audits_clean(session, price_book):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT, bonus=2 * CREDIT)
    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=3 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)

    assert audit.is_consistent
    assert audit.balances_match
    assert audit.holds_match
    assert audit.first_divergent_group_id is None
    # One top-up, one bonus grant, and two more for the debit: 3 credits
    # against a 2-credit bonus splits into a bonus row and a paid row.
    assert audit.entry_count == 4


async def test_a_split_debit_does_not_look_like_a_divergence(session):
    """The replay has to group by operation.

    Walking row by row would diverge on the first two-row group and report a
    problem that is not there — which is worse than not checking at all,
    because it trains people to ignore the alarm.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT, bonus=2 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=5 * CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
        release_reserved_micros=4 * CREDIT,
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)

    assert audit.balances_match
    assert audit.first_divergent_group_id is None


async def test_a_tampered_balance_is_detected_and_localised(session):
    """Corrupt the wallet, not the ledger — the ledger cannot be corrupted."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.debit(
        session, wallet_id=snapshot.wallet_id, amount_micros=CREDIT,
        idempotency_key="d1", ref_type=LedgerRefType.USAGE_EVENT,
    )
    await session.execute(
        update(Wallet).where(Wallet.id == snapshot.wallet_id).values(paid_micros=42)
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)

    assert not audit.is_consistent
    assert not audit.balances_match
    assert audit.paid_micros == 42
    assert audit.paid_ledger_micros == 9 * CREDIT


async def test_a_leaked_hold_is_detected(session, price_book):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    # A session that ended without releasing: exactly the bug this catches.
    await _session_row(
        session, snapshot, price_book, reserved=4 * CREDIT, status=AiSessionStatus.CLOSED
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)

    assert audit.balances_match, "no credit went missing"
    assert audit.holds_match, "the session still claims the hold, so nothing has leaked yet"

    # Now lose the session's claim on it, which is what a crashed release does.
    await session.execute(
        update(AiSession)
        .where(AiSession.wallet_id == snapshot.wallet_id)
        .values(hold_released_at=utcnow(), reserved_micros=0)
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)
    assert not audit.holds_match
    assert audit.reserved_drift_micros == 4 * CREDIT


async def test_healing_frees_a_leaked_hold_and_leaves_the_ledger_consistent(session, price_book):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    row = await _session_row(
        session, snapshot, price_book, reserved=4 * CREDIT, status=AiSessionStatus.CLOSED
    )
    await session.execute(
        update(AiSession).where(AiSession.id == row.id).values(reserved_micros=0)
    )

    freed = await reconcile_service.heal_reserved(session, snapshot.wallet_id)

    assert freed == 4 * CREDIT
    after = await reconcile_service.verify_wallet(session, snapshot.wallet_id)
    assert after.is_consistent
    assert after.reserved_micros == 0
    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.available_micros == 10 * CREDIT, "the customer's money is unfrozen"


async def test_healing_stamps_the_session_so_the_drift_is_not_rediscovered(session, price_book):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    await _session_row(
        session, snapshot, price_book, reserved=0, status=AiSessionStatus.CLOSED
    )

    assert await reconcile_service.heal_reserved(session, snapshot.wallet_id) == 4 * CREDIT
    assert await reconcile_service.heal_reserved(session, snapshot.wallet_id) == 0


async def test_healing_never_invents_a_hold(session, price_book):
    """Under-reservation is logged, not healed.

    Adding a hold nobody asked for would freeze money on a live wallet, which
    is a worse outcome than letting a caller spend what they were going to.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await _session_row(
        session, snapshot, price_book, reserved=4 * CREDIT, status=AiSessionStatus.ACTIVE
    )

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)
    assert audit.reserved_drift_micros == -4 * CREDIT

    assert await reconcile_service.heal_reserved(session, snapshot.wallet_id) == 0
    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 0, "nothing was added"


async def test_a_live_session_keeps_its_hold(session, price_book):
    """Healing must not steal credit from a call that is still running."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=4 * CREDIT, idempotency_key="h1"
    )
    await _session_row(
        session, snapshot, price_book, reserved=4 * CREDIT, status=AiSessionStatus.ACTIVE
    )

    assert await reconcile_service.heal_reserved(session, snapshot.wallet_id) == 0
    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 4 * CREDIT


# --- the reaper --------------------------------------------------------------


async def test_a_session_past_its_deadline_is_reaped_and_its_hold_released(
    session, price_book
):
    """The backstop `expires_at` promised for a long time and nothing provided.

    A metered call places its hold before any work starts, on purpose, so that a
    process dying mid-call leaves evidence rather than free work — but evidence
    is only worth having if something reads it. Nothing did: the credit stayed
    frozen until somebody edited the database by hand.

    The first two assertions are why this needs a pass of its own. The audit
    reads clean and healing frees nothing, because a live session's hold is
    supposed to be held and neither pass has any way to tell a call still
    running from one that is never coming back. The deadline is the only signal
    that separates them.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    row = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=4 * CREDIT,
        expires_at=utcnow() - timedelta(minutes=1),
    )
    await session.commit()

    audit = await reconcile_service.verify_wallet(session, snapshot.wallet_id)
    assert audit.holds_match, "invisible to the audit: the session still looks live"
    assert await reconcile_service.heal_reserved(session, snapshot.wallet_id) == 0

    assert await reconcile_service.reap_expired_sessions(session) == 1

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 0
    assert fresh.available_micros == 10 * CREDIT, "the customer's money is unfrozen"
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.TIMEOUT
    assert row.error_code == "session_reaped"
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None


async def test_a_session_still_inside_its_deadline_is_left_alone(session, price_book):
    """The other half of the same read. A batch is allowed six hours, and a
    reaper that cannot wait that long would settle healthy jobs out from under
    the poller that is about to charge for them."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    row = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=4 * CREDIT,
        expires_at=utcnow() + timedelta(hours=6),
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 0

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 4 * CREDIT, "a running call keeps its hold"
    assert row.status is AiSessionStatus.ACTIVE


async def test_a_ticket_nobody_ever_claimed_expires_rather_than_fails(
    session, price_book
):
    """`EXPIRED` and `FAILED` are not decoration.

    "The call died" and "the call never began" are the first thing anyone
    reading this table wants told apart, and the enum reserves a member for each.
    A reaped session is stamped by `abandon_oneshot` — the same code every other
    terminal session goes through — so there is one definition of over, not two.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    row = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=CREDIT,
        expires_at=utcnow() - timedelta(minutes=1),
        claimed=False,
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 1

    assert row.status is AiSessionStatus.EXPIRED
    assert row.end_reason is SessionEndReason.MAX_DURATION
    assert row.hold_released_at is not None
    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 0


async def test_a_claimed_session_is_billed_and_an_unclaimed_one_is_not(
    session, price_book
):
    """`claimed_at` decides whether a reap is a bill or a write-off.

    Two rows, one wallet, one deadline apart, and only the claim timestamp
    different — which is the whole of the decision the reaper makes. It used to
    make neither: every expired session was abandoned at zero, and that turned
    this pass into a way of getting metered work for free. A one-shot's hold is
    placed only after the entire text has been handed to the supplier, so a
    claimed session still holding credit is work that was bought whether or not
    anybody ever came back to report it; abandoning it released the hold, and a
    client that had stalled on purpose then drained its audio into a session
    already terminal and paid nothing. `tests/test_tts_billing.py` walks that
    end to end through the streaming path; this is the split itself.

    The other direction is the reason the split exists rather than a blanket
    charge. A ticket nobody ever claimed was minted and never taken up — no text
    reached any supplier — so billing its estimate would invent a sale. It keeps
    `EXPIRED`, writes no usage event, and its whole hold goes back.

    Both rows carry an estimate on purpose. With `estimated_micros` at zero
    `settle_at_estimate` falls back to abandoning the session, so a test written
    without one passes whichever branch the reaper takes and proves nothing.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    billed = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=4 * CREDIT,
        estimated=4 * CREDIT,
        expires_at=utcnow() - timedelta(minutes=2),
    )
    forgiven = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=3 * CREDIT,
        estimated=3 * CREDIT,
        expires_at=utcnow() - timedelta(minutes=1),
        claimed=False,
    )
    await session.commit()

    assert await reconcile_service.reap_expired_sessions(session) == 2

    # Work that was committed, charged at the price its hold was placed for.
    assert billed.status is AiSessionStatus.CLOSED
    assert billed.end_reason is SessionEndReason.TIMEOUT
    assert billed.settled_micros == 4 * CREDIT
    assert billed.hold_released_at is not None
    # Not decoration: a charge raised on a deadline rather than on a report is
    # always arguable, and this flag is the predicate support lists them with.
    assert billed.disputed is True

    # A ticket nobody took up. Nothing was done for it, so nothing is owed.
    assert forgiven.status is AiSessionStatus.EXPIRED
    assert forgiven.end_reason is SessionEndReason.MAX_DURATION
    assert forgiven.settled_micros == 0
    assert forgiven.hold_released_at is not None
    assert forgiven.disputed is False

    events = list(
        (
            await session.execute(
                select(UsageEvent).order_by(UsageEvent.created_at)
            )
        ).scalars()
    )
    assert [event.ai_session_id for event in events] == [billed.id], (
        "one bill for the claimed session and none for the ticket"
    )
    assert events[0].price_micros == 4 * CREDIT
    assert events[0].debited_micros == 4 * CREDIT

    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 0, "both holds are gone either way"
    assert fresh.available_micros == 6 * CREDIT, "only the claimed one was paid for"


async def test_the_reconcile_route_gives_a_stranded_hold_back(client, session, price_book):
    """The whole pass over HTTP, which is how it is actually run.

    `reaped` above zero is a bug upstream of the reconciler rather than routine
    housekeeping — something failed to settle a call it started — which is why
    every reaped session id is logged at WARNING and why the count is on the
    response at all. `healed_micros` staying zero is the ordering in
    `reconcile_all`: reaping runs first, so the hold it hands back is not then
    rediscovered as drift on the following run.
    """
    tokens = await register_and_verify(client, email="admin@example.com")
    await session.execute(
        update(User).where(User.email == "admin@example.com").values(is_superuser=True)
    )
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    row = await _stranded_hold(
        session,
        snapshot,
        price_book,
        micros=4 * CREDIT,
        expires_at=utcnow() - timedelta(hours=7),
    )
    await session.commit()

    response = await client.post(
        "/admin/reconcile", headers=auth(tokens["access_token"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reaped"] == 1
    assert body["healed_micros"] == 0, "the reap released it; there is no drift left"
    assert body["diverged"] == 0

    reaped = await _reload(row.id)
    assert reaped.status is AiSessionStatus.FAILED
    assert reaped.hold_released_at is not None
    fresh = await wallet_repo.snapshot_by_id(session, snapshot.wallet_id)
    assert fresh.reserved_micros == 0
    assert fresh.available_micros == 10 * CREDIT


async def test_reconcile_all_reports_what_it_found(session, price_book):
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=10 * CREDIT)
    await wallet_repo.place_hold(
        session, wallet_id=snapshot.wallet_id, amount_micros=3 * CREDIT, idempotency_key="h1"
    )

    report = await reconcile_service.reconcile_all(session)

    assert report["checked"] >= 1
    # The hold is real but unclaimed by any session, so it is released.
    assert report["healed_micros"] == 3 * CREDIT
    assert report["diverged"] == 0
