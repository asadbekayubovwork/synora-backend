"""baseline: the auth schema as `init_db()` had been creating it

This revision exists so a database that predates Alembic and one created from
scratch end up identical. It creates nothing new — `users`, `otp_codes` and
`oauth_accounts` are exactly what `Base.metadata.create_all` produced before
the billing work started.

An already-deployed database is brought under Alembic without re-running this:

    alembic stamp 0001

Note that a database created by `create_all` *before* the naming convention
landed carries database-picked constraint names, so its primary keys are not
called `pk_users`. That only matters if a later migration needs to drop one by
name; the README explains the recreate-or-hand-ALTER choice.

Revision ID: 0001
Revises:
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `func.now()` rather than a literal, so this renders `now()` on Postgres and
# `CURRENT_TIMESTAMP` on SQLite instead of baking one dialect's spelling in.
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=512), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        # `native_enum=False` keeps this a VARCHAR, so adding a purpose later
        # needs no DDL at all.
        sa.Column(
            "purpose",
            sa.Enum("REGISTER", "RESET_PASSWORD", name="otppurpose", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_codes")),
    )
    op.create_index(op.f("ix_otp_codes_email"), "otp_codes", ["email"], unique=False)

    op.create_table(
        "oauth_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "GOOGLE", "GITHUB", "TELEGRAM",
                name="oauthprovidername", native_enum=False, length=32,
            ),
            nullable=False,
        ),
        sa.Column("provider_account_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_accounts")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_oauth_accounts_user_id_users"), ondelete="CASCADE",
        ),
        # Named in the model itself, so it keeps that name rather than the one
        # the convention would generate.
        sa.UniqueConstraint("provider", "provider_account_id", name="uq_oauth_provider_account"),
    )
    op.create_index(op.f("ix_oauth_accounts_user_id"), "oauth_accounts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_accounts_user_id"), table_name="oauth_accounts")
    op.drop_table("oauth_accounts")
    op.drop_index(op.f("ix_otp_codes_email"), table_name="otp_codes")
    op.drop_table("otp_codes")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
