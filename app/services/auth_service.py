"""The registration and sign-in flows.

Registration is two steps: `register` stores the account as unverified and
mails a code; `verify_otp` turns it into a real account and signs the user in.
An unverified row cannot log in.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.db.base import utcnow
from app.models.otp import OtpPurpose
from app.models.user import User, normalize_email
from app.services.otp_service import IssuedOtp, consume_otp, issue_otp

# Compared against when no user matches, so a missing address and a wrong
# password take the same time to answer and cannot be told apart by timing.
_DUMMY_HASH = hash_password("synora-timing-equaliser")

# Deliberately vague: it is the same text whether the address is unknown or the
# password is wrong, so login cannot be used to enumerate registered emails.
_INVALID_CREDENTIALS = "No user is found with these credentials."


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    user: User


def _issue_tokens(user: User) -> AuthTokens:
    subject = str(user.id)
    return AuthTokens(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
        expires_in=settings.access_token_ttl_minutes * 60,
        user=user,
    )


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == normalize_email(email)))
    return result.scalar_one_or_none()


# --- Step 1: register ------------------------------------------------------


async def register(session: AsyncSession, email: str, password: str) -> IssuedOtp:
    email = normalize_email(email)
    user = await get_user_by_email(session, email)

    if user is not None and user.is_verified:
        raise ConflictError(
            "An account with this email already exists.",
            code="email_already_registered",
        )

    if user is None:
        user = User(email=email, password_hash=hash_password(password), is_verified=False)
        session.add(user)
    else:
        # The signup was never finished, so this attempt owns the account:
        # take the newer password and send a fresh code.
        user.password_hash = hash_password(password)

    await session.flush()

    issued = await issue_otp(session, email, OtpPurpose.REGISTER)
    await session.commit()
    return issued


# --- Step 2: verify --------------------------------------------------------


async def verify_registration_otp(session: AsyncSession, email: str, code: str) -> AuthTokens:
    email = normalize_email(email)
    user = await get_user_by_email(session, email)

    if user is None:
        raise BadRequestError(
            "No pending verification for this email.",
            code="otp_not_found",
        )
    if user.is_verified:
        raise BadRequestError(
            "This email is already verified. Please sign in.",
            code="email_already_verified",
        )

    await consume_otp(session, email, code, OtpPurpose.REGISTER)

    now = utcnow()
    user.is_verified = True
    user.verified_at = now
    user.last_login_at = now

    tokens = _issue_tokens(user)
    await session.commit()
    return tokens


async def resend_registration_otp(session: AsyncSession, email: str) -> IssuedOtp:
    email = normalize_email(email)
    user = await get_user_by_email(session, email)

    # One message for "never registered" and for "already verified" — neither
    # answer should reveal whether the address is in use.
    if user is None or user.is_verified:
        raise BadRequestError(
            "No pending verification for this email.",
            code="otp_not_found",
        )

    issued = await issue_otp(session, email, OtpPurpose.REGISTER)
    await session.commit()
    return issued


# --- Login -----------------------------------------------------------------


async def login(session: AsyncSession, email: str, password: str) -> AuthTokens:
    user = await get_user_by_email(session, email)

    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")

    if not verify_password(password, user.password_hash):
        raise UnauthorizedError(_INVALID_CREDENTIALS, code="invalid_credentials")

    if not user.is_verified:
        # Distinct from the vague message above on purpose: the password was
        # right, so the caller already knows the account exists.
        raise ForbiddenError(
            "Verify your email before signing in.",
            code="email_not_verified",
        )

    if not user.is_active:
        raise ForbiddenError("This account has been disabled.", code="account_disabled")

    user.last_login_at = utcnow()
    tokens = _issue_tokens(user)
    await session.commit()
    return tokens


# --- Refresh ---------------------------------------------------------------


async def refresh_tokens(session: AsyncSession, user: User) -> AuthTokens:
    tokens = _issue_tokens(user)
    await session.commit()
    return tokens
