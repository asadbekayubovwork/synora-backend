"""What one credit costs in som.

Kept in its own versioned table rather than on the price book, and rather than
in `Settings`. Two reasons, in order:

- **It must be settable without a deploy.** Whoever owns pricing changes this;
  an env var means an ssh session and a restart.
- **It must not be coupled to consumption prices.** If the rate lived on the
  price book, publishing a new TTS price would also republish the credit rate,
  so every change to what a character costs would silently reprice every
  top-up in flight.

Every `topups` row records the rate it used, so an old receipt stays
explicable after the rate has changed three times.
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import CreditRateStatus


class CreditRate(Base):
    """One published UZS-per-credit rate."""

    __tablename__ = "credit_rates"
    __table_args__ = (
        UniqueConstraint("version", name="uq_credit_rates_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("uzs_per_credit_tiyin > 0", name="rate_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="window_ordered",
        ),
        Index("ix_credit_rates_status_effective", "status", "effective_from"),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The price of ONE credit, in tiyin. 150 UZS per credit is 15_000.
    uzs_per_credit_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[CreditRateStatus] = mapped_column(
        Enum(CreditRateStatus, native_enum=False, length=32),
        default=CreditRateStatus.DRAFT,
        server_default=CreditRateStatus.DRAFT.value,
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CreditRate v{self.version} {self.uzs_per_credit_tiyin} tiyin/credit>"
