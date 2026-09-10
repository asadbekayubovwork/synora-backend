"""The upstream speech box, and nothing else.

This is the only module that knows the TTS service's URL, its API key and the
shape of its JSON. No holds, no prices, no sessions: a client that also billed
would have to decide what a half-delivered stream costs, and that decision
belongs next to the wallet, not next to the socket. So this file is deliberately
dull, and `tts_service` is where the interesting part lives.

## The gateway trade, restated in HTTP terms

Our `sk_live_...` key never leaves this process. The caller authenticates to us
with their own JWT and we authenticate to upstream with ours, which is what
makes the usage ours to meter — and what makes every upstream *authentication*
failure our own fault rather than theirs. 401 and 403 are therefore the one
status class that is never relayed: they become a 503 plus an ERROR line,
because a user who mistyped nothing must not be handed a 401 they cannot act
on. Upstream's 402 gets the same treatment for a sharper reason, spelled out at
`_raise_for_upstream`.

## One place to fail

Every call funnels through `_raise_for_upstream`, so routes never touch an
`httpx.Response` and a new endpoint cannot invent a new failure vocabulary. The
codes it can raise, all stable:

    tts_not_configured   no TTS_BASE_URL/TTS_API_KEY on this deployment (503)
    tts_key_rejected     upstream refused our key (503, logged at ERROR)
    tts_quota_exhausted  our tenant quota is spent (503, logged at ERROR)
    tts_not_found        no such voice or job (404)
    tts_rejected_input   upstream refused the caller's own payload (400)
    tts_busy             upstream is saturated (429, with Retry-After)
    tts_unreachable      timeout, transport failure, redirect or 5xx (502)
    tts_unreadable       a 2xx whose body is not the JSON we expected (502)

## Timeouts point in two directions

Connecting is either instant or broken, so it gets a short budget. Reading is
not: synthesis holds the connection open while the GPU works, and the read
timeout has to cover the whole of a five-thousand-character job. Hence
`connect=10s, read=300s` rather than one number for both — a single timeout
generous enough for the second is a hung request pretending to work for the
first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadGatewayError,
    BadRequestError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)

logger = logging.getLogger("synora.tts")

PATH_READYZ = "/readyz"
PATH_STREAM = "/v1/tts/stream"
PATH_VOICES = "/v1/voices"
PATH_BATCH = "/v1/batch"
PATH_USAGE = "/v1/usage"

# Upstream's only usage-ish response header on the streaming endpoint. There is
# no character or duration header, which is why full-text billing is computed
# on our side instead of read off the response.
HEADER_SAMPLE_RATE = "x-audio-sample-rate"

# A 429 with no Retry-After invites an immediate retry, which is exactly what a
# saturated GPU cannot absorb. When upstream declines to say, we say.
DEFAULT_RETRY_AFTER_SECONDS = 5


def build_client() -> httpx.AsyncClient:
    """Construct the outbound client. Also the seam the tests replace.

    Upstream has to be faked in the test suite and there is no `respx` in
    `requirements-dev.txt`; adding a dev dependency for one fake is not worth
    it. So a test monkeypatches this function with one that returns an
    `httpx.AsyncClient(transport=httpx.MockTransport(handler))` and every
    function below goes through the fake untouched.
    """
    return httpx.AsyncClient(
        base_url=settings.tts_base_url.rstrip("/"),
        headers={
            "X-API-Key": settings.tts_api_key,
            "User-Agent": f"{settings.app_name}/1.0 (+gateway)",
        },
        timeout=httpx.Timeout(
            connect=settings.tts_connect_timeout_seconds,
            read=settings.tts_read_timeout_seconds,
            # httpx refuses a partial Timeout, so write and pool have to be
            # named too. Both are local operations — pushing a request body,
            # waiting for a free pooled connection — and both get the connect
            # budget: when the pool is full the GPU is already saturated, and
            # failing fast beats queueing a second time on our own side.
            write=settings.tts_connect_timeout_seconds,
            pool=settings.tts_connect_timeout_seconds,
        ),
        # No redirect following: we send an API key on every request, and an
        # upstream that wants to redirect us is a misconfiguration, not a route.
        follow_redirects=False,
    )


_client_instance: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """The process-wide client, built on first use.

    Lazily, for the same reason `get_cache()` is: `tests/conftest.py` drives the
    app through `ASGITransport`, where the lifespan never fires, so anything
    constructed in the lifespan is `None` for the whole suite.
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = build_client()
    return _client_instance


