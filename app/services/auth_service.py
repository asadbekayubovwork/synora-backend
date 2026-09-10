"""The registration and sign-in flows.

Registration is two steps: `register` stores the account as unverified and
mails a code; `verify_otp` turns it into a real account and signs the user in.
An unverified row cannot log in.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
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
    create_reset_token,
    decode_token,
    hash_password,
    reset_token_matches_password,
    verify_password,
)
from app.db.base import utcnow
from app.models.oauth import OAuthAccount
from app.models.otp import OtpPurpose
from app.models.user import User, normalize_email
from app.services.oauth.registry import label_for
from app.services.billing.wallet_service import grant_signup_bonus
from app.services.otp_service import IssuedOtp, consume_otp, discard_codes, issue_otp

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


def issue_tokens(user: User) -> AuthTokens:
    """Public because `oauth_service` signs users in through the same door."""
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

    tokens = issue_tokens(user)
    # Creates the wallet and grants the welcome credit, both keyed so that
    # calling this again can never grant twice — which is why it is safe to
    # call unconditionally rather than tracking whether it already happened.
    await grant_signup_bonus(session, user.id)
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

    if user.password_hash is None:
        # A provider-only account. Naming the providers is no new leak — the
        # register endpoint already answers 409 for an address that has an
        # account — and without it the user is stuck guessing a password that
        # was never set.
        verify_password(password, _DUMMY_HASH)
        raise ForbiddenError(
            f"This account signs in with {await _login_methods(session, user)}. "
            "Use that, or set a password with 'Forgot password'.",
            code="password_login_unavailable",
        )

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
    tokens = issue_tokens(user)
    await session.commit()
    return tokens


async def _login_methods(session: AsyncSession, user: User) -> str:
    """Reads as `Google`, or `Google or GitHub` — for the message above."""
    providers = (
        await session.execute(
            select(OAuthAccount.provider)
            .where(OAuthAccount.user_id == user.id)
            .order_by(OAuthAccount.created_at)
        )
    ).scalars()
    labels = [label_for(provider) for provider in providers]
    return " or ".join(labels) if labels else "a linked account"


# --- Refresh ---------------------------------------------------------------


async def refresh_tokens(session: AsyncSession, user: User) -> AuthTokens:
    tokens = issue_tokens(user)
    await session.commit()
    return tokens


# --- Password reset --------------------------------------------------------


async def forgot_password(session: AsyncSession, email: str) -> IssuedOtp | None:
    """Mail a reset code, or do nothing if there is no account to reset.

    `None` means "nothing was sent". The route answers the same either way, so
    this endpoint cannot be used to discover which emails are registered.
    """
    email = normalize_email(email)
    user = await get_user_by_email(session, email)

    # An unverified signup has no confirmed mailbox to send to, and its owner
    # has a simpler route anyway: registering again replaces the password.
    if user is None or not user.is_verified or not user.is_active:
        return None

    issued = await issue_otp(session, email, OtpPurpose.RESET_PASSWORD)
    await session.commit()
    return issued


async def verify_reset_otp(session: AsyncSession, email: str, code: str) -> tuple[str, int]:
    """Exchange a correct reset code for the token that authorises the change."""
    email = normalize_email(email)
    user = await get_user_by_email(session, email)

    # The code is checked before the account is, so an address with no account
    # fails on the missing code and is answered exactly like an address whose
    # code has expired — rather than with a distinct "no such user".
    await consume_otp(session, email, code, OtpPurpose.RESET_PASSWORD)

    if user is None or not user.is_verified:
        raise BadRequestError(
            "No pending verification for this email.",
            code="otp_not_found",
        )

    token = create_reset_token(str(user.id), user.password_hash)
    await session.commit()
    return token, settings.reset_token_ttl_minutes * 60


async def reset_password(
    session: AsyncSession,
    email: str,
    reset_token: str,
    password: str,
) -> None:
    email = normalize_email(email)

    try:
        payload = decode_token(reset_token, "password_reset")
    except jwt.ExpiredSignatureError:
        raise BadRequestError(
            "This reset link has expired. Start again.",
            code="reset_token_expired",
        ) from None
    except jwt.InvalidTokenError:
        raise BadRequestError(
            "This reset link is not valid. Start again.",
            code="reset_token_invalid",
        ) from None

    user = await get_user_by_email(session, email)
    if user is None or str(user.id) != payload.get("sub"):
        raise BadRequestError(
            "This reset link is not valid. Start again.",
            code="reset_token_invalid",
        )

    # The token carries a fingerprint of the password it was issued against, so
    # a token that has already been spent no longer matches.
    if not reset_token_matches_password(payload, user.password_hash):
        raise BadRequestError(
            "This reset link has already been used. Start again.",
            code="reset_token_used",
        )

    user.password_hash = hash_password(password)

    # Any code still outstanding for this address is now moot.
    await discard_codes(session, email, OtpPurpose.RESET_PASSWORD)
    await session.commit()
