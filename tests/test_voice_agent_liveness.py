"""Asking the agent whether a call is still up, and what the answer is allowed to change.

The other three voice files run against an agent that answers 200 to every
`PATCH`, which the liveness canary reads as "cannot be asked" — so they pin the
heartbeat-only behaviour this module falls back to. This file runs against one
that knows its peer connections and 404s the rest, as the real agent does, and
pins what that buys:

* a client that never heartbeats, or stops, is *kept* and billed while the agent
  holds its call, instead of ending at a bill it chose by going quiet;
* "the agent holds it" is not "it connected": a never-heartbeated call is nudged
  and billed only once it has outlived the nudge, so an ICE failure the agent
  has not dropped yet is never charged;
* a call settled while its agent connection is still up keeps its user's line,
  so one account can never hold more of the shared agent than its cap — unless
  it never connected, which must not refuse the retry after a failed connect;
* an agent that answers 200 to a `pc_id` that cannot exist is never believed.

Plus the other things two review rounds turned up: ephemeral TURN credentials,
the open throttle counted without Redis, a candidate batch cut to what fits,
orphans the reaper must not bill, and an abandon that loses a race.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta

import pytest

from app.core.config import Settings, settings
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import AiSessionStatus, SessionEndReason
from app.services.ai import voice_agent_client, voice_call_lifecycle
from app.services.billing import reconcile_service, session_service, wallet_repo
from tests.voice_agent_support import (  # noqa: F401 - `agent` and `no_agent` are fixtures
    HOLD,
    PER_MINUTE,
    agent,
    backdate_session,
    beat,
    caller,
    code_of,
    grace_seconds,
    hang_up,
    host_candidate,
    load_call,
    load_row,
    money,
    no_agent,
    opened_call,
    post_offer,
    relay,
    reply,
    rewind,
    sweep_now,
    voice_session,
)

INTERVAL = 15  # settings.voice_agent_heartbeat_seconds
NUDGE_GRACE = voice_call_lifecycle.NUDGE_GRACE_SECONDS
NEVER_CONNECTED_GRACE = voice_call_lifecycle.NEVER_CONNECTED_GRACE_SECONDS


@pytest.fixture
def live_agent(agent):
    """The agent that can be asked: it 404s a `pc_id` it does not hold."""
    return agent.track_peers()


# --- the canary --------------------------------------------------------------


async def test_an_agent_that_404s_the_unknown_is_trusted_and_asked_once(live_agent):
    assert await voice_agent_client.liveness_is_trustworthy() is True
    assert await voice_agent_client.liveness_is_trustworthy() is True
    assert len(live_agent.probes) == 1, "the verdict is cached, not re-asked per call"
    assert live_agent.probes[0]["pc_id"].startswith("synora-liveness-canary-")


async def test_an_agent_that_says_yes_to_everything_is_not_believed(agent):
    assert await voice_agent_client.liveness_is_trustworthy() is False


async def test_an_unreachable_agent_is_not_believed(agent):
    agent.candidates_reply = reply(502)
    assert await voice_agent_client.liveness_is_trustworthy() is False
    assert await voice_agent_client.probe("anything") is None


async def test_no_agent_means_nothing_to_ask():
    assert await voice_agent_client.probe("anything") is None
    assert await voice_agent_client.liveness_is_trustworthy() is False
    assert await voice_agent_client.nudge("anything") is False


# --- the client that never heartbeats ----------------------------------------------


async def test_a_call_that_never_heartbeats_is_nudged_then_billed_once_it_outlives_the_nudge(
    client, session, price_book, live_agent
):
    """The free-call trick: answer, never heartbeat, keep talking.

    Found quiet and still held, the call is kept and nudged — not yet billed,
    because a peer failing ICE looks exactly like this. Still held a nudge-grace
    later, it connected: it is billed from its answer until the agent lets go.
    """
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=50, seen=50)

    assert await sweep_now() == 0, "kept, not ended"
    call = await load_call(call_id)
    assert call.ended_at is None and call.agent_seen_at is not None
    assert call.nudged_at is not None, "a never-heartbeated call is nudged the first time"
    assert call.connected_at is None, "held is not connected, not yet"
    assert len(live_agent.nudges) == 1

    # The line is busy for as long as the call is up, whatever the client says.
    refused = await post_offer(client, headers)
    assert refused.status_code == 429 and code_of(refused) == "voice_call_limit"

    # Past both graces and still held: connected, inferred from the answer.
    await rewind(call_id, answered=NEVER_CONNECTED_GRACE + 10, seen=NEVER_CONNECTED_GRACE + 10,
                 agent_seen=50, nudged=NUDGE_GRACE + 5)
    assert await sweep_now() == 0
    call = await load_call(call_id)
    assert call.connected_at == call.answered_at
    assert len(live_agent.nudges) == 1, "nudged once, never again"

    live_agent.drop(live_agent.pc_id_of(1))
    # `connected=True` moves the inferred connection with the answer: it *is*
    # the answer for a call that never heartbeated.
    await rewind(call_id, answered=200, seen=200, agent_seen=100, connected=True)
    assert await sweep_now() == 1
    row = await load_row(call_id)
    assert row.status is AiSessionStatus.CLOSED
    assert row.disputed, "billed on the agent's word, not the client's"
    # Answer to the agent's last "still here" plus one interval: 115 s, two
    # started minutes.
    assert row.cum_session_ms == (200 - 100 + INTERVAL) * 1000
    assert row.settled_micros == 2 * PER_MINUTE
    assert (await load_call(call_id)).gone_at is not None


async def test_a_held_call_that_drops_before_its_nudge_grace_is_free(
    client, session, price_book, live_agent
):
    """An ICE failure the agent had not dropped yet — the case the grace exists for."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=50, seen=50)
    assert await sweep_now() == 0

    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(call_id, answered=100, seen=100, agent_seen=50)
    assert await sweep_now() == 1
    row = await load_row(call_id)
    assert row.status is AiSessionStatus.FAILED
    assert row.settled_micros == 0
    assert await money(user.id) == (100 * 1_000_000, 0)


