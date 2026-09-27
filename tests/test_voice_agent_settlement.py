"""The calls nobody ended, and the three passes that end them.

The agent has no route that ends a call, lists calls or reports usage, so a
tab that crashes mid-call tells us nothing at all. What it leaves is a call row
whose heartbeat has stopped, holding a ceiling of the user's credit, and three
things may find it: the sweep on that user's next open, the in-process loop,
and the admin reconcile. Whichever gets there first, the price is the same —
the last heartbeat plus one interval, capped at the ceiling, and `disputed`,
because that end is an inference rather than a hang-up — since it is a function
of the row's timestamps and not of when anybody looked.

Just as much of this file is about what those passes must *not* touch. The
generic reaper bills a claimed session past its deadline at its estimate, which
for a voice call is the whole ceiling; it stands off every session with a call
row. And `voice_agent` is also the service agent-reported sessions bill under,
which have no call row by design; the voice sweep stands off every session it
did not open itself.

See `tests/voice_agent_support.py` for the fake agent and the arithmetic, and
`test_voice_agent.py` for the overview.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_SESSION_STATUSES,
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    LedgerBucket,
    LedgerEntryKind,
    SessionEndReason,
)
from app.models.ledger import LedgerEntry
from app.models.usage import UsageEvent
from app.models.voice_call import VoiceCall
from app.services.ai import voice_agent_service, voice_call_lifecycle
from app.services.billing import reconcile_service, session_service, wallet_service
from tests.conftest import auth, fund, make_user
from tests.voice_agent_support import (  # noqa: F401 - `agent` and `no_agent` are fixtures
    AGENT_KEY,
    AGENT_URL,
    FUNDED,
    HOLD,
    PER_MINUTE,
    agent,
    backdate_session,
    caller,
    grace_seconds,
    hang_up,
    ledger_of,
    load_call,
    load_row,
    money,
    no_agent,
    opened_call,
    post_offer,
    reply,
    rewind,
    rows_of,
    sweep_now,
    voice_session,
)


# --- the sweep ------------------------------------------------------------------------


async def test_the_sweep_bills_a_lapsed_call_to_its_last_heartbeat_and_flags_it(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=300, seen=120, connected=True)

    assert await sweep_now() == 1

    row = await load_row(call_id)
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT
    assert row.disputed is True
    # 180 s of heard call plus one 15 s interval: 195 s, four started minutes.
    assert row.cum_session_ms == 195_000
    assert row.settled_micros == 4 * PER_MINUTE
    assert await money(user.id) == (FUNDED - 4 * PER_MINUTE, 0)
    assert await sweep_now() == 0, "a settled call is not found twice"


async def test_the_sweep_caps_a_lapsed_call_at_the_ceiling(client, session, price_book, agent):
    """A crashed tab costs the same whether it is found in thirty seconds or a week."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=2_000, seen=1_000, connected=True)

    assert await sweep_now() == 1

    row = await load_row(call_id)
    assert row.cum_session_ms == 600_000
    assert row.settled_micros == HOLD
    assert row.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT
    assert row.disputed is True
    assert await money(user.id) == (FUNDED - HOLD, 0)


async def test_the_sweep_bills_a_call_past_its_ceiling_exactly_the_ceiling_undisputed(
    client, session, price_book, agent
):
    """Still heartbeating when the ceiling passed: a measured end, not an inferred one."""
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=610, seen=10, connected=True)

    assert await sweep_now() == 1

    row = await load_row(call_id)
    assert row.cum_session_ms == 600_000
    assert row.end_reason is SessionEndReason.MAX_DURATION
    assert row.disputed is False


async def test_the_sweep_releases_an_answered_call_that_never_connected(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=60, seen=60)

    assert await sweep_now() == 1

    row = await load_row(call_id)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT
    assert row.error_code == "voice_never_connected"
    assert row.settled_micros == 0
    assert await money(user.id) == (FUNDED, 0)


