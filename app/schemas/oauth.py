from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ConfigDict, Field, field_validator

from app.models.oauth import OAuthProviderName
from app.schemas.auth import _Schema

# --- Requests --------------------------------------------------------------


class OAuthCallbackRequest(_Schema):
    """The two values the provider hands back on the redirect."""

    code: str = Field(min_length=1, max_length=4096, examples=["4/0AeanS0b…"])
    state: str = Field(
        min_length=1,
        max_length=4096,
        description="From `GET /auth/oauth/{provider}/authorize`.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…"],
    )


class TelegramLoginRequest(_Schema):
    """Exactly what the Telegram Login Widget passes to its callback.

    `extra="allow"` on purpose: the signature covers every field Telegram sent,
    so a field we did not model still has to reach the digest. Pass the widget
    object through untouched.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="allow")

    id: int = Field(description="The Telegram user id.", examples=[123456789])
    auth_date: int = Field(description="Unix seconds, as the widget sent it.", examples=[1735689600])
    hash: str = Field(description="The widget's HMAC-SHA256 signature.", examples=["a3f1…"])
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


# --- Responses -------------------------------------------------------------


class OAuthProviderInfo(_Schema):
    name: OAuthProviderName
    label: str
    supports_code_flow: bool = Field(
        description="False for Telegram, which is a widget rather than a redirect.",
    )
    bot_username: str | None = Field(
        default=None,
        description="Telegram only — the bot the login widget must be rendered for.",
    )


class OAuthProvidersResponse(_Schema):
    ok: bool = True
    providers: list[OAuthProviderInfo]


class OAuthAuthorizeResponse(_Schema):
    ok: bool = True
    provider: OAuthProviderName
    authorization_url: str = Field(description="Send the browser here.")
    state: str = Field(description="Comes back on the redirect; post it to `/callback` unchanged.")
    redirect_uri: str = Field(description="The URI the provider will redirect to.")
    expires_in: int = Field(description="Seconds before the state stops being accepted.")


class OAuthAccountResponse(_Schema):
    provider: OAuthProviderName
    provider_account_id: str
    email: str | None = None
    username: str | None = Field(default=None, description="The handle to show, e.g. `@ali`.")
    linked_at: datetime

    @field_validator("linked_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # Same reason as `UserResponse.created_at`: SQLite hands timestamps back
        # without an offset, and a naive one reads as local time on the client.
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class OAuthAccountsResponse(_Schema):
    ok: bool = True
    accounts: list[OAuthAccountResponse]
