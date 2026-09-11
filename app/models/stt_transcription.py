"""What was transcribed, kept after the text has been returned.

The mirror of `tts_recordings`, on the other gateway, and the same argument: a
transcription used to leave counters and nothing else, so the money was
auditable and the work was not. `usage_events` can say a wallet paid 1.2
credits for 920 milliseconds of audio; it cannot say what was said, in which
language, or hand the recording back when somebody disputes the transcript.

The columns are the mirror image of the speech side's. There the text is the
input and the audio is what we produced; here the audio is the input and the
text is what came back. Both rows carry both, because a transcript without the
audio it came from is unverifiable, and audio without its transcript is a file
nobody can search.

## The file is shared with `tts_recordings`, on purpose

Both tables name a file under `RECORDINGS_DIR`, content-addressed by sha256,
and they can name the *same* one. That is not a corner case: synthesising a
sentence and then transcribing the result is the obvious way to exercise both
gateways, and the bytes uploaded are byte-for-byte the bytes produced.

So deleting a row never unlinks a file on sight. `stored_audio.release_key`
counts the references across **both** tables and unlinks only at zero — see the
module docstring there, which is where the whole argument lives.

## Nothing here is on the billing path

No column is read by pricing, settlement or reconciliation, and the row is
written after the charge has committed on a session of its own. `audio_ms` is
duplicated from the settlement for querying and is not the authority for it —
`usage_event_items` is.

## Keeping this means keeping customer recordings

Heavier than the speech side, and worth saying once in the place that stores
it: what is kept here is audio the *user* supplied — a meeting, a call, a voice
note — alongside its transcript, indefinitely.
`RECORDINGS_ENABLED=false` turns off both gateways' keeping, and
`DELETE /stt/transcriptions/{id}` is how one user erases one of these.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SttTranscription(Base):
    """One charged transcription: the audio that went up and the text that came back.

    `id`, `created_at` and `updated_at` come from `Base`.
    """

    __tablename__ = "stt_transcriptions"
    __table_args__ = (
        CheckConstraint("audio_ms >= 0", name="transcription_audio_ms_nonneg"),
        CheckConstraint("audio_bytes >= 0", name="transcription_bytes_nonneg"),
        Index("ix_stt_transcriptions_user_created", "user_id", "created_at", "id"),
        # Read on every delete, on this table and on `tts_recordings`, to answer
        # "is anything still pointing at this file?" before unlinking it.
        Index("ix_stt_transcriptions_storage_key", "storage_key"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ai_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    # --- what came back ----------------------------------------------------
    # `body` for the reason `tts_recordings.body` is: `text` is `sqlalchemy.text`
    # in every module that touches this table. Empty is legal and ordinary —
    # audio with no speech in it transcribes to nothing and is still charged
    # for, because the model still ran.
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    # The duration the service measured, which is what the charge was computed
    # from — never our own pre-upload estimate.
    audio_ms: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Reported by the service and never billed: how long the model took. Kept
    # because it is the number that says whether a slow call was the GPU or the
    # tunnel.
    infer_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # --- what went up -------------------------------------------------------
    # The name the client sent, for a list somebody has to recognise their own
    # uploads in. Untrusted input: stored, never used to build a path.
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audio_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # `ab/cd/<sha256>.<ext>` relative to `RECORDINGS_DIR`, and possibly the
    # same key a `tts_recordings` row carries.
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
