"""tts_batch_jobs: the durable record of an asynchronous synthesis

Purely additive — one new table, no column touched on an existing one — so the
deploy can migrate first and restart afterwards, and a rollback to the previous
release keeps working against the migrated database.

`server_default` uses `sa.func.now()` rather than a literal for the reason 0002
gives: it renders `now()` on Postgres and `CURRENT_TIMESTAMP` on SQLite instead
of freezing one dialect's spelling into the history.

Every constraint is named here rather than left to the database, matching the
template in `app/db/base.py`. Autogenerate can only recognise two constraints as
the same one when both ends agree on the name, and a nameless check constraint
is one autogenerate proposes dropping and recreating on every run.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()

BATCH_STATE = sa.Enum(
    "QUEUED",
    "SUBMITTED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    name="ttsbatchjobstate",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    op.create_table(
        "tts_batch_jobs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("ai_session_id", sa.Uuid(), nullable=False),
        sa.Column("state", BATCH_STATE, server_default="queued", nullable=False),
        sa.Column("upstream_job_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("voice_id", sa.String(length=64), nullable=True),
        sa.Column("audio_format", sa.String(length=16), nullable=False),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("style", sa.String(length=255), nullable=True),
        sa.Column("total_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "submitted_characters", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "billed_characters", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("audio_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_json", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("poll_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("audio_ms >= 0", name=op.f("ck_tts_batch_jobs_audio_ms_nonneg")),
        sa.CheckConstraint(
            "billed_characters >= 0", name=op.f("ck_tts_batch_jobs_billed_characters_nonneg")
        ),
        sa.CheckConstraint(
            "completed_items >= 0", name=op.f("ck_tts_batch_jobs_completed_items_nonneg")
        ),
        sa.CheckConstraint(
            "failed_items >= 0", name=op.f("ck_tts_batch_jobs_failed_items_nonneg")
        ),
        sa.CheckConstraint("poll_count >= 0", name=op.f("ck_tts_batch_jobs_poll_count_nonneg")),
        sa.CheckConstraint(
            "submitted_characters >= 0", name=op.f("ck_tts_batch_jobs_submitted_characters_nonneg")
        ),
        sa.CheckConstraint("total_items >= 0", name=op.f("ck_tts_batch_jobs_total_items_nonneg")),
        sa.ForeignKeyConstraint(
            ["ai_session_id"],
            ["ai_sessions.id"],
            name=op.f("fk_tts_batch_jobs_ai_session_id_ai_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tts_batch_jobs_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_tts_batch_jobs_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tts_batch_jobs")),
        sa.UniqueConstraint("ai_session_id", name="uq_tts_batch_jobs_ai_session_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_tts_batch_jobs_idempotency_key"),
        sa.UniqueConstraint("upstream_job_id", name="uq_tts_batch_jobs_upstream_job_id"),
    )
    op.create_index(
        "ix_tts_batch_jobs_state_next_poll",
        "tts_batch_jobs",
        ["state", "next_poll_at"],
        unique=False,
    )
    op.create_index(
        "ix_tts_batch_jobs_user_created", "tts_batch_jobs", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        op.f("ix_tts_batch_jobs_wallet_id"), "tts_batch_jobs", ["wallet_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tts_batch_jobs_wallet_id"), table_name="tts_batch_jobs")
    op.drop_index("ix_tts_batch_jobs_user_created", table_name="tts_batch_jobs")
    op.drop_index("ix_tts_batch_jobs_state_next_poll", table_name="tts_batch_jobs")
    op.drop_table("tts_batch_jobs")
