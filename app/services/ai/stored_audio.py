"""One question, asked by both gateways: is anything still using this file?

`recording_store` owns bytes and paths and knows nothing about rows.
`tts_recording_service` and `stt_transcription_service` own rows and knowing
whose they are. This module is the seam between them, and it exists because the
answer to "may I delete this file" is not a question either table can answer
alone.

## Why one file can have two owners in two tables

Audio is stored under the sha256 of its own contents, so identical bytes are
one file. Synthesise a sentence and then transcribe the result — which is the
obvious way to exercise both gateways, and what the dev UI's "read the last
synthesis" button does — and `tts_recordings` and `stt_transcriptions` end up
naming the same key. Neither row is wrong and neither is a duplicate: one is
what we produced, the other is what a user uploaded, and they happen to be the
same bytes.

Counting only one table would then unlink a file the other still serves. The
symptom arrives weeks later as a `200` with an empty body, from a row that
looks perfectly healthy, and nothing in either table records why. So the count
is over both tables, in one transaction, and the unlink happens only at zero.

Adding a third table that stores audio means adding it to `_TABLES` here. That
is the whole maintenance burden, and it is a deliberate one: the alternative —
each service unlinking its own files — is correct exactly until two of them
store the same bytes.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stt_transcription import SttTranscription
from app.models.tts_recording import TtsRecording
from app.services.ai import recording_store

logger = logging.getLogger("synora.recordings")

# Every table that names a file in the store. See the module docstring.
_TABLES = (TtsRecording, SttTranscription)


async def reference_count(session: AsyncSession, storage_key: str) -> int:
    """How many rows, across every owning table, still name this file."""
    total = 0
    for model in _TABLES:
        total += (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.storage_key == storage_key)
            )
        ).scalar_one()
    return total


async def release_key(session: AsyncSession, storage_key: str, *, owner: str) -> bool:
    """Unlink the file if nothing references it any more. Returns whether it went.

    Called *after* the deleting transaction has committed. Inside it, a
    rollback would leave a row pointing at a file that no longer exists; after
    it, the worst case is a file with no rows, which is disk rather than a lie.
    """
    remaining = await reference_count(session, storage_key)
    if remaining:
        logger.info(
            "audio_kept key=%s after %s delete (%d row(s) remain)",
            storage_key,
            owner,
            remaining,
        )
        return False

    unlinked = await recording_store.delete(storage_key)
    logger.info(
        "audio_released key=%s after %s delete (%s)",
        storage_key,
        owner,
        "unlinked" if unlinked else "already gone",
    )
    return unlinked
