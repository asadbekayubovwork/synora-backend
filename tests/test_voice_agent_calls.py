"""A voice call from its offer to its hang-up: candidates, heartbeats, the bill.

Billable time starts at the agent's answer, on our clock, and nothing the
client sends carries a clock of its own — a heartbeat moves `last_seen_at` to
*our* now. So a client can stop reporting; it cannot report a shorter call than
the one it had, or a longer one than we saw. Everything here is a consequence
of that, and each test moves a call's timestamps back rather than waiting,
because the price is a function of those timestamps and nothing else.

The first heartbeat is the line between free and billed. A call whose media
never came up — ICE that failed for want of a TURN server, the commonest failure
the agent's guide lists — never sends one, and costs nothing however it ends.

See `tests/voice_agent_support.py` for the fake agent and the arithmetic, and
`test_voice_agent.py` for the overview.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.base import as_utc
from app.db.session import SessionLocal
from app.models.billing_enums import (
    AiSessionStatus,
    BillingService,
    LedgerEntryKind,
    RoundingMode,
    SessionEndReason,
    UsageMetric,
)
from app.models.price_book import Price
from app.models.voice_call import VoiceCall
from app.services.ai import voice_agent_client, voice_agent_service, voice_call_lifecycle
from tests.voice_agent_support import (  # noqa: F401 - `agent` and `no_agent` are fixtures
    AGENT_KEY,
    AGENT_SHAPED_CANDIDATE,
    BROWSER_CANDIDATE,
    END_OF_CANDIDATES,
    FUNDED,
    HOLD,
    PC_ID_PREFIX,
    PER_MINUTE,
    agent,
    beat,
    caller,
    calls_of,
    code_of,
    fail,
    grace_seconds,
    hang_up,
    host_candidate,
    ledger_of,
    load_call,
    load_row,
    metric_value,
    money,
    no_agent,
    opened_call,
    post_offer,
    relay,
    reply,
    rewind,
    sweep_now,
)


# --- while the offer is in flight -----------------------------------------------------


async def _in_flight(user_id: uuid.UUID) -> uuid.UUID:
    async with SessionLocal() as db:
        return (
            await db.execute(select(VoiceCall.id).where(VoiceCall.user_id == user_id))
        ).scalar_one()


async def test_a_call_in_flight_can_neither_heartbeat_nor_relay_nor_be_swept(
    client, session, price_book, agent
):
    """Everything a client could do before the answer, done before the answer.

    This also proves the offer holds no database transaction open while it
    waits on the agent: on SQLite a transaction held across it would make the
    heartbeat's `UPDATE` below wait on the file lock until it timed out.
    """
    user, headers = await caller(session)
    seen: dict[str, object] = {}

    async def meanwhile() -> None:
        agent.while_offering = None
        call_id = await _in_flight(user.id)
        seen["heartbeat"] = await beat(client, headers, call_id)
        seen["candidates"] = await relay(client, headers, call_id, [BROWSER_CANDIDATE])
        seen["swept"] = await sweep_now()
        seen["second"] = await post_offer(client, headers)

    agent.while_offering = meanwhile

    opened = await post_offer(client, headers)

    assert opened.status_code == 201, opened.text
    for name in ("heartbeat", "candidates"):
        response = seen[name]
        assert response.status_code == 409, (name, response.text)
        assert code_of(response) == "voice_call_not_answered"
    assert seen["swept"] == 0, "an offer in flight is inside its grace"
    assert code_of(seen["second"]) == "voice_call_limit", "an unanswered call still holds the line"
    assert agent.patches == []

    call_id = uuid.UUID(opened.json()["ai_session_id"])
    assert (await beat(client, headers, call_id)).json()["action"] == "continue"
    assert (await load_call(call_id)).candidates == 0


async def test_a_call_released_while_its_offer_was_in_flight_is_a_503_and_free(
    client, session, price_book, agent
):
    """The answer landing on a call that has already given its hold back.

    The sweeper waits twice the offer's own deadline before it may release an
    unanswered call, so reaching this takes a clock that jumped. When it does,
    the call is already free and the answer is not recorded against it.
    """
    user, headers = await caller(session)

    async def meanwhile() -> None:
        agent.while_offering = None
        call_id = await _in_flight(user.id)
        await rewind(call_id, seen=grace_seconds() + 5)
        assert await sweep_now() == 1

    agent.while_offering = meanwhile

    response = await post_offer(client, headers)

    assert response.status_code == 503
    assert code_of(response) == "voice_offer_expired"
    (call,) = await calls_of(user.id)
    assert call.ended_at is not None
    assert call.upstream_pc_id is None and call.answered_at is None
    row = await load_row(call.id)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.TIMEOUT
    assert await money(user.id) == (FUNDED, 0)


# --- trickle ICE ----------------------------------------------------------------------


async def test_candidates_are_relayed_in_one_patch_in_the_agents_spelling(
    client, session, price_book, agent
):
    """`toJSON()` as it stands in, the agent's field names out, one round trip.

    The end-of-candidates marker is dropped, `usernameFragment` has nowhere to
    go and goes nowhere, and the batch is one `PATCH` carrying the `pc_id` the
    browser never saw.
    """
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)

    response = await relay(
        client, headers, call_id, [BROWSER_CANDIDATE, AGENT_SHAPED_CANDIDATE, END_OF_CANDIDATES]
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "relayed": 2}
    assert agent.patches == [
        {
            "pc_id": f"{PC_ID_PREFIX}1",
            "candidates": [
                {"candidate": BROWSER_CANDIDATE["candidate"], "sdp_mid": "0", "sdp_mline_index": 0},
                {"candidate": AGENT_SHAPED_CANDIDATE["candidate"], "sdp_mid": "0",
                 "sdp_mline_index": 0},
            ],
        }
    ]
    (patch,) = agent.sent("PATCH")
    assert patch.headers["x-api-key"] == AGENT_KEY
    assert "authorization" not in patch.headers
    assert (await load_call(call_id)).candidates == 2


async def test_an_end_of_candidates_marker_alone_costs_no_round_trip(
    client, session, price_book, agent
):
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)

    response = await relay(client, headers, call_id, [END_OF_CANDIDATES])

    assert response.json()["relayed"] == 0
    assert agent.patches == []
    assert (await load_call(call_id)).candidates == 0


async def test_a_call_may_relay_at_most_its_candidate_budget(client, session, price_book, agent):
    """Every candidate is a request to the agent on the one key all users share.

    A client looping on `onicecandidate` runs into this refusal, rather than
    into the agent's rate limit for everybody.
    """
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)
    per_request = voice_agent_service.MAX_CANDIDATES_PER_REQUEST
    assert 2 * per_request == voice_agent_service.MAX_CANDIDATES_PER_CALL

    for batch in range(2):
        batch_of = [host_candidate(batch * per_request + n) for n in range(per_request)]
        relayed = await relay(client, headers, call_id, batch_of)
        assert relayed.json()["relayed"] == per_request
    exhausted = await relay(client, headers, call_id, [host_candidate(999)])

    assert exhausted.status_code == 400
    assert code_of(exhausted) == "voice_candidates_exhausted"
    assert len(agent.patches) == 2
    assert (await load_call(call_id)).candidates == voice_agent_service.MAX_CANDIDATES_PER_CALL


@pytest.mark.parametrize(
    "candidates",
    [
        pytest.param([], id="empty-batch"),
        pytest.param(
            [host_candidate(n) for n in range(voice_agent_service.MAX_CANDIDATES_PER_REQUEST + 1)],
            id="batch-too-large",
        ),
        pytest.param(
            [{"candidate": "candidate:1 1 udp 1 1.2.3.4 5 typ host\r\na=ice-lite", "sdpMid": "0"}],
            id="a-second-sdp-line-smuggled-in",
        ),
        pytest.param(
            [{"candidate": "c" * (voice_agent_service.MAX_CANDIDATE_CHARACTERS + 1)}],
            id="candidate-too-long",
        ),
        pytest.param([{"sdpMid": "0", "sdpMLineIndex": 0}], id="no-candidate-field"),
        pytest.param([{**BROWSER_CANDIDATE, "sdpMLineIndex": -1}], id="negative-mline"),
    ],
)
async def test_a_malformed_candidate_batch_is_a_422_and_nothing_is_relayed(
    client, session, price_book, agent, candidates
):
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)

    response = await relay(client, headers, call_id, candidates)

    assert response.status_code == 422, response.text
    assert agent.patches == []
    assert (await load_call(call_id)).candidates == 0


async def test_candidates_for_somebody_elses_call_are_a_404_and_after_the_end_a_409(
    client, session, price_book, agent
):
    _, owner = await caller(session)
    _, stranger = await caller(session)
    call_id = await opened_call(client, owner)

    for batch in ([BROWSER_CANDIDATE], [END_OF_CANDIDATES]):
        response = await relay(client, stranger, call_id, batch)
        assert response.status_code == 404, "somebody else's id is a 404, never a 403"
        assert code_of(response) == "voice_call_not_found"
    assert (await relay(client, owner, uuid.uuid4(), [BROWSER_CANDIDATE])).status_code == 404

    await hang_up(client, owner, call_id)
    ended = await relay(client, owner, call_id, [BROWSER_CANDIDATE])

    assert ended.status_code == 409
    assert code_of(ended) == "voice_call_ended"
    # Nothing of the caller's reached the agent. The one PATCH there is ours:
    # the call never connected, so its hang-up nudged the peer the agent may
    # still be holding for it into failing ICE.
    assert agent.patches == [
        {"pc_id": agent.pc_id_of(1), "candidates": [voice_agent_client.NUDGE_CANDIDATE]}
    ]


@pytest.mark.parametrize(
    ("connected", "price"), [(False, 0), (True, PER_MINUTE)], ids=["never-connected", "connected"]
)
async def test_an_agent_that_forgot_the_call_ends_it_here_too(
    client, session, price_book, agent, connected, price
):
    """A 404 on `PATCH` is the one authoritative end this module ever hears.

    The agent tore the call down — the browser closed it, or ICE never
    completed — so it is settled now rather than left for the heartbeat to
    lapse: free if it never connected, answer-to-now if it did.
    """
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    if connected:
        await rewind(call_id, answered=30, seen=5, connected=True)
    agent.candidates_reply = reply(404, body={"detail": "Peer connection not found"})

    response = await relay(client, headers, call_id, [BROWSER_CANDIDATE])

    assert response.status_code == 409
    assert code_of(response) == "voice_call_gone"
    record = (await client.get(f"/voice/sessions/{call_id}", headers=headers)).json()
    assert record["status"] == "ended"
    assert record["end_reason"] == "upstream_error"
    assert record["price_micros"] == price
    assert await money(user.id) == (FUNDED - price, 0)


async def test_an_unreachable_agent_on_a_relay_is_a_502_and_the_call_stays_up(
    client, session, price_book, agent
):
    """A relay that failed in transit is not evidence the call is over."""
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)
    agent.candidates_reply = fail(httpx.ConnectError)

    response = await relay(client, headers, call_id, [BROWSER_CANDIDATE])

    assert response.status_code == 502
    assert code_of(response) == "voice_agent_unreachable"
    assert (await load_call(call_id)).ended_at is None


# --- the heartbeat ----------------------------------------------------------------------


async def test_the_first_heartbeat_connects_the_call_and_later_ones_keep_it(
    client, session, price_book, agent
):
    """The first beat is what turns "would be released free" into "is billed".

    It stamps `connected_at` once; later beats move `last_seen_at` to our own
    clock and leave `connected_at` where it was. None of them touches money.
    """
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)

    first = await beat(client, headers, call_id)

    assert first.status_code == 200
    pulse = first.json()
    assert pulse["action"] == "continue"
    assert 0 <= pulse["elapsed_ms"] < 5_000
    # Both are floored to the millisecond separately, so they can sum to one short.
    assert 599_999 <= pulse["elapsed_ms"] + pulse["remaining_ms"] <= 600_000
    assert pulse["next_heartbeat_seconds"] == settings.voice_agent_heartbeat_seconds
    after_first = await load_call(call_id)
    assert after_first.connected_at is not None
    assert after_first.heartbeats == 1

    rewound = await rewind(call_id, seen=20)
    second = await beat(client, headers, call_id)

    assert second.json()["action"] == "continue"
    after_second = await load_call(call_id)
    assert after_second.heartbeats == 2
    assert after_second.connected_at == after_first.connected_at
    assert as_utc(after_second.last_seen_at) >= rewound
    row = await load_row(call_id)
    assert row.status is AiSessionStatus.ACTIVE and row.reserved_micros == HOLD


async def test_a_heartbeat_inside_the_last_minute_warns(client, session, price_book, agent):
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)

    await rewind(call_id, answered=530, seen=5, connected=True)
    early = (await beat(client, headers, call_id)).json()
    await rewind(call_id, answered=550, seen=5, connected=True)
    late = (await beat(client, headers, call_id)).json()

    assert early["action"] == "continue"
    assert late["action"] == "warn"
    assert 0 < late["remaining_ms"] <= voice_agent_service.WARN_BEFORE_END_SECONDS * 1000
    assert (await load_call(call_id)).ended_at is None, "a warning is not an ending"


async def test_a_heartbeat_past_the_ceiling_stops_the_call_billed_exactly_the_ceiling(
    client, session, price_book, agent
):
    """Whatever the client did after it should have stopped, the bill stops here."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=700, seen=5, connected=True)

    pulse = (await beat(client, headers, call_id)).json()

    assert pulse["action"] == "stop"
    assert pulse["elapsed_ms"] == 600_000
    assert pulse["remaining_ms"] == 0
    record = (await client.get(f"/voice/sessions/{call_id}", headers=headers)).json()
    assert record["status"] == "ended"
    assert record["end_reason"] == "max_duration"
    assert record["billed_ms"] == 600_000
    assert record["price_micros"] == HOLD
    assert record["disputed"] is False
    assert await money(user.id) == (FUNDED - HOLD, 0)


