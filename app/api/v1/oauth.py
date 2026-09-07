from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Path, Query, status

from app.api.deps import CredentialsDep, CurrentUser, SessionDep, get_current_user
from app.core.config import settings
from app.models.oauth import OAuthAccount
from app.schemas.auth import ErrorResponse, MessageResponse, TokenResponse, UserResponse
from app.schemas.oauth import (
    OAuthAccountResponse,
    OAuthAccountsResponse,
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    OAuthProviderInfo,
    OAuthProvidersResponse,
    TelegramLoginRequest,
)
from app.services import oauth_service
from app.services.auth_service import AuthTokens
from app.services.oauth import configured_providers, telegram_provider

router = APIRouter(prefix="/oauth", tags=["OAuth"])

ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Unknown provider"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    502: {"model": ErrorResponse, "description": "The provider was unreachable"},
    503: {"model": ErrorResponse, "description": "The provider is not configured here"},
}

ProviderPath = Path(
    description="`google`, `github` or `telegram`.",
    examples=["google"],
)


def _tokens(tokens: AuthTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(tokens.user),
    )


def _accounts(accounts: list[OAuthAccount]) -> OAuthAccountsResponse:
    return OAuthAccountsResponse(
        accounts=[
            OAuthAccountResponse(
                provider=account.provider,
                provider_account_id=account.provider_account_id,
                email=account.email,
                username=account.username,
                linked_at=account.created_at,
            )
            for account in accounts
        ]
    )


@router.get(
    "/providers",
    response_model=OAuthProvidersResponse,
    summary="Which providers this server can sign in with",
    description=(
        "Only the providers whose credentials are present in the environment, so "
        "the frontend can render exactly the buttons that will work.\n\n"
        "`supports_code_flow` is `true` for Google and GitHub — start those at "
        "`/authorize`. Telegram is `false`: render its login widget for "
        "`bot_username` and post what the widget gives you to "
        "`/auth/oauth/telegram/callback`."
    ),
)
async def providers() -> OAuthProvidersResponse:
    return OAuthProvidersResponse(
        providers=[
            OAuthProviderInfo(
                name=provider.name,
                label=provider.label,
                supports_code_flow=provider.supports_code_flow,
                bot_username=(
                    settings.telegram_bot_username if provider is telegram_provider else None
                ),
            )
            for provider in configured_providers()
        ]
    )


# --- Telegram --------------------------------------------------------------
# Declared before the `/{provider}/…` routes, which would otherwise match
# these paths first and look for a `code` that Telegram never issues.


@router.post(
    "/telegram/callback",
    response_model=TokenResponse,
    responses=ERRORS,
    summary="Sign in with Telegram",
    description=(
        "Takes the object the Telegram Login Widget hands its callback — pass it "
        "through unchanged, including any field not listed here, because the "
        "signature covers all of them.\n\n"
        "Verifying that signature *is* the authentication: Telegram runs no OAuth "
        "server and there is nothing to exchange. Payloads older than "
        "`TELEGRAM_AUTH_TTL_SECONDS` are refused, so one captured from a browser "
        "does not work forever.\n\n"
        "**Telegram gives no email address**, so an account created this way has "
        "`email: null` and `has_password: false`, and Telegram is its only way in."
    ),
)
async def telegram_callback(payload: TelegramLoginRequest, session: SessionDep) -> TokenResponse:
    identity = telegram_provider.identity_from_widget(payload.model_dump(exclude_none=True))
    return _tokens(await oauth_service.sign_in(session, identity))


@router.post(
    "/telegram/link",
    response_model=OAuthAccountsResponse,
    responses=ERRORS,
    summary="Link Telegram to the signed-in account",
    description="Requires `Authorization: Bearer <access_token>`.",
)
async def telegram_link(
    payload: TelegramLoginRequest,
    user: CurrentUser,
    session: SessionDep,
) -> OAuthAccountsResponse:
    identity = telegram_provider.identity_from_widget(payload.model_dump(exclude_none=True))
    await oauth_service.link(session, user, identity)
    return _accounts(await oauth_service.linked_accounts(session, user.id))


# --- Linked accounts -------------------------------------------------------


@router.get(
    "/accounts",
    response_model=OAuthAccountsResponse,
    responses=ERRORS,
    summary="Providers linked to the signed-in account",
)
async def accounts(user: CurrentUser, session: SessionDep) -> OAuthAccountsResponse:
    return _accounts(await oauth_service.linked_accounts(session, user.id))


