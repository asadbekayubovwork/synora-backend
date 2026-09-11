"""The transcription box, and nothing else.

The mirror of `tts_client`, against a different upstream and with the same rule:
this module knows the STT service's URL, its token and the shape of its JSON,
and it knows nothing about wallets. A client that also billed would have to
decide what a transcription that failed after the upload costs, and that
decision belongs next to the money.

Two differences from the speech box are worth naming, because both are places a
copy-paste from `tts_client` would be quietly wrong.

**The credential is `X-Token`, not `X-API-Key`.** Same trade — our key never
leaves this process, the caller authenticates to us with their own JWT — and a
different header name, so a 401 from upstream is still our fault and still
never relayed as a 401.

**The request is multipart, not JSON.** The audio goes up as a file part, which
means the request body is the caller's upload and can be tens of megabytes.
`stt_service` bounds it before we get here; nothing in this module buffers a
second copy.

## One place to fail

Every call funnels through `_raise_for_upstream`, so routes never touch an
`httpx.Response`. The codes it can raise, all stable:

    stt_not_configured   no STT_BASE_URL/STT_API_KEY on this deployment (503)
    stt_key_rejected     upstream refused our token (503, logged at ERROR)
    stt_not_ready        the model is still loading (503, with Retry-After)
    stt_rejected_input   upstream refused the caller's own audio (400)
    stt_busy             upstream is saturated (429, with Retry-After)
    stt_unreachable      timeout, transport failure, redirect or 5xx (502)
    stt_unreadable       a 2xx whose body is not the JSON we expected (502)

`stt_not_ready` is the one with no counterpart on the TTS side. The service
answers 503 until the checkpoint is warm and publishes `/readyz` to say so, and
that is a genuinely different instruction to a client from "our key was
refused": one is worth retrying in a few seconds and the other will never
succeed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadGatewayError,
    BadRequestError,
    ServiceUnavailableError,
    TooManyRequestsError,
)

logger = logging.getLogger("synora.stt")

PATH_TRANSCRIBE = "/v1/transcribe"
PATH_READYZ = "/readyz"

# Upstream declines anything else by name, so the set is restated here to be
# refused in our own validation pass rather than after a hold has been placed.
LANGUAGES = ("uz", "ru", "en")

DEFAULT_RETRY_AFTER_SECONDS = 5
# A model that is still loading is worth waiting longer for than a saturated
# one: the wait is bounded by a checkpoint load rather than by a queue.
NOT_READY_RETRY_AFTER_SECONDS = 15


def build_client() -> httpx.AsyncClient:
    """Construct the outbound client. Also the seam the tests replace."""
    return httpx.AsyncClient(
        base_url=settings.stt_base_url.rstrip("/"),
        headers={
            "X-Token": settings.stt_api_key,
            "User-Agent": f"{settings.app_name}/1.0 (+gateway)",
        },
        timeout=httpx.Timeout(
            connect=settings.stt_connect_timeout_seconds,
            read=settings.stt_read_timeout_seconds,
            # The write budget is the *read* one here, unlike the TTS client.
            # What gets written is the caller's upload — up to
            # `STT_MAX_AUDIO_BYTES` — and a 25 MB body over a slow tunnel
            # legitimately takes longer than a connect.
            write=settings.stt_read_timeout_seconds,
            pool=settings.stt_connect_timeout_seconds,
        ),
        follow_redirects=False,
    )


_client_instance: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = build_client()
    return _client_instance


async def aclose_client() -> None:
    """Drop the client and its pool. The lifespan calls it; tests reset with it."""
    global _client_instance
    if _client_instance is not None:
        await _client_instance.aclose()
        _client_instance = None


def require_configured() -> None:
    if not settings.has_stt:
        raise ServiceUnavailableError(
            "Transcription is not configured on this server.",
            code="stt_not_configured",
        )


def _detail(response: httpx.Response) -> str | None:
    """Upstream's own words about a rejected upload, if it said anything useful."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict) and isinstance(first.get("msg"), str):
                field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
                return f"{field}: {first['msg']}" if field else str(first["msg"])
    return None


def _retry_after(response: httpx.Response, fallback: int) -> int:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(1, int(raw.strip()))
    except (TypeError, ValueError):
        return fallback