async def test_a_heartbeat_after_the_timeout_stops_the_call_billed_to_the_last_beat(
    client, session, price_book, agent
):
    """Silence ended the call a while ago, and this beat is only the first to notice.

    Billed to the last beat heard plus one interval — the latest the call could
    have ended without its client noticing it owed a report — which is a
    hundred and fifteen seconds here, two started minutes, and `disputed`
    because that end is inferred.
    """
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=200, seen=100, connected=True)

    pulse = (await beat(client, headers, call_id)).json()

    assert pulse["action"] == "stop"
    assert pulse["elapsed_ms"] == 115_000
    record = (await client.get(f"/voice/sessions/{call_id}", headers=headers)).json()
    assert record["end_reason"] == "heartbeat_timeout"
    assert record["disputed"] is True
    assert record["billed_ms"] == 115_000
    assert record["price_micros"] == 2 * PER_MINUTE
    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE, 0)


async def test_a_call_that_never_connected_and_then_heartbeats_too_late_is_free(
    client, session, price_book, agent
):
    """ICE that took longer than the timeout never carried a word. Nothing is owed."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=60, seen=60)

    pulse = (await beat(client, headers, call_id)).json()

    assert pulse["action"] == "stop"
    assert pulse["elapsed_ms"] == 0
    record = (await client.get(f"/voice/sessions/{call_id}", headers=headers)).json()
    assert record["price_micros"] == 0
    assert record["connected_at"] is None
    assert await money(user.id) == (FUNDED, 0)


async def test_a_heartbeat_on_an_ended_call_answers_stop_rather_than_an_error(
    client, session, price_book, agent
):
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=90, seen=5, connected=True)
    bill = (await hang_up(client, headers, call_id)).json()

    late = await beat(client, headers, call_id)

    assert late.status_code == 200
    assert late.json()["action"] == "stop"
    assert late.json()["elapsed_ms"] == bill["billed_ms"]
    assert late.json()["remaining_ms"] == 0
    assert (await load_call(call_id)).heartbeats == 0


async def test_a_heartbeat_for_somebody_elses_call_is_a_404_and_moves_nothing(
    client, session, price_book, agent
):
    _, owner = await caller(session)
    _, stranger = await caller(session)
    call_id = await opened_call(client, owner)

    theirs = await beat(client, stranger, call_id)
    unknown = await beat(client, owner, uuid.uuid4())
    garbled = await client.post("/voice/sessions/not-a-uuid/heartbeat", headers=owner)

    assert theirs.status_code == 404 and code_of(theirs) == "voice_call_not_found"
    assert unknown.status_code == 404
    assert garbled.status_code == 422
    call = await load_call(call_id)
    assert call.heartbeats == 0 and call.connected_at is None


# --- hanging up -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(1, 1), (59, 1), (61, 2), (90, 2), (599, 10)],
    ids=["1s", "59s", "61s", "90s", "599s"],
)
async def test_hanging_up_bills_answer_to_now_by_the_started_minute(
    client, session, price_book, agent, seconds, minutes
):
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=seconds, seen=min(seconds, 5), connected=True)

    response = await hang_up(client, headers, call_id)

    assert response.status_code == 200
    bill = response.json()
    assert bill["status"] == "ended"
    assert bill["end_reason"] == "client_hangup"
    assert seconds * 1000 <= bill["billed_ms"] <= min(seconds * 1000 + 2_000, 600_000)
    assert bill["price_micros"] == minutes * PER_MINUTE
    assert bill["reserved_micros"] == 0
    assert bill["disputed"] is False
    assert bill["connected_at"] is not None and bill["ended_at"] is not None
    assert await money(user.id) == (FUNDED - minutes * PER_MINUTE, 0)


async def test_a_call_hung_up_the_moment_it_connected_still_pays_its_first_minute(
    client, session, price_book, agent
):
    """Never zero: a zero quantity skips the price line, and with it the floor."""
    _, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await beat(client, headers, call_id)

    bill = (await hang_up(client, headers, call_id)).json()

    assert bill["billed_ms"] >= 1
    assert bill["price_micros"] == PER_MINUTE


async def test_the_connection_fee_minimum_applies_to_a_very_short_call(
    client, session, price_book, agent
):
    """With an exact per-millisecond rate, the minimum is the only floor there is.

    The fixture's CEIL rate already charges a whole minute for a second, so its
    quarter-credit minimum never fires. A row for this model beats the `*` row,
    and prices the call exactly — six seconds is fifty thousand micros — which
    the minimum then lifts to a quarter of a credit.
    """
    session.add(
        Price(
            price_book_version_id=price_book.id,
            service=BillingService.VOICE_AGENT,
            model_key=settings.voice_agent_model_key,
            metric=UsageMetric.SESSION_MS,
            unit_size=60_000,
            price_micros_per_unit=PER_MINUTE,
            rounding=RoundingMode.EXACT,
            min_charge_micros=250_000,
        )
    )
    await session.commit()
    user, headers = await caller(session)
    config = (await client.get("/voice/config", headers=headers)).json()
    assert config["hold_micros"] == HOLD, "ten exact minutes is still five credits"
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=6, seen=1, connected=True)

    bill = (await hang_up(client, headers, call_id)).json()

    assert 6_000 <= bill["billed_ms"] < 8_000
    assert bill["price_micros"] == 250_000
    assert await money(user.id) == (FUNDED - 250_000, 0)


async def test_hanging_up_twice_answers_the_first_bill_and_charges_once(
    client, session, price_book, agent
):
    """The Stop button and `pagehide` routinely both fire for one call."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=90, seen=5, connected=True)

    first = await hang_up(client, headers, call_id)
    second = await hang_up(client, headers, call_id)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    debits = [e for e in await ledger_of(user.id) if e.kind is LedgerEntryKind.DEBIT]
    assert [e.amount_micros for e in debits] == [-2 * PER_MINUTE]
    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE, 0)


