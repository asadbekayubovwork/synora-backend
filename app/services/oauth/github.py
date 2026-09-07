"""GitHub sign-in.

Two wrinkles GitHub has and the others do not: the token endpoint answers
`200` with an `error` key instead of a 4xx, and `/user` may carry no email at
all — a private profile hides it — so the address comes from `/user/emails`,
where we take only the primary verified one.
"""

from __future__ import annotations

from typing import Any
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

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
EMAILS_URL = "https://api.github.com/user/emails"


def _primary_email(entries: Any) -> tuple[str | None, bool]:
    """The address GitHub would send mail to, and whether it is verified."""
    if not isinstance(entries, list):
        return None, False

    verified = [e for e in entries if isinstance(e, dict) and e.get("verified") and e.get("email")]
    for entry in verified:
        if entry.get("primary"):
            return str(entry["email"]).strip().lower(), True
    if verified:
        return str(verified[0]["email"]).strip().lower(), True
    return None, False


class GitHubProvider(OAuthProvider):
    name = OAuthProviderName.GITHUB
    label = "GitHub"

    @property
    def is_configured(self) -> bool:
        return bool(settings.github_client_id and settings.github_client_secret)

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": settings.github_client_id or "",
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
            "allow_signup": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def identity_from_code(self, code: str, redirect_uri: str) -> OAuthIdentity:
        async with http_client() as client:
            token = await post_form(
                client,
                TOKEN_URL,
                {
                    "code": code,
                    "client_id": settings.github_client_id or "",
                    "client_secret": settings.github_client_secret or "",
                    "redirect_uri": redirect_uri,
                },
                label=self.label,
            )

            access_token = token.get("access_token")
            if token.get("error") or not access_token:
                raise BadRequestError(
                    "GitHub rejected the sign-in. Please try again.",
                    code="oauth_exchange_failed",
                )

            access_token = str(access_token)
            profile = await get_json(client, USER_URL, token=access_token, label=self.label)
            if not isinstance(profile, dict) or not profile.get("id"):
                raise BadRequestError(
                    "GitHub did not tell us who signed in. Please try again.",
                    code="oauth_profile_unavailable",
                )

            email, email_verified = _primary_email(
                await get_json(client, EMAILS_URL, token=access_token, label=self.label)
            )

        return OAuthIdentity(
            provider=self.name,
            account_id=str(profile["id"]),
            email=email,
            email_verified=email_verified,
            full_name=profile.get("name") or None,
            avatar_url=profile.get("avatar_url") or None,
            username=profile.get("login") or None,
        )
