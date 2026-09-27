"""What the voice-agent tests share: realistic SDP, a fake agent, and the clock.

Three files test the gateway — `test_voice_agent.py` opens calls,
`test_voice_agent_calls.py` lives through them, `test_voice_agent_settlement.py`
settles the ones nobody ended — and all three need the same agent, the same
offer and the same way of moving a call through time. They live here rather
than in `conftest.py` because nothing outside those files should get an autouse
fixture that reconfigures the voice agent, and a conftest fixture is every
test's. Each test module imports `no_agent` and `agent` by name, which is how
pytest finds a fixture that is not in a conftest.

## The agent is faked at the transport

Exactly as `test_tts_api.py` fakes the speech box: `voice_agent_client.build_client`
is the seam, an `httpx.MockTransport` goes in behind it, and the real client,
the real error mapping and the real deadline all run on top. The fake client is
built with the headers, timeouts and redirect policy the production factory
produced, so an assertion that our key went upstream — or that a redirect was
not followed — is an assertion about `build_client` rather than about this file.

## Time is moved on the row

`rewind` edits the three timestamps on `voice_calls` directly. That is honest
here in a way it would not be elsewhere: the price of a call is a pure function
of `answered_at`, `connected_at` and `last_seen_at` against our own `now`, which
is the whole design of `voice_agent_service`, so moving the row back ninety
seconds *is* a ninety-second call as far as billing can ever tell.

## The numbers

The `price_book` fixture's: `session_ms` at half a credit per started minute,
CEIL, with a quarter-credit minimum that CEIL already clears — so the ten-minute
ceiling holds exactly five credits, and every expectation in the three files is
a count of started minutes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core import metrics
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import AiSessionKind, BillingService
from app.models.ledger import LedgerEntry
from app.models.user import User
from app.models.voice_call import VoiceCall
from app.services.ai import voice_agent_client, voice_agent_service, voice_call_lifecycle
from app.services.billing import session_service, wallet_service
from tests.conftest import auth, fund, make_user

CREDIT = 1_000_000
PER_MINUTE = 500_000
# Ten started minutes: the ceiling is 600 s and the rate is per started minute.
HOLD = 10 * PER_MINUTE
FUNDED = 100 * CREDIT

AGENT_URL = "http://agent.test"
# A placeholder in the agent's own key format. Never a real one: the whole
# point of this gateway is that the real one lives in one server's environment.
AGENT_KEY = "pv_ak_replace-me"
# Pipecat's SmallWebRTC handle is a class name and a counter.
PC_ID_PREFIX = "SmallWebRTCConnection#"
DEFAULT_ICE_SERVERS = '[{"urls": ["stun:stun.l.google.com:19302"]}]'


# --- SDP ----------------------------------------------------------------------
#
# Shaped like what Chrome produces for the agent guide's client: one audio
# transceiver with the microphone, one video transceiver that never opens a
# camera, and the data channel the client creates. CRLF line endings, because
# that is what SDP is and what a relay that "normalised" it would break.

_SESSION = [
    "v=0",
    "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
    "s=-",
    "t=0 0",
    "a=extmap-allow-mixed",
    "a=msid-semantic: WMS",
]
_AUDIO = [
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126",
    "c=IN IP4 0.0.0.0",
    "a=rtcp:9 IN IP4 0.0.0.0",
    "a=ice-ufrag:Qm9v",
    "a=ice-pwd:c2VjcmV0LXNlY3JldC1zZWNyZXQ",
    "a=ice-options:trickle",
    "a=fingerprint:sha-256 4A:AD:B9:B1:3F:82:18:3B:54:02:12:DF:3E:5D:49:6B:"
    "19:E5:7C:AB:4A:AD:B9:B1:3F:82:18:3B:54:02:12:DF",
    "a=setup:actpass",
    "a=mid:0",
    "a=sendrecv",
    "a=msid:- 7c1f0a9e-mic",
    "a=rtcp-mux",
    "a=rtpmap:111 opus/48000/2",
    "a=fmtp:111 minptime=10;useinbandfec=1",
]
_VIDEO = [
    "m=video 9 UDP/TLS/RTP/SAVPF 96 97",
    "c=IN IP4 0.0.0.0",
    "a=mid:1",
    "a=sendrecv",
    "a=rtcp-mux",
    "a=rtpmap:96 VP8/90000",
    "a=rtpmap:97 rtx/90000",
    "a=fmtp:97 apt=96",
]
_DATA = [
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
    "c=IN IP4 0.0.0.0",
    "a=mid:2",
    "a=sctp-port:5000",
    "a=max-message-size:262144",
]


def offer_sdp(*, audio: bool = True, video: bool = True, data: bool = True) -> str:
    sections = [(audio, "0", _AUDIO), (video, "1", _VIDEO), (data, "2", _DATA)]
    mids = " ".join(mid for present, mid, _ in sections if present)
    lines = [*_SESSION, f"a=group:BUNDLE {mids}"]
    for present, _, section in sections:
        if present:
            lines.extend(section)
    return "\r\n".join(lines) + "\r\n"


OFFER = offer_sdp()
# The agent's answer, with a line no browser would write, so a relay that
# parsed and re-serialised it rather than passing it through would show.
ANSWER_SDP = (
    "v=0\r\n"
    "o=- 3918244711 3918244711 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1 2\r\n"
    "a=x-agent-note:  two spaces, kept exactly  \r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=setup:active\r\n"
    "a=mid:0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=mid:1\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "a=mid:2\r\n"
)

# `RTCIceCandidate.toJSON()` as Chrome hands it to `onicecandidate`: camelCase,
# plus a `usernameFragment` the agent has no field for.
BROWSER_CANDIDATE = {
    "candidate": (
        "candidate:842163049 1 udp 1677729535 203.0.113.7 51234 typ srflx "
        "raddr 192.168.1.20 rport 51234 generation 0 ufrag Qm9v network-cost 999"
    ),
    "sdpMid": "0",
    "sdpMLineIndex": 0,
    "usernameFragment": "Qm9v",
}
# The same thing in the agent's own spelling, which is also accepted.
AGENT_SHAPED_CANDIDATE = {
    "candidate": "candidate:1 1 udp 2122260223 192.168.1.20 50000 typ host generation 0",
    "sdp_mid": "0",
    "sdp_mline_index": 0,
}
END_OF_CANDIDATES = {"candidate": "", "sdpMid": "0", "sdpMLineIndex": 0}


def host_candidate(n: int) -> dict:
    return {
        "candidate": f"candidate:{n} 1 udp 2122260223 192.168.1.{n % 250} {50000 + n} typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
    }


# --- the agent ------------------------------------------------------------------

Reply = Callable[[httpx.Request], httpx.Response]


def reply(status: int, *, body: object = None, content: bytes | None = None,
          headers: dict[str, str] | None = None) -> Reply:
    """A scripted upstream response, built fresh per request.

    A factory rather than a `Response`, because an `httpx.Response` is a
    one-shot stream and a test that retries would otherwise read an empty body
    the second time and fail somewhere misleading.
    """

    def make(_: httpx.Request) -> httpx.Response:
        if body is not None:
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, content=content or b"", headers=headers)

    return make


def fail(error: type[httpx.TransportError]) -> Reply:
    def make(request: httpx.Request) -> httpx.Response:
        raise error("scripted transport failure", request=request)

    return make


class FakeAgent:
    """protoVoice, as far as `voice_agent_client` can tell.

    Records every request so a test can assert on what we sent — the body, the
    key, the absence of the caller's bearer token — and not only on what came
    back. `while_offering` runs *inside* the offer, before the answer, which is
    how a test gets at a call whose offer is still in flight.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answered = 0
        self.offer_reply: Reply = self._answer
        self.candidates_reply: Reply = reply(200, body={"status": "success"})
        self.health_reply: Reply = reply(200, body={"status": "ok"})
        self.while_offering: Callable[[], Awaitable[None]] | None = None
        # `None` is the agent that answers 200 to every PATCH — which the
        # liveness canary reads as "cannot be asked". A set is the real one:
        # it knows its peer connections and 404s a `pc_id` it does not hold.
        self.peers: set[str] | None = None
        self.on_probe: Callable[[str], None] | None = None

    def track_peers(self) -> FakeAgent:
        self.peers = set()
        return self

    def drop(self, pc_id: str) -> None:
        """The browser closed the connection, or the agent timed it out."""
        assert self.peers is not None
        self.peers.discard(pc_id)

    def pc_id_of(self, n: int) -> str:
        return f"{PC_ID_PREFIX}{n}"

    def _answer(self, _: httpx.Request) -> httpx.Response:
        self.answered += 1
        pc_id = self.pc_id_of(self.answered)
        if self.peers is not None:
            self.peers.add(pc_id)
        return httpx.Response(200, json={"sdp": ANSWER_SDP, "type": "answer", "pc_id": pc_id})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        if self.peers is not None:
            body = json.loads(request.content)
            pc_id = str(body.get("pc_id"))
            if not body.get("candidates") and self.on_probe is not None:
                self.on_probe(pc_id)
            if pc_id not in self.peers:
                return httpx.Response(404, json={"detail": "Peer connection not found"})
        return self.candidates_reply(request)

    @property
    def probes(self) -> list[dict]:
        return [body for body in self.patches if not body.get("candidates")]

    @property
    def nudges(self) -> list[dict]:
        return [
            body
            for body in self.patches
            if body.get("candidates") == [voice_agent_client.NUDGE_CANDIDATE]
        ]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/healthz":
            return self.health_reply(request)
        if request.url.path != "/api/offer":
            # The agent's whole surface is three routes; everything else 404s.
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "POST":
            if self.while_offering is not None:
                await self.while_offering()
            return self.offer_reply(request)
        if request.method == "PATCH":
            return self._patch(request)
        return httpx.Response(405)

    def sent(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == "/api/offer"]

    @property
    def offers(self) -> list[dict]:
        return [json.loads(r.content) for r in self.sent("POST")]

    @property
    def patches(self) -> list[dict]:
        return [json.loads(r.content) for r in self.sent("PATCH")]


