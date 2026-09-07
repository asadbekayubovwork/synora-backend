from app.services.oauth.base import OAuthIdentity, OAuthProvider
from app.services.oauth.registry import (
    configured_providers,
    get_provider,
    label_for,
    provider_name,
    resolve_redirect_uri,
    telegram_provider,
)

__all__ = [
    "OAuthIdentity",
    "OAuthProvider",
    "configured_providers",
    "get_provider",
    "label_for",
    "provider_name",
    "resolve_redirect_uri",
    "telegram_provider",
]