async def test_the_sweep_releases_an_unanswered_call_only_after_the_grace(
    session, price_book
):
    """Our own process died mid-offer, and left a row that never got an answer.

    The grace is twice the offer's deadline, because until then the offer may
    still be in flight on another worker and its answer about to land.
    """
    user = await make_user(session)
    await fund(session, user.id, paid=FUNDED)
    await session.commit()
    stranded, young = (await voice_session(user.id)), (await voice_session(user.id))
    async with SessionLocal() as db:
        db.add_all(
            [
                VoiceCall(id=stranded, user_id=user.id,
                          last_seen_at=utcnow() - timedelta(seconds=grace_seconds() + 10)),
                VoiceCall(id=young, user_id=user.id,
                          last_seen_at=utcnow() - timedelta(seconds=grace_seconds() - 10)),
            ]
        )
        await db.commit()

    assert await sweep_now() == 1

    row = await load_row(stranded)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.TIMEOUT
    assert row.reserved_micros == 0
    assert (await load_call(stranded)).ended_at is not None
    assert (await load_row(young)).status is AiSessionStatus.ACTIVE
    assert (await load_call(young)).ended_at is None
    assert await money(user.id) == (FUNDED - HOLD, HOLD)


async def test_the_sweep_releases_an_orphaned_hold_only_after_the_grace(session, price_book):
    """A hold whose call row was never written: killed between two commits.

    The generic reaper stands off voice sessions, so without this nothing would
    ever give that ceiling of credit back. Nothing was relayed for it, so
    nothing is owed.
    """
    user = await make_user(session)
    await fund(session, user.id, paid=FUNDED)
    await session.commit()
    orphan, young = (await voice_session(user.id)), (await voice_session(user.id))
    await backdate_session(orphan, seconds=grace_seconds() + 10)

    assert await sweep_now() == 1

    row = await load_row(orphan)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.INTERNAL_ERROR
    assert row.error_code == "voice_call_orphaned"
    assert (await load_row(young)).status is AiSessionStatus.ACTIVE
    assert await money(user.id) == (FUNDED - HOLD, HOLD)


@pytest.mark.parametrize(
    ("scope", "key"),
    [("agent", None), (voice_agent_service.IDEMPOTENCY_SCOPE, "agent-report-7")],
    ids=["another-scope", "voice-scope-with-a-callers-key"],
)
async def test_the_sweep_never_touches_a_voice_session_it_did_not_open(
    session, price_book, scope, key
):
    """`voice_agent` is also the service agent-reported sessions bill under.

    Those have no call row by design, so "voice, and no row" alone would free
    every one of them at zero. What only `open_call` writes is the minted key,
    and a session without it is somebody else's, however old.
    """
    user = await make_user(session)
    await fund(session, user.id, paid=FUNDED)
    await session.commit()
    theirs = await voice_session(user.id, scope=scope, key=key)
    await backdate_session(theirs, seconds=10 * grace_seconds())

    assert await sweep_now() == 0

    row = await load_row(theirs)
    assert row.status is AiSessionStatus.ACTIVE
    assert row.reserved_micros == HOLD


async def test_the_sweep_leaves_a_healthy_call_alone_and_can_be_scoped_to_one_user(
    client, session, price_book, agent
):
    _, first = await caller(session)
    other, second = await caller(session)
    _, third = await caller(session)
    healthy = await opened_call(client, first)
    await rewind(healthy, answered=100, seen=10, connected=True)
    lapsed = await opened_call(client, second)
    elsewhere = await opened_call(client, third)
    for call_id in (lapsed, elsewhere):
        await rewind(call_id, answered=200, seen=100, connected=True)

    assert await sweep_now(user_id=other.id) == 1

    assert (await load_call(lapsed)).ended_at is not None
    assert (await load_call(elsewhere)).ended_at is None, "another user's call waits its turn"
    assert (await load_call(healthy)).ended_at is None
    assert await sweep_now() == 1
    assert (await load_call(healthy)).ended_at is None


