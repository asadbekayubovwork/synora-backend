"""What a microservice reported, priced and debited.

Microservices report **cumulative** quantities, never deltas. `stored =
max(stored, incoming)` is idempotent by construction, survives reordering and
duplication, and repairs itself — a report that never arrived is made good by
the next one, because the next one restates the total. It also means a service
that cannot reach us keeps exactly *one* pending report per session, in
constant memory, instead of an unbounded queue it has to persist or lose money.

`UsageEvent.quantity` on the items below is therefore the delta we *computed*
(`incoming - stored`), not the delta anyone sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import (
    BillingService,
    RoundingMode,
    UsageEventKind,
    UsageEventStatus,
    UsageMetric,
)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
        # A structural second guard. A resent report #7 carrying a freshly
        # generated key would slip past the key above; it still collides here.
        # A collision on this one with a *different* key is a conflict, not a
        # replay — the payload changed, and returning the old answer quietly
        # would hide a real bug upstream.
        UniqueConstraint("ai_session_id", "sequence", name="uq_usage_events_session_sequence"),
        CheckConstraint("price_micros >= 0", name="price_nonneg"),
        CheckConstraint("cost_micros >= 0", name="cost_nonneg"),
        CheckConstraint("debited_micros >= 0", name="debited_nonneg"),
        CheckConstraint("writeoff_micros >= 0", name="writeoff_nonneg"),
        CheckConstraint(
            "debited_micros + writeoff_micros <= price_micros",
            name="debit_within_price",
        ),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        Index("ix_usage_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_usage_events_wallet_created", "wallet_id", "created_at"),
        Index("ix_usage_events_status_created", "status", "created_at"),
    )

    ai_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_sessions.id", ondelete="RESTRICT"), nullable=False
    )  # covered by uq_usage_events_session_sequence
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    service: Mapped[BillingService] = mapped_column(
        Enum(BillingService, native_enum=False, length=32), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Denormalised from the session so an event can be re-priced and checked
    # without loading the session it belongs to.
    price_book_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_book_versions.id", ondelete="RESTRICT"), nullable=False
    )

    kind: Mapped[UsageEventKind] = mapped_column(
        Enum(UsageEventKind, native_enum=False, length=32), nullable=False
    )
    status: Mapped[UsageEventStatus] = mapped_column(
        Enum(UsageEventStatus, native_enum=False, length=32),
        default=UsageEventStatus.RECORDED,
        server_default=UsageEventStatus.RECORDED.value,
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the microservice observed it. `created_at` from `Base` is when we
    # stored it, and the gap between the two is what diagnoses a backlog.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- money -------------------------------------------------------------
    # What this event costs: cumulative price now, minus what was settled
    # before it. Never a per-event rounding of a per-event quantity.
    price_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # The session total through this event — the anti-drift anchor.
    cumulative_price_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # What actually reached the wallet. Below `price_micros` only under grace.
    debited_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    writeoff_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # The ledger group this event produced. Null when nothing moved.
    ledger_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    # True when a reported total was above `budget + overdraft` and we clamped
    # it. The excess is not billed; the flag is what makes it reviewable.
    clamped: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UsageEvent {self.service.value} #{self.sequence} {self.price_micros}>"


class UsageEventItem(Base):
    """One metric line of one usage event, with the price it was charged at.

    A child table rather than a JSON column, for reasons that all point the
    same way on a money path: a `BigInteger` with a non-negative check makes a
    malformed quantity a rejected request instead of a silent rounding error;
    `price_id` can be a real foreign key, which is what stops a price row being
    deleted out from under an invoice; `GROUP BY metric` over a btree beats
    unpacking JSON with unindexable expressions; and an unknown metric gets
    *rejected* rather than quietly stored, which is what you want when a
    stored-but-unpriced metric is revenue you never charged.
    """

    __tablename__ = "usage_event_items"
    __table_args__ = (
        UniqueConstraint("usage_event_id", "metric", name="uq_usage_event_items_event_metric"),
        CheckConstraint("quantity >= 0", name="quantity_nonneg"),
        CheckConstraint("cumulative_quantity >= 0", name="cumulative_nonneg"),
        CheckConstraint("unit_size > 0", name="unit_size_positive"),
        CheckConstraint("price_micros >= 0", name="price_nonneg"),
        Index("ix_usage_event_items_metric", "metric"),
    )

    # CASCADE here, unlike everywhere else in billing: an item has no meaning
    # apart from its parent, and the parent is itself RESTRICT-protected, so
    # this can never be the route by which financial history disappears.
    usage_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("usage_events.id", ondelete="CASCADE"), nullable=False
    )  # covered by uq_usage_event_items_event_metric
    metric: Mapped[UsageMetric] = mapped_column(
        Enum(UsageMetric, native_enum=False, length=32), nullable=False
    )
    # The delta this event added.
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The session total for this metric through this event — what was priced.
    cumulative_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)

    price_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prices.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    # A snapshot of the three numbers that produced the charge. Redundant with
    # `prices` because prices are immutable — kept so an invoice renders with
    # no joins, and so a manual price edit shows up as a mismatch rather than
    # silently rewriting history.
    unit_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_micros_per_unit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rounding: Mapped[RoundingMode] = mapped_column(
        Enum(RoundingMode, native_enum=False, length=32), nullable=False
    )

    price_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_price_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UsageEventItem {self.metric.value} q={self.quantity}>"