def _raise_for_upstream(response: httpx.Response) -> None:
    """Turn an upstream status into one of this app's errors. Returns on 2xx."""
    status = response.status_code
    if status < 300:
        return

    if status in (401, 403):
        # ERROR, not WARNING: nothing the caller did produced this and nothing
        # retries it away. Somebody has to rotate a token.
        logger.error(
            "STT rejected our token: %s %s answered %s",
            response.request.method,
            response.request.url.path,
            status,
        )
        raise ServiceUnavailableError(
            "Transcription is unavailable right now. Please try again shortly.",
            code="stt_key_rejected",
        )

    if status == 429:
        raise TooManyRequestsError(
            "The transcription service is busy. Please try again in a moment.",
            code="stt_busy",
            retry_after=_retry_after(response, DEFAULT_RETRY_AFTER_SECONDS),
        )

    if status == 503:
        # The documented "model still loading" answer. Told apart from a
        # rejected token because the instruction to the client is the opposite:
        # this one succeeds on its own in a few seconds.
        logger.warning("STT is not ready yet (%s)", response.request.url.path)
        raise ServiceUnavailableError(
            "The transcription model is still starting. Please try again shortly.",
            code="stt_not_ready",
            retry_after=_retry_after(response, NOT_READY_RETRY_AFTER_SECONDS),
        )

    if 400 <= status < 500:
        # The caller's own audio and the caller's own language. Upstream's
        # message names which — "unsupported language 'fr'", "audio decode
        # failed" — and neither leaks anything of ours, so it is relayed.
        raise BadRequestError(
            _detail(response) or "The transcription service rejected this audio.",
            code="stt_rejected_input",
        )

    logger.warning(
        "STT %s %s answered %s",
        response.request.method,
        response.request.url.path,
        status,
    )
    raise BadGatewayError(
        "Could not reach the transcription service. Please try again.",
        code="stt_unreachable",
    )


def _unreachable(exc: httpx.HTTPError) -> BadGatewayError:
    if isinstance(exc, httpx.TimeoutException):
        logger.warning("STT timed out: %s", exc)
        return BadGatewayError(
            "The transcription service did not answer in time. Please try again.",
            code="stt_unreachable",
        )
    logger.warning("STT transport failure: %s", exc)
    return BadGatewayError(
        "Could not reach the transcription service. Please try again.",
        code="stt_unreachable",
    )


async def transcribe(
    audio: bytes, *, filename: str, content_type: str | None, language: str
) -> dict[str, Any]:
    """One upload, one transcript. Returns upstream's JSON object.

    The response carries `audio_seconds`, which is what settlement bills on:
    upstream decoded the file and we did not, so its count is the only one
    either side can check.
    """
    require_configured()
    started = time.perf_counter()
    try:
        response = await _client().post(
            PATH_TRANSCRIBE,
            files={"file": (filename, audio, content_type or "application/octet-stream")},
            data={"language": language},
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        error = _unreachable(exc)
        metrics.record_stt_upstream_error(code=error.code)
        raise error from exc

    # Timed before the status is judged, so a slow refusal is still measured —
    # a box taking nine seconds to answer 503 is the interesting case.
    metrics.observe_stt_upstream(seconds=time.perf_counter() - started)
    try:
        _raise_for_upstream(response)
    except AppError as error:
        metrics.record_stt_upstream_error(code=error.code)
        raise

    try:
        payload = response.json()
    except ValueError as exc:
        metrics.record_stt_upstream_error(code="stt_unreadable")
        raise BadGatewayError(
            "The transcription service sent a response we could not read.",
            code="stt_unreadable",
        ) from exc

    if not isinstance(payload, dict) or "text" not in payload:
        # A 2xx in the wrong shape is an upstream failure, not a transcript.
        # Checked here rather than in the schema builder so the route never has
        # to wonder whether `text` is a string or missing entirely.
        metrics.record_stt_upstream_error(code="stt_unreadable")
        raise BadGatewayError(
            "The transcription service sent a response we could not read.",
            code="stt_unreadable",
        )
    return payload


async def readyz() -> bool:
    """Whether the model is warm. Answers instead of raising, as TTS's does."""
    require_configured()
    try:
        response = await _client().get(PATH_READYZ, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.warning("STT readiness probe failed: %s", exc)
        return False

    if response.status_code in (401, 403):
        logger.error("STT rejected our token on the readiness probe")
        return False
    if response.status_code >= 300:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    # `{"status": "ready"}`, which is its own spelling rather than TTS's
    # `{"ready": true}`. Two services, two vocabularies, and this is the module
    # whose job is to know that.
    return isinstance(payload, dict) and payload.get("status") == "ready"