async def test_held_past_the_answer_grace_but_not_the_nudge_grace_is_not_yet_connected(
    client, session, price_book, live_agent
):
    """Both graces, not either: the old rule billed at sixty seconds after the answer alone."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=NEVER_CONNECTED_GRACE + 30, seen=NEVER_CONNECTED_GRACE + 30,
                 nudged=10)
    assert await sweep_now() == 0
    assert (await load_call(call_id)).connected_at is None


async def test_a_nudge_the_agent_refused_does_not_start_the_grace(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=50, seen=50)

    def refuse_nudges(request):
        import json

        body = json.loads(request.content)
        if body.get("candidates") == [voice_agent_client.NUDGE_CANDIDATE]:
            return reply(400, body={"detail": "no"})(request)
        return reply(200, body={"status": "success"})(request)

    live_agent.candidates_reply = refuse_nudges
    assert await sweep_now() == 0
    call = await load_call(call_id)
    assert call.agent_seen_at is not None and call.nudged_at is None


async def test_hanging_up_a_call_that_never_connected_nudges_its_peer(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    assert (await hang_up(client, headers, call_id)).json()["price_micros"] == 0
    assert live_agent.nudges == [
        {"pc_id": live_agent.pc_id_of(1), "candidates": [voice_agent_client.NUDGE_CANDIDATE]}
    ]
    assert (await load_call(call_id)).nudged_at is not None

    # And the retry right after a failed connect is not refused as "still connected".
    assert (await post_offer(client, headers)).status_code == 201


async def test_hanging_up_a_connected_call_nudges_nothing(client, session, price_book, live_agent):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await beat(client, headers, call_id)
    await hang_up(client, headers, call_id)
    assert live_agent.nudges == []


# --- the client that stops -----------------------------------------------------------


async def test_a_connected_call_that_went_quiet_is_kept_then_billed_when_it_drops(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=100, seen=80, connected=True)

    assert await sweep_now() == 0
    call = await load_call(call_id)
    assert call.agent_seen_at is not None
    assert call.nudged_at is None, "a call that heartbeated is never nudged"

    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(call_id, answered=200, seen=180, agent_seen=60, connected=True)
    assert await sweep_now() == 1
    row = await load_row(call_id)
    assert row.cum_session_ms == (200 - 60 + INTERVAL) * 1000
    assert row.disputed
    assert row.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT


async def test_a_quiet_call_whose_agent_let_go_is_billed_to_its_last_heartbeat(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(call_id, answered=100, seen=60, connected=True)

    assert await sweep_now() == 1
    row = await load_row(call_id)
    assert row.cum_session_ms == (100 - 60 + INTERVAL) * 1000
    assert (await load_call(call_id)).gone_at is not None


async def test_a_kept_call_at_its_ceiling_is_billed_to_the_ceiling(
    client, session, price_book, live_agent
):
    """Asked again at the ceiling, so a stale "still here" does not bill it short."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    ceiling = settings.voice_agent_max_session_seconds
    await rewind(call_id, answered=ceiling + 5, seen=ceiling - 100, connected=True, agent_seen=60)

    assert await sweep_now() == 1
    row = await load_row(call_id)
    assert row.cum_session_ms == ceiling * 1000
    assert row.end_reason is SessionEndReason.MAX_DURATION
    assert row.disputed, "the agent vouched for the end of it"


