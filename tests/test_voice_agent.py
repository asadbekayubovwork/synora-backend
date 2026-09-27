"""The voice-agent gateway: signalling through us, and a bill made of timestamps.

Every other metered session in this codebase has its work pass through our
side of the gateway, so its tests can say "this many characters went out" and
check the wallet against it. A voice call has no such quantity. The audio flows
between the browser and the agent over WebRTC, and what reaches us is an SDP
offer, its answer and a burst of ICE candidates in the first second — after
which the only evidence of the call is the client's heartbeat. So the cases
worth pinning down are all about *which clock* a charge was read off, and they
are split three ways by when in a call's life they happen:

* **opening**, here — every refusal, ours or the agent's, gives the hold back
  whole, and a refusal we can make without the agent is made before anything
  is held; the agent's key and its `pc_id` never reach a browser, and the
  browser's SDP reaches the agent byte for byte;
* **the call itself**, in `test_voice_agent_calls.py` — billable time starts
  at the agent's answer on our clock, a call that never sent a heartbeat never
  connected and costs nothing, and the ceiling is a hard stop on the bill;
* **the calls nobody ended**, in `test_voice_agent_settlement.py` — silence is
  billed to the last heartbeat plus one interval and flagged `disputed`, and
  the sweep, the reaper and the admin reconcile agree about which sessions are
  whose.

The fake agent, the SDP and the clock are `tests/voice_agent_support.py`'s.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core import throttle
from app.core.cache import NullCache
from app.core.config import settings
from app.core.exceptions import BadRequestError, ServiceUnavailableError
from app.models.billing_enums import (
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    SessionEndReason,
)
from app.models.user import User
from app.models.voice_call import UPSTREAM_PC_ID_MAX_LENGTH
from app.services.ai import voice_agent_client, voice_agent_service
from tests.conftest import auth, fund, register_and_verify
from tests.voice_agent_support import (  # noqa: F401 - `agent` and `no_agent` are fixtures
    AGENT_KEY,
    ANSWER_SDP,
    BROWSER_CANDIDATE,
    DEFAULT_ICE_SERVERS,
    FUNDED,
    HOLD,
    OFFER,
    PC_ID_PREFIX,
    PER_MINUTE,
    agent,
    caller,
    calls_of,
    code_of,
    fail,
    hang_up,
    load_call,
    load_row,
    metric_value,
    money,
    no_agent,
    offer_sdp,
    opened_call,
    post_offer,
    relay,
    reply,
    rewind,
    rows_of,
)


# --- configuration -------------------------------------------------------------


async def test_config_on_a_deployment_with_no_agent_says_so_instead_of_a_503(client, session):
    """`available: false` is how a client decides not to draw the call button.

    A 503 here would make the ordinary state of a deployment without an agent
    look like an outage to every client that asks.
    """
    _, headers = await caller(session)

    assert (await client.get("/voice/config")).status_code == 401
    response = await client.get("/voice/config", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["per_minute_micros"] is None and body["hold_micros"] is None
    assert body["ice_servers"] == json.loads(DEFAULT_ICE_SERVERS)
    assert body["heartbeat_interval_seconds"] == settings.voice_agent_heartbeat_seconds
    assert body["heartbeat_timeout_seconds"] == settings.voice_agent_heartbeat_timeout_seconds


async def test_config_prices_a_minute_and_the_hold_from_the_price_book(
    client, session, price_book, agent, monkeypatch
):
    """The hold is what an account needs to start a call, so it is published.

    And the ICE servers go to the browser verbatim — TURN credentials included,
    because `new RTCPeerConnection({iceServers})` cannot use them otherwise —
    while the agent's own key appears nowhere in the body.
    """
    ice = [
        {"urls": ["stun:stun.example.test:3478"]},
        {
            "urls": ["turn:turn.example.test:3478?transport=udp", "turns:turn.example.test:5349"],
            "username": "synora",
            "credential": "turn-credential-replace-me",
        },
    ]
    monkeypatch.setattr(settings, "voice_agent_ice_servers", json.dumps(ice))
    _, headers = await caller(session)

    response = await client.get("/voice/config", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["per_minute_micros"] == PER_MINUTE
    assert body["per_minute"] == "0.500000"
    assert body["hold_micros"] == HOLD
    assert body["hold"] == "5.000000"
    assert body["max_session_seconds"] == 600
    assert body["max_concurrent_calls"] == 1
    assert body["ice_servers"] == ice
    assert AGENT_KEY not in response.text
    assert agent.requests == [], "reading the config must not wake the agent"


async def test_config_without_a_price_book_is_available_but_unpriced(client, session, agent):
    _, headers = await caller(session)

    body = (await client.get("/voice/config", headers=headers)).json()

    assert body["available"] is True
    assert body["per_minute_micros"] is None and body["hold_micros"] is None


@pytest.mark.parametrize(
    "ice_servers",
    ["not json at all", '[{"username": "no-urls"}]', '{"urls": ["stun:x"]}'],
    ids=["not-json", "entry-without-urls", "not-a-list"],
)
async def test_malformed_ice_servers_are_a_503_naming_the_variable(
    client, session, agent, monkeypatch, ice_servers
):
    monkeypatch.setattr(settings, "voice_agent_ice_servers", ice_servers)
    _, headers = await caller(session)

    response = await client.get("/voice/config", headers=headers)

    assert response.status_code == 503
    assert code_of(response) == "voice_agent_misconfigured"
    assert "VOICE_AGENT_ICE_SERVERS" in response.json()["detail"]


async def test_an_unconfigured_deployment_refuses_calls_before_holding_anything(
    client, session, price_book
):
    user, headers = await caller(session)

    opened = await post_offer(client, headers)
    relayed = await relay(client, headers, uuid.uuid4(), [BROWSER_CANDIDATE])

    assert opened.status_code == 503 and code_of(opened) == "voice_agent_not_configured"
    assert relayed.status_code == 503 and code_of(relayed) == "voice_agent_not_configured"
    assert await rows_of(user.id) == []
    assert await money(user.id) == (FUNDED, 0)
    with pytest.raises(ServiceUnavailableError):
        await voice_agent_client.healthz()


# --- the offer ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sdp", "code"),
    [
        pytest.param(offer_sdp(video=False), "voice_offer_no_video", id="no-video-transceiver"),
        pytest.param(
            # A video line that is not a media line — inside an attribute — is
            # not a negotiated transceiver, and the regex is anchored for this.
            offer_sdp(video=False).replace("a=sendrecv", "a=sendrecv x m=video", 1),
            "voice_offer_no_video",
            id="video-mentioned-not-negotiated",
        ),
        pytest.param(offer_sdp(audio=False), "voice_offer_no_audio", id="no-audio"),
        pytest.param("hello, agent", "voice_offer_invalid", id="not-sdp"),
        pytest.param(OFFER.replace("v=0", "v=1", 1), "voice_offer_invalid", id="wrong-version"),
    ],
)
async def test_an_offer_that_cannot_work_is_refused_before_anything_is_held(
    client, session, price_book, agent, sdp, code
):
    """The worst of the guide's three client mistakes is visible in the offer.

    No video transceiver connects a call in which no audio ever flows and
    nothing anywhere reports an error. Refusing it here, with a message that
    names the fix, costs the user nothing: no hold, no session row, no offer
    handed to the agent.
    """
    user, headers = await caller(session)

    response = await post_offer(client, headers, sdp=sdp)

    assert response.status_code == 400, response.text
    assert code_of(response) == code
    assert await rows_of(user.id) == []
    assert await calls_of(user.id) == []
    assert agent.offers == []
    assert await money(user.id) == (FUNDED, 0)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"sdp": OFFER, "type": "answer"}, id="an-answer"),
        pytest.param({"sdp": OFFER, "type": "pranswer"}, id="a-provisional-answer"),
        pytest.param({"sdp": "", "type": "offer"}, id="empty"),
        pytest.param({"type": "offer"}, id="missing"),
        pytest.param(
            {"sdp": OFFER + "a=x-padding:" + "p" * voice_agent_service.MAX_SDP_CHARACTERS,
             "type": "offer"},
            id="too-large",
        ),
    ],
)
async def test_an_offer_the_schema_refuses_is_a_422_and_holds_nothing(
    client, session, price_book, agent, body
):
    """The type and the size are the schema's to check, so these are 422s.

    `validate_offer` checks both again for a caller that is not this route, and
    the next test holds it to that; over HTTP the schema answers first.
    """
    user, headers = await caller(session)

    response = await client.post("/voice/sessions", headers=headers, json=body)

    assert response.status_code == 422, response.text
    assert await rows_of(user.id) == []
    assert agent.offers == []


def test_validate_offer_names_each_refusal():
    voice_agent_service.validate_offer(OFFER, "offer")
    # A call without a data channel is legitimate, just blind to transcript
    # and state events — the guide says so, and it is deliberately let through.
    voice_agent_service.validate_offer(offer_sdp(data=False), "offer")

    cases = [
        ((OFFER, "answer"), "voice_offer_invalid"),
        (("v=0\r\n" + "a=x\r\n" * voice_agent_service.MAX_SDP_CHARACTERS, "offer"),
         "voice_offer_too_large"),
        (("m=audio 9 RTP/AVP 0\r\n", "offer"), "voice_offer_invalid"),
        ((offer_sdp(audio=False), "offer"), "voice_offer_no_audio"),
        ((offer_sdp(video=False), "offer"), "voice_offer_no_video"),
    ]
    for (sdp, sdp_type), code in cases:
        with pytest.raises(BadRequestError) as refused:
            voice_agent_service.validate_offer(sdp, sdp_type)
        assert refused.value.code == code


async def test_an_offer_without_a_data_channel_is_accepted(client, session, price_book, agent):
    _, headers = await caller(session)

    response = await post_offer(client, headers, sdp=offer_sdp(data=False))

    assert response.status_code == 201, response.text
    assert agent.offers == [{"sdp": offer_sdp(data=False), "type": "offer"}]


# --- the happy path -----------------------------------------------------------------


async def test_a_call_opens_holding_the_ceiling_and_relays_the_answer_verbatim(
    client, session, price_book, agent
):
    """Through the real sign-in once, and every property of an open at once.

    The browser's SDP reaches the agent byte for byte and the agent's comes back
    the same way; our key goes upstream and the caller's bearer token does not;
    the agent's `pc_id` is kept and never shown. The hold is the price of a call
    that runs to the ceiling, and nothing is charged yet.
    """
    tokens = await register_and_verify(client, email="caller@example.com")
    user_id = (
        await session.execute(select(User.id).where(User.email == "caller@example.com"))
    ).scalar_one()
    await fund(session, user_id, paid=FUNDED)
    await session.commit()
    headers = auth(tokens["access_token"])

    response = await post_offer(client, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    call_id = uuid.UUID(body["ai_session_id"])
    assert body["sdp"] == ANSWER_SDP
    assert body["type"] == "answer"
    assert body["reserved_micros"] == HOLD
    assert body["reserved"] == "5.000000"
    assert body["heartbeat_interval_seconds"] == settings.voice_agent_heartbeat_seconds
    assert body["heartbeat_timeout_seconds"] == settings.voice_agent_heartbeat_timeout_seconds
    answered_at = datetime.fromisoformat(body["answered_at"])
    assert datetime.fromisoformat(body["expires_at"]) - answered_at == timedelta(seconds=600)

    # What must not leave this process.
    assert "pc_id" not in body
    assert PC_ID_PREFIX not in response.text
    assert AGENT_KEY not in response.text

    (sent,) = agent.sent("POST")
    assert json.loads(sent.content) == {"sdp": OFFER, "type": "offer"}
    assert sent.headers["x-api-key"] == AGENT_KEY
    assert "authorization" not in sent.headers, "the caller's token is not the supplier's business"

    call = await load_call(call_id)
    assert call.user_id == user_id
    assert call.upstream_pc_id == f"{PC_ID_PREFIX}1"
    assert call.answered_at is not None and call.connected_at is None
    assert call.ended_at is None and call.heartbeats == 0

    row = await load_row(call_id)
    assert row.service is BillingService.VOICE_AGENT
    assert row.kind is AiSessionKind.REALTIME
    assert row.status is AiSessionStatus.ACTIVE
    assert row.model_key == settings.voice_agent_model_key
    assert row.reserved_micros == HOLD
    assert row.settled_micros == 0
    assert row.idempotency_key.startswith(f"{user_id}:voice:auto:")

    assert await money(user_id) == (FUNDED - HOLD, HOLD)

    fetched = await client.get(f"/voice/sessions/{call_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "live"
    assert fetched.json()["reserved_micros"] == HOLD
    assert PC_ID_PREFIX not in fetched.text


# --- the agent's refusals -----------------------------------------------------------


@pytest.mark.parametrize(
    ("upstream", "status", "code", "retry_after"),
    [
        pytest.param(
            reply(401, body={"detail": "unknown or missing api key"}),
            503, "voice_agent_key_rejected", None, id="401-our-key",
        ),
        pytest.param(reply(403, body={"detail": "forbidden"}),
                     503, "voice_agent_key_rejected", None, id="403-our-key"),
        pytest.param(
            reply(429, body={"detail": "at capacity"}),
            429, "voice_agent_busy", str(voice_agent_client.DEFAULT_RETRY_AFTER_SECONDS),
            id="429-every-line-busy",
        ),
        pytest.param(
            reply(503, body={"detail": "warming up"}),
            503, "voice_agent_unavailable",
            str(voice_agent_client.UNAVAILABLE_RETRY_AFTER_SECONDS), id="503-warming-up",
        ),
        pytest.param(reply(400, body={"detail": "Invalid SDP"}),
                     400, "voice_offer_rejected", None, id="400-their-sdp"),
        pytest.param(reply(422, body={"detail": [{"msg": "field required"}]}),
                     400, "voice_offer_rejected", None, id="422-their-body"),
        pytest.param(reply(404, body={"detail": "Not Found"}),
                     502, "voice_agent_unreachable", None, id="404-wrong-service"),
        pytest.param(reply(500, content=b"Internal Server Error"),
                     502, "voice_agent_unreachable", None, id="500"),
        pytest.param(
            reply(307, headers={"Location": "http://elsewhere.test/api/offer"}),
            502, "voice_agent_unreachable", None, id="redirect-is-not-followed",
        ),
        pytest.param(fail(httpx.ReadTimeout), 502, "voice_agent_unreachable", None,
                     id="read-timeout"),
        pytest.param(fail(httpx.ConnectError), 502, "voice_agent_unreachable", None,
                     id="connection-refused"),
    ],
)
async def test_an_upstream_refusal_gives_the_hold_back_whole(
    client, session, price_book, agent, upstream, status, code, retry_after
):
    """A call the agent never answered is not a call, whoever's fault that was.

    The hold was placed before the offer went out — it has to be, or a 402
    would arrive after the agent had built a pipeline — so every one of these
    has something to give back, and each must give all of it back and charge
    nothing. The line is free afterwards: a refused call is not a live one.
    """
    user, headers = await caller(session)
    agent.offer_reply = upstream
    errors_before = metric_value(
        "synora_voice_upstream_errors_total", operation="offer", code=code
    )

    response = await post_offer(client, headers)

    assert response.status_code == status, response.text
    assert code_of(response) == code
    assert response.headers.get("retry-after") == retry_after
    assert AGENT_KEY not in response.text
    assert len(agent.requests) == 1, "one request, and a redirect is never followed"

    (row,) = await rows_of(user.id)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.UPSTREAM_ERROR
    assert row.error_code == code
    assert row.reserved_micros == 0 and row.settled_micros == 0
    (call,) = await calls_of(user.id)
    assert call.ended_at is not None
    assert call.upstream_pc_id is None and call.answered_at is None
    assert await money(user.id) == (FUNDED, 0)
    assert metric_value(
        "synora_voice_upstream_errors_total", operation="offer", code=code
    ) == errors_before + 1

    agent.offer_reply = agent._answer
    assert (await post_offer(client, headers)).status_code == 201, "the line is free again"


@pytest.mark.parametrize(
    "upstream",
    [
        pytest.param(reply(200, content=b"<html>tunnel login</html>",
                           headers={"content-type": "text/html"}), id="html"),
        pytest.param(reply(200, body=["v=0"]), id="a-list"),
        pytest.param(reply(200, body={"sdp": ANSWER_SDP, "type": "answer"}), id="no-pc-id"),
        pytest.param(reply(200, body={"sdp": ANSWER_SDP, "type": "offer", "pc_id": "pc"}),
                     id="not-an-answer"),
        pytest.param(reply(200, body={"sdp": "hello", "type": "answer", "pc_id": "pc"}),
                     id="not-sdp"),
        pytest.param(reply(200, body={"sdp": ANSWER_SDP, "type": "answer", "pc_id": ""}),
                     id="empty-pc-id"),
        pytest.param(
            reply(200, body={"sdp": ANSWER_SDP, "type": "answer",
                             "pc_id": "p" * (UPSTREAM_PC_ID_MAX_LENGTH + 1)}),
            id="pc-id-longer-than-its-column",
        ),
        pytest.param(reply(200, body={"sdp": ANSWER_SDP, "type": "answer", "pc_id": 7}),
                     id="numeric-pc-id"),
    ],
)
async def test_an_answer_we_cannot_read_is_a_502_and_charges_nothing(
    client, session, price_book, agent, upstream
):
    """A 2xx in the wrong shape, checked in full before anything is stored.

    A `pc_id` too long for its column is refused rather than truncated, because
    a truncated handle names a different call on the agent.
    """
    user, headers = await caller(session)
    agent.offer_reply = upstream

    response = await post_offer(client, headers)

    assert response.status_code == 502
    assert code_of(response) == "voice_agent_unreadable"
    (row,) = await rows_of(user.id)
    assert row.status is AiSessionStatus.FAILED
    assert row.error_code == "voice_agent_unreadable"
    assert await money(user.id) == (FUNDED, 0)


async def test_the_agents_own_retry_after_is_passed_on(client, session, price_book, agent):
    _, headers = await caller(session)

    agent.offer_reply = reply(429, body={"detail": "busy"}, headers={"Retry-After": "7"})
    busy = await post_offer(client, headers)
    agent.offer_reply = reply(503, body={"detail": "warming"}, headers={"Retry-After": "0"})
    warming = await post_offer(client, headers)
    agent.offer_reply = reply(503, body={"detail": "warming"}, headers={"Retry-After": "soon"})
    unparseable = await post_offer(client, headers)

    assert busy.headers["retry-after"] == "7"
    assert busy.json()["retryAfter"] == 7
    assert warming.headers["retry-after"] == "1", "never tell a client to retry in zero seconds"
    assert unparseable.headers["retry-after"] == str(
        voice_agent_client.UNAVAILABLE_RETRY_AFTER_SECONDS
    )


async def test_the_agents_words_about_a_refused_offer_are_relayed_bounded(
    client, session, price_book, agent
):
    _, headers = await caller(session)
    agent.offer_reply = reply(400, body={"detail": "  " + "x" * 1000})

    response = await post_offer(client, headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "x" * 300


async def test_an_agent_that_never_answers_is_cut_off_by_the_whole_offer_deadline(
    client, session, price_book, agent, monkeypatch
):
    """httpx's `read` timeout bounds each read; `asyncio.timeout` bounds the call.

    An upstream that never answers must end as a 502 inside the offer deadline,
    with the hold given back — not as a request that hangs until the browser
    gives up and leaves a ceiling of credit held behind it.
    """
    monkeypatch.setattr(settings, "voice_agent_offer_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "voice_agent_connect_timeout_seconds", 0.1)

    async def never() -> None:
        await asyncio.sleep(30)

    agent.while_offering = never
    user, headers = await caller(session)

    started = asyncio.get_running_loop().time()
    response = await post_offer(client, headers)
    elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 502
    assert code_of(response) == "voice_agent_unreachable"
    assert elapsed < 5
    assert await money(user.id) == (FUNDED, 0)


# --- money before the agent -------------------------------------------------------


async def test_less_credit_than_the_hold_is_a_402_before_the_agent_is_asked(
    client, session, price_book, agent
):
    """The hold is the least an account needs, to the micro-credit, both ways."""
    user, headers = await caller(session, paid=HOLD - 1)

    refused = await post_offer(client, headers)

    assert refused.status_code == 402
    assert code_of(refused) == "insufficient_balance"
    assert refused.json()["requiredMicros"] == HOLD
    assert refused.json()["availableMicros"] == HOLD - 1
    assert agent.offers == []
    assert await calls_of(user.id) == []
    (row,) = await rows_of(user.id)
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.INSUFFICIENT_CREDIT

    await fund(session, user.id, paid=1)
    await session.commit()
    assert (await post_offer(client, headers)).status_code == 201
    assert await money(user.id) == (0, HOLD)


async def test_no_price_book_is_a_503_before_the_agent_is_asked(client, session, agent):
    user, headers = await caller(session)

    response = await post_offer(client, headers)

    assert response.status_code == 503
    assert code_of(response) == "price_book_missing"
    assert agent.offers == []
    assert await calls_of(user.id) == []


class CountingCache(NullCache):
    """Counts every increment, whatever the key.

    The key carries the window start, so a counter that respected it would let
    this test pass or fail on which side of a minute boundary it ran.
    """

    def __init__(self) -> None:
        self.count = 0

    async def increment(self, key: str, ttl_seconds: int) -> int:  # noqa: ARG002
        self.count += 1
        return self.count


async def test_opening_too_many_calls_in_a_minute_is_throttled_before_the_hold(
    client, session, price_book, agent, monkeypatch
):
    stub = CountingCache()
    monkeypatch.setattr(throttle, "get_cache", lambda: stub)
    monkeypatch.setattr(settings, "voice_agent_max_opens_per_minute", 2)
    user, headers = await caller(session)

    for _ in range(2):
        call_id = await opened_call(client, headers)
        assert (await hang_up(client, headers, call_id)).status_code == 200
    refused = await post_offer(client, headers)

    assert refused.status_code == 429
    assert code_of(refused) == "voice_call_rate_limited"
    assert int(refused.headers["retry-after"]) >= 1
    assert len(agent.offers) == 2
    assert len(await rows_of(user.id)) == 2
    assert (await money(user.id))[1] == 0


# --- one line per account -------------------------------------------------------------


async def test_a_second_concurrent_call_is_refused_without_a_second_hold(
    client, session, price_book, agent
):
    user, headers = await caller(session)
    await opened_call(client, headers)

    second = await post_offer(client, headers)

    assert second.status_code == 429
    assert code_of(second) == "voice_call_limit"
    assert second.headers["retry-after"] == str(settings.voice_agent_heartbeat_timeout_seconds)
    assert len(agent.offers) == 1
    assert len(await rows_of(user.id)) == 1, "refused before the hold, so no second session"
    assert await money(user.id) == (FUNDED - HOLD, HOLD)


async def test_a_lapsed_call_is_swept_on_the_next_open_and_frees_the_line(
    client, session, price_book, agent
):
    """A tab that crashed a minute ago must not be what refuses the next call.

    Nor may it keep a whole ceiling reserved while the user makes it: the dead
    call is billed to its last heartbeat plus one interval — sixty seconds of
    call and fifteen of benefit of the doubt, two started minutes — and the new
    call's hold is the only one left.
    """
    user, headers = await caller(session)
    dead = await opened_call(client, headers)
    await rewind(dead, answered=160, seen=100, connected=True)

    second = await post_offer(client, headers)

    assert second.status_code == 201, second.text
    assert (await load_call(dead)).ended_at is not None
    row = await load_row(dead)
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT
    assert row.disputed is True
    assert row.cum_session_ms == 75_000
    assert row.settled_micros == 2 * PER_MINUTE
    assert await money(user.id) == (FUNDED - 2 * PER_MINUTE - HOLD, HOLD)


async def test_the_recount_closes_the_race_and_the_loser_gives_its_hold_back(
    client, session, price_book, agent, monkeypatch
):
    """Two opens racing both pass the first count; the second count is the lock.

    Simulated by making the first count miss the call that is already up —
    exactly what it sees when the other open inserts its row a moment later.
    The loser has already placed its hold by then, and must give it back.
    """
    user, headers = await caller(session)
    first = await opened_call(client, headers)
    counted = voice_agent_service.live_calls

    async def racing(db, user_id, *, excluding=None):
        if excluding is None:
            return 0
        return await counted(db, user_id, excluding=excluding)

    monkeypatch.setattr(voice_agent_service, "live_calls", racing)

    loser = await post_offer(client, headers)

    assert loser.status_code == 429
    assert code_of(loser) == "voice_call_limit"
    assert len(agent.offers) == 1, "the loser never reached the agent"
    (row,) = [r for r in await rows_of(user.id) if r.id != first]
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.USER_CANCELLED
    assert row.error_code == "voice_call_limit"
    assert row.reserved_micros == 0
    assert (await load_call(row.id)).ended_at is not None
    assert (await load_call(first)).ended_at is None, "the winner is untouched"
    assert await money(user.id) == (FUNDED - HOLD, HOLD)


# --- odds and ends ------------------------------------------------------------


async def test_healthz_answers_rather_than_raising(agent):
    assert await voice_agent_client.healthz() is True

    for broken in (
        reply(503, body={"status": "starting"}),
        reply(200, body={"status": "starting"}),
        reply(200, content=b"ok"),
        fail(httpx.ConnectError),
    ):
        agent.health_reply = broken
        assert await voice_agent_client.healthz() is False


async def test_the_voice_routes_publish_their_refusals(client):
    schema = (await client.get("http://test/openapi.json")).json()
    opened = schema["paths"]["/api/v1/voice/sessions"]["post"]
    relayed = schema["paths"]["/api/v1/voice/sessions/{session_id}/candidates"]["post"]

    assert opened["tags"] == ["Voice agent"]
    assert {"400", "401", "402", "403", "422", "429", "502", "503"} <= set(opened["responses"])
    assert "voice_offer_no_video" in opened["responses"]["400"]["description"]
    assert "voice_call_limit" in opened["responses"]["429"]["description"]
    assert "voice_call_gone" in relayed["responses"]["409"]["description"]
    assert "pc_id" not in json.dumps(schema["components"]["schemas"]["VoiceSessionResponse"])
