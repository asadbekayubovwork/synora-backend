"""voice_calls: the signalling handle and liveness behind a voice-agent call

Additive, like 0006 and 0007 — one new table, nothing touched on an existing
one — so the release migrates before it restarts and a rollback keeps serving
against the migrated database.

The primary key is also a foreign key to `ai_sessions.id`: a call is an
extension of its metered session rather than a second session beside it, so the
money stays on `ai_sessions` exactly where every other service keeps it. See
`app/models/voice_call.py`.

The two partial indexes carry the same `ended_at IS NULL` predicate the model
declares, on both dialects: a live call is a handful of rows among every call
ever made, and both queries that read them — the per-user concurrency count
and the sweeper — only ever ask about live ones.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()
LIVE = sa.text("ended_at IS NULL")


def upgrade() -> None:
    op.create_table(
        "voice_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_pc_id", sa.String(length=128), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("nudged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeats", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("candidates", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("candidates >= 0", name=op.f("ck_voice_calls_candidates_nonneg")),
        sa.CheckConstraint("heartbeats >= 0", name=op.f("ck_voice_calls_heartbeats_nonneg")),
        sa.ForeignKeyConstraint(
            ["id"],
            ["ai_sessions.id"],
            name=op.f("fk_voice_calls_id_ai_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_voice_calls_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_calls")),
    )
    op.create_index(
        "ix_voice_calls_user_created",
        "voice_calls",
        ["user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_voice_calls_live_user",
        "voice_calls",
        ["user_id"],
        unique=False,
        postgresql_where=LIVE,
        sqlite_where=LIVE,
    )
    op.create_index(
        "ix_voice_calls_live_seen",
        "voice_calls",
        ["last_seen_at"],
        unique=False,
        postgresql_where=LIVE,
        sqlite_where=LIVE,
    )


def downgrade() -> None:
    # The sessions stay. Every call's money is on `ai_sessions` and the ledger,
    # and dropping this table loses only which agent handle each call had and
    # when its heartbeats arrived — not what anybody paid.
    op.drop_index("ix_voice_calls_live_seen", table_name="voice_calls")
    op.drop_index("ix_voice_calls_live_user", table_name="voice_calls")
    op.drop_index("ix_voice_calls_user_created", table_name="voice_calls")
    op.drop_table("voice_calls")