async def test_the_generic_reaper_stands_off_a_voice_call_but_not_an_agent_reported_session(
    client, session, price_book, agent
):
    """Its rule — a claimed session past its deadline is billed at its estimate —
    would charge a tab that crashed ten seconds in the whole ten-minute ceiling.

    The exclusion is by call row, not by service, so a voice-agent session the
    agent reported for itself is still this pass's to finish.
    """
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    reported = await voice_session(user.id, scope="agent")
    async with SessionLocal() as db:
        await db.execute(
            update(AiSession)
            .where(AiSession.id.in_([call_id, reported]))
            .values(expires_at=utcnow() - timedelta(hours=1))
        )
        await db.commit()

    async with SessionLocal() as db:
        reaped = await reconcile_service.reap_expired_sessions(db)

    assert reaped == 1
    row = await load_row(call_id)
    assert row.status is AiSessionStatus.ACTIVE
    assert row.reserved_micros == HOLD
    assert (await load_row(reported)).status in TERMINAL_SESSION_STATUSES


async def test_admin_reconcile_reports_the_voice_calls_it_ended(
    client, session, price_book, agent
):
    """The same sweep, on demand, before the passes that would misread its holds.

    `healed_micros` staying zero is that ordering: the sweep gives the holds
    back first, so `heal_reserved` never sees them as drift.
    """
    admin = await make_user(session)
    admin.is_superuser = True
    await session.commit()
    admin_headers = auth(create_access_token(str(admin.id)))
    billed_user, billed = await caller(session)
    freed_user, freed = await caller(session)
    lapsed = await opened_call(client, billed)
    await rewind(lapsed, answered=200, seen=100, connected=True)
    never = await opened_call(client, freed)
    await rewind(never, answered=60, seen=60)

    response = await client.post("/admin/reconcile", headers=admin_headers)

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["voice_ended"] == 2
    assert report["reaped"] == 0
    assert report["healed_micros"] == 0
    assert report["diverged"] == 0
    assert await money(billed_user.id) == (FUNDED - 2 * PER_MINUTE, 0)
    assert await money(freed_user.id) == (FUNDED, 0)
    assert (await client.post("/admin/reconcile", headers=billed)).status_code == 403


async def test_the_in_process_sweeper_bills_a_lapsed_call_and_stops_when_asked(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=200, seen=100, connected=True)

    voice_agent_service.start_sweeper()
    task = voice_call_lifecycle._sweeper_task
    voice_agent_service.start_sweeper()
    assert voice_call_lifecycle._sweeper_task is task, "one loop per process"
    try:
        for _ in range(200):
            if (await load_call(call_id)).ended_at is not None:
                break
            await asyncio.sleep(0.025)
    finally:
        await voice_agent_service.stop_sweeper(timeout_seconds=5)

    assert task is not None and task.done() and not task.cancelled()
    assert voice_call_lifecycle._sweeper_task is None
    row = await load_row(call_id)
    assert row.cum_session_ms == 115_000
    assert row.disputed is True
    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE, 0)


