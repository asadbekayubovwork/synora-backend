"""The append-only record of every micro-credit that moved.

The sign rule, stated once here and never varied anywhere else:
**`amount_micros` is the delta applied to `bucket`.** Nothing more. So a debit
is negative on `paid`/`bonus`, a hold is positive on `reserved`, a release is
negative on `reserved`, and a bonus expiry is negative on `bonus`.

The invariant this table exists to support is

    wallets.<bucket>_micros == SUM(ledger_entries.amount_micros for that bucket)

and it holds because `app/services/billing/wallet_repo.py` writes both sides in
one transaction and is the only code permitted to write either. A test walks
the source tree to keep that second half true.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    DDL,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType


class LedgerEntry(Base):
    """One bucket movement.

    An operation that spans both buckets — a debit taking the last of a bonus
    and the rest from paid credit — is two rows sharing one `group_id` and one
    `wallet_version`. They were written by a single wallet UPDATE, so they
    record the *same* `balance_after_*`; a statement replay therefore has to
    group by `group_id` and apply the group total, not walk row by row.

    Never UPDATEd, never DELETEd — revision 0003 installs a trigger that
    raises on both, so this is enforced by the database and not only by
    review. A correction is a new `adjustment`, `refund` or `reversal` entry.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        # Key *and* bucket, not key alone: the two halves of a split debit
        # share one idempotency key and both must insert. A replay of either
        # half still collides, which is the point.
        UniqueConstraint("idempotency_key", "bucket", name="uq_ledger_entries_idempotency_bucket"),
        CheckConstraint("amount_micros <> 0", name="amount_nonzero"),
        CheckConstraint("wallet_version > 0", name="wallet_version_positive"),
        # At most one typed reference, so "what was this?" has one answer.
        CheckConstraint(
            "(CASE WHEN topup_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN payment_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN ai_session_id IS NULL THEN 0 ELSE 1 END"
            " + CASE WHEN usage_event_id IS NULL THEN 0 ELSE 1 END) <= 1",
            name="single_reference",
        ),
        # The replay order for reconciliation, and the statement query.
        Index("ix_ledger_entries_wallet_version", "wallet_id", "wallet_version"),
        Index("ix_ledger_entries_wallet_created", "wallet_id", "created_at"),
        Index("ix_ledger_entries_group", "group_id"),
    )

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )  # covered by the two composite indexes above
    # Denormalised so a user-scoped statement needs no join. `wallet_repo` is
    # the only writer, so it cannot drift.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    kind: Mapped[LedgerEntryKind] = mapped_column(
        Enum(LedgerEntryKind, native_enum=False, length=32), nullable=False
    )
    bucket: Mapped[LedgerBucket] = mapped_column(
        Enum(LedgerBucket, native_enum=False, length=32), nullable=False
    )
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The wallet as it stood immediately after the UPDATE that wrote this row,
    # taken from that statement's RETURNING. Turns reconciliation from "the
    # totals disagree" into "this operation is where they diverged".
    balance_after_paid_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_bonus_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_reserved_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # `wallets.version` after the UPDATE. Per-wallet total order is
    # ORDER BY wallet_version, bucket.
    wallet_version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Ties the rows written by one logical operation. Always set, even for a
    # single-row operation, so `GROUP BY group_id` never needs a special case.
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)

    ref_type: Mapped[LedgerRefType] = mapped_column(
        Enum(LedgerRefType, native_enum=False, length=32), nullable=False
    )
    topup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topups.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    ai_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    usage_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("usage_events.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    # For an admin action: who authorised it. Attribution lives on the entry
    # rather than in a separate audit table, because the audit belongs in the
    # append-only place.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    # Nullable: a bonus expiry and a reconciliation correction have no
    # caller-supplied key. NULLs do not collide in a UNIQUE index, which is
    # exactly the behaviour wanted here.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LedgerEntry {self.kind.value} {self.bucket.value} {self.amount_micros:+d}>"


# --- append-only, enforced by the database ---------------------------------
#
# Review catches a stray `update(LedgerEntry)` most of the time. A trigger
# catches it every time, including from a psql session at 2am, which is
# exactly the situation where someone is tempted to "just fix the row".
#
# Attached to table creation rather than living only in the migration, so the
# SQLite test suite — which builds its schema with `create_all` — exercises the
# same guard production has. Revision 0003 carries the identical statements for
# databases that were created before this existed.

# Note the doubled percent in the RAISE below. SQLAlchemy's DDL construct runs
# the statement through `%`-interpolation before sending it, so a literal
# percent has to be escaped; plpgsql then sees a single one and substitutes
# TG_OP. Keep every other percent out of this string, including out of SQL
# comments inside it.
#
# Function and trigger are separate statements because asyncpg refuses to
# "insert multiple commands into a prepared statement" — one DDL object per
# statement, always.
_PG_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION
            'ledger_entries is append-only; %% is not permitted. Post a correcting entry instead.',
            TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """
)

_PG_TRIGGER = DDL(
    """
    CREATE TRIGGER trg_ledger_entries_append_only
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only()
    """
)

# SQLite needs one trigger per operation and has no parameterised message.
_SQLITE_NO_UPDATE = DDL(
    """
    CREATE TRIGGER trg_ledger_entries_no_update BEFORE UPDATE ON ledger_entries
    BEGIN
        SELECT RAISE(ABORT, 'ledger_entries is append-only; UPDATE is not permitted');
    END
    """
)
_SQLITE_NO_DELETE = DDL(
    """
    CREATE TRIGGER trg_ledger_entries_no_delete BEFORE DELETE ON ledger_entries
    BEGIN
        SELECT RAISE(ABORT, 'ledger_entries is append-only; DELETE is not permitted');
    END
    """
)

for _statement, _dialect in (
    (_PG_FUNCTION, "postgresql"),
    (_PG_TRIGGER, "postgresql"),
    (_SQLITE_NO_UPDATE, "sqlite"),
    (_SQLITE_NO_DELETE, "sqlite"),
):
    event.listen(
        LedgerEntry.__table__, "after_create", _statement.execute_if(dialect=_dialect)
    )