async def test_a_heartbeat_after_a_long_silence_revives_a_call_the_agent_still_holds(
    client, session, price_book, live_agent
):
    """Cut off from us, not from the agent — a network that reaches one and not the other."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=120, seen=90, connected=True)

    pulse = await beat(client, headers, call_id)
    assert pulse.status_code == 200
    assert pulse.json()["action"] == "continue"
    call = await load_call(call_id)
    assert call.ended_at is None and call.agent_seen_at is not None

    asked = len(live_agent.probes)
    assert (await beat(client, headers, call_id)).json()["action"] == "continue"
    assert len(live_agent.probes) == asked, "the next beat takes the fast path, no probe"


async def test_a_heartbeat_after_a_long_silence_stops_a_call_the_agent_dropped(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(call_id, answered=120, seen=90, connected=True)

    pulse = await beat(client, headers, call_id)
    assert pulse.json()["action"] == "stop"
    assert (await load_row(call_id)).cum_session_ms == (120 - 90 + INTERVAL) * 1000
    assert (await load_call(call_id)).gone_at is not None, "confirmed gone, never asked again"


# --- the line --------------------------------------------------------------------


async def test_a_hung_up_call_still_connected_to_the_agent_keeps_the_line(
    client, session, price_book, live_agent, monkeypatch
):
    """`DELETE`, then keep talking: billed to the `DELETE`, and one line only."""
    monkeypatch.setattr(voice_call_lifecycle, "HANGUP_SETTLE_SECONDS", 1)
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await beat(client, headers, first)
    bill = (await hang_up(client, headers, first)).json()
    assert bill["status"] == "ended" and bill["billed_ms"] < 5_000
    assert bill["price_micros"] == PER_MINUTE, "billed to the DELETE: one started minute"

    refused = await post_offer(client, headers)
    assert refused.status_code == 429
    assert code_of(refused) == "voice_call_still_connected"
    assert refused.headers["Retry-After"] == "1"
    assert "still connected" in refused.json()["detail"]

    live_agent.drop(live_agent.pc_id_of(1))
    # A client honouring Retry-After comes back after the window the refusal
    # was cached for; inside it, the cached answer stands.
    await rewind(first, agent_seen=2)
    second = await opened_call(client, headers)
    assert second != first
    assert (await load_call(first)).gone_at is not None, "confirmed gone, never asked again"
    assert (await load_row(first)).settled_micros == PER_MINUTE, "and never billed again"


async def test_a_refused_retry_reuses_the_last_answer_instead_of_probing_again(
    client, session, price_book, live_agent, monkeypatch
):
    monkeypatch.setattr(voice_call_lifecycle, "HANGUP_SETTLE_SECONDS", 3)
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await beat(client, headers, first)
    await hang_up(client, headers, first)
    await rewind(first, ended=10)

    assert code_of(await post_offer(client, headers)) == "voice_call_still_connected"
    asked = len(live_agent.probes)
    assert code_of(await post_offer(client, headers)) == "voice_call_still_connected"
    assert len(live_agent.probes) == asked, "the second refusal came from the cached answer"


async def test_a_hang_up_the_agent_notices_a_moment_later_does_not_refuse_the_next_call(
    client, session, price_book, live_agent, monkeypatch
):
    """The agent drops a closed call within seconds; the next open waits that long."""
    monkeypatch.setattr(voice_call_lifecycle, "HANGUP_SETTLE_SECONDS", 2)
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await beat(client, headers, first)
    await hang_up(client, headers, first)

    seen = {"probes": 0}

    def drop_on_second_probe(pc_id: str) -> None:
        if pc_id == live_agent.pc_id_of(1):
            seen["probes"] += 1
            if seen["probes"] == 2:
                live_agent.drop(pc_id)

    live_agent.on_probe = drop_on_second_probe
    assert (await post_offer(client, headers)).status_code == 201


async def test_a_never_connected_call_holds_the_line_only_once_it_proves_it_connected(
    client, session, price_book, live_agent
):
    """Hang up without ever heartbeating, keep the peer: free, but not a free extra line."""
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await hang_up(client, headers, first)
    # Inside the grace the retry works: this is what an honest failed connect looks like.
    second = await opened_call(client, headers)
    await hang_up(client, headers, second)

    # Both peers are still held long past both graces: those connected.
    for call_id in (first, second):
        await rewind(call_id, answered=NEVER_CONNECTED_GRACE + 60, seen=NEVER_CONNECTED_GRACE + 60,
                     nudged=NUDGE_GRACE + 30, ended=NUDGE_GRACE + 30)
    refused = await post_offer(client, headers)
    assert refused.status_code == 429 and code_of(refused) == "voice_call_still_connected"


async def test_the_line_is_not_refused_when_the_agent_cannot_be_asked(
    client, session, price_book, agent
):
    """No canary, no probes: refusing honest users because of it would be worse."""
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    await beat(client, headers, first)
    await hang_up(client, headers, first)
    assert (await post_offer(client, headers)).status_code == 201


async def test_an_agent_that_tore_the_call_down_frees_the_line_at_once(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    live_agent.drop(live_agent.pc_id_of(1))
    gone = await relay(client, headers, call_id, [host_candidate(1)])
    assert gone.status_code == 409 and code_of(gone) == "voice_call_gone"
    call = await load_call(call_id)
    assert call.ended_at is not None and call.gone_at is not None
    assert live_agent.nudges == [], "a peer the agent already dropped is not nudged"
    assert (await post_offer(client, headers)).status_code == 201


# --- candidates, throttle, TURN ----------------------------------------------------


async def test_a_batch_that_would_cross_the_cap_is_cut_to_what_fits(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    for start in (0, 30):
        batch = [host_candidate(n) for n in range(start, start + 30)]
        assert (await relay(client, headers, call_id, batch)).json()["relayed"] == 30

    cut = await relay(client, headers, call_id, [host_candidate(n) for n in range(60, 70)])
    assert cut.status_code == 200
    assert cut.json()["relayed"] == 4
    assert len(agent.patches[-1]["candidates"]) == 4, "only what fits goes upstream"

    spent = await relay(client, headers, call_id, [host_candidate(99)])
    assert spent.status_code == 400 and code_of(spent) == "voice_candidates_exhausted"


async def test_an_end_of_candidates_marker_on_an_ended_call_is_a_409(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await hang_up(client, headers, call_id)
    marker = await relay(client, headers, call_id, [{"candidate": "", "sdpMid": "0"}])
    assert marker.status_code == 409 and code_of(marker) == "voice_call_ended"


async def test_a_refused_candidate_has_its_own_code(client, session, price_book, agent):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    agent.candidates_reply = reply(400, body={"detail": "bad candidate"})
    refused = await relay(client, headers, call_id, [host_candidate(1)])
    assert refused.status_code == 400
    assert code_of(refused) == "voice_candidate_rejected"


async def test_the_open_throttle_counts_in_the_database_without_redis(
    client, session, price_book, agent, monkeypatch
):
    monkeypatch.setattr(settings, "voice_agent_max_opens_per_minute", 2)
    user, headers = await caller(session)
    for _ in range(2):
        call_id = await opened_call(client, headers)
        await hang_up(client, headers, call_id)

    throttled = await post_offer(client, headers)
    assert throttled.status_code == 429
    assert code_of(throttled) == "voice_call_rate_limited"
    assert 1 <= int(throttled.headers["Retry-After"]) <= 61
    _, reserved = await money(user.id)
    assert reserved == 0, "refused before anything was held"


async def test_a_zero_open_limit_refuses_cleanly_rather_than_crashing(
    client, session, price_book, agent, monkeypatch
):
    monkeypatch.setattr(settings, "voice_agent_max_opens_per_minute", 0)
    user, headers = await caller(session)
    refused = await post_offer(client, headers)
    assert refused.status_code == 429 and code_of(refused) == "voice_call_rate_limited"
    assert refused.headers["Retry-After"] == "60"
    problems = Settings(environment="production", voice_agent_max_opens_per_minute=0)
    with pytest.raises(RuntimeError, match="VOICE_AGENT_MAX_OPENS_PER_MINUTE"):
        problems.assert_production_ready()


async def test_config_mints_a_turn_credential_per_user(client, session, price_book, agent, monkeypatch):
    secret = "turn-shared-secret-long-enough"
    monkeypatch.setattr(settings, "voice_agent_turn_urls", "turn:turn.example.com:3478?transport=udp")
    monkeypatch.setattr(settings, "voice_agent_turn_secret", secret)
    user, headers = await caller(session)

    servers = (await client.get("/voice/config", headers=headers)).json()["ice_servers"]
    turn = [entry for entry in servers if entry.get("username")]
    assert len(turn) == 1
    expiry, _, owner = turn[0]["username"].partition(":")
    assert owner == str(user.id)
    assert int(expiry) > int(utcnow().timestamp()) + settings.voice_agent_max_session_seconds
    expected = base64.b64encode(
        hmac.new(secret.encode(), turn[0]["username"].encode(), hashlib.sha1).digest()
    ).decode()
    assert turn[0]["credential"] == expected
    assert turn[0]["urls"] == ["turn:turn.example.com:3478?transport=udp"]


async def test_no_turn_credential_is_handed_out_without_an_agent(client, session, monkeypatch):
    monkeypatch.setattr(settings, "voice_agent_turn_urls", "turn:turn.example.com:3478")
    monkeypatch.setattr(settings, "voice_agent_turn_secret", "turn-shared-secret-long-enough")
    user, headers = await caller(session)
    config = (await client.get("/voice/config", headers=headers)).json()
    assert config["available"] is False
    assert not [entry for entry in config["ice_servers"] if entry.get("credential")]


def test_a_turn_url_without_a_credential_is_refused_at_boot():
    broken = Settings(voice_agent_ice_servers='[{"urls": "turn:turn.example.com:3478"}]')
    with pytest.raises(ValueError, match="username and credential"):
        _ = broken.voice_agent_ice_server_list


# --- orphans, the reaper and a lost race ---------------------------------------------


async def test_the_reaper_never_bills_a_voice_orphan_its_ceiling(session, price_book):
    """A hold whose call row was never written is the voice sweep's to free, at zero."""
    user, _ = await caller(session)
    orphan = await voice_session(user.id)
    await backdate_session(orphan, seconds=grace_seconds() + 5)
    async with SessionLocal() as db:
        row = await db.get(AiSession, orphan)
        row.expires_at = utcnow() - timedelta(hours=1)
        await db.commit()

    async with SessionLocal() as db:
        assert await reconcile_service.reap_expired_sessions(db) == 0
    assert await sweep_now() == 1
    row = await load_row(orphan)
    assert row.status is AiSessionStatus.FAILED and row.settled_micros == 0


