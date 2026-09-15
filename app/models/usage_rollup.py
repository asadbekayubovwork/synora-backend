"""Pre-aggregated usage, so a dashboard never scans `usage_events`.

**Nothing writes this table and nothing reads it.** The design below is what it
was migrated for; none of it was built. `rollup_service` does not exist,
`GET /v1/usage` scans `usage_events` directly — which is the cost this was
meant to remove — and `billing_rollup_timezone` was deleted along with the
other settings that looked load-bearing and were read by nobody.

The table and this class are kept rather than dropped: the schema is migrated,
the design is sound, and the work to finish it is smaller than the work to
remove and re-add it. Treat the paragraph below as a specification, not a
description of running code.

`day` was to be the local calendar day rather than UTC — the product is
Uzbekistan-facing and a user comparing "today" against their own clock is five
hours out otherwise — maintained by upsert as usage arrives and rebuildable
from `usage_events`, with a test asserting the two agree, because a rollup
nobody can reconstruct is a number nobody can trust.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import BillingService, UsageMetric


class UsageDailyRollup(Base):
    __tablename__ = "usage_daily_rollups"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "day", "service", "model_key", "metric",
            name="uq_usage_daily_rollups_dimension",
        ),
        CheckConstraint("quantity >= 0", name="quantity_nonneg"),
        CheckConstraint("event_count >= 0", name="event_count_nonneg"),
        Index("ix_usage_daily_rollups_day", "day"),
        Index("ix_usage_daily_rollups_user_day", "user_id", "day"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )  # covered by uq_usage_daily_rollups_dimension
    day: Mapped[date] = mapped_column(Date, nullable=False)
    service: Mapped[BillingService] = mapped_column(
        Enum(BillingService, native_enum=False, length=32), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[UsageMetric] = mapped_column(
        Enum(UsageMetric, native_enum=False, length=32), nullable=False
    )

    quantity: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    price_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    cost_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    writeoff_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    event_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UsageDailyRollup {self.day} {self.service.value} {self.metric.value}>"
