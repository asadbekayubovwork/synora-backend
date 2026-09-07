"""Which providers exist, which are usable, and where they may redirect."""

from __future__ import annotations

from app.core.config import settings
from app.core.exceptions import (
    BadRequestError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models.oauth import OAuthProviderName
from app.services.oauth.base import OAuthProvider
from app.services.oauth.github import GitHubProvider
from app.services.oauth.google import GoogleProvider
from app.services.oauth.telegram import TelegramProvider

telegram_provider = TelegramProvider()

_PROVIDERS: dict[OAuthProviderName, OAuthProvider] = {
    OAuthProviderName.GOOGLE: GoogleProvider(),
    OAuthProviderName.GITHUB: GitHubProvider(),
    OAuthProviderName.TELEGRAM: telegram_provider,
}


def provider_name(value: str) -> OAuthProviderName:
    try:
        return OAuthProviderName(value.strip().lower())
    except ValueError:
        raise NotFoundError(
            f"'{value}' is not a sign-in provider we support.",
            code="oauth_provider_unknown",
        ) from None


def get_provider(value: str) -> OAuthProvider:
    """The provider, or an error the caller can show as-is.

    An unconfigured provider answers `503` rather than `404`: the provider is
    real, this deployment just has no credentials for it.
    """
    provider = _PROVIDERS[provider_name(value)]
    if not provider.is_configured:
        raise ServiceUnavailableError(
            f"{provider.label} sign-in is not configured on this server.",
            code="oauth_provider_unconfigured",
        )
    return provider


def label_for(name: OAuthProviderName) -> str:
    return _PROVIDERS[name].label


def configured_providers() -> list[OAuthProvider]:
    return [provider for provider in _PROVIDERS.values() if provider.is_configured]


def resolve_redirect_uri(requested: str | None) -> str:
    """Check a requested redirect against the allowlist, or fall back to it.

    An unchecked `redirect_uri` is how a client id gets turned into a code
    thief: the provider will happily send the code anywhere the caller names.
    """
    allowed = settings.oauth_redirect_uri_list
    if not allowed:
        raise ServiceUnavailableError(
            "No OAuth redirect URI is configured on this server.",
            code="oauth_redirect_uri_unconfigured",
        )
    if requested is None:
        return allowed[0]
    if requested not in allowed:
        raise BadRequestError(
            "That redirect_uri is not allowed. It must match OAUTH_REDIRECT_URIS exactly.",
            code="oauth_redirect_uri_not_allowed",
        )
    return requested
