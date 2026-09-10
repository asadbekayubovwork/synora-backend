from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Alembic compares the database against this metadata, and it can only tell
# that two constraints are the same one if both ends agree on the name. Left to
# SQLAlchemy's defaults, indexes and constraints get names the database picked,
# which autogenerate then proposes dropping and recreating on every run. Naming
# them from a template also means a migration can say `op.drop_constraint(
# "ck_wallets_paid_micros_nonneg", ...)` and be sure what it is dropping.
#
# This is effectively a one-way door: changing the template later renames every
# constraint in the schema.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite drops the offset on the way back out — reattach it before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Declarative base with the columns every table carries."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        nullable=False,
    )
    # `onupdate` is applied by SQLAlchemy in Python, so it does NOT fire on a
    # bulk `update(...).values(...)`. Anything that writes a row that way — the
    # whole billing path does — has to set `updated_at` itself.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
