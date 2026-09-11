"""stt_transcriptions: what was transcribed, kept after the text was returned

Additive, like 0006 — one new table, nothing touched on an existing one — so
the release migrates before it restarts and a rollback keeps serving against
the migrated database.

No backfill and none possible: until this revision the upload was relayed and
discarded and the transcript was never stored, so every transcription before
now is unrecoverable by construction.

The table deliberately shares `RECORDINGS_DIR` with `tts_recordings` rather
than getting a directory of its own. Audio is addressed by the sha256 of its
own contents, so a synthesis that is then uploaded to `/stt/transcribe` is one
file with a row in each table — and giving each table its own tree would store
those identical bytes twice. What it costs is that neither table may unlink a
file alone; see `app/services/ai/stored_audio.py`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "stt_transcriptions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("ai_session_id", sa.Uuid(), nullable=False),
        # `body`, not `text`: `sa.text` is imported in every module that
        # touches this table. Empty is legal — audio with no speech in it
        # transcribes to nothing and is still charged for, because the model
        # still ran.
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("audio_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("infer_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # The client's own filename, stored so a user recognises their upload
        # in a list. Never used to build a path — the path is the digest.
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("audio_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "audio_bytes >= 0", name=op.f("ck_stt_transcriptions_transcription_bytes_nonneg")
        ),
        sa.CheckConstraint(
            "audio_ms >= 0", name=op.f("ck_stt_transcriptions_transcription_audio_ms_nonneg")
        ),
        sa.ForeignKeyConstraint(
            ["ai_session_id"],
            ["ai_sessions.id"],
            name=op.f("fk_stt_transcriptions_ai_session_id_ai_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_stt_transcriptions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stt_transcriptions")),
    )
    op.create_index(
        "ix_stt_transcriptions_user_created",
        "stt_transcriptions",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    # Read on every delete, here and on `tts_recordings`: the two tables share
    # a file store, so neither can answer "is anything still using this?" alone.
    op.create_index(
        "ix_stt_transcriptions_storage_key", "stt_transcriptions", ["storage_key"], unique=False
    )
    op.create_index(
        op.f("ix_stt_transcriptions_ai_session_id"),
        "stt_transcriptions",
        ["ai_session_id"],
        unique=False,
    )


def downgrade() -> None:
    # The files stay. Dropping a table is a schema decision, and unlinking
    # customer audio because of one would be an irreversible answer to a
    # reversible question — and here it would take `tts_recordings`' own audio
    # with it wherever the two share a file.
    op.drop_index(op.f("ix_stt_transcriptions_ai_session_id"), table_name="stt_transcriptions")
    op.drop_index("ix_stt_transcriptions_storage_key", table_name="stt_transcriptions")
    op.drop_index("ix_stt_transcriptions_user_created", table_name="stt_transcriptions")
    op.drop_table("stt_transcriptions")
