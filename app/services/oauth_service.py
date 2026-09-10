"""Turning a provider identity into a Synora session.

The providers differ; everything from here on does not. One identity maps to
one `OAuthAccount` row, that row points at a `User`, and signing in means
issuing the same token pair `/auth/login` would.

Matching rules, in order:

1. **A linked account signs in as its owner.** The provider's account id is
   the key, so changing the email on the Google side keeps the same Synora
   account.
2. **A verified provider email joins the account that holds it.** The provider
   has proved control of the mailbox, which is exactly what our own OTP proves,
   so this is a link and not a takeover.
3. **Anything else creates an account.** With no password: `has_password` is
   false and `/auth/login` says so.

An *unverified* provider email is refused outright rather than used to create
an account, since nothing stops someone from typing an address they do not own
into a throwaway provider profile.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.security import create_oauth_state, decode_token
from app.db.base import utcnow
from app.models.oauth import OAuthAccount, OAuthProviderName
from app.models.otp import OtpPurpose
from app.models.user import User
from app.services.auth_service import AuthTokens, get_user_by_email, issue_tokens
from app.services.billing.wallet_service import grant_signup_bonus
from app.services.oauth import (
    OAuthIdentity,
    get_provider,
    label_for,
    provider_name,
    resolve_redirect_uri,
)
from app.services.otp_service import discard_codes


@dataclass(frozen=True)
class AuthorizationRequest:
    provider: OAuthProviderName
    authorization_url: str
    state: str
    redirect_uri: str
    expires_in: int


# --- Step 1: where to send the browser -------------------------------------


def start_authorization(
    provider_value: str,
    requested_redirect_uri: str | None,
    link_user_id: str | None = None,
) -> AuthorizationRequest:
    provider = get_provider(provider_value)
    redirect_uri = resolve_redirect_uri(requested_redirect_uri)
    state = create_oauth_state(provider.name.value, redirect_uri, link_user_id)

    return AuthorizationRequest(
        provider=provider.name,
        authorization_url=provider.authorization_url(state, redirect_uri),
        state=state,
        redirect_uri=redirect_uri,
        expires_in=settings.oauth_state_ttl_minutes * 60,
    )


def _redirect_uri_from_state(
    provider_value: str,
    state: str,
    *,
    expect_link_for: str | None = None,
) -> str:
    """Validate the state and return the redirect URI it was issued for.

    The redirect URI comes back out of the signed state rather than from the
    request, because the provider will only accept the one the authorization
    was started with — and a caller who could name a different one here could
    make the exchange fail in interesting ways.
    """
    try:
        payload = decode_token(state, "oauth_state")
    except jwt.ExpiredSignatureError:
        raise BadRequestError(
            "This sign-in took too long. Please try again.",
            code="oauth_state_expired",
        ) from None
    except jwt.InvalidTokenError:
        raise BadRequestError(
            "This sign-in could not be verified. Please try again.",
            code="oauth_state_invalid",
        ) from None

    if payload.get("prv") != provider_name(provider_value).value:
        raise BadRequestError(
            "This sign-in was started with a different provider.",
            code="oauth_state_invalid",
        )

    # A state minted for linking must not be redeemable as a sign-in, and vice
    # versa: otherwise a link URL handed to someone else logs them in as its
    # owner, or a plain sign-in silently attaches to whoever is holding a token.
    if payload.get("lnk") != expect_link_for:
        raise BadRequestError(
            "This sign-in was started for something else. Please try again.",
            code="oauth_state_invalid",
        )

    redirect_uri = payload.get("rdu")
    if not isinstance(redirect_uri, str) or not redirect_uri:
        raise BadRequestError(
            "This sign-in could not be verified. Please try again.",
            code="oauth_state_invalid",
        )
    return redirect_uri


async def identity_from_callback(
    provider_value: str,
    code: str,
    state: str,
    *,
    expect_link_for: str | None = None,
) -> OAuthIdentity:
    provider = get_provider(provider_value)
    redirect_uri = _redirect_uri_from_state(
        provider_value, state, expect_link_for=expect_link_for
    )
    return await provider.identity_from_code(code, redirect_uri)


# --- Queries ---------------------------------------------------------------


async def account_for_identity(session: AsyncSession, identity: OAuthIdentity) -> OAuthAccount | None:
    result = await session.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == identity.provider,
            OAuthAccount.provider_account_id == identity.account_id,
        )
    )
    return result.scalar_one_or_none()


async def linked_accounts(session: AsyncSession, user_id: uuid.UUID) -> list[OAuthAccount]:
    result = await session.execute(
        select(OAuthAccount)
        .where(OAuthAccount.user_id == user_id)
        .order_by(OAuthAccount.created_at)
    )
    return list(result.scalars())


async def _account_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(OAuthAccount).where(OAuthAccount.user_id == user_id)
    )
    return int(result.scalar_one())


# --- Shared bits -----------------------------------------------------------


def _fill_profile(user: User, identity: OAuthIdentity) -> None:
    """Adopt the provider's name and picture only where we have none."""
    if identity.full_name and not user.full_name:
        user.full_name = identity.full_name
    if identity.avatar_url and not user.avatar_url:
        user.avatar_url = identity.avatar_url