async def test_the_reaper_still_reaps_a_keyless_voice_session(session, price_book):
    """The orphan exclusion must not swallow every voice session with no key."""
    user, _ = await caller(session)
    ticket = await voice_session(user.id)
    async with SessionLocal() as db:
        row = await db.get(AiSession, ticket)
        row.idempotency_key = None
        row.expires_at = utcnow() - timedelta(hours=1)
        row.estimated_micros = 0
        await db.commit()
    async with SessionLocal() as db:
        assert await reconcile_service.reap_expired_sessions(db) == 1


async def test_an_abandon_that_read_a_stale_row_keeps_the_winners_answer(session, price_book):
    """The loser re-reads the winner's terminal row instead of stamping over it."""
    user, _ = await caller(session)
    ticket = await voice_session(user.id)
    async with SessionLocal() as loser:
        stale = await loser.get(AiSession, ticket)
        assert stale.status is AiSessionStatus.ACTIVE
        async with SessionLocal() as winner:
            await session_service.abandon_oneshot(
                winner, ai_session_id=ticket, end_reason=SessionEndReason.CLIENT_HANGUP, error_code=None
            )
        await session_service.abandon_oneshot(
            loser, ai_session_id=ticket, end_reason=SessionEndReason.TIMEOUT, error_code=None
        )
    row = await load_row(ticket)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.CLIENT_HANGUP, "the winner's answer stands"
    assert (await money(user.id))[1] == 0


