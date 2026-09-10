"""Money in: our order, and the provider transactions against it.

Two tables, because the relationship is genuinely one-to-many. A Payme order
receives `CheckPerformTransaction`, `CreateTransaction`, `PerformTransaction`
and possibly a much later `CancelTransaction` — several provider transactions
against one order, each of them retryable. Collapsing them into one row would
mean either losing that history or bolting a second state machine onto the row
that also holds the credit grant.

A top-up can also have no payment at all: an admin grant, or a promo.
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
from app.models.billing_enums import PaymentState, TopupProvider, TopupStatus


class Topup(Base):
    """N som in, M micro-credits out, at a rate recorded on the row.

    `uzs_per_credit_tiyin` is copied here from the active `CreditRate` at
    creation and never recomputed. Recording it is what makes a two-year-old
    receipt explicable after the price of a credit has changed three times.
    """

    __tablename__ = "topups"
    __table_args__ = (
        # Our own public reference, echoed back by the provider — Payme's
        # `account.order_id`, Click's `merchant_trans_id`.
        UniqueConstraint("order_key", name="uq_topups_order_key"),
        UniqueConstraint("idempotency_key", name="uq_topups_idempotency_key"),
        CheckConstraint("amount_tiyin >= 0", name="amount_nonneg"),
        CheckConstraint("credit_micros >= 0", name="credit_nonneg"),
        CheckConstraint("bonus_micros >= 0", name="bonus_nonneg"),
        CheckConstraint("refunded_micros >= 0", name="refunded_nonneg"),
        CheckConstraint("uzs_per_credit_tiyin > 0", name="rate_positive"),
        CheckConstraint(
            "refunded_micros <= credit_micros + bonus_micros",
            name="refund_within_grant",
        ),
        Index("ix_topups_user_created", "user_id", "created_at"),
        # The reconciliation alarm: anything stuck in `paid` for too long.
        Index("ix_topups_status_created", "status", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[TopupProvider] = mapped_column(
        Enum(TopupProvider, native_enum=False, length=32), nullable=False
    )
    status: Mapped[TopupStatus] = mapped_column(
        Enum(TopupStatus, native_enum=False, length=32),
        default=TopupStatus.CREATED,
        server_default=TopupStatus.CREATED.value,
        nullable=False,
    )
    order_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # UZS in tiyin. Integers all the way down; no currency type anywhere.
    amount_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Price of one credit, in tiyin, as it stood when this order was created.
    uzs_per_credit_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credit_rates.id", ondelete="RESTRICT"), nullable=True
    )

    credit_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    bonus_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    bonus_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    promo_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refunded_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    # Non-null exactly once, when the credits were granted. This is what makes
    # crediting idempotent: the status CAS carries `ledger_group_id IS NULL`,
    # so a repeated `PerformTransaction` finds it set and returns the original
    # answer without touching the wallet.
    ledger_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cooldown anchor for `POST /topups/{id}/refresh`, so an outbound provider
    # call can never be triggered in a loop by a user hammering a button.
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Payme identifies a transaction by its own id, but numbers the
    # `transaction` field in its responses from ours. A small integer per
    # top-up is what it expects there.
    prepare_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Non-null for a MANUAL grant: which admin authorised it.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    version: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    @property
    def granted_micros(self) -> int:
        return self.credit_micros + self.bonus_micros

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Topup {self.provider.value} {self.order_key} {self.status.value}>"


class Payment(Base):
    """One provider transaction against a top-up."""

    __tablename__ = "payments"
    __table_args__ = (
        # (provider, ref) rather than ref alone: two providers can mint the
        # same id, and this is the shape `uq_oauth_provider_account` already
        # established for exactly this reason.
        UniqueConstraint("provider", "provider_ref", name="uq_payments_provider_provider_ref"),
        CheckConstraint("amount_tiyin >= 0", name="amount_nonneg"),
        CheckConstraint("callback_count >= 0", name="callback_count_nonneg"),
        Index("ix_payments_topup", "topup_id"),
        Index("ix_payments_state", "state"),
    )

    topup_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topups.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[TopupProvider] = mapped_column(
        Enum(TopupProvider, native_enum=False, length=32), nullable=False
    )
    provider_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    # The provider's own state code, stored verbatim. Their numbering is not
    # ours, and translating it away destroys the evidence.
    provider_state_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[PaymentState] = mapped_column(
        Enum(PaymentState, native_enum=False, length=32),
        default=PaymentState.CREATED,
        server_default=PaymentState.CREATED.value,
        nullable=False,
    )
    amount_tiyin: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Provider-supplied millisecond timestamps, kept as sent. Payme echoes
    # these back in `CheckTransaction`, so a retry has to answer with exactly
    # what it answered the first time.
    provider_created_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_performed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_cancelled_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The callback body as received, JSON-serialised. Length-bounded rather
    # than `Text`, because this repo has no `Text` column and both providers'
    # bodies are comfortably under 2 KB; the flag records the day one is not.
    raw_payload: Mapped[str | None] = mapped_column(String(16384), nullable=True)
    raw_payload_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    signature_ok: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    callback_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Payment {self.provider.value}:{self.provider_ref} {self.state.value}>"
