"""The voice agent's signalling endpoint, and nothing else.

The third client in this directory and the same rule as the other two: this
module knows the agent's URL, its key and the shape of its JSON, and nothing
about wallets. What is different is how little there is to know. The agent's
whole public surface is three routes —

    GET   /healthz      no auth — reachability
    POST  /api/offer    X-API-Key — SDP offer in, SDP answer and `pc_id` out
    PATCH /api/offer    X-API-Key — trickle ICE candidates for a `pc_id`

— and every other path answers 404 by design. There is no route to end a call,
list calls or report usage: a call ends when the browser closes its peer
connection, and the agent tears it down on its own. That absence is why
`voice_agent_service` bills on the client's heartbeat rather than on anything
the agent could tell us, and it is the first thing to change if the agent ever
grows one.

## The key is the `ui` roster entry, and it stays here

The agent's own guide says a key in frontend JavaScript is public and that
anything past a prototype should proxy `POST`/`PATCH /api/offer` through its
own backend, adding the key server-side. That is this module. A 401 or 403
from upstream is therefore never the caller's fault — nobody but us ever sends
the key — and is a 503 plus an ERROR line, exactly as on the speech gateways.

## One place to fail

Every call funnels through `_raise_for_upstream`. The codes, all stable:

    voice_agent_not_configured   no VOICE_AGENT_BASE_URL/_API_KEY here (503)
    voice_agent_key_rejected     upstream refused our key (503, ERROR)
    voice_agent_busy             upstream is at its call ceiling (429, Retry-After)
    voice_agent_unavailable      upstream answered 503 — warming up (503, Retry-After)
    voice_offer_rejected         upstream refused the caller's own SDP (400)
    voice_candidate_rejected     upstream refused a relayed ICE candidate (400)
    voice_call_gone              upstream no longer knows this `pc_id` (409)
    voice_agent_unreachable      timeout, transport failure, redirect or 5xx (502)
    voice_agent_unreadable       a 2xx whose body is not what we expected (502)

The SDP strings are passed through byte for byte in both directions. Nothing
here parses one: a proxy that rewrote an SDP would be a second WebRTC
implementation with none of the testing the browser's has had.

## Asking whether a call is still up

There is no route for it, but there is an answer to it. `PATCH /api/offer`
with an empty candidate list does nothing to a peer connection the agent holds
and is a 404 for one it has dropped, so `probe` turns the candidate route into
a liveness check — the only way this side of the gateway can tell a call that
ended from a client that merely stopped saying so.

That is a reading of the agent's behaviour, not a promise in its guide, so it
is checked before it is believed: `liveness_is_trustworthy` probes a `pc_id`
that cannot exist, and only an agent that answers *that* with a 404 has its
other answers acted on. An agent that says 200 to everything — or that cannot be
reached — is simply not asked, and billing falls back to the heartbeat alone.
Never raises; a probe that fails is an answer of "unknown".
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadGatewayError,
    BadRequestError,
    ConflictError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.models.voice_call import UPSTREAM_PC_ID_MAX_LENGTH

logger = logging.getLogger("synora.voice")

PATH_HEALTHZ = "/healthz"
PATH_OFFER = "/api/offer"

# A liveness probe is a no-op on the agent's side and must stay cheap on ours:
# it runs inside a sweep and inside `POST /voice/sessions`.
PROBE_TIMEOUT_SECONDS = 5.0
# How long a canary verdict is believed. A good one for ten minutes, a bad one
# — which includes "unreachable" — for one, so an agent that comes back is
# trusted again soon and one that was never probe-able is asked rarely.
CANARY_TRUSTED_SECONDS = 600
CANARY_UNTRUSTED_SECONDS = 60

DEFAULT_RETRY_AFTER_SECONDS = 5
# A 503 from the agent is the warm-up answer, and warm-up is bounded by loading
# models rather than by a queue — worth waiting longer for than a busy 429.
UNAVAILABLE_RETRY_AFTER_SECONDS = 15


# Every code `_raise_for_upstream` and the answer check can produce, by the
# operation that produces it — the closed set the error panel reads, exported
# at zero so its first outage is not the one it cannot see.
_OFFER_CODES = (
    "voice_agent_key_rejected",
    "voice_agent_busy",
    "voice_agent_unavailable",
    "voice_offer_rejected",
    "voice_agent_unreachable",
    "voice_agent_unreadable",
)
_CANDIDATE_CODES = (
    "voice_agent_key_rejected",
    "voice_agent_busy",
    "voice_agent_unavailable",
    "voice_candidate_rejected",
    "voice_call_gone",
    "voice_agent_unreachable",
)
metrics.prime_voice_series(
    errors=[("offer", code) for code in _OFFER_CODES]
    + [("candidates", code) for code in _CANDIDATE_CODES],
    operations=("offer", "candidates", "probe"),
)


@dataclass(frozen=True, slots=True)
class Answer:
    """The agent's half of the negotiation. `sdp` goes to the browser verbatim."""

    sdp: str
    type: str
    pc_id: str


