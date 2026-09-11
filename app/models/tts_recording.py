"""What was synthesised, kept after the audio has been delivered.

Until this table existed a synthesis left counters and nothing else: the money
was auditable and the work was not. `usage_events` can say a wallet paid a
quarter of a credit for a thousand characters; it cannot say which thousand,
in whose voice, or hand the audio back when a customer says the pronunciation
was wrong. This row can.

## The bytes are not in here

`storage_key` names a file under `RECORDINGS_DIR`, content-addressed by its own
sha256 — see `app/services/ai/recording_store.py`. A `BYTEA` column would have
been one fewer moving part and was the first design; at `TTS_MAX_CHARACTERS` a
wav is tens of megabytes, so it would also have put a gigabyte in the database
for every forty syntheses, on a deployment whose database is a single SQLite
file that is copied by the backup script. Rows are cheap and stay queryable;
blobs are neither.

The same address is why two rows may name one file. A retry, a page that
re-renders, two customers asking for the same sentence in the same voice — the
bytes are identical, so the key is identical, and the file is stored once.
Deleting a recording therefore deletes the *row* and only unlinks the file once
no row references it any more (`tts_recording_service.delete`). Unlinking on
sight would be one user erasing another user's audio.

## Nothing here is on the billing path

No column in this table is read by pricing, settlement or reconciliation, and
nothing in the money path waits on a write to it. That is deliberate and it is
the reason `recording_store` swallows its own failures: a full disk must cost a
recording, never a synthesis and never a charge. `characters` and `audio_ms`
are duplicated from the session for querying convenience and are not the
authority for either — `usage_event_items` is.

## Keeping this means keeping text

Every row holds the exact text a user submitted, indefinitely, because that is
what was asked for. `RECORDINGS_ENABLED=false` turns the whole thing off for a
deployment that would rather not, and `DELETE /tts/recordings/{id}` is how one
user erases one recording. There is no automatic expiry; if one is ever wanted,
it belongs next to `reconcile_all` as a sweep, not as a cascade on this table.
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


class TtsRecording(Base):
    """One delivered synthesis: its text, its parameters and its audio file.

    `id`, `created_at` and `updated_at` come from `Base`.
    """

    __tablename__ = "tts_recordings"
    __table_args__ = (
        CheckConstraint("characters >= 0", name="recording_characters_nonneg"),
        CheckConstraint("audio_bytes >= 0", name="recording_bytes_nonneg"),
        CheckConstraint("audio_ms >= 0", name="recording_audio_ms_nonneg"),
        # The list route is keyset-paginated newest-first per user, exactly as
        # the batch list and the statement are, so the index carries the tie
        # breaker the cursor sorts on.
        Index("ix_tts_recordings_user_created", "user_id", "created_at", "id"),
        # Read on every delete, to answer "is anyone else pointing at this
        # file?" before unlinking it.
        Index("ix_tts_recordings_storage_key", "storage_key"),
    )

    # RESTRICT rather than CASCADE, matching `ai_sessions`: deleting a user out
    # from under their recordings would orphan files that nothing then unlinks,
    # because the unlink is decided by counting rows.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # The metered session this audio was delivered under. Not unique: a session
    # that is settled once can still be the session a `409`-free replay was
    # served from, and a null would lose the only link back to the charge.
    ai_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    # --- what was asked for ------------------------------------------------
    # `Text`, because `TTS_MAX_CHARACTERS` is 5 000 today and is a setting.
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    audio_format: Mapped[str] = mapped_column(String(16), nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    style: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- what came back ----------------------------------------------------
    characters: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    audio_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Derived from the byte count for linear formats and zero for the
    # container ones, exactly as the response header is. Never priced — see the
    # module docstring of `app/models/tts_job.py` for why that matters.
    audio_ms: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    # --- where the bytes are ------------------------------------------------
    # `ab/cd/<sha256>.<ext>`, relative to `RECORDINGS_DIR`. Relative on
    # purpose: the directory moves between a laptop, a container and
    # `/opt/synora-backend/data`, and an absolute path in a row is a row that
    # stops resolving the first time it does.
    storage_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # The digest the key was built from, stored again so a file can be verified
    # against the row without parsing its own name.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

