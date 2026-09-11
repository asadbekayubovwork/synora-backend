"""One metered transcription: estimate, hold, transcribe, settle.

The shape `tts_service` established, with the two halves swapped. There, the
billable quantity is known before the work — it is the length of the text in
the request — and the audio is the unknown. Here the request *is* the audio and
the billable quantity is its duration, which nothing on our side has decoded.

That inversion is the whole design problem, and this module's answer is:

    hold on our estimate  →  transcribe  →  settle on upstream's count

## Why we estimate at all

The alternative is to charge after the fact with no hold, and it gives away
transcription to anyone with an empty wallet: the work is done and the bill
arrives at a balance that cannot pay it. `wallet_repo` would write off the
difference, which is the mechanism for a session that ran out mid-call, not a
door to leave open. So a hold goes on first, and something has to price it.

For WAV and PCM the estimate is exact, read out of the RIFF header — which is
also the format the integration examples use. For everything else it is bytes
divided by `STT_ASSUMED_BYTES_PER_SECOND`, deliberately set low (64 kbps, where
speech is usually 128) so the estimate lands *above* the true duration.

The direction matters more than the accuracy. Over-estimating holds more credit
than the call costs and gives the remainder straight back at settlement — a
customer briefly sees a larger `reserved` and pays the real price. Under-
estimating is silent: `settle_oneshot` clamps a charge at the hold, so the call
would bill less than the audio it transcribed and only a `disputed` flag would
record it. One of those is a support question, the other is revenue nobody
counts.

## Why settlement uses upstream's number

`audio_seconds` comes back on every successful response. Upstream decoded the
file and we did not, so its count is the only one either side can check — and
billing on our own estimate would mean charging for a duration nobody can
reproduce from the audio.

The clamp stays as the backstop: a service that starts reporting minutes for a
ten-second clip is charged at the hold and the session is flagged, rather than
quietly emptying a wallet.

## Failures land before or after the transcription, never during

There is no stream here, so the rule is simpler than TTS's. Upstream refusing
the upload — bad audio, an unsupported language, a token of ours it does not
like — charges nothing and hands the whole hold back. A response that arrives
is billed at what it reports. Nothing is charged for work that did not happen.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import AppError, BadRequestError, ConflictError
from app.core.money import format_credits
from app.db.session import SessionLocal
from app.models.billing_enums import BillingService, SessionEndReason, UsageMetric
from app.models.user import User
from app.services.ai import recording_store, stt_client, stt_transcription_service
from app.services.billing import session_service

logger = logging.getLogger("synora.stt")

IDEMPOTENCY_SCOPE = "transcribe"

HEADER_SESSION_ID = "X-Synora-Session-Id"
HEADER_AUDIO_MS = "X-Synora-Audio-Ms"
HEADER_PRICE_MICROS = "X-Synora-Price-Micros"
HEADER_PRICE = "X-Synora-Price"

EXPOSED_HEADERS: tuple[str, ...] = (
    HEADER_SESSION_ID,
    HEADER_AUDIO_MS,
    HEADER_PRICE_MICROS,
    HEADER_PRICE,
)

# RIFF/WAVE, enough of it to find the sample rate and the data chunk. Parsed by
# hand rather than with `wave`, which raises on the perfectly legal extra
# chunks that recorders put in front of `data` and which we have no reason to
# reject — this is a *billing estimate*, and the file still has to satisfy
# upstream's decoder afterwards either way.
_RIFF = b"RIFF"
_WAVE = b"WAVE"


@dataclass(slots=True)
class Transcription:
    """What one charged transcription produced, and what it cost."""

    ai_session_id: uuid.UUID
    text: str
    language: str
    audio_ms: int
    price_micros: int
    infer_seconds: float | None
    replayed: bool

    @property
    def headers(self) -> dict[str, str]:
        return {
            HEADER_SESSION_ID: str(self.ai_session_id),
            HEADER_AUDIO_MS: str(self.audio_ms),
            HEADER_PRICE_MICROS: str(self.price_micros),
            HEADER_PRICE: format_credits(self.price_micros),
        }


def wav_duration_ms(audio: bytes) -> int | None:
    """Milliseconds from a RIFF header, or `None` if this is not one.

    Walks the chunk list rather than assuming `fmt ` and `data` sit at fixed
    offsets, because `LIST`/`INFO` blocks in front of the audio are ordinary in
    files produced by phones and by ffmpeg.
    """
    if len(audio) < 12 or audio[:4] != _RIFF or audio[8:12] != _WAVE:
        return None

    byte_rate = 0
    offset = 12
    while offset + 8 <= len(audio):
        chunk_id = audio[offset : offset + 4]
        (size,) = struct.unpack_from("<I", audio, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt " and size >= 16 and body + 16 <= len(audio):
            # Bytes per second is field four, and it is the only one needed:
            # duration is data bytes over byte rate regardless of channels,
            # width or compression.
            (byte_rate,) = struct.unpack_from("<I", audio, body + 8)
        elif chunk_id == b"data":
            if byte_rate <= 0:
                return None
            # The declared size can exceed what is actually present in a file
            # that was truncated mid-recording; bill the bytes that are here.
            available = min(size, len(audio) - body)
            return max(0, available * 1000 // byte_rate)
        # Chunks are word-aligned: an odd size carries a pad byte.
        offset = body + size + (size % 2)
    return None


def estimate_ms(audio: bytes) -> int:
    """What to hold for, before anything has decoded the file.

    Exact for WAV, and a deliberate over-estimate for everything else. See the
    module docstring for why the direction is chosen rather than the accuracy.
    """
    exact = wav_duration_ms(audio)
    if exact is not None:
        return exact
    return math.ceil(len(audio) * 1000 / settings.stt_assumed_bytes_per_second)


async def transcribe(
    session: AsyncSession,
    user: User,
    *,
    audio: bytes,
    filename: str,
    content_type: str | None,
    language: str,
    idempotency_key: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> Transcription:
    """Hold, transcribe, settle. Returns the transcript and what it cost."""
    stt_client.require_configured()

    if not audio:
        raise BadRequestError("The upload is empty.", code="stt_audio_empty")

    # Both ceilings are checked before the wallet is touched, for the reason
    # `tts_service` checks its character limit there: a refusal after a hold is
    # credit that has to be given back, and a release never written cannot leak.
    #
    # Duration first, but only when it is *known* — a WAV header gives it
    # exactly. For anything else the duration is inferred from the byte count
    # and is deliberately an over-estimate, so leading with it would tell a
    # caller their thirty-minute recording is fifty minutes long. There, size
    # is the number we can actually defend.
    exact_ms = wav_duration_ms(audio)
    if exact_ms is not None:
        _refuse_if_too_long(exact_ms, exact=True)
    _refuse_if_too_large(audio, exact_ms)
    estimated_ms = exact_ms if exact_ms is not None else estimate_ms(audio)
    if exact_ms is None:
        _refuse_if_too_long(estimated_ms, exact=False)

    digest = session_service.request_digest_for(
        filename, content_type, language, len(audio), estimated_ms
    )

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.STT,
        model_key=settings.stt_model_key,
        quantities={UsageMetric.STT_AUDIO_MS: estimated_ms},
        scope=IDEMPOTENCY_SCOPE,
        idempotency_key=idempotency_key,
        request_digest=digest,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    if ticket.replayed:
        # A key already spent on a transcription. One code rather than TTS's
        # two, because the distinction TTS draws does not exist here: there is
        # no stream to join, and a transcript is not stored, so "the same
        # request again" and "a different request under this key" have the same
        # answer — send a fresh key. Re-running the model under a session
        # somebody has already paid for is work nobody is charged for.
        logger.info(
            "stt_replay session=%s user=%s", ticket.ai_session_id, ticket.user_id
        )
        raise ConflictError(
            "This idempotency key has already been used. Send a new one.",
            code="stt_idempotency_spent",
        )

    try:
        payload = await stt_client.transcribe(
            audio, filename=filename, content_type=content_type, language=language
        )
    except BaseException as error:
        # Nothing was produced, so nothing is charged. Its own session, not the
        # request's: the caller's transaction is about to be unwound by the
        # error on its way out, and the release has to survive that.
        #
        # `BaseException`, so a cancelled request — the client hung up during a
        # five-minute transcription — releases the hold too. Without that the
        # credit sits reserved until the reaper comes past, for a call nobody
        # was ever charged for.
        #
        # Shielded because an `await` in a cancelled task re-raises at its
        # first suspension, and that suspension is the write that gives the
        # credit back. `tts_service` shields its settlement for the same
        # reason and spells the argument out at greater length.
        cancelled = isinstance(error, asyncio.CancelledError)
        await asyncio.shield(
            _release(
                ticket.ai_session_id,
                end_reason=(
                    SessionEndReason.CLIENT_DISCONNECTED
                    if cancelled
                    else SessionEndReason.UPSTREAM_ERROR
                ),
                # The code the caller is about to be shown, so the row explains
                # itself: `stt_rejected_input` is the caller's audio and
                # `stt_key_rejected` is our own token, and a table of
                # abandoned sessions that cannot tell them apart is a table
                # nobody can act on.
                error_code=(
                    None
                    if cancelled
                    else error.code
                    if isinstance(error, AppError)
                    else "stt_unreachable"
                ),
            )
        )
        raise

    audio_ms = _audio_ms_of(payload, fallback=estimated_ms)

    async with SessionLocal() as own:
        settlement = await session_service.settle_oneshot(
            own,
            ai_session_id=ticket.ai_session_id,
            quantities={UsageMetric.STT_AUDIO_MS: audio_ms},
            end_reason=SessionEndReason.COMPLETED,
        )

    metrics.record_transcription(audio_seconds=audio_ms / 1000)

    # After the settlement and on a session of its own, exactly as the speech
    # side keeps its recordings: a keepsake that cannot be written must not be
    # able to roll back a charge that has already committed. Guarded end to end
    # — `_keep` never raises — because by this point the caller has paid and
    # the transcript is in hand.
    await _keep(
        ticket,
        audio=audio,
        filename=filename,
        content_type=content_type,
        language=str(payload.get("language") or language),
        body=str(payload.get("text") or ""),
        audio_ms=audio_ms,
        infer_seconds=_float_or_none(payload.get("infer_seconds")),
    )
    logger.info(
        "stt_transcribed session=%s user=%s bytes=%d estimated_ms=%d billed_ms=%d "
        "charge=%s clamped=%s",
        ticket.ai_session_id,
        ticket.user_id,
        len(audio),
        estimated_ms,
        audio_ms,
        settlement.price_micros,
        settlement.clamped,
    )

    return Transcription(
        ai_session_id=ticket.ai_session_id,
        text=str(payload.get("text") or ""),
        language=str(payload.get("language") or language),
        audio_ms=audio_ms,
        price_micros=settlement.price_micros,
        infer_seconds=_float_or_none(payload.get("infer_seconds")),
        replayed=False,
    )


def _refuse_if_too_long(audio_ms: int, *, exact: bool) -> None:
    if audio_ms <= settings.stt_max_audio_seconds * 1000:
        return
    about = "" if exact else "about "
    raise BadRequestError(
        f"This audio is {about}{audio_ms // 1000} seconds; the limit is "
        f"{settings.stt_max_audio_seconds}. Split it into shorter clips.",
        code="stt_audio_too_long",
    )


def _refuse_if_too_large(audio: bytes, exact_ms: int | None) -> None:
    """Refuse an over-large upload, and say what to do about it.

    The plain version of this message — "N bytes, the limit is M" — is a wall.
    It is also, nine times out of ten, the wrong diagnosis of the caller's
    problem: they did not record something enormous, they recorded five minutes
    of speech as uncompressed 48 kHz WAV, which is ten times the size of the
    same audio as mp3 and not one bit more useful to a transcription model that
    resamples to 16 kHz before it looks at anything.

    A WAV header tells us that for certain — the duration is in it — so when
    the file is one, the refusal names the cause and the fix instead of the
    number. It is the same information a support reply would contain, and this
    way nobody has to write the support reply.
    """
    limit = settings.stt_max_audio_bytes
    if len(audio) <= limit:
        return

    advice = ""
    if exact_ms:
        # What the same speech would weigh compressed, using the bitrate the
        # hold estimate already assumes. Deliberately the same constant: a
        # caller who follows this advice lands inside the estimate too.
        compressed_mb = (exact_ms / 1000) * settings.stt_assumed_bytes_per_second / 1_048_576
        advice = (
            f" This is {exact_ms // 1000} seconds of uncompressed audio; the "
            f"same recording as mp3 or m4a would be around "
            f"{max(0.1, compressed_mb):.1f} MB. Transcription resamples to "
            f"16 kHz, so compressing costs nothing."
        )

    raise BadRequestError(
        f"This upload is {len(audio) / 1_048_576:.1f} MB; the limit is "
        f"{limit / 1_048_576:.0f} MB.{advice}",
        code="stt_audio_too_large",
    )


async def _keep(
    ticket,
    *,
    audio: bytes,
    filename: str,
    content_type: str | None,
    language: str,
    body: str,
    audio_ms: int,
    infer_seconds: float | None,
) -> None:
    """Store the upload and its transcript. Never raises.

    The bytes are already in memory — they had to be, to be uploaded — so this
    writes them once rather than streaming, which is the one place it differs
    from `tts_service`'s capture. The store is the same one and the file is
    addressed the same way, which is what lets a synthesis and its own
    transcription share a single file on disk.
    """
    try:
        writer = await recording_store.begin()
        if writer is None:
            return
        writer.write(audio)
        committed = await writer.commit(audio_format=_extension_of(filename))
        if committed is None:
            return
        storage_key, digest = committed
        async with SessionLocal() as own:
            await stt_transcription_service.save(
                own,
                user_id=ticket.user_id,
                ai_session_id=ticket.ai_session_id,
                body=body,
                language=language,
                audio_ms=audio_ms,
                infer_ms=int((infer_seconds or 0) * 1000),
                filename=filename[:255],
                content_type=content_type[:128] if content_type else None,
                audio_bytes=len(audio),
                storage_key=storage_key,
                sha256=digest,
            )
    except Exception:  # noqa: BLE001 - never worth the transcript already paid for
        logger.exception(
            "transcription_keep_failed session=%s bytes=%d",
            ticket.ai_session_id,
            len(audio),
        )


def _extension_of(filename: str) -> str:
    """The upload's own extension, for the stored file's name.

    Taken from the client's filename rather than sniffed, and passed through
    `recording_store`'s whitelist — an unknown one is stored as `.bin`, which
    is honest about the fact that nothing here decoded the file. The extension
    never becomes part of a path we build: the path is the digest.
    """
    _, _, suffix = filename.rpartition(".")
    return suffix.lower() if suffix and suffix != filename else "bin"


async def _release(
    ai_session_id: uuid.UUID,
    *,
    end_reason: SessionEndReason,
    error_code: str | None,
) -> None:
    """Give the hold back, on a session of its own. Never raises.

    Documented not to raise for the reason `tts_service._finalise` is: it runs
    while another exception is already on its way out, and a failure here would
    replace the mapped upstream error — the one the caller can act on — with a
    database traceback.
    """
    try:
        async with SessionLocal() as own:
            await session_service.abandon_oneshot(
                own,
                ai_session_id=ai_session_id,
                end_reason=end_reason,
                error_code=error_code,
            )
    except Exception:  # noqa: BLE001 - the hold is the reaper's problem now
        logger.exception("stt_release_failed session=%s", ai_session_id)


def _audio_ms_of(payload: dict, *, fallback: int) -> int:
    """Upstream's `audio_seconds`, in milliseconds.

    Falls back to our own estimate rather than to zero: a response with no
    duration in it is upstream's bug, and billing zero for work it did would
    make that bug free to have.
    """
    seconds = _float_or_none(payload.get("audio_seconds"))
    if seconds is None or seconds < 0:
        logger.warning("stt_missing_duration payload_keys=%s", sorted(payload))
        return fallback
    return int(seconds * 1000)


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