@pytest.fixture(autouse=True)
async def no_agent(monkeypatch) -> AsyncGenerator[None]:
    """Every test starts on a deployment with no agent, whatever `.env` says.

    `settings` reads `.env`, and a developer who has put the real agent's URL
    and key there to try the dev UI must not have this suite place calls on it.
    The client singleton is dropped both ways for the reason `test_tts_api.py`
    gives, and the sweeper is stopped in case a test that started it failed
    before it could: a loop left running would outlive this test's event loop.
    """
    monkeypatch.setattr(settings, "voice_agent_base_url", "")
    monkeypatch.setattr(settings, "voice_agent_api_key", "")
    monkeypatch.setattr(settings, "voice_agent_ice_servers", DEFAULT_ICE_SERVERS)
    await voice_agent_client.aclose_client()
    yield
    await voice_agent_service.stop_sweeper(timeout_seconds=5)
    await voice_agent_client.aclose_client()


@pytest.fixture
async def agent(monkeypatch) -> FakeAgent:
    """A configured agent that answers from memory.

    The production factory is run once, for its headers and timeouts, and the
    fake client is built with exactly those. So `X-API-Key` arriving upstream
    is proof that `build_client` attaches it, not that this fixture did.
    """
    fake = FakeAgent()
    monkeypatch.setattr(settings, "voice_agent_base_url", AGENT_URL)
    monkeypatch.setattr(settings, "voice_agent_api_key", AGENT_KEY)
    production = voice_agent_client.build_client()
    headers, timeout = production.headers, production.timeout
    follow_redirects = production.follow_redirects
    await production.aclose()
    monkeypatch.setattr(
        voice_agent_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url=AGENT_URL,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            transport=httpx.MockTransport(fake.handle),
        ),
    )
    await voice_agent_client.aclose_client()
    return fake