async def aclose_client() -> None:
    """Drop the client and its connection pool. Called from the lifespan.

    Also the reset hook for tests: after monkeypatching `build_client`, await
    this to discard a client that was built from the real settings.
    """
    global _client_instance
    if _client_instance is not None:
        await _client_instance.aclose()
        _client_instance = None


def require_configured() -> None:
    """Refuse the call up front when this deployment has no TTS box.

    Every route calls this first, so a missing `TTS_BASE_URL` is a clean 503
    with a code the frontend can branch on, rather than a connection error
    surfacing six frames down as a 502 that looks like an upstream outage.
    """
    if not settings.has_tts:
        raise ServiceUnavailableError(
            "Speech synthesis is not configured on this server.",
            code="tts_not_configured",
        )


# --- failure mapping, in one place -----------------------------------------


def _log_key_rejected(response: httpx.Response) -> None:
    # ERROR, not WARNING: nothing the user does can fix this and nothing
    # retries it away. Someone has to rotate a key or fix an .env.
    logger.error(
        "TTS rejected our API key: %s %s answered %s",
        response.request.method,
        response.request.url.path,
        response.status_code,
    )


def _upstream_detail(response: httpx.Response) -> str | None:
    """Upstream's own words about a rejected payload, if it said anything useful."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        # FastAPI's validation shape: a list of {loc, msg} under `detail`.
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict) and isinstance(first.get("msg"), str):
                field = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
                return f"{field}: {first['msg']}" if field else str(first["msg"])
    return None


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("Retry-After", "")
    try:
        # Only the delta-seconds form. The HTTP-date form is legal and nobody
        # sends it; parsing it would be more code than the case deserves.
        return max(1, int(raw.strip()))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS


def _raise_for_upstream(response: httpx.Response) -> None:
    """Turn an upstream status into one of this app's errors. Returns on 2xx.

    The body must already be in memory — callers on the streaming path do
    `await response.aread()` first — because the 422 branch relays upstream's
    own field message.
    """
    status = response.status_code
    if status < 300:
        return

    if status in (401, 403):
        _log_key_rejected(response)
        raise ServiceUnavailableError(
            "Speech synthesis is unavailable right now. Please try again shortly.",
            code="tts_key_rejected",
        )

    if status == 402:
        # Our tenant-wide character quota, not the caller's wallet. Relaying a
        # 402 would be actively harmful: this API already uses 402 for "top up
        # your credits" and the Nuxt client opens the top-up dialog on it, so a
        # user would be asked to pay for a shortfall on our account.
        logger.error("TTS reports our tenant quota is exhausted (%s)", response.request.url.path)
        raise ServiceUnavailableError(
            "Speech synthesis is temporarily unavailable. Please try again later.",
            code="tts_quota_exhausted",
        )

    if status == 404:
        raise NotFoundError(
            "The speech service has no such voice or job.",
            code="tts_not_found",
        )

    if status == 429:
        raise TooManyRequestsError(
            "The speech service is busy. Please try again in a moment.",
            code="tts_busy",
            retry_after=_retry_after(response),
        )

    if 400 <= status < 500:
        # 422 is the interesting one and the rest behave the same way: the
        # payload came from the caller's own text, voice and format, so their
        # message is about their own input and is safe to relay. Upstream never
        # sees our key id or our user ids, so there is nothing here to leak.
        detail = _upstream_detail(response)
        raise BadRequestError(
            detail or "The speech service rejected this request.",
            code="tts_rejected_input",
        )

    # 3xx lands here too, deliberately: redirects are not followed, so one means
    # the base URL is wrong, and "unreachable" is the truthful answer.
    logger.warning(
        "TTS %s %s answered %s",
        response.request.method,
        response.request.url.path,
        status,
    )
    raise BadGatewayError(
        "Could not reach the speech service. Please try again.",
        code="tts_unreachable",
    )


def _unreachable(exc: httpx.HTTPError) -> BadGatewayError:
    if isinstance(exc, httpx.TimeoutException):
        logger.warning("TTS timed out: %s", exc)
        return BadGatewayError(
            "The speech service did not answer in time. Please try again.",
            code="tts_unreachable",
        )
    logger.warning("TTS transport failure: %s", exc)
    return BadGatewayError(
        "Could not reach the speech service. Please try again.",
        code="tts_unreachable",
    )


def _operation(method: str, path: str) -> str:
    """A metric label for one upstream call, from a closed set.

    The path carries voice ids and job ids, so it cannot be a label (see the
    label rule in `app/core/metrics.py`). This collapses it to the operation
    the path names — `voices`, `batch`, `usage` — which is what a dashboard
    groups by anyway.
    """
    if path.startswith(PATH_VOICES):
        return f"voices.{method.lower()}"
    if path.startswith(PATH_BATCH):
        if path.endswith("/results"):
            return "batch.results"
        return f"batch.{method.lower()}"
    if path.startswith(PATH_USAGE):
        return "usage"
    return "other"


async def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
) -> Any | None:
    """One request, one error vocabulary. `None` when the answer has no body."""
    require_configured()
    operation = _operation(method, path)
    started = time.perf_counter()
    try:
        response = await _client().request(
            method,
            path,
            json=json,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        # TimeoutException is a subclass of TransportError, so both arrive here
        # and `_unreachable` tells them apart for the log line only — the code
        # the caller sees is the same either way.
        error = _unreachable(exc)
        metrics.record_upstream_error(operation=operation, code=error.code)
        raise error from exc

    # Observed before the status is judged, so a slow refusal is still timed:
    # a box that takes nine seconds to answer 429 is the interesting case, and
    # timing only the successes would hide it.
    metrics.observe_upstream(operation=operation, seconds=time.perf_counter() - started)
    try:
        _raise_for_upstream(response)
    except AppError as error:
        metrics.record_upstream_error(operation=operation, code=error.code)
        raise

    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        # A 2xx whose body is not JSON is an upstream failure too, and the one
        # most likely to be a proxy in the way rather than the box itself.
        metrics.record_upstream_error(operation=operation, code="tts_unreadable")
        raise BadGatewayError(
            "The speech service sent a response we could not read.",
            code="tts_unreadable",
        ) from exc


async def _request_object(method: str, path: str, *, json: Any = None) -> dict[str, Any]:
    """`_request` where the caller needs an object and cannot use anything else."""
    payload = await _request(method, path, json=json)
    if not isinstance(payload, dict):
        raise BadGatewayError(
            "The speech service sent a response we could not read.",
            code="tts_unreadable",
        )
    return payload


# --- synthesis --------------------------------------------------------------


@asynccontextmanager
async def stream_speech(body: dict[str, Any]) -> AsyncIterator[httpx.Response]:
    """Open `POST /v1/tts/stream` and hand back the live response.

    The response is yielded rather than its bytes because the caller must
    consume it with **`aiter_raw()`**, and only the caller knows what to do
    between chunks. Upstream's own documentation measures the difference:
    `iter_raw()` reaches the first audio in 100 ms, `iter_bytes()` in 1253 ms.
    `iter_bytes()` runs the body through content decoding, which buffers, and
    buffering a stream discards the entire reason this endpoint exists. Anyone
    editing this file: relaying the whole body and returning it would still
    pass the tests and would still be wrong.

    The headers arrive before the first chunk, so `response.headers` — in
    particular `x-audio-sample-rate` — can be relayed onto our own 200 while
    the GPU is still working.

    Errors are mapped as everywhere else, which means an upstream refusal
    raises here, before a single byte has been yielded, and the route can still
    turn it into a real status code.
    """
    require_configured()
    client = _client()
    request = client.build_request(
        "POST",
        PATH_STREAM,
        json=body,
        # `aiter_raw()` yields the body exactly as it came off the wire, so a
        # compressed response would be relayed to our own caller as gzip
        # without the header that says so. Asking for identity keeps raw and
        # decoded identical, and costs nothing: audio does not compress.
        headers={"Accept-Encoding": "identity"},
    )
    # The headers, not the audio. This is the number that decides time to first
    # sound, and the one worth an alert: the whole relay is measured separately
    # by `synora_tts_stream_seconds`, where a long value means a long text
    # rather than a struggling card.
    started = time.perf_counter()
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        error = _unreachable(exc)
        metrics.record_upstream_error(operation="stream", code=error.code)
        raise error from exc

    metrics.observe_upstream(operation="stream", seconds=time.perf_counter() - started)

    try:
        if response.status_code >= 300:
            # An error body is small and JSON; read it so the 4xx branches can
            # quote upstream's own words about the caller's text.
            await response.aread()
            try:
                _raise_for_upstream(response)
            except AppError as error:
                metrics.record_upstream_error(operation="stream", code=error.code)
                raise
        yield response
    finally:
        await response.aclose()


# --- voices -----------------------------------------------------------------


async def list_voices() -> list[dict[str, Any]]:
    """Every voice this tenant can synthesise with, cloned ones included."""
    payload = await _request("GET", PATH_VOICES)
    # The envelope carries nothing but the list. Unwrapping it here keeps all
    # of the shape-guessing about upstream in this one module.
    if isinstance(payload, dict):
        voices = payload.get("voices")
    else:
        voices = payload
    if not isinstance(voices, list):
        raise BadGatewayError(
            "The speech service sent a voice list we could not read.",
            code="tts_unreadable",
        )
    return [voice for voice in voices if isinstance(voice, dict)]


async def register_voice(body: dict[str, Any]) -> dict[str, Any]:
    """Clone a voice from a 3-30 second base64 clip. Returns the new voice."""
    return await _request_object("POST", PATH_VOICES, json=body)


async def delete_voice(voice_id: str) -> None:
    """Remove a cloned voice. A 404 from upstream stays a 404 for the caller."""
    # Percent-encoded rather than interpolated: the id reaches us from the
    # caller's URL, and a stray slash in it must not reshape the upstream path.
    await _request("DELETE", f"{PATH_VOICES}/{quote(voice_id, safe='')}")


# --- batch ------------------------------------------------------------------


async def submit_batch(body: dict[str, Any]) -> dict[str, Any]:
    """Hand a whole job to upstream. Answers 202 with the job in `pending`.

    The body carries our own `idempotency_key`, so a redelivered queue message
    gets the original job back instead of synthesising everything twice.
    """
    return await _request_object("POST", PATH_BATCH, json=body)


async def batch_status(job_id: str) -> dict[str, Any]:
    """Counters and state for one job: pending, running, succeeded, failed, cancelled."""
    return await _request_object("GET", f"{PATH_BATCH}/{quote(job_id, safe='')}")


async def batch_results(job_id: str) -> dict[str, Any]:
    """Per-item results, including the characters and audio seconds we settle on."""
    return await _request_object("GET", f"{PATH_BATCH}/{quote(job_id, safe='')}/results")


async def cancel_batch(job_id: str) -> dict[str, Any] | None:
    """Ask upstream to stop a job. `None` when it answers without a body.

    Whatever it reports as used up to that point is still billable, so the
    caller settles on the counters rather than on this return value.
    """
    payload = await _request("DELETE", f"{PATH_BATCH}/{quote(job_id, safe='')}")
    return payload if isinstance(payload, dict) else None


# --- account ----------------------------------------------------------------


async def usage() -> dict[str, Any]:
    """Upstream's own character counters.

    Tenant-wide, not per user: this is what *we* have spent against the TTS
    provider, and it is an operations number. A user's own consumption comes
    from `usage_events` in our database, which is the whole point of metering
    on this side of the gateway.
    """
    return await _request_object("GET", PATH_USAGE)


async def readyz() -> bool:
    """Whether upstream is ready to take traffic. `/readyz`, not `/healthz`.

    The one call here that answers instead of raising, because its answer *is*
    the failure state: a health panel that 502s when the thing it is reporting
    on is down has nothing left to report. A rejected key still gets its ERROR
    line, since a probe that quietly says "not ready" for weeks is how a stale
    credential survives.
    """
    require_configured()
    try:
        response = await _client().get(PATH_READYZ, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.warning("TTS readiness probe failed: %s", exc)
        return False

    if response.status_code in (401, 403):
        _log_key_rejected(response)
        return False
    if response.status_code >= 300:
        logger.warning("TTS readiness probe answered %s", response.status_code)
        return False

    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(payload.get("ready"))