def build_client() -> httpx.AsyncClient:
    """Construct the outbound client. Also the seam the tests replace."""
    return httpx.AsyncClient(
        base_url=settings.voice_agent_base_url.rstrip("/"),
        headers={
            "X-API-Key": settings.voice_agent_api_key,
            "User-Agent": f"{settings.app_name}/1.0 (+gateway)",
        },
        timeout=httpx.Timeout(
            connect=settings.voice_agent_connect_timeout_seconds,
            # The offer's budget, because the offer is the slow call: the agent
            # answers once it has built the call's pipeline. Candidate relays
            # pass a shorter one per request.
            read=settings.voice_agent_offer_timeout_seconds,
            write=settings.voice_agent_connect_timeout_seconds,
            pool=settings.voice_agent_connect_timeout_seconds,
        ),
        # No redirect following: every request carries our key, and an agent
        # that redirects is a misconfigured tunnel, not a route.
        follow_redirects=False,
    )


_client_instance: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """The process-wide client, built on first use — lazily, as `tts_client`'s is."""
    global _client_instance
    if _client_instance is None:
        _client_instance = build_client()
    return _client_instance


async def aclose_client() -> None:
    """Drop the client, its pool and the canary's verdict. The lifespan calls it; tests reset with it."""
    global _client_instance, _canary
    _canary = None
    if _client_instance is not None:
        await _client_instance.aclose()
        _client_instance = None


def require_configured() -> None:
    if not settings.has_voice_agent:
        raise ServiceUnavailableError(
            "The voice agent is not configured on this server.",
            code="voice_agent_not_configured",
        )


def _detail(response: httpx.Response) -> str | None:
    """Upstream's own words about a refused offer, if it said anything usable."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")
        if isinstance(detail, str) and detail.strip():
            # Bounded: this is relayed to a browser, and an upstream stack
            # trace in a `detail` field is not something to pass along whole.
            return detail.strip()[:300]
    return None


def _retry_after(response: httpx.Response, fallback: int) -> int:
    try:
        return max(1, int(response.headers.get("Retry-After", "").strip()))
    except (TypeError, ValueError):
        return fallback


def _raise_for_upstream(response: httpx.Response, *, operation: str) -> None:
    """Turn an upstream status into one of this app's errors. Returns on 2xx."""
    status = response.status_code
    if status < 300:
        return

    if status in (401, 403):
        # ERROR: nothing a caller did produces this. Somebody rotated the
        # agent's `ui` key and this deployment still has the old one.
        logger.error("voice agent rejected our key: %s answered %s", operation, status)
        raise ServiceUnavailableError(
            "Voice calls are unavailable right now. Please try again shortly.",
            code="voice_agent_key_rejected",
        )

    if status == 429:
        # The agent's concurrent-call ceiling. The guide says every deployment
        # has one and asks callers to refuse gracefully past it.
        raise TooManyRequestsError(
            "Every voice line is busy. Please try again in a moment.",
            code="voice_agent_busy",
            retry_after=_retry_after(response, DEFAULT_RETRY_AFTER_SECONDS),
        )

    if status == 503:
        logger.warning("voice agent is not ready (%s)", operation)
        raise ServiceUnavailableError(
            "The voice agent is still starting. Please try again shortly.",
            code="voice_agent_unavailable",
            retry_after=_retry_after(response, UNAVAILABLE_RETRY_AFTER_SECONDS),
        )

    if status == 404 and operation == "candidates":
        # The agent forgot the peer connection: it already tore the call down,
        # because the browser closed it or ICE never completed. Not our 502 —
        # the call is simply over, and the client should stop feeding it.
        raise ConflictError(
            "This call has already ended on the voice agent.",
            code="voice_call_gone",
        )

    if 400 <= status < 500 and status != 404:
        # The caller's own SDP or candidate. Upstream's message says what it
        # disliked and names nothing of ours, so it is relayed.
        if operation == "candidates":
            raise BadRequestError(
                _detail(response) or "The voice agent refused a network candidate.",
                code="voice_candidate_rejected",
            )
        raise BadRequestError(
            _detail(response) or "The voice agent refused this connection offer.",
            code="voice_offer_rejected",
        )

    # 404 on the offer itself is a base URL pointing at the wrong service, and
    # every 5xx is theirs. Neither is anything the caller can fix.
    logger.warning("voice agent %s answered %s", operation, status)
    raise BadGatewayError(
        "Could not reach the voice agent. Please try again.",
        code="voice_agent_unreachable",
    )


