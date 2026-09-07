"""Telegram sign-in, via the Login Widget.

Telegram runs no OAuth server. The widget authenticates the user in Telegram
itself and hands the browser a small profile object signed with
`HMAC-SHA256(sha256(bot_token), data_check_string)`. There is nothing to
exchange: verifying that signature *is* the authentication, so the payload
arrives straight from the client and every field must be treated as hostile
until the digest checks out.

Telegram never gives an email address, so an account created this way has
`email = null` and can only sign in through Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.exceptions import BadRequestError, UnauthorizedError
from app.models.oauth import OAuthProviderName
from app.services.oauth.base import OAuthIdentity, OAuthProvider

# Present in the payload but not part of the signed data.
_HASH_FIELD = "hash"


def _data_check_string(payload: dict[str, Any]) -> str:
    """`key=value` for every field except `hash`, newline joined, key sorted.

    Built from whatever the client actually sent rather than from a fixed list
    of fields: Telegram is free to add one, and a field we dropped on the floor
    would break the digest for everybody.
    """
    return "\n".join(
        f"{key}={value}"
        for key, value in sorted(payload.items())
        if key != _HASH_FIELD and value is not None
    )


class TelegramProvider(OAuthProvider):
    name = OAuthProviderName.TELEGRAM
    label = "Telegram"
    supports_code_flow = False

    @property
    def is_configured(self) -> bool:
        return bool(settings.telegram_bot_token)

    def identity_from_widget(self, payload: dict[str, Any]) -> OAuthIdentity:
        if not settings.telegram_bot_token:
            raise BadRequestError(
                "Telegram sign-in is not configured.",
                code="oauth_provider_unconfigured",
            )

        received = str(payload.get(_HASH_FIELD) or "")
        secret_key = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).digest()
        expected = hmac.new(
            secret_key,
            _data_check_string(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, received):
            raise UnauthorizedError(
                "This Telegram sign-in could not be verified.",
                code="telegram_signature_invalid",
            )

        # The signature never expires on its own, so a payload captured from a
        # browser would work forever without this window.
        try:
            auth_date = datetime.fromtimestamp(int(payload["auth_date"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            raise BadRequestError(
                "This Telegram sign-in could not be verified.",
                code="telegram_signature_invalid",
            ) from None

        age = (datetime.now(UTC) - auth_date).total_seconds()
        if age > settings.telegram_auth_ttl_seconds or age < -300:
            raise BadRequestError(
                "This Telegram sign-in has expired. Please try again.",
                code="telegram_auth_expired",
            )

        first = str(payload.get("first_name") or "").strip()
        last = str(payload.get("last_name") or "").strip()
        username = payload.get("username")

        return OAuthIdentity(
            provider=self.name,
            account_id=str(payload["id"]),
            email=None,
            email_verified=False,
            full_name=" ".join(part for part in (first, last) if part) or None,
            avatar_url=str(payload["photo_url"]) if payload.get("photo_url") else None,
            username=f"@{username}" if username else None,
        )