# --- Google and GitHub -----------------------------------------------------


@router.get(
    "/{provider}/authorize",
    response_model=OAuthAuthorizeResponse,
    responses=ERRORS,
    summary="Start a sign-in — where to send the browser",
    description=(
        "Returns the provider's consent URL. Send the browser to "
        "`authorization_url`; the provider redirects back to `redirect_uri` with "
        "`code` and `state` in the query string, and those two go to "
        "`POST /auth/oauth/{provider}/callback`.\n\n"
        "`redirect_uri` must be one of `OAUTH_REDIRECT_URIS` **exactly**, and be "
        "registered with the provider too. Omit it to use the first configured "
        "one.\n\n"
        "`intent=link` adds the provider to the account the bearer token belongs "
        "to instead of signing in, and the resulting `state` is only accepted by "
        "`/link`. It needs an `Authorization` header; `intent=login` does not."
    ),
)
async def authorize(
    session: SessionDep,
    credentials: CredentialsDep,
    provider: str = ProviderPath,
    redirect_uri: str | None = Query(
        default=None,
        description="Must match `OAUTH_REDIRECT_URIS` exactly. Defaults to the first entry.",
    ),
    intent: Literal["login", "link"] = Query(
        default="login",
        description="`link` requires a bearer token and yields a state only `/link` accepts.",
    ),
) -> OAuthAuthorizeResponse:
    link_user_id: str | None = None
    if intent == "link":
        user = await get_current_user(session, credentials)
        link_user_id = str(user.id)

    request = oauth_service.start_authorization(provider, redirect_uri, link_user_id)
    return OAuthAuthorizeResponse(
        provider=request.provider,
        authorization_url=request.authorization_url,
        state=request.state,
        redirect_uri=request.redirect_uri,
        expires_in=request.expires_in,
    )


@router.post(
    "/{provider}/callback",
    response_model=TokenResponse,
    responses=ERRORS,
    summary="Finish a sign-in — code + state to tokens",
    description=(
        "Exchanges the `code` from the redirect for a token pair, the same pair "
        "`/auth/login` returns.\n\n"
        "What it matches on, in order: a provider account already linked signs in "
        "as its owner; otherwise a **verified** provider email joins the account "
        "that holds that address; otherwise a new account is created, with no "
        "password (`has_password: false`).\n\n"
        "An unverified provider email is refused (`oauth_email_unverified`) rather "
        "than used, since anyone can type someone else's address into a throwaway "
        "profile."
    ),
)
async def callback(
    payload: OAuthCallbackRequest,
    session: SessionDep,
    provider: str = ProviderPath,
) -> TokenResponse:
    identity = await oauth_service.identity_from_callback(provider, payload.code, payload.state)
    return _tokens(await oauth_service.sign_in(session, identity))


@router.post(
    "/{provider}/link",
    response_model=OAuthAccountsResponse,
    responses=ERRORS,
    summary="Link a provider to the signed-in account",
    description=(
        "Same exchange as `/callback`, but it attaches the provider to the "
        "current account instead of opening a session. The `state` must come "
        "from `/authorize?intent=link`.\n\n"
        "One account per provider: `409` if a different one is already linked, or "
        "if this provider account belongs to another Synora user."
    ),
)
async def link(
    payload: OAuthCallbackRequest,
    user: CurrentUser,
    session: SessionDep,
    provider: str = ProviderPath,
) -> OAuthAccountsResponse:
    identity = await oauth_service.identity_from_callback(
        provider, payload.code, payload.state, expect_link_for=str(user.id)
    )
    await oauth_service.link(session, user, identity)
    return _accounts(await oauth_service.linked_accounts(session, user.id))


@router.delete(
    "/{provider}/link",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    responses=ERRORS,
    summary="Unlink a provider",
    description=(
        "Refused with `oauth_last_login_method` when it would leave the account "
        "with no way to sign in — an account with no password and no other "
        "linked provider has to set a password first."
    ),
)
async def unlink(
    user: CurrentUser,
    session: SessionDep,
    provider: str = ProviderPath,
) -> MessageResponse:
    await oauth_service.unlink(session, user, provider)
    return MessageResponse(message="That account has been unlinked.")
