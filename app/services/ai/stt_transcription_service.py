"""Rows for transcribed audio: writing one, listing them, deleting one.

The mirror of `tts_recording_service`, and the same three rules.

`save` cannot fail loudly, because it runs after the charge has committed: the
worst it may produce is a log line and a file nobody has a row for. Both are
recoverable by hand; a traceback there would replace a transcript the caller
has already paid for with a 500.

Somebody else's id is a `404` rather than a `403`, because a 403 confirms the
id exists.

And `delete` never unlinks on sight. The file may be named by a `tts_recordings`
row as well — audio we synthesised and the user then uploaded here is the same
bytes and therefore the same key — so the counting is
`stored_audio.release_key`'s, across both tables, after the commit.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.stt_transcription import SttTranscription
from app.schemas.common import Cursor
from app.services.ai import stored_audio

logger = logging.getLogger("synora.stt")


async def save(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    ai_session_id: uuid.UUID,
    body: str,
    language: str,
    audio_ms: int,
    infer_ms: int,
    filename: str | None,
    content_type: str | None,
    audio_bytes: int,
    storage_key: str,
    sha256: str,
) -> SttTranscription | None:
    """Write the row for a file the store has already committed. Never raises."""
    transcription = SttTranscription(
        user_id=user_id,
        ai_session_id=ai_session_id,
        body=body,
        language=language,
        audio_ms=audio_ms,
        infer_ms=infer_ms,
        filename=filename,
        content_type=content_type,
        audio_bytes=audio_bytes,
        storage_key=storage_key,
        sha256=sha256,
    )
    session.add(transcription)
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 - a keepsake is not worth the transcript
        logger.exception(
            "transcription_save_failed session=%s key=%s", ai_session_id, storage_key
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - the database is what failed
            logger.warning("transcription_rollback_failed session=%s", ai_session_id)
        return None
    logger.info(
        "transcription_saved id=%s session=%s bytes=%d key=%s",
        transcription.id,
        ai_session_id,
        audio_bytes,
        storage_key,
    )
    return transcription


async def page(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int,
    position: Cursor | None,
) -> tuple[list[SttTranscription], bool]:
    """One keyset page, newest first, tied on `(created_at, id)`."""
    query = select(SttTranscription).where(SttTranscription.user_id == user_id)
    if position is not None:
        query = query.where(
            tuple_(SttTranscription.created_at, SttTranscription.id)
            < tuple_(position.created_at, position.row_id)
        )
    query = query.order_by(
        SttTranscription.created_at.desc(), SttTranscription.id.desc()
    ).limit(limit + 1)

    rows = list((await session.execute(query)).scalars().all())
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def require_own(
    session: AsyncSession, *, user_id: uuid.UUID, transcription_id: uuid.UUID
) -> SttTranscription:
    transcription = (
        await session.execute(
            select(SttTranscription).where(
                SttTranscription.id == transcription_id,
                SttTranscription.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if transcription is None:
        raise NotFoundError("No such transcription.", code="transcription_not_found")
    return transcription


async def delete(
    session: AsyncSession, *, user_id: uuid.UUID, transcription_id: uuid.UUID
) -> None:
    """Erase one transcription, and its audio once nothing else names it."""
    transcription = await require_own(
        session, user_id=user_id, transcription_id=transcription_id
    )
    storage_key = transcription.storage_key

    await session.delete(transcription)
    await session.commit()

    await stored_audio.release_key(session, storage_key, owner="transcription")
    logger.info(
        "transcription_deleted id=%s key=%s", transcription_id, storage_key
    )