# --- helpers ----------------------------------------------------------------------


async def caller(session, *, paid: int = FUNDED) -> tuple[User, dict[str, str]]:
    """A signed-in, funded user, without a round trip through bcrypt.

    `register_and_verify` costs a password hash per call and most tests here
    need two users; the happy path in `test_voice_agent.py` goes through the
    real sign-in once, and everything else mints the access token the login
    route would.
    """
    user = await make_user(session)
    if paid:
        await fund(session, user.id, paid=paid)
    await session.commit()
    return user, auth(create_access_token(str(user.id)))


async def post_offer(client, headers, *, sdp: str = OFFER, **body) -> httpx.Response:
    return await client.post(
        "/voice/sessions", headers=headers, json={"sdp": sdp, "type": "offer", **body}
    )


async def opened_call(client, headers) -> uuid.UUID:
    response = await post_offer(client, headers)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["ai_session_id"])


async def beat(client, headers, call_id) -> httpx.Response:
    return await client.post(f"/voice/sessions/{call_id}/heartbeat", headers=headers)


async def hang_up(client, headers, call_id) -> httpx.Response:
    return await client.delete(f"/voice/sessions/{call_id}", headers=headers)


async def relay(client, headers, call_id, candidates: list[dict]) -> httpx.Response:
    return await client.post(
        f"/voice/sessions/{call_id}/candidates", headers=headers, json={"candidates": candidates}
    )


