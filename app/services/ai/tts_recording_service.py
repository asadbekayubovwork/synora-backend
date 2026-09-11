"""Rows for delivered audio: writing one, listing them, deleting one.

The split against `recording_store` is the same one `tts_service` keeps against
`tts_client`: that module owns bytes and paths, this one owns rows and
ownership. The only place they meet is `delete`, where the decision to unlink a
file is a question about rows.

## `save` cannot fail loudly

It is called from `tts_service._finalise`, which is documented never to raise
and runs in the `finally` of a stream whose settlement has already committed.
An exception escaping here would land where nobody is left to catch it, and it
would do so *after* the customer has been charged and the audio delivered — so
the worst outcome this function is allowed to produce is a log line and a file
nobody has a row for. Both are recoverable by hand; a traceback out of that
`finally` is not.

## `delete` counts before it unlinks, and not only here

Files are content-addressed, so two rows naming the same key is the ordinary
case rather than a corner one — the same text in the same voice produces the
same bytes. Unlinking on sight is how one user's delete silently empties
another user's recording, and the symptom would be a 200 with a zero-byte body
weeks later.

The counting is `stored_audio.release_key`'s, because the rows are not all in
this table: an `/stt/transcribe` upload of audio we synthesised names the same
key from `stt_transcriptions`. It runs after the commit — inside it, a rollback
would leave a row pointing at a file that is gone; after it, the worst case is
a file with no rows, which is disk and not a lie.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.tts_recording import TtsRecording
from app.schemas.common import Cursor
from app.services.ai import stored_audio

logger = logging.getLogger("synora.recordings")


async def save(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    ai_session_id: uuid.UUID,
    body: str,
    voice_id: str | None,
    quality: str,
    audio_format: str,
    sample_rate: int,
    style: str | None,
    characters: int,
    audio_bytes: int,
    audio_ms: int,
    storage_key: str,
    sha256: str,
) -> TtsRecording | None:
    """Write the row for a file the store has already committed. Never raises."""
    recording = TtsRecording(
        user_id=user_id,
        ai_session_id=ai_session_id,
        body=body,
        voice_id=voice_id,
        quality=quality,
        audio_format=audio_format,
        sample_rate=sample_rate,
        style=style,
        characters=characters,
        audio_bytes=audio_bytes,
        audio_ms=audio_ms,
        storage_key=storage_key,
        sha256=sha256,
    )
    session.add(recording)
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 - a recording is not worth a traceback here
        logger.exception(
            "recording_save_failed session=%s key=%s", ai_session_id, storage_key
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - the database is what failed
            logger.warning("recording_rollback_failed session=%s", ai_session_id)
        return None
    logger.info(
        "recording_saved id=%s session=%s bytes=%d key=%s",
        recording.id,
        ai_session_id,
        audio_bytes,
        storage_key,
    )
    return recording


async def page(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int,
    position: Cursor | None,
) -> tuple[list[TtsRecording], bool]:
    """One keyset page, newest first. Returns the rows and whether more follow.

    Keyed on `(created_at, id)` for the reason the batch list is: `created_at`
    resolves to whole seconds on SQLite, so two recordings from one second
    share a sort key and an untied cursor would skip or repeat a row.
    """
    query = select(TtsRecording).where(TtsRecording.user_id == user_id)
    if position is not None:
        query = query.where(
            tuple_(TtsRecording.created_at, TtsRecording.id)
            < tuple_(position.created_at, position.row_id)
        )
    query = query.order_by(
        TtsRecording.created_at.desc(), TtsRecording.id.desc()
    ).limit(limit + 1)

    rows = list((await session.execute(query)).scalars().all())
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def require_own(
    session: AsyncSession, *, user_id: uuid.UUID, recording_id: uuid.UUID
) -> TtsRecording:
    """One recording belonging to this user.

    Somebody else's id is a 404 rather than a 403, exactly as a batch job is: a
    403 confirms the id exists, which is the one thing a stranger guessing ids
    is trying to learn.
    """
    recording = (
        await session.execute(
            select(TtsRecording).where(
                TtsRecording.id == recording_id, TtsRecording.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if recording is None:
        raise NotFoundError("No such recording.", code="recording_not_found")
    return recording


async def delete(
    session: AsyncSession, *, user_id: uuid.UUID, recording_id: uuid.UUID
) -> None:
    """Erase one recording, and its file once nothing else names it."""
    recording = await require_own(
        session, user_id=user_id, recording_id=recording_id
    )
    storage_key = recording.storage_key

    await session.delete(recording)
    await session.commit()

    # Counted across every table that stores audio, not just this one: a
    # synthesis that was then uploaded to `/stt/transcribe` leaves a
    # `stt_transcriptions` row naming these same bytes, and unlinking on the
    # strength of this table alone would empty that transcription's playback.
    # `stored_audio` is where that argument lives.
    await stored_audio.release_key(session, storage_key, owner="recording")
    logger.info("recording_deleted id=%s key=%s", recording_id, storage_key)
