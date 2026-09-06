"""Issuing, resending and consuming one-time codes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BadRequestError, TooManyRequestsError
from app.core.security import generate_otp, hash_otp, verify_otp
from app.db.base import as_utc, utcnow
from app.models.otp import OtpCode, OtpPurpose
from app.services.mailer import send_otp_email, send_password_reset_email


@dataclass(frozen=True)
class IssuedOtp:
    code: str
    expires_in: int
    resend_available_in: int


async def _active_code(session: AsyncSession, email: str, purpose: OtpPurpose) -> OtpCode | None:
    result = await session.execute(
        select(OtpCode)
        .where(OtpCode.email == email, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def issue_otp(
    session: AsyncSession,
    email: str,
    purpose: OtpPurpose = OtpPurpose.REGISTER,
    *,
    enforce_cooldown: bool = True,
) -> IssuedOtp:
    """Replace any live code for this address with a fresh one and mail it.

    The cooldown stops the resend button from being used to hammer someone's
    inbox. `enforce_cooldown=False` is for the first code of a flow, where no
    previous code is the user's fault.
    """
    existing = await _active_code(session, email, purpose)

    if existing is not None and enforce_cooldown:
        elapsed = (utcnow() - as_utc(existing.created_at)).total_seconds()
        remaining = int(settings.otp_resend_cooldown_seconds - elapsed)
        if remaining > 0:
            raise TooManyRequestsError(
                f"Please wait {remaining} seconds before requesting a new code.",
                code="otp_cooldown",
                retry_after=remaining,
            )

    await session.execute(
        delete(OtpCode).where(OtpCode.email == email, OtpCode.purpose == purpose)
    )

    code = generate_otp()
    session.add(
        OtpCode(
            email=email,
            purpose=purpose,
            code_hash=hash_otp(code),
            expires_at=utcnow() + timedelta(minutes=settings.otp_ttl_minutes),
        )
    )
    await session.flush()

    send = (
        send_password_reset_email
        if purpose is OtpPurpose.RESET_PASSWORD
        else send_otp_email
    )
    await send(email, code, settings.otp_ttl_minutes)

    return IssuedOtp(
        code=code,
        expires_in=settings.otp_ttl_minutes * 60,
        resend_available_in=settings.otp_resend_cooldown_seconds,
    )


async def consume_otp(
    session: AsyncSession,
    email: str,
    code: str,
    purpose: OtpPurpose = OtpPurpose.REGISTER,
) -> None:
    """Validate a code and burn it. Raises when it does not check out."""
    entry = await _active_code(session, email, purpose)

    if entry is None:
        raise BadRequestError(
            "No pending verification for this email.",
            code="otp_not_found",
        )

    if utcnow() > as_utc(entry.expires_at):
        await session.delete(entry)
        raise BadRequestError(
            "That code has expired. Request a new one.",
            code="otp_expired",
        )

    if entry.attempts >= settings.otp_max_attempts:
        await session.delete(entry)
        raise TooManyRequestsError(
            "Too many incorrect attempts. Request a new code.",
            code="otp_too_many_attempts",
        )

    if not verify_otp(code, entry.code_hash):
        entry.attempts += 1
        # Committed here so a wrong guess is counted even though the request
        # ends in an error — otherwise the attempt cap could never be reached.
        await session.commit()
        raise BadRequestError(
            "That code is not correct. Please try again.",
            code="otp_invalid",
        )

    await session.delete(entry)


async def discard_codes(session: AsyncSession, email: str, purpose: OtpPurpose) -> None:
    await session.execute(
        delete(OtpCode).where(OtpCode.email == email, OtpCode.purpose == purpose)
    )