async def test_a_call_that_never_connected_ends_free(client, session, price_book, agent):
    """No heartbeat, no media: ICE failed, or the tab went away first."""
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)

    bill = (await hang_up(client, headers, call_id)).json()

    assert bill["status"] == "ended"
    assert bill["end_reason"] == "client_hangup"
    assert bill["billed_ms"] == 0 and bill["price_micros"] == 0
    assert bill["connected_at"] is None
    assert bill["reserved_micros"] == 0
    row = await load_row(call_id)
    assert row.status is AiSessionStatus.FAILED
    assert row.error_code == "voice_never_connected"
    assert await money(user.id) == (FUNDED, 0)
    kinds = sorted(e.kind.value for e in await ledger_of(user.id) if e.ai_session_id == call_id)
    assert kinds == ["hold", "release"], "held and given back, never debited"


async def test_ending_somebody_elses_call_is_a_404(client, session, price_book, agent):
    _, owner = await caller(session)
    _, stranger = await caller(session)
    call_id = await opened_call(client, owner)

    response = await hang_up(client, stranger, call_id)

    assert response.status_code == 404
    assert code_of(response) == "voice_call_not_found"
    assert (await load_call(call_id)).ended_at is None


async def test_a_settlement_racing_a_hang_up_moves_the_wallet_once(
    client, session, price_book, agent
):
    """Whoever notices first settles; whoever comes second replays.

    A sweeper that read the row before the client's `DELETE` landed arrives at
    `_settle` holding a stale copy of a live call. The second settlement of a
    session is a replay of the first, and only whoever stamps `ended_at` counts
    the call.
    """
    user, headers = await caller(session)
    call_id = await opened_call(client, headers)
    await rewind(call_id, answered=90, seen=5, connected=True)
    counted = metric_value("synora_voice_calls_total", end_reason="client_hangup", connected="yes")

    async with SessionLocal() as db:
        stale = (await db.execute(select(VoiceCall).where(VoiceCall.id == call_id))).scalar_one()
        bill = (await hang_up(client, headers, call_id)).json()
        await voice_call_lifecycle.settle(db, stale, end_reason=SessionEndReason.HEARTBEAT_TIMEOUT)

    record = (await client.get(f"/voice/sessions/{call_id}", headers=headers)).json()
    assert record == bill
    debits = [e for e in await ledger_of(user.id) if e.kind is LedgerEntryKind.DEBIT]
    assert len(debits) == 1
    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE, 0)
    assert metric_value(
        "synora_voice_calls_total", end_reason="client_hangup", connected="yes"
    ) == counted + 1


