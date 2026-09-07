"""Google sign-in, over plain OAuth 2.0 authorization code + userinfo.

The token response also carries an `id_token` that holds the same claims, but
trusting it means fetching and rotating Google's JWKS. Reading the claims back
from the userinfo endpoint over TLS gets the same guarantee with no key
handling, at the cost of one extra request per sign-in.
"""

from __future__ import annotations

from urllib.parse import urlencode

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.models.oauth import OAuthProviderName
from app.services.oauth.base import (
    OAuthIdentity,
    OAuthProvider,
    get_json,
    http_client,
    post_form,
)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleProvider(OAuthProvider):
    name = OAuthProviderName.GOOGLE
    label = "Google"

    @property
    def is_configured(self) -> bool:
        return bool(settings.google_client_id and settings.google_client_secret)

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": settings.google_client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            # No refresh token: we never call Google again on the user's
            # behalf, we only need to know who they are once.
            "access_type": "online",
            # Without this a browser with one Google session is bounced
            # straight back, and "sign in with a different account" is
            # impossible from our side.
            "prompt": "select_account",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def identity_from_code(self, code: str, redirect_uri: str) -> OAuthIdentity:
        async with http_client() as client:
            token = await post_form(
                client,
                TOKEN_URL,
                {
                    "code": code,
                    "client_id": settings.google_client_id or "",
                    "client_secret": settings.google_client_secret or "",
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                label=self.label,
            )

            access_token = token.get("access_token")
            if not access_token:
                raise BadRequestError(
                    "Google rejected the sign-in. Please try again.",
                    code="oauth_exchange_failed",
                )

            profile = await get_json(client, USERINFO_URL, token=str(access_token), label=self.label)

        if not isinstance(profile, dict) or not profile.get("sub"):
            raise BadRequestError(
                "Google did not tell us who signed in. Please try again.",
                code="oauth_profile_unavailable",
            )

        email = profile.get("email")
        return OAuthIdentity(
            provider=self.name,
            account_id=str(profile["sub"]),
            email=str(email).strip().lower() if email else None,
            email_verified=bool(profile.get("email_verified")),
            full_name=profile.get("name") or None,
            avatar_url=profile.get("picture") or None,
            # Google has no handle; the address is the closest thing to one.
            username=str(email) if email else None,
        )
