"""The line cap's edges, the ceiling on the heartbeat path, and slow ICE.

Split from `test_voice_agent_liveness.py`, which it shares its fake agent with:
these are the cases the third review round and the live agent turned up — how
many unproven calls an account may leave behind, which cached answers may be
trusted, a quiet heartbeat past the ceiling, a nudge the agent refuses, a lost
nudge, and a network where ICE takes half a minute.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.billing_enums import SessionEndReason
from app.services.ai import voice_agent_client, voice_call_lifecycle
from tests.voice_agent_support import (  # noqa: F401 - `agent` and `no_agent` are fixtures
    PER_MINUTE,
    agent,
    beat,
    caller,
    code_of,
    hang_up,
    load_call,
    load_row,
    no_agent,
    opened_call,
    post_offer,
    reply,
    rewind,
)

NUDGE_GRACE = voice_call_lifecycle.NUDGE_GRACE_SECONDS
NEVER_CONNECTED_GRACE = voice_call_lifecycle.NEVER_CONNECTED_GRACE_SECONDS


@pytest.fixture
def live_agent(agent):
    """The agent that can be asked: it 404s a `pc_id` it does not hold."""
    return agent.track_peers()




async def test_open_delete_and_keep_talking_is_bounded_to_two_extra_lines(
    client, session, price_book, live_agent
):
    """The loop the per-call exemption allowed: open, DELETE at once, keep the peer.

    Only the two most recent unproven calls are exempt, so the second and third
    opens go through — they might be retries after failed connects — and the
    fourth, with the first still held, is refused. Cap plus two, whatever the
    client does, and told the truth: those peers never connected.
    """
    user, headers = await caller(session)
    calls = []
    for _ in range(3):
        call_id = await opened_call(client, headers)
        await hang_up(client, headers, call_id)
        calls.append(call_id)

    refused = await post_offer(client, headers)
    assert refused.status_code == 429
    assert code_of(refused) == "voice_call_still_connected"
    assert "being closed" in refused.json()["detail"], "they never connected: nothing to close"
    assert refused.headers["Retry-After"] == str(voice_call_lifecycle.ATTEMPT_TEARDOWN_SECONDS)
    assert len(live_agent.peers) == 3

    # Once the agent lets the first go, the account can open again.
    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(calls[0], agent_seen=30)
    assert (await post_offer(client, headers)).status_code == 201


async def test_a_third_try_after_two_failed_connects_still_held_goes_through(
    client, session, price_book, live_agent
):
    """Measured on the live agent: it keeps a peer that never connected for a minute.

    One exemption refused exactly this — the third press after two ICE failures
    — with "close the other tab", to a user with one tab.
    """
    user, headers = await caller(session)
    for _ in range(2):
        call_id = await opened_call(client, headers)
        await hang_up(client, headers, call_id)
    assert len(live_agent.peers) == 2, "both failed peers still held by the agent"
    assert (await post_offer(client, headers)).status_code == 201


async def test_a_retry_after_two_failed_connects_the_agent_has_dropped_goes_through(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    for n in (1, 2):
        call_id = await opened_call(client, headers)
        await hang_up(client, headers, call_id)
        live_agent.drop(live_agent.pc_id_of(n))
    assert (await post_offer(client, headers)).status_code == 201


async def test_a_stamp_from_before_the_hang_up_is_not_taken_as_still_held(
    client, session, price_book, live_agent, monkeypatch
):
    """Only an answer recorded after the call ended is reused; anything earlier is re-asked."""
    monkeypatch.setattr(voice_call_lifecycle, "HANGUP_SETTLE_SECONDS", 1)
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await beat(client, headers, first)
    await hang_up(client, headers, first)
    # Revived or kept a moment before the hang-up: agent_seen_at is recent, but
    # it says nothing about after the DELETE.
    call = await load_call(first)
    await rewind(first, agent_seen=0.2, ended=0)
    live_agent.drop(live_agent.pc_id_of(1))
    assert (await post_offer(client, headers)).status_code == 201
    assert call.ended_at is not None


async def test_a_quiet_heartbeat_past_the_ceiling_is_billed_to_the_ceiling(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    ceiling = settings.voice_agent_max_session_seconds
    await rewind(call_id, answered=ceiling + 5, seen=ceiling - 100, connected=True, agent_seen=60)

    pulse = await beat(client, headers, call_id)
    assert pulse.json()["action"] == "stop"
    row = await load_row(call_id)
    assert row.cum_session_ms == ceiling * 1000
    assert row.end_reason is SessionEndReason.MAX_DURATION


async def test_an_ended_call_whose_nudge_was_lost_is_timed_from_its_end(
    client, session, price_book, live_agent
):
    """A lost settle-time nudge must not exempt the call for ever."""
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await hang_up(client, headers, first)
    second = await opened_call(client, headers)
    await hang_up(client, headers, second)
    live_agent.drop(live_agent.pc_id_of(2))
    # The first: no nudge on record, ended and answered long ago, still held.
    async with SessionLocal() as db:
        from sqlalchemy import update

        from app.models.voice_call import VoiceCall

        await db.execute(update(VoiceCall).where(VoiceCall.id == first).values(nudged_at=None))
        await db.commit()
    await rewind(first, answered=NEVER_CONNECTED_GRACE + 60, ended=NUDGE_GRACE + 30)
    await rewind(second, ended=0)

    call = await load_call(first)
    assert voice_call_lifecycle.proven_connected(call, utcnow())
    refused = await post_offer(client, headers)
    assert code_of(refused) == "voice_call_still_connected"


async def test_a_nudge_the_agent_refuses_is_counted_and_logged(live_agent, caplog):
    from app.core import metrics

    live_agent.peers.add("pc-under-test")
    live_agent.candidates_reply = reply(400, body={"detail": "no such mid"})
    before = metrics.REGISTRY.get_sample_value("synora_voice_nudges_total", {"result": "refused"}) or 0

    assert await voice_agent_client.nudge("pc-under-test") is False
    after = metrics.REGISTRY.get_sample_value("synora_voice_nudges_total", {"result": "refused"}) or 0
    assert after == before + 1
    assert any("refused the nudge" in record.getMessage() for record in caplog.records)

    # A peer already gone is what the nudge is for: not a refusal.
    assert await voice_agent_client.nudge("pc-nobody-holds") is False
    assert (
        metrics.REGISTRY.get_sample_value("synora_voice_nudges_total", {"result": "refused"}) or 0
    ) == after


async def test_slow_ice_is_not_billed_the_bill_starts_at_the_first_heartbeat(
    client, session, price_book, agent
):
    """Sixty seconds of ICE through two NATs, then thirty of conversation: one minute, not two."""
    from sqlalchemy import update

    from app.models.voice_call import VoiceCall

    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await beat(client, headers, call_id)
    now = utcnow()
    async with SessionLocal() as db:
        await db.execute(
            update(VoiceCall)
            .where(VoiceCall.id == call_id)
            .values(
                answered_at=now - timedelta(seconds=90),
                connected_at=now - timedelta(seconds=30),
                last_seen_at=now,
            )
        )
        await db.commit()
    bill = (await hang_up(client, headers, call_id)).json()
    assert 30_000 <= bill["billed_ms"] < 31_000
    assert bill["price_micros"] == PER_MINUTE


# --- what the dashboard reads ---------------------------------------------------


def _metric(name: str, **labels: str) -> float:
    from app.core import metrics

    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def test_an_offer_the_agent_refused_is_unanswered_not_unconnected(
    client, session, price_book, agent
):
    """An agent that is down or full must not read as ICE failing on the dashboard."""
    before_unanswered = _metric(
        "synora_voice_calls_total", end_reason="upstream_error", connected="unanswered"
    )
    before_no = _metric("synora_voice_calls_total", end_reason="upstream_error", connected="no")
    user, headers = await caller(session)
    agent.offer_reply = reply(429, body={"detail": "full"})
    assert (await post_offer(client, headers)).status_code == 429

    assert _metric(
        "synora_voice_calls_total", end_reason="upstream_error", connected="unanswered"
    ) == before_unanswered + 1
    assert _metric("synora_voice_calls_total", end_reason="upstream_error", connected="no") == before_no


async def test_the_canary_is_counted_apart_from_real_probes(live_agent):
    before_gone = _metric("synora_voice_probes_total", result="gone")
    before_trusted = _metric("synora_voice_probes_total", result="canary_trusted")
    assert await voice_agent_client.liveness_is_trustworthy() is True
    assert _metric("synora_voice_probes_total", result="canary_trusted") == before_trusted + 1
    assert _metric("synora_voice_probes_total", result="gone") == before_gone, (
        "a healthy agent's canary 404 is not a call that went away"
    )


async def test_one_silence_is_one_kept_spell_however_often_it_is_rechecked(
    client, session, price_book, live_agent
):
    from tests.voice_agent_support import sweep_now

    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=100, seen=80, connected=True)
    before = _metric("synora_voice_calls_kept_total")

    assert await sweep_now() == 0
    await rewind(call_id, agent_seen=50)  # the sweep comes back to the same silence
    assert await sweep_now() == 0
    assert _metric("synora_voice_calls_kept_total") == before + 1

    # A heartbeat ends the spell; the next silence is a new one.
    assert (await beat(client, headers, call_id)).json()["action"] == "continue"
    await rewind(call_id, answered=200, seen=80, agent_seen=90, connected=True)
    assert await sweep_now() == 0
    assert _metric("synora_voice_calls_kept_total") == before + 2
