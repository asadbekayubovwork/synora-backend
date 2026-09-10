"""Wallet lifecycle and the read model behind `GET /v1/wallet`.

Balance changes happen in `wallet_repo`; this module is what the rest of the
app talks to. It owns the commits, following the repo convention that the
service layer does and the route never does.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import utcnow
from app.models.billing_enums import LedgerEntryKind, LedgerRefType
from app.models.wallet import Wallet
from app.services.billing import wallet_repo
from app.services.billing.wallet_repo import WalletSnapshot

logger = logging.getLogger("synora.billing")


@dataclass(frozen=True)
class Balance:
    """What a client is shown. Micros for arithmetic, strings for display."""

    wallet_id: uuid.UUID
    paid_micros: int
    bonus_micros: int
    reserved_micros: int
    available_micros: int
    bonus_expires_at: object | None
    is_frozen: bool
    low_balance_threshold_micros: int

    @property
    def is_low(self) -> bool:
        threshold = self.low_balance_threshold_micros
        return threshold > 0 and self.available_micros <= threshold


def _balance(snapshot: WalletSnapshot) -> Balance:
    return Balance(
        wallet_id=snapshot.wallet_id,
        paid_micros=snapshot.paid_micros,
        # The *effective* bonus, not the raw column. Showing a bonus that
        # `available_micros` refuses to spend reads as a bug to the user, and
        # they are right.
        bonus_micros=snapshot.effective_bonus_micros,
        reserved_micros=snapshot.reserved_micros,
        available_micros=snapshot.available_micros,
        bonus_expires_at=snapshot.bonus_expires_at if snapshot.effective_bonus_micros else None,
        is_frozen=snapshot.is_frozen,
        low_balance_threshold_micros=snapshot.low_balance_threshold_micros,
    )


async def ensure_wallet(session: AsyncSession, user_id: uuid.UUID) -> WalletSnapshot:
    """The user's wallet, created on first sight.

    Lazily rather than in the registration transaction, so a billing problem
    can never stop somebody signing up. The unique constraint on `user_id`
    settles the race when two requests arrive together — whoever loses reads
    the winner's row.
    """
    existing = (
        await session.execute(select(Wallet.id).where(Wallet.user_id == user_id))
    ).scalar_one_or_none()
    if existing is not None:
        return await wallet_repo.snapshot_by_id(session, existing)

    session.add(
        Wallet(
            user_id=user_id,
            low_balance_threshold_micros=settings.billing_low_balance_micros,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return await wallet_repo.snapshot_by_user(session, user_id)

    logger.info("Created wallet for user %s", user_id)
    return await wallet_repo.snapshot_by_user(session, user_id)


async def get_balance(session: AsyncSession, user_id: uuid.UUID) -> Balance:
    return _balance(await ensure_wallet(session, user_id))


async def grant_signup_bonus(session: AsyncSession, user_id: uuid.UUID) -> Balance:
    """Give a new account its starting credit, at most once, ever.

    Keyed on the user id rather than on a flag column, so the idempotency is
    the ledger's own unique constraint. Calling this on every verification and
    every first OAuth login is therefore safe, which is exactly how it is
    wired — no caller has to remember whether it already happened.
    """
    snapshot = await ensure_wallet(session, user_id)
    amount = settings.billing_signup_bonus_micros
    if amount <= 0:
        return _balance(snapshot)

    expires_at = None
    if settings.billing_signup_bonus_days > 0:
        expires_at = utcnow() + timedelta(days=settings.billing_signup_bonus_days)

    movement = await wallet_repo.credit(
        session,
        wallet_id=snapshot.wallet_id,
        bonus_micros=amount,
        bonus_expires_at=expires_at,
        kind=LedgerEntryKind.BONUS_GRANT,
        ref_type=LedgerRefType.SIGNUP_BONUS,
        idempotency_key=f"signup:{user_id}",
        note="Welcome bonus",
    )
    if not movement.replayed:
        logger.info("Granted %s signup bonus micros to user %s", amount, user_id)
    return _balance(await wallet_repo.snapshot_by_id(session, snapshot.wallet_id))