async def test_an_abandon_whose_release_collides_and_rolls_back_does_not_crash(
    session, price_book, monkeypatch
):
    """The rollback branch: the loser's release hits the winner's ledger key.

    `_replay` is made to miss once, as it does when the winner commits between
    the loser's replay check and its insert, so the loser's flush meets the
    unique key, `_flush_ledger` rolls the transaction back and every loaded
    instance — the session row included — is expired. Touching it then would
    raise; the fresh re-read is what reads the winner's row instead.
    """
    user, _ = await caller(session)
    ticket = await voice_session(user.id)
    async with SessionLocal() as winner:
        await session_service.abandon_oneshot(
            winner, ai_session_id=ticket, end_reason=SessionEndReason.CLIENT_HANGUP, error_code=None
        )

    real_replay = wallet_repo._replay
    missed = {"once": False}

    async def miss_once(db, key):
        if not missed["once"] and key == f"release:{ticket}":
            missed["once"] = True
            return None
        return await real_replay(db, key)

    monkeypatch.setattr(wallet_repo, "_replay", miss_once)
    async with SessionLocal() as loser:
        stale = await loser.get(AiSession, ticket)
        # What the loser saw before the winner committed: still live, still holding.
        stale.status = AiSessionStatus.ACTIVE
        stale.reserved_micros = HOLD
        await session_service.abandon_oneshot(
            loser, ai_session_id=ticket, end_reason=SessionEndReason.TIMEOUT, error_code=None
        )
    assert missed["once"], "the collision path was really taken"
    row = await load_row(ticket)
    assert row.end_reason is SessionEndReason.CLIENT_HANGUP
    assert (await money(user.id))[1] == 0


async def test_every_hold_comes_back_across_the_whole_liveness_path(
    client, session, price_book, live_agent
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=50, seen=50)
    await sweep_now()
    _, reserved = await money(user.id)
    assert reserved == HOLD, "a kept call still holds its ceiling"
    live_agent.drop(live_agent.pc_id_of(1))
    await rewind(call_id, answered=150, seen=150, agent_seen=60)
    await sweep_now()
    assert (await money(user.id))[1] == 0