# Read back on a connection of their own, as `test_tts_api.py` does: the
# settlement commits from sessions the test never sees, and anything cached in
# the test's own session is a snapshot from before the charge.


async def load_call(call_id: uuid.UUID) -> VoiceCall:
    async with SessionLocal() as db:
        return (await db.execute(select(VoiceCall).where(VoiceCall.id == call_id))).scalar_one()


async def load_row(ai_session_id: uuid.UUID) -> AiSession:
    async with SessionLocal() as db:
        return (
            await db.execute(select(AiSession).where(AiSession.id == ai_session_id))
        ).scalar_one()


async def rows_of(user_id: uuid.UUID) -> list[AiSession]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(AiSession)
                    .where(AiSession.user_id == user_id)
                    .order_by(AiSession.created_at)
                )
            ).scalars()
        )


async def calls_of(user_id: uuid.UUID) -> list[VoiceCall]:
    async with SessionLocal() as db:
        return list(
            (await db.execute(select(VoiceCall).where(VoiceCall.user_id == user_id))).scalars()
        )


async def money(user_id: uuid.UUID) -> tuple[int, int]:
    """`(available, reserved)`."""
    async with SessionLocal() as db:
        balance = await wallet_service.get_balance(db, user_id)
        await db.commit()
        return balance.available_micros, balance.reserved_micros


async def ledger_of(user_id: uuid.UUID) -> list[LedgerEntry]:
    async with SessionLocal() as db:
        wallet_id = (await wallet_service.get_balance(db, user_id)).wallet_id
        await db.commit()
        return list(
            (
                await db.execute(select(LedgerEntry).where(LedgerEntry.wallet_id == wallet_id))
            ).scalars()
        )


async def rewind(
    call_id: uuid.UUID,
    *,
    answered: float | None = None,
    seen: float | None = None,
    connected: bool = False,
    agent_seen: float | None = None,
    ended: float | None = None,
    nudged: float | None = None,
) -> datetime:
    """Move a call's clock back. `answered` and `seen` are seconds before one `now`.

    Both are measured from the same instant, so a test can compute an inferred
    end — `last_seen + interval - answered` — to the millisecond. `connected`
    stamps `connected_at` at the answer, as a first heartbeat right after it
    would have.
    """
    now = utcnow()
    values: dict[str, datetime] = {}
    if answered is not None:
        values["answered_at"] = now - timedelta(seconds=answered)
        if connected:
            values["connected_at"] = values["answered_at"]
    if seen is not None:
        values["last_seen_at"] = now - timedelta(seconds=seen)
    if agent_seen is not None:
        values["agent_seen_at"] = now - timedelta(seconds=agent_seen)
    if ended is not None:
        values["ended_at"] = now - timedelta(seconds=ended)
    if nudged is not None:
        values["nudged_at"] = now - timedelta(seconds=nudged)
    async with SessionLocal() as db:
        await db.execute(update(VoiceCall).where(VoiceCall.id == call_id).values(**values))
        await db.commit()
    return now


async def backdate_session(ai_session_id: uuid.UUID, *, seconds: float) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(AiSession)
            .where(AiSession.id == ai_session_id)
            .values(created_at=utcnow() - timedelta(seconds=seconds))
        )
        await db.commit()


async def sweep_now(**kwargs) -> int:
    async with SessionLocal() as db:
        return await voice_agent_service.sweep(db, **kwargs)


async def voice_session(
    user_id: uuid.UUID, *, scope: str = voice_agent_service.IDEMPOTENCY_SCOPE,
    key: str | None = None,
) -> uuid.UUID:
    """A held voice-agent session with no call row, opened the way `open_call` opens one.

    With the defaults it is exactly what a process killed between the hold and
    the call row leaves behind. With another scope or a key of its own it is
    the other kind of voice session — the agent-reported ones
    `docs/INTERNAL_API.md` describes — which this module must never touch.
    """
    async with SessionLocal() as db:
        ticket = await session_service.open_oneshot(
            db,
            user_id=user_id,
            service=BillingService.VOICE_AGENT,
            model_key=settings.voice_agent_model_key,
            quantities=voice_agent_service.ceiling_quantities(),
            scope=scope,
            idempotency_key=key,
            kind=AiSessionKind.REALTIME,
        )
    return ticket.ai_session_id


def grace_seconds() -> float:
    return voice_call_lifecycle.unanswered_grace().total_seconds()


def metric_value(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def code_of(response: httpx.Response) -> str:
    return response.json()["code"]
