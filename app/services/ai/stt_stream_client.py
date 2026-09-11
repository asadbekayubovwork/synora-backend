"""The transcription box's websocket, and nothing else.

`stt_client` talks to the same service over HTTP for one file at a time; this
module is the live half. Same rule as everywhere in `app/services/ai/`: it
knows the socket, the token and the event shapes, and it knows nothing about
wallets. The metering lives in `stt_stream_service`, next to the money.

## The protocol, as upstream publishes it

    → {"type":"start","language":"uz","sample_rate":16000}
    → binary frames: little-endian PCM16, mono, at the declared rate
    → {"type":"stop"}

    ← {"type":"ready","session_id":"…"}
    ← {"type":"speech_started","session_id":"…"}
    ← {"type":"final","seq":0,"text":"…","audio_seconds":2.1,"infer_seconds":…}
    ← {"type":"done","segments":3}
    ← {"type":"error","message":"…"}          the socket stays open

`final` arrives once per VAD-closed segment, in spoken order, and **`done` is
the end of the transcript, not the first `final`** — one spoken turn routinely
closes several segments. A gap in `seq` means upstream dropped a segment
because it was behind, and an `error` event will have said so.

The `audio_seconds` on each `final` is the number that gets billed, and it is
the sum over segments rather than the wall clock: VAD trims the silence, and
charging for silence through this route while the file route charges for
speech would make the same recording cost two different amounts depending on
how it was sent.

## Why the SSL context is built here

`websockets` uses the standard library's default context, which on a
python.org macOS build has no CA bundle at all — the first connection dies with
`CERTIFICATE_VERIFY_FAILED` and reads like an upstream outage. `httpx` avoids
this by shipping `certifi`, so the same bundle is used here rather than
discovering the difference once per developer.

## Failures map to the same codes as the HTTP client

The handshake can fail with a real status, and those mean what they mean on
`/v1/transcribe`: 401 is our token, 503 is a model still loading. Anything else
is `stt_unreachable`. Once the socket is open a drop is not an error code at
all — it is the end of the session, and `stt_stream_service` settles for what
was transcribed before it.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import certifi
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus, WebSocketException

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import BadGatewayError, ServiceUnavailableError

logger = logging.getLogger("synora.stt")

PATH_STREAM = "/v1/stream"

# Upstream resamples anything else, and says so; 16 kHz is what its model wants
# and what skips that work.
PREFERRED_SAMPLE_RATE = 16_000
MIN_SAMPLE_RATE, MAX_SAMPLE_RATE = 8_000, 192_000

_ssl_context: ssl.SSLContext | None = None


def _ssl() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context


def stream_url() -> str:
    """`wss://…/v1/stream`, from the same base URL the HTTP client uses."""
    base = settings.stt_base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + PATH_STREAM
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + PATH_STREAM
    return base + PATH_STREAM


class UpstreamStream:
    """One open session with the transcription service."""

    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket

    async def start(self, *, language: str, sample_rate: int) -> None:
        await self._socket.send(
            json.dumps(
                {"type": "start", "language": language, "sample_rate": sample_rate}
            )
        )

    async def send_audio(self, chunk: bytes) -> None:
        await self._socket.send(chunk)

    async def stop(self) -> None:
        """Ask for the tail of the transcript. `done` follows, eventually."""
        await self._socket.send(json.dumps({"type": "stop"}))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Every JSON event, in order. Binary from upstream is ignored.

        A frame that is not JSON is dropped with a log line rather than raised:
        the caller is in the middle of a billed session, and one malformed
        frame is not a reason to end it.
        """
        async for raw in self._socket:
            if isinstance(raw, bytes):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                logger.warning("stt_stream_unreadable_event %r", raw[:120])
                continue
            if isinstance(event, dict):
                yield event


@asynccontextmanager
async def connect(*, language: str, sample_rate: int) -> AsyncIterator[UpstreamStream]:
    """Open the upstream socket and send `start`. The seam the tests replace."""
    if not settings.has_stt:
        raise ServiceUnavailableError(
            "Transcription is not configured on this server.",
            code="stt_not_configured",
        )

    started = time.perf_counter()
    try:
        socket = await websockets.connect(
            stream_url(),
            additional_headers={"X-Token": settings.stt_api_key},
            ssl=_ssl(),
            # Audio frames are small; the events are smaller. The default cap
            # is a megabyte and nothing here approaches it, but an upstream
            # that sends a long transcript in one frame should not be cut off
            # by a limit of ours.
            max_size=None,
            open_timeout=settings.stt_connect_timeout_seconds,
            # The library's own keepalive. A half-open socket on a tunnel that
            # went away otherwise holds a wallet's credit until the reaper.
            ping_interval=20,
            ping_timeout=20,
        )
    except InvalidStatus as error:
        status = error.response.status_code
        metrics.record_stt_upstream_error(code=_code_for(status))
        raise _handshake_error(status) from error
    except (WebSocketException, OSError, TimeoutError) as error:
        metrics.record_stt_upstream_error(code="stt_unreachable")
        logger.warning("stt_stream_connect_failed: %s", error)
        raise BadGatewayError(
            "Could not reach the transcription service. Please try again.",
            code="stt_unreachable",
        ) from error

    metrics.observe_stt_upstream(seconds=time.perf_counter() - started)
    stream = UpstreamStream(socket)
    try:
        await stream.start(language=language, sample_rate=sample_rate)
        yield stream
    finally:
        await socket.close()


def _code_for(status: int) -> str:
    if status in (401, 403):
        return "stt_key_rejected"
    if status == 503:
        return "stt_not_ready"
    if status == 429:
        return "stt_busy"
    return "stt_unreachable"


def _handshake_error(status: int) -> Exception:
    """The same vocabulary `stt_client` raises, from a websocket handshake."""
    if status in (401, 403):
        logger.error("STT rejected our token on the stream handshake (%s)", status)
        return ServiceUnavailableError(
            "Transcription is unavailable right now. Please try again shortly.",
            code="stt_key_rejected",
        )
    if status == 503:
        return ServiceUnavailableError(
            "The transcription model is still starting. Please try again shortly.",
            code="stt_not_ready",
            retry_after=15,
        )
    logger.warning("STT stream handshake answered %s", status)
    return BadGatewayError(
        "Could not reach the transcription service. Please try again.",
        code="stt_unreachable",
    )
