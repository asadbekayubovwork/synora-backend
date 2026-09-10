"""Versioned, immutable prices.

Publishing is the only way to change a price: an `active` book is frozen, and a
change means a new version. That is what makes an invoice from March
reproducible in December — the rows it points at cannot have moved.

The effective window lives on the version, not on the individual price. Giving
each price its own window would recreate, inside a version, exactly the problem
versioning solves: you could no longer name "the prices in force at time T" as
a single object that a session is able to pin.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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
from app.models.billing_enums import BillingService, PriceBookStatus, RoundingMode, UsageMetric

# The price row that applies when no exact `model_key` match exists.
MODEL_KEY_ANY = "*"


class PriceBookVersion(Base):
    """One set of prices, with one effective window."""

    __tablename__ = "price_book_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_price_book_versions_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="window_ordered",
        ),
        Index("ix_price_book_versions_status_effective", "status", "effective_from"),
    )

    # Human-facing 1, 2, 3 — quoted in API responses and on receipts, so it
    # needs to be something a support conversation can say out loud.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[PriceBookStatus] = mapped_column(
        Enum(PriceBookStatus, native_enum=False, length=32),
        default=PriceBookStatus.DRAFT,
        server_default=PriceBookStatus.DRAFT.value,
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PriceBookVersion v{self.version} {self.status.value}>"


class Price(Base):
    """One priced dimension: (service, model_key, metric).

    `unit_size` is how many base units make one *priced* unit — 1000 for
    per-1k-tokens, 60000 for per-minute when the metric is milliseconds, 1 for
    per-character. `price_micros_per_unit` is the charge for one priced unit.
    Keeping the two apart makes rounding granularity data rather than code.

    `cost_micros_per_unit` is what the unit costs *us* upstream, in the same
    micro-credit unit as the price — so margin is a subtraction with no second
    currency and no rate to join against. The backend fills it in from this
    column; microservices never report money.
    """

    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint(
            "price_book_version_id", "service", "model_key", "metric",
            name="uq_prices_dimension",
        ),
        CheckConstraint("unit_size > 0", name="unit_size_positive"),
        CheckConstraint("price_micros_per_unit >= 0", name="rate_nonneg"),
        CheckConstraint("cost_micros_per_unit >= 0", name="cost_nonneg"),
        CheckConstraint("min_charge_micros >= 0", name="min_charge_nonneg"),
        CheckConstraint("included_quantity >= 0", name="included_nonneg"),
        Index("ix_prices_lookup", "price_book_version_id", "service", "metric"),
    )

    price_book_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_book_versions.id", ondelete="RESTRICT"), nullable=False
    )  # covered by uq_prices_dimension / ix_prices_lookup
    service: Mapped[BillingService] = mapped_column(
        Enum(BillingService, native_enum=False, length=32), nullable=False
    )
    model_key: Mapped[str] = mapped_column(
        String(128), default=MODEL_KEY_ANY, server_default=MODEL_KEY_ANY, nullable=False
    )
    metric: Mapped[UsageMetric] = mapped_column(
        Enum(UsageMetric, native_enum=False, length=32), nullable=False
    )

    unit_size: Mapped[int] = mapped_column(
        BigInteger, default=1, server_default=text("1"), nullable=False
    )
    price_micros_per_unit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rounding: Mapped[RoundingMode] = mapped_column(
        Enum(RoundingMode, native_enum=False, length=32),
        default=RoundingMode.CEIL,
        server_default=RoundingMode.CEIL.value,
        nullable=False,
    )
    # Applied to the *cumulative* line total, once per session — never once per
    # heartbeat. This is the per-call minimum and the connection fee's floor.
    min_charge_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Free allowance, subtracted from the cumulative quantity before pricing.
    included_quantity: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    cost_micros_per_unit: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    # What `GET /v1/pricing` shows a human: "per 1 000 characters".
    display_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Price {self.service.value}/{self.model_key}/{self.metric.value}>"
