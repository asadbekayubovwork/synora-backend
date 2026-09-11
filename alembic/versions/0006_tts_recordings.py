"""tts_recordings: what was said, kept after the audio was delivered

Purely additive — one new table, nothing touched on an existing one — so the
release can migrate before it restarts and a rollback to the previous commit
keeps serving against the migrated database. The old code simply never writes
here.

No backfill is possible and none is attempted. Before this revision the audio
was relayed and discarded and the text was never stored at all, so every
synthesis that happened until now is unrecoverable by construction; the table
starts empty and fills from the next stream onwards.

`server_default` uses `sa.func.now()` for the reason 0002 gives: it renders
`now()` on Postgres and `CURRENT_TIMESTAMP` on SQLite rather than freezing one
dialect's spelling into the history.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "tts_recordings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("ai_session_id", sa.Uuid(), nullable=False),
        # `body`, not `text`: `sa.text` is imported in every module that
        # touches this table, and a column named after it reads as a call.
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("voice_id", sa.String(length=64), nullable=True),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("audio_format", sa.String(length=16), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("style", sa.String(length=255), nullable=True),
        sa.Column("characters", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        # `ab/cd/<64 hex>.<ext>` relative to RECORDINGS_DIR. 128 is four times
        # what that spelling needs, which is the room a longer digest or a
        # deeper fan-out would want.
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("audio_bytes >= 0", name=op.f("ck_tts_recordings_recording_bytes_nonneg")),
        sa.CheckConstraint("audio_ms >= 0", name=op.f("ck_tts_recordings_recording_audio_ms_nonneg")),
        sa.CheckConstraint(
            "characters >= 0", name=op.f("ck_tts_recordings_recording_characters_nonneg")
        ),
        # RESTRICT on both, matching `ai_sessions` and `tts_batch_jobs`.
        # Cascading a user delete through here would drop rows without ever
        # unlinking their files, and the unlink is decided by counting rows.
        sa.ForeignKeyConstraint(
            ["ai_session_id"],
            ["ai_sessions.id"],
            name=op.f("fk_tts_recordings_ai_session_id_ai_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tts_recordings_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tts_recordings")),
    )
    # The list route's keyset, tie breaker included: `created_at` is whole
    # seconds on SQLite, so the id is what keeps the ordering total.
    op.create_index(
        "ix_tts_recordings_user_created",
        "tts_recordings",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    # Read on every delete, to answer "does another row still name this file?"
    # before unlinking it. Not unique: sharing one file is the ordinary case.
    op.create_index(
        "ix_tts_recordings_storage_key", "tts_recordings", ["storage_key"], unique=False
    )
    op.create_index(
        op.f("ix_tts_recordings_ai_session_id"),
        "tts_recordings",
        ["ai_session_id"],
        unique=False,
    )


def downgrade() -> None:
    # The files under RECORDINGS_DIR are left where they are. This revision
    # created no file and it deletes none: dropping a table is a schema
    # decision, and unlinking customer audio because of one would be an
    # irreversible answer to a reversible question.
    op.drop_index(op.f("ix_tts_recordings_ai_session_id"), table_name="tts_recordings")
    op.drop_index("ix_tts_recordings_storage_key", table_name="tts_recordings")
    op.drop_index("ix_tts_recordings_user_created", table_name="tts_recordings")
    op.drop_table("tts_recordings")