def _assert_usable(user: User) -> None:
    if not user.is_active:
        raise ForbiddenError("This account has been disabled.", code="account_disabled")


def _record(user_id: uuid.UUID, identity: OAuthIdentity) -> OAuthAccount:
    return OAuthAccount(
        user_id=user_id,
        provider=identity.provider,
        provider_account_id=identity.account_id,
        email=identity.email,
        username=identity.username,
    )


# --- Sign in ---------------------------------------------------------------


async def sign_in(session: AsyncSession, identity: OAuthIdentity) -> AuthTokens:
    label = label_for(identity.provider)
    account = await account_for_identity(session, identity)

    if account is not None:
        user = (
            await session.execute(select(User).where(User.id == account.user_id))
        ).scalar_one_or_none()
        if user is None:
            # The row outlived its user; nothing to sign in as.
            await session.delete(account)
            await session.commit()
            raise UnauthorizedError("This account no longer exists.", code="user_not_found")

        _assert_usable(user)
        account.email = identity.email
        account.username = identity.username
        _fill_profile(user, identity)
        user.last_login_at = utcnow()
        tokens = issue_tokens(user)
        # Idempotent, so an existing account simply gets its wallet ensured.
        await grant_signup_bonus(session, user.id)
        await session.commit()
        return tokens

    user = None
    if identity.email:
        if not identity.email_verified:
            raise BadRequestError(
                f"Your {label} email address is not verified. Verify it with {label} "
                "first, or sign up with an email and password.",
                code="oauth_email_unverified",
            )
        user = await get_user_by_email(session, identity.email)

    now = utcnow()

    if user is None:
        user = User(
            email=identity.email,
            password_hash=None,
            is_verified=True,
            verified_at=now,
            full_name=identity.full_name,
            avatar_url=identity.avatar_url,
        )
        session.add(user)
        await session.flush()
    else:
        _assert_usable(user)
        if not user.is_verified:
            # A signup that never proved it could read this mailbox — and the
            # provider just did. Claim the row, but drop the password it came
            # with: whoever typed it never verified anything, and leaving it in
            # place would hand them a way into the account.
            user.is_verified = True
            user.verified_at = now
            user.password_hash = None
            if user.email:
                await discard_codes(session, user.email, OtpPurpose.REGISTER)
        _fill_profile(user, identity)

    session.add(_record(user.id, identity))
    user.last_login_at = now
    tokens = issue_tokens(user)
    await grant_signup_bonus(session, user.id)
    await session.commit()
    return tokens


# --- Link and unlink -------------------------------------------------------


async def link(session: AsyncSession, user: User, identity: OAuthIdentity) -> OAuthAccount:
    label = label_for(identity.provider)
    existing = await account_for_identity(session, identity)

    if existing is not None:
        if existing.user_id != user.id:
            raise ConflictError(
                f"That {label} account is already linked to another Synora account.",
                code="oauth_account_already_linked",
            )
        # Idempotent: linking twice just refreshes what we display.
        existing.email = identity.email
        existing.username = identity.username
        _fill_profile(user, identity)
        await session.commit()
        return existing

    same_provider = (
        await session.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user.id,
                OAuthAccount.provider == identity.provider,
            )
        )
    ).scalar_one_or_none()
    if same_provider is not None:
        raise ConflictError(
            f"A different {label} account is already linked. Remove it first.",
            code="oauth_provider_already_linked",
        )

    account = _record(user.id, identity)
    session.add(account)
    _fill_profile(user, identity)
    await session.commit()
    return account


async def unlink(session: AsyncSession, user: User, provider_value: str) -> None:
    name = provider_name(provider_value)
    account = (
        await session.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user.id,
                OAuthAccount.provider == name,
            )
        )
    ).scalar_one_or_none()

    if account is None:
        raise NotFoundError(
            f"No {label_for(name)} account is linked to this account.",
            code="oauth_account_not_linked",
        )

    # Removing the last way in would lock the user out of their own account.
    if not user.has_password and await _account_count(session, user.id) <= 1:
        raise BadRequestError(
            "This is the only way to sign in to this account. Set a password first.",
            code="oauth_last_login_method",
        )

    await session.execute(delete(OAuthAccount).where(OAuthAccount.id == account.id))
    await session.commit()
