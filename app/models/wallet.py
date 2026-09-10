from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.money import MICROS_PER_CREDIT
from app.db.base import Base, as_utc, utcnow


class Wallet(Base):
    """One prepaid balance per user, in micro-credits.

    Three counters, and the definition of "can this user afford it" is
    `paid + effective_bonus - reserved`:

    - `paid_micros`   credit bought with money. Never expires.
    - `bonus_micros`  granted credit, spent first because it is perishable.
    - `reserved_micros` committed to sessions that have not settled yet. Not
      money that has moved — a ceiling, so two concurrent calls cannot both
      spend the same som.

    `version` is doing two jobs. It is the compare-and-swap token for every
    balance change (`app/services/billing/wallet_repo.py` is the only writer),
    and it is a per-wallet monotonic sequence that each ledger entry records,
    which is what lets a statement be replayed in exact order without a global
    sequence.

    There is deliberately no `reserved_micros <= paid_micros + bonus_micros`
    check. It is true at every hold, but bonus expiry shrinks the right-hand
    side underneath a live hold, so the constraint would turn a perfectly
    legitimate expiry into an `IntegrityError`.
    """

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_wallets_user"),
        CheckConstraint("paid_micros >= 0", name="paid_nonneg"),
        CheckConstraint("bonus_micros >= 0", name="bonus_nonneg"),
        CheckConstraint("reserved_micros >= 0", name="reserved_nonneg"),
        CheckConstraint("version >= 0", name="version_nonneg"),
        CheckConstraint("lifetime_spend_micros >= 0", name="lifetime_spend_nonneg"),
        CheckConstraint("lifetime_topup_micros >= 0", name="lifetime_topup_nonneg"),
        CheckConstraint("lifetime_writeoff_micros >= 0", name="lifetime_writeoff_nonneg"),
        CheckConstraint("low_balance_threshold_micros >= 0", name="low_balance_threshold_nonneg"),
    )

    # RESTRICT, not CASCADE, and this is the one place in the repo that
    # differs from `OAuthAccount`. Financial records outlive the account they
    # belong to: `DELETE FROM users` must fail for anyone who ever held credit,
    # so deleting an account is an anonymise-in-place operation rather than a
    # row removal. The ledger restricts the wallet in turn, so the refusal
    # holds even if this ever loosened.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    paid_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    bonus_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Null means the bonus never expires. Once past, the bonus is invisible
    # immediately — `effective_bonus_micros` does not wait for the sweeper.
    bonus_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reserved_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    version: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    # Set when a reversal drove the balance negative, or by an admin. A frozen
    # wallet cannot open sessions or top up until someone looks at it.
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    low_balance_threshold_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    # Without this, the low-balance warning fires on every single request once
    # the balance is low. It is cleared when the balance climbs back above the
    # threshold plus hysteresis, so the user is warned once per crossing.
    low_balance_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Denormalised totals, for reporting and for spotting drift. `spend` and
    # `topup` are checked against the ledger by the reconcile job; `writeoff`
    # is NOT, because a write-off moves no money and so writes no entry.
    lifetime_spend_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    lifetime_topup_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    lifetime_writeoff_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    @property
    def is_frozen(self) -> bool:
        return self.frozen_at is not None

    @property
    def effective_bonus_micros(self) -> int:
        """Bonus that still counts towards `available`.

        Expired bonus reads as zero from the moment it expires, rather than
        when a background job gets round to zeroing the column. The debit path
        then writes the `expiry` ledger entry lazily, so the wallet keeps
        matching its ledger without depending on the sweeper having run.
        """
        if self.bonus_micros <= 0:
            return 0
        if self.bonus_expires_at is None:
            return self.bonus_micros
        return self.bonus_micros if as_utc(self.bonus_expires_at) > utcnow() else 0

    @property
    def available_micros(self) -> int:
        return self.paid_micros + self.effective_bonus_micros - self.reserved_micros

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        total = (self.paid_micros + self.bonus_micros) / MICROS_PER_CREDIT
        return f"<Wallet user={self.user_id} {total:.6f} credits v{self.version}>"