def _unreachable(exc: BaseException, *, operation: str) -> BadGatewayError:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        logger.warning("voice agent %s timed out: %s", operation, exc)
        return BadGatewayError(
            "The voice agent did not answer in time. Please try again.",
            code="voice_agent_unreachable",
        )
    logger.warning("voice agent %s transport failure: %s", operation, exc)
    return BadGatewayError(
        "Could not reach the voice agent. Please try again.",
        code="voice_agent_unreachable",
    )


async def _request(
    method: str,
    *,
    operation: str,
    json: dict[str, Any],
    deadline_seconds: float,
) -> httpx.Response:
    """One timed request, with a deadline on the whole of it.

    httpx's `read` timeout bounds each read, not the response: an upstream that
    trickles a byte every forty seconds never trips it. `asyncio.timeout` is
    the total, and the one that matters to `voice_agent_service` — it is what
    lets the sweeper know an offer still in flight has to be over by now.
    """
    require_configured()
    started = time.perf_counter()
    try:
        async with asyncio.timeout(deadline_seconds):
            response = await _client().request(
                method,
                PATH_OFFER,
                json=json,
                headers={"Accept": "application/json"},
                # `USE_CLIENT_DEFAULT`, never `None`: to httpx a `None` here
                # means "no timeout at all", which is the one value this call
                # must never have.
                timeout=(
                    httpx.USE_CLIENT_DEFAULT
                    if operation == "offer"
                    else httpx.Timeout(settings.voice_agent_connect_timeout_seconds)
                ),
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        metrics.observe_voice_upstream(operation=operation, seconds=time.perf_counter() - started)
        error = _unreachable(exc, operation=operation)
        metrics.record_voice_upstream_error(operation=operation, code=error.code)
        raise error from exc

    metrics.observe_voice_upstream(operation=operation, seconds=time.perf_counter() - started)
    try:
        _raise_for_upstream(response, operation=operation)
    except AppError as error:
        metrics.record_voice_upstream_error(operation=operation, code=error.code)
        raise
    return response


def offer_deadline_seconds() -> float:
    """The longest an offer can possibly take, connect included.

    Exported because the sweeper reads it: a call whose offer is still in
    flight has no answer yet, and releasing it before this has elapsed would
    free a hold the agent is about to be answered against.
    """
    return settings.voice_agent_offer_timeout_seconds + settings.voice_agent_connect_timeout_seconds


async def offer(*, sdp: str, sdp_type: str) -> Answer:
    """Hand the browser's offer to the agent. Returns its answer.

    Reads get the client's own budget, `voice_agent_offer_timeout_seconds` —
    the warm-up allowance — and the whole call gets `offer_deadline_seconds`.
    """
    response = await _request(
        "POST",
        operation="offer",
        json={"sdp": sdp, "type": sdp_type},
        deadline_seconds=offer_deadline_seconds(),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if not (
        isinstance(payload, dict)
        and isinstance(payload.get("sdp"), str)
        and payload["sdp"].startswith("v=")
        and payload.get("type") == "answer"
        and isinstance(payload.get("pc_id"), str)
        and 0 < len(payload["pc_id"]) <= UPSTREAM_PC_ID_MAX_LENGTH
    ):
        # A 2xx in the wrong shape. Checked in full here, so the service can
        # store `pc_id` and hand `sdp` to a browser without wondering.
        metrics.record_voice_upstream_error(operation="offer", code="voice_agent_unreadable")
        logger.warning("voice agent answered an offer with an unreadable body")
        raise BadGatewayError(
            "The voice agent sent an answer we could not read.",
            code="voice_agent_unreadable",
        )
    return Answer(sdp=payload["sdp"], type=payload["type"], pc_id=payload["pc_id"])


async def add_candidates(*, pc_id: str, candidates: list[dict[str, Any]]) -> None:
    """Relay one batch of trickle-ICE candidates for an answered call.

    One request per batch, whatever its size: the agent takes a list, and a
    browser gathers its candidates in a burst, so a client that batches them
    costs the agent one round trip where the guide's reference code costs five.
    """
    await _request(
        "PATCH",
        operation="candidates",
        json={"pc_id": pc_id, "candidates": candidates},
        deadline_seconds=2 * settings.voice_agent_connect_timeout_seconds,
    )


async def probe(pc_id: str, *, canary: bool = False) -> bool | None:
    """Whether the agent still holds `pc_id`: True, False, or None for "cannot tell".

    An empty candidate batch, which the agent applies as nothing. Deliberately
    outside `_request`: a probe is asked, not relied on, so it never raises and
    never counts as an upstream error — a 404 here is the answer, not a fault.
    """
    if not settings.has_voice_agent:
        return None
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            response = await _client().request(
                "PATCH",
                PATH_OFFER,
                json={"pc_id": pc_id, "candidates": []},
                headers={"Accept": "application/json"},
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
            )
    except (httpx.HTTPError, TimeoutError):
        verdict = None
    else:
        verdict = True if response.status_code < 300 else False if response.status_code == 404 else None
    metrics.observe_voice_upstream(operation="probe", seconds=time.perf_counter() - started)
    # The canary under results of its own: its right answer is a 404, which as
    # a probe of a call would read "gone" on a healthy agent, and its wrong one
    # a 200, which would read "alive" while nothing is being believed.
    if canary:
        metrics.record_voice_probe(result="canary_trusted" if verdict is False else "canary_untrusted")
    else:
        metrics.record_voice_probe(result={True: "alive", False: "gone", None: "unknown"}[verdict])
    return verdict


# An unroutable host candidate — TEST-NET-1 and the discard port, reserved by
# RFC 5737 so it can reach nobody. Relayed to a peer that never connected, it
# gives an ICE agent stuck with no candidate pairs one pair to fail, and aioice
# fails an unanswered pair in about sixty-four seconds, which drops the peer.
# Harmless to a peer that did connect: its check list is already complete.
NUDGE_CANDIDATE = {
    "candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host",
    "sdp_mid": "0",
    "sdp_mline_index": 0,
}


async def nudge(pc_id: str) -> bool:
    """Relay `NUDGE_CANDIDATE` to `pc_id`. True when the agent took it; never raises."""
    if not settings.has_voice_agent:
        return False
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            response = await _client().request(
                "PATCH",
                PATH_OFFER,
                json={"pc_id": pc_id, "candidates": [NUDGE_CANDIDATE]},
                headers={"Accept": "application/json"},
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
            )
    except (httpx.HTTPError, TimeoutError):
        metrics.record_voice_nudge(result="unreachable")
        return False
    if response.status_code < 300:
        metrics.record_voice_nudge(result="taken")
        return True
    if response.status_code != 404:
        # A 404 is a peer already gone, which is what the nudge was for. Any
        # other refusal means this agent will not take it — the `sdp_mid` does
        # not name a transport it has, most likely — and never-heartbeated calls
        # can then never prove they connected. Worth an operator's attention.
        logger.warning("voice agent refused the nudge candidate: %s", response.status_code)
        metrics.record_voice_nudge(result="refused")
    return False


_canary: tuple[bool, float] | None = None


async def liveness_is_trustworthy() -> bool:
    """Whether this agent's probe answers can be acted on. Cached; never raises.

    Asked with a `pc_id` no call has ever had. The right answer is a 404, and
    only an agent that gives it has told us its 2xx means something — one that
    answered 200 would otherwise keep every call "alive" until its ceiling and
    bill each one for ten minutes.
    """
    global _canary
    now = time.monotonic()
    if _canary is not None and _canary[1] > now:
        return _canary[0]
    verdict = await probe(f"synora-liveness-canary-{uuid.uuid4().hex}", canary=True)
    trusted = verdict is False
    if _canary is None or _canary[0] != trusted:
        # Logged on a change of mind only, so a steady state is one line.
        (logger.info if trusted else logger.warning)(
            "voice agent liveness probes %s",
            "trusted" if trusted else f"not trusted (canary answered {verdict!r}); billing on heartbeats alone",
        )
    _canary = (trusted, now + (CANARY_TRUSTED_SECONDS if trusted else CANARY_UNTRUSTED_SECONDS))
    return trusted


async def healthz() -> bool:
    """Whether the agent answers at all. Answers instead of raising."""
    require_configured()
    try:
        response = await _client().get(
            PATH_HEALTHZ,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(settings.voice_agent_connect_timeout_seconds),
        )
    except httpx.HTTPError as exc:
        logger.warning("voice agent health probe failed: %s", exc)
        return False
    if response.status_code >= 300:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"