# --- reading --------------------------------------------------------------------------


async def test_calls_list_newest_first_in_keyset_pages_and_only_the_callers_own(
    client, session, price_book, agent
):
    _, headers = await caller(session)
    _, stranger = await caller(session)
    placed = []
    for _ in range(3):
        call_id = await opened_call(client, headers)
        await hang_up(client, headers, call_id)
        placed.append(str(call_id))

    first = (await client.get("/voice/sessions", headers=headers, params={"limit": 2})).json()
    second = (
        await client.get(
            "/voice/sessions",
            headers=headers,
            params={"limit": 2, "cursor": first["page"]["next_cursor"]},
        )
    ).json()
    theirs = await client.get("/voice/sessions", headers=stranger)

    assert [c["ai_session_id"] for c in first["calls"]] == placed[:0:-1]
    assert first["page"]["has_more"] is True and first["page"]["limit"] == 2
    assert [c["ai_session_id"] for c in second["calls"]] == placed[:1]
    assert second["page"]["has_more"] is False and second["page"]["next_cursor"] is None
    assert theirs.json()["calls"] == []
    assert PC_ID_PREFIX not in theirs.text and "upstream_pc_id" not in json.dumps(first)


async def test_get_call_is_the_callers_own_or_a_404(client, session, price_book, agent):
    _, owner = await caller(session)
    _, stranger = await caller(session)
    call_id = await opened_call(client, owner)
    await beat(client, owner, call_id)

    mine = await client.get(f"/voice/sessions/{call_id}", headers=owner)
    theirs = await client.get(f"/voice/sessions/{call_id}", headers=stranger)
    nobodys = await client.get(f"/voice/sessions/{uuid.uuid4()}", headers=owner)

    assert mine.status_code == 200
    record = mine.json()
    assert record["status"] == "live" and record["heartbeats"] == 1
    assert record["connected_at"] is not None and record["ended_at"] is None
    assert record["billed_ms"] == 0 and record["price_micros"] == 0
    assert theirs.status_code == 404 and code_of(theirs) == "voice_call_not_found"
    assert nobodys.status_code == 404
