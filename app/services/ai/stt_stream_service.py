"""One metered realtime transcription: hold, relay, settle.

The first live session this codebase bills. Everything before it — a synthesis,
a transcription, a batch job — is a one-shot: the work is bounded by a request,
and the only question at the end is what it cost. A websocket has no such
bound. It ends when somebody closes it, which may be in two seconds or in two
hours, and the credit has to be committed before the first word is spoken.

## It is a bounded one-shot, not a lifecycle

`session_service` has `open_oneshot` and `settle_oneshot` and no realtime
counterpart: there is no `extend_hold`, no grace ladder, no `finalize`. Those
are described in `docs/INTERNAL_API.md` as the shape the microservice API will
take, and they do not exist yet.

So this module does not pretend to have them. It holds for the **ceiling** —
`STT_STREAM_MAX_SECONDS` of connection *and* the same span of continuous speech
— cuts the session off at that ceiling, and settles on what actually happened.
The customer is quoted nothing and pays for what was transcribed; what they
briefly see is a larger `reserved`.

The cost of that shortcut is honest and worth writing down: a ten-minute cap
reserves about fourteen credits at the seeded prices, so an account with less
than that cannot open a stream at all even for a ten-second question. The fix
is hold extension, and when this needs longer sessions that is the work — not
a bigger ceiling.

## What is billed, and why it is two metrics

`stt_audio_ms` is the sum of `audio_seconds` over the `final` events: the audio
VAD actually closed a segment on. `session_ms` is the wall clock the socket was
open.

Both, deliberately. Audio alone lets a caller hold a GPU slot open in silence
for free, and upstream's own documentation is explicit that a live session
occupies the card. Wall clock alone would charge the same for ten minutes of
speech as for ten minutes of nothing, and would make the identical recording
cost differently depending on whether it was streamed or uploaded. Components
plus a connection fee is the shape the price book already uses for
`voice_agent`, and this is the same trade.

A consequence to keep in mind when reading an invoice: the two numbers do not
add up to the same thing. Six seconds of speech in this module's own testing
closed three segments totalling five seconds of audio across a socket that was
open for eight. VAD trims the silence; the connection fee is what covers it.

## Nothing is charged for a session that produced nothing

A handshake upstream refuses, a token of ours it rejects, a model still
loading — all of them release the hold in full and write no usage event, the
same rule the file route follows. A session that connected and transcribed
nothing is charged the connection fee, because the slot was genuinely held.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import AppError, BadRequestError
from app.core.money import format_credits
from app.db.session import SessionLocal
from app.models.billing_enums import (
    AiSessionKind,
    BillingService,
    SessionEndReason,
    UsageMetric,
)
from app.models.user import User
from app.services.ai import stt_client, stt_stream_client
from app.services.billing import session_service

logger = logging.getLogger("synora.stt")

IDEMPOTENCY_SCOPE = "stream"

# How often progress is stamped on the session row while audio is moving. The
# reaper asks "has anything happened lately", and without this a stream that
# outlives `expires_at` is reaped mid-sentence — the same reason
# `tts_service` touches its own sessions as bytes go past.
PROGRESS_TOUCH_INTERVAL_SECONDS = 30


class ClientSocket(Protocol):
    """The caller's side of the socket, narrowed to what this module uses.

    A `Protocol` rather than `fastapi.WebSocket` so the whole session can be
    driven by a fake in tests. What is under test here is money, and it should
    not need a real socket to check it.
    """

    async def receive(self) -> dict[str, Any]: ...
    async def send_json(self, message: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class StreamOutcome:
    """What one session transcribed, and what it cost."""

    ai_session_id: uuid.UUID
    segments: int = 0
    audio_ms: int = 0
    session_ms: int = 0
    price_micros: int = 0
    end_reason: SessionEndReason = SessionEndReason.COMPLETED
    texts: list[str] = field(default_factory=list)


def validate_start(message: dict[str, Any]) -> tuple[str, int]:
    """The `start` message, checked before anything is held.

    Refused here rather than relayed for the reason the file route refuses a
    language: upstream would answer too, but only after a hold had been placed
    and released again.
    """
    language = str(message.get("language") or "uz")
    if language not in stt_client.LANGUAGES:
        raise BadRequestError(
            f"`{language}` is not one of {', '.join(stt_client.LANGUAGES)}.",
            code="stt_language_unsupported",
        )
    raw_rate = message.get("sample_rate", stt_stream_client.PREFERRED_SAMPLE_RATE)
    try:
        sample_rate = int(raw_rate)
    except (TypeError, ValueError):
        raise BadRequestError(
            "`sample_rate` must be a whole number of hertz.",
            code="stt_sample_rate_invalid",
        ) from None
    if not (
        stt_stream_client.MIN_SAMPLE_RATE
        <= sample_rate
        <= stt_stream_client.MAX_SAMPLE_RATE
    ):
        raise BadRequestError(
            f"`sample_rate` must be between {stt_stream_client.MIN_SAMPLE_RATE} "
            f"and {stt_stream_client.MAX_SAMPLE_RATE}.",
            code="stt_sample_rate_invalid",
        )
    return language, sample_rate


def _elapsed_ms(started: float) -> int:
    """How long the socket was open, and never zero.

    `max(1, …)` rather than the raw difference, and it is not cosmetic. A
    session measured at zero milliseconds reports a zero quantity, and
    `price_cumulative` skips a zero — so the connection fee's `min_charge`
    would never apply to the shortest sessions, which are precisely the ones a
    floor exists for. A socket that connected did occupy the slot, and one
    millisecond is the smallest true thing this clock can say about it.
    """
    return max(1, int((time.monotonic() - started) * 1000))


def ceiling_quantities() -> dict[UsageMetric, int]:
    """What the hold is priced from: the worst case a session can cost.

    The full cap on both metrics — a socket open for the whole allowance,
    talking for all of it. Anything less is a hold that does not cover the
    session it is holding for, and a settlement clamped below the work.
    """
    cap_ms = settings.stt_stream_max_seconds * 1000
    return {UsageMetric.SESSION_MS: cap_ms, UsageMetric.STT_AUDIO_MS: cap_ms}


async def run(
    client: ClientSocket,
    user: User,
    *,
    language: str,
    sample_rate: int,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> StreamOutcome:
    """Hold, relay the socket both ways, settle. Raises before the hold only.

    Once the hold exists every exit goes through `_settle`, including a client
    that vanished and an upstream that dropped: a live session whose credit is
    never returned is the failure this whole module is arranged around.
    """
    async with SessionLocal() as own:
        ticket = await session_service.open_oneshot(
            own,
            user_id=user.id,
            service=BillingService.STT,
            model_key=settings.stt_model_key,
            quantities=ceiling_quantities(),
            scope=IDEMPOTENCY_SCOPE,
            # The one place in this codebase that writes it. `AiSessionKind`
            # has had a `REALTIME` member since the billing core landed and
            # nothing has ever set it; a live socket is what it was for.
            kind=AiSessionKind.REALTIME,
            request_digest=session_service.request_digest_for(
                "stream", language, sample_rate
            ),
            # The hold has to outlive the session it covers, or the reaper
            # closes a stream that is still talking.
            ttl_seconds=settings.stt_stream_max_seconds + 120,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    outcome = StreamOutcome(ai_session_id=ticket.ai_session_id)
    started = time.monotonic()
    inflight = False

    try:
        async with stt_stream_client.connect(
            language=language, sample_rate=sample_rate
        ) as upstream:
            # Incremented once the socket is actually open, and decremented in
            # the `finally` below whatever ends it. Each one of these is a GPU
            # slot upstream held for the whole session — the number worth
            # watching when live sessions start queueing behind each other.
            metrics.stt_streams_inflight.inc()
            inflight = True
            await client.send_json(
                {
                    "type": "ready",
                    "ai_session_id": str(ticket.ai_session_id),
                    "language": language,
                    "sample_rate": sample_rate,
                    "max_seconds": settings.stt_stream_max_seconds,
                }
            )
            await _relay(client, upstream, outcome, started)
    except AppError:
        # Upstream refused the handshake. Nothing was transcribed and the
        # socket never carried audio, so the hold goes back whole.
        outcome.end_reason = SessionEndReason.UPSTREAM_ERROR
        await _abandon(ticket.ai_session_id, outcome.end_reason)
        raise
    except asyncio.CancelledError:
        outcome.end_reason = SessionEndReason.CLIENT_DISCONNECTED
        outcome.session_ms = _elapsed_ms(started)
        await asyncio.shield(_settle(ticket.ai_session_id, outcome))
        raise
    except Exception:
        outcome.end_reason = SessionEndReason.UPSTREAM_ERROR
        outcome.session_ms = _elapsed_ms(started)
        logger.exception("stt_stream_failed session=%s", ticket.ai_session_id)
        await _settle(ticket.ai_session_id, outcome)
        return outcome

    finally:
        if inflight:
            metrics.stt_streams_inflight.dec()

    outcome.session_ms = _elapsed_ms(started)
    await _settle(ticket.ai_session_id, outcome)
    return outcome


async def _relay(
    client: ClientSocket,
    upstream: stt_stream_client.UpstreamStream,
    outcome: StreamOutcome,
    started: float,
) -> None:
    """Pump audio one way and events the other until somebody stops.

    Two tasks rather than one loop, because both directions can block: the
    client may be silent while upstream is still transcribing a segment it
    closed ten seconds ago, and a single loop would make the transcript wait on
    the microphone.
    """
    stopping = asyncio.Event()

    async def pump_audio() -> None:
        last_audio = time.monotonic()
        last_touch = time.monotonic()
        while True:
            timeout = settings.stt_stream_idle_seconds
            try:
                message = await asyncio.wait_for(client.receive(), timeout=timeout)
            except TimeoutError:
                outcome.end_reason = SessionEndReason.HEARTBEAT_TIMEOUT
                logger.info("stt_stream_idle session=%s", outcome.ai_session_id)
                break

            if message.get("type") == "websocket.disconnect":
                outcome.end_reason = SessionEndReason.CLIENT_DISCONNECTED
                break

            chunk = message.get("bytes")
            if chunk:
                await upstream.send_audio(chunk)
                last_audio = time.monotonic()
                now = time.monotonic()
                if now - last_touch >= PROGRESS_TOUCH_INTERVAL_SECONDS:
                    last_touch = now
                    await _touch(outcome.ai_session_id)
            elif message.get("text"):
                # The only control message a client sends mid-session.
                if _is_stop(message["text"]):
                    outcome.end_reason = SessionEndReason.STOP_REQUESTED
                    break

            if time.monotonic() - started >= settings.stt_stream_max_seconds:
                outcome.end_reason = SessionEndReason.GRACE_EXHAUSTED
                logger.info("stt_stream_capped session=%s", outcome.ai_session_id)
                break
            if time.monotonic() - last_audio >= settings.stt_stream_idle_seconds:
                outcome.end_reason = SessionEndReason.HEARTBEAT_TIMEOUT
                break

        # Whatever ended the loop, upstream is asked for the tail: segments it
        # has already closed are transcript the caller has paid for, and
        # dropping the socket here would throw them away.
        stopping.set()
        with contextlib.suppress(Exception):
            await upstream.stop()

    async def pump_events() -> None:
        async for event in upstream.events():
            kind = event.get("type")
            if kind == "final":
                outcome.segments += 1
                seconds = float(event.get("audio_seconds") or 0)
                outcome.audio_ms += int(seconds * 1000)
                outcome.texts.append(str(event.get("text") or ""))
                await client.send_json(
                    {
                        "type": "final",
                        "seq": event.get("seq", outcome.segments - 1),
                        "text": event.get("text", ""),
                        "language": event.get("language"),
                        "audio_ms": int(seconds * 1000),
                    }
                )
            elif kind == "speech_started":
                # Relayed unchanged: this is the caller's barge-in trigger and
                # every millisecond of it matters to them.
                await client.send_json({"type": "speech_started"})
            elif kind == "error":
                logger.warning(
                    "stt_stream_upstream_error session=%s: %s",
                    outcome.ai_session_id,
                    event.get("message"),
                )
                await client.send_json(
                    {"type": "error", "code": "stt_stream_upstream", "message": event.get("message", "")}
                )
            elif kind == "done":
                return

    audio_task = asyncio.create_task(pump_audio())
    events_task = asyncio.create_task(pump_events())
    try:
        # The audio side decides when the session is over; the event side then
        # has upstream's `done` to wait for, which is the tail of the
        # transcript and is not optional — see `stt_stream_client`.
        await audio_task
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(events_task, timeout=settings.stt_read_timeout_seconds)
    finally:
        for task in (audio_task, events_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _is_stop(text: str) -> bool:
    try:
        return json.loads(text).get("type") == "stop"
    except (ValueError, AttributeError):
        return False


async def _touch(ai_session_id: uuid.UUID) -> None:
    try:
        async with SessionLocal() as session:
            await session_service.touch_session(session, ai_session_id=ai_session_id)
    except Exception:  # noqa: BLE001 - a missed heartbeat is not worth the stream
        logger.warning("stt_stream_touch_failed session=%s", ai_session_id)


async def _abandon(ai_session_id: uuid.UUID, end_reason: SessionEndReason) -> None:
    try:
        async with SessionLocal() as session:
            await session_service.abandon_oneshot(
                session,
                ai_session_id=ai_session_id,
                end_reason=end_reason,
                error_code="stt_stream_failed",
            )
    except Exception:  # noqa: BLE001 - the reaper is the backstop
        logger.exception("stt_stream_abandon_failed session=%s", ai_session_id)


async def _settle(ai_session_id: uuid.UUID, outcome: StreamOutcome) -> None:
    """Charge for the connection and the audio. Never raises.

    Runs where the caller is already on their way out — a closed socket, a
    cancelled task — so an exception here would replace a recorded charge with
    a stack trace and a stranded hold.
    """
    try:
        async with SessionLocal() as session:
            settlement = await session_service.settle_oneshot(
                session,
                ai_session_id=ai_session_id,
                quantities={
                    UsageMetric.SESSION_MS: outcome.session_ms,
                    UsageMetric.STT_AUDIO_MS: outcome.audio_ms,
                },
                end_reason=outcome.end_reason,
            )
        outcome.price_micros = settlement.price_micros
        metrics.record_transcription(audio_seconds=outcome.audio_ms / 1000)
        metrics.record_stream_session(
            seconds=outcome.session_ms / 1000,
            segments=outcome.segments,
            end_reason=outcome.end_reason.value,
        )
        logger.info(
            "stt_stream_settled session=%s segments=%d audio_ms=%d session_ms=%d "
            "charge=%s reason=%s",
            ai_session_id,
            outcome.segments,
            outcome.audio_ms,
            outcome.session_ms,
            settlement.price_micros,
            outcome.end_reason.value,
        )
    except Exception:  # noqa: BLE001 - the reaper is the backstop
        logger.exception("stt_stream_settle_failed session=%s", ai_session_id)


def done_message(outcome: StreamOutcome) -> dict[str, Any]:
    """The last thing the caller is sent: the transcript's end, and the bill."""
    return {
        "type": "done",
        "ai_session_id": str(outcome.ai_session_id),
        "segments": outcome.segments,
        "audio_ms": outcome.audio_ms,
        "session_ms": outcome.session_ms,
        "price_micros": outcome.price_micros,
        "price": format_credits(outcome.price_micros),
        "end_reason": outcome.end_reason.value,
    }
