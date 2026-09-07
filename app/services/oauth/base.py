"""What every provider looks like from the outside, plus the HTTP plumbing.

A provider's whole job is to turn whatever the browser came back with into an
`OAuthIdentity`. Everything after that — matching it to a user, creating one,
issuing tokens — is the same for all three and lives in `oauth_service`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import BadGatewayError, BadRequestError
from app.models.oauth import OAuthProviderName


@dataclass(frozen=True)
class OAuthIdentity:
    """Who the provider says this is, normalised across providers."""

    provider: OAuthProviderName
    account_id: str
    email: str | None = None
    # Whether the provider states it has verified the address. We only trust an
    # email — to match or create an account by — when this is true.
    email_verified: bool = False
    full_name: str | None = None
    avatar_url: str | None = None
    username: str | None = None


class OAuthProvider(ABC):
    name: OAuthProviderName
    label: str

    # Telegram signs a payload in the browser rather than handing back a code,
    # so it has no authorization URL to send anyone to.
    supports_code_flow: bool = True

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether the .env carries the credentials this provider needs."""

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        raise BadRequestError(
            f"{self.label} does not use a redirect flow.",
            code="oauth_flow_unsupported",
        )

    async def identity_from_code(self, code: str, redirect_uri: str) -> OAuthIdentity:
        raise BadRequestError(
            f"{self.label} does not use a redirect flow.",
            code="oauth_flow_unsupported",
        )


def http_client() -> httpx.AsyncClient:
    # No redirect following: a token endpoint that wants to redirect us is not
    # a token endpoint we should be posting a client secret to.
    return httpx.AsyncClient(timeout=settings.oauth_http_timeout_seconds, follow_redirects=False)


def _decode(response: httpx.Response, label: str) -> Any:
    if response.status_code >= 400:
        # Deliberately not passed through: a provider's error text names our
        # client id and the code that failed, neither of which the caller needs.
        raise BadRequestError(
            f"{label} rejected the sign-in. Please try again.",
            code="oauth_exchange_failed",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise BadGatewayError(
            f"{label} sent a response we could not read.",
            code="oauth_provider_unreadable",
        ) from exc


async def post_form(
    client: httpx.AsyncClient,
    url: str,
    data: dict[str, str],
    *,
    label: str,
) -> dict[str, Any]:
    try:
        response = await client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise BadGatewayError(
            f"Could not reach {label}. Please try again.",
            code="oauth_provider_unreachable",
        ) from exc

    body = _decode(response, label)
    if not isinstance(body, dict):
        raise BadGatewayError(
            f"{label} sent a response we could not read.",
            code="oauth_provider_unreadable",
        )
    return body


async def get_json(client: httpx.AsyncClient, url: str, *, token: str, label: str) -> Any:
    try:
        response = await client.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise BadGatewayError(
            f"Could not reach {label}. Please try again.",
            code="oauth_provider_unreachable",
        ) from exc
    return _decode(response, label)
