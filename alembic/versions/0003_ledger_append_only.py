"""ledger_entries is append-only, enforced by the database

Review catches a stray `update(LedgerEntry)` most of the time. A trigger
catches it every time, including from a psql session at 2am — which is exactly
the situation where someone is tempted to "just fix the row". A correction is a
new `adjustment`, `refund` or `reversal` entry; the history never moves.

The same statements are attached to table creation in `app/models/ledger.py`,
so a database built by `create_all` (the SQLite test suite) has the guard too.
This revision is what installs it on a database that was migrated rather than
created, and it is idempotent enough to run either way.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One statement per `op.execute`: asyncpg refuses to "insert multiple commands
# into a prepared statement". Unlike the DDL construct in the model, `%` here
# needs no escaping — `op.execute` does not interpolate.
PG_FUNCTION = """
CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'ledger_entries is append-only; % is not permitted. Post a correcting entry instead.',
        TG_OP;
END;
$$ LANGUAGE plpgsql
"""

PG_TRIGGER = """
CREATE TRIGGER trg_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only()
"""

# SQLite wants one trigger per operation and has no parameterised message.
SQLITE_TRIGGERS = (
    """
    CREATE TRIGGER trg_ledger_entries_no_update BEFORE UPDATE ON ledger_entries
    BEGIN
        SELECT RAISE(ABORT, 'ledger_entries is append-only; UPDATE is not permitted');
    END
    """,
    """
    CREATE TRIGGER trg_ledger_entries_no_delete BEFORE DELETE ON ledger_entries
    BEGIN
        SELECT RAISE(ABORT, 'ledger_entries is append-only; DELETE is not permitted');
    END
    """,
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(PG_FUNCTION)
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_append_only ON ledger_entries")
        op.execute(PG_TRIGGER)
    elif dialect == "sqlite":
        # `create_all` may already have installed these.
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_no_delete")
        for statement in SQLITE_TRIGGERS:
            op.execute(statement)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_append_only ON ledger_entries")
        op.execute("DROP FUNCTION IF EXISTS ledger_entries_append_only()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_no_delete")