async def test_the_sweeper_runs_without_an_agent_and_not_with_a_zero_interval(
    monkeypatch, session, price_book
):
    """Sweeping needs only the database, so switching the agent off must not stop it.

    Otherwise a deployment that takes voice offline mid-incident leaves every
    crashed tab's ceiling hold frozen until somebody runs the admin reconcile.
    """
    user, _ = await caller(session)
    async with SessionLocal() as db:
        ticket = await session_service.open_oneshot(
            db,
            user_id=user.id,
            service=BillingService.VOICE_AGENT,
            model_key=settings.voice_agent_model_key,
            quantities=voice_agent_service.ceiling_quantities(),
            scope=voice_agent_service.IDEMPOTENCY_SCOPE,
            kind=AiSessionKind.REALTIME,
        )
        db.add(VoiceCall(id=ticket.ai_session_id, user_id=user.id, last_seen_at=utcnow()))
        await db.commit()
    await rewind(ticket.ai_session_id, answered=120, seen=100, connected=True)

    monkeypatch.setattr(settings, "voice_agent_sweep_seconds", 1)
    voice_agent_service.start_sweeper()
    assert voice_call_lifecycle._sweeper_task is not None, "no agent is no reason to stop sweeping"
    for _ in range(50):
        if (await load_call(ticket.ai_session_id)).ended_at is not None:
            break
        await asyncio.sleep(0.1)
    await voice_agent_service.stop_sweeper(timeout_seconds=5)
    assert (await load_call(ticket.ai_session_id)).ended_at is not None
    assert (await money(user.id))[1] == 0, "the hold came back with no agent configured"

    monkeypatch.setattr(settings, "voice_agent_sweep_seconds", 0)
    voice_agent_service.start_sweeper()
    assert voice_call_lifecycle._sweeper_task is None


# --- the wallet afterwards ----------------------------------------------------------


async def test_the_wallet_ledger_adds_up_after_billed_free_and_refused_calls(
    client, session, price_book, agent
):
    """Three calls, three holds, three releases and exactly one debit.

    A billed call, a call that never connected and a call the agent refused
    each place the same ceiling hold and give all of it back; only the first
    is charged, and only for what it ran. The wallet's own audit agrees.
    """
    user, headers = await caller(session)

    billed = await opened_call(client, headers)
    await rewind(billed, answered=90, seen=5, connected=True)
    await hang_up(client, headers, billed)
    free = await opened_call(client, headers)
    await hang_up(client, headers, free)
    agent.offer_reply = reply(503, body={"detail": "warming up"})
    assert (await post_offer(client, headers)).status_code == 503
    refused = next(r.id for r in await rows_of(user.id) if r.id not in (billed, free))

    by_kind: dict[LedgerEntryKind, list[LedgerEntry]] = {}
    for entry in await ledger_of(user.id):
        by_kind.setdefault(entry.kind, []).append(entry)
    assert set(by_kind) == {
        LedgerEntryKind.TOPUP, LedgerEntryKind.HOLD, LedgerEntryKind.RELEASE, LedgerEntryKind.DEBIT,
    }
    for kind, sign in ((LedgerEntryKind.HOLD, 1), (LedgerEntryKind.RELEASE, -1)):
        assert {e.ai_session_id for e in by_kind[kind]} == {billed, free, refused}
        assert all(e.bucket is LedgerBucket.RESERVED for e in by_kind[kind])
        assert all(e.amount_micros == sign * HOLD for e in by_kind[kind])
    # A debit points at the usage event it paid for rather than at the session,
    # and the event is what names the session — so that is the join.
    (debit,) = by_kind[LedgerEntryKind.DEBIT]
    async with SessionLocal() as db:
        event = (
            await db.execute(select(UsageEvent).where(UsageEvent.id == debit.usage_event_id))
        ).scalar_one()
    assert event.ai_session_id == billed
    assert debit.bucket is LedgerBucket.PAID
    assert debit.amount_micros == -2 * PER_MINUTE == -event.debited_micros

    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE, 0)
    async with SessionLocal() as db:
        wallet_id = (await wallet_service.get_balance(db, user.id)).wallet_id
        audit = await reconcile_service.verify_wallet(db, wallet_id)
    assert audit.is_consistent
    assert audit.reserved_held_micros == 0


def test_a_connected_call_is_never_billed_zero():
    """A zero quantity would skip the price line, and the connection fee with it."""
    now = utcnow()
    call = VoiceCall(answered_at=now, connected_at=now, last_seen_at=now)

    billed_ms, lapsed, reason = voice_agent_service.billed_duration(
        call, now=now, end_reason=SessionEndReason.CLIENT_HANGUP
    )

    assert billed_ms == 1
    assert lapsed is False
    assert reason is SessionEndReason.CLIENT_HANGUP
