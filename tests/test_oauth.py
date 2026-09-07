from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.security import create_oauth_state
from app.services.oauth import OAuthIdentity
from app.services.oauth.github import GitHubProvider
from app.services.oauth.google import GoogleProvider

BOT_TOKEN = "123456:test-bot-token"
REDIRECT_URI = "http://localhost:3000/auth/callback"

EMAIL = "ali@example.com"
PASSWORD = "Str0ngPassw0rd"


@pytest.fixture(autouse=True)
def configured_providers(monkeypatch: pytest.MonkeyPatch):
    """Credentials for all three, so every provider looks enabled."""
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(settings, "github_client_id", "github-client-id")
    monkeypatch.setattr(settings, "github_client_secret", "github-client-secret")
    monkeypatch.setattr(settings, "telegram_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_bot_username", "synora_login_bot")
    monkeypatch.setattr(settings, "oauth_redirect_uris", REDIRECT_URI)


def fake_identity(monkeypatch: pytest.MonkeyPatch, identity: OAuthIdentity, provider=GoogleProvider):
    """Stand in for the provider's token + profile round-trip."""

    async def _identity(self, code: str, redirect_uri: str) -> OAuthIdentity:  # noqa: ARG001
        assert redirect_uri == REDIRECT_URI
        return identity

    monkeypatch.setattr(provider, "identity_from_code", _identity)


def google_identity(**overrides: Any) -> OAuthIdentity:
    from app.models.oauth import OAuthProviderName

    defaults: dict[str, Any] = {
        "provider": OAuthProviderName.GOOGLE,
        "account_id": "google-123",
        "email": EMAIL,
        "email_verified": True,
        "full_name": "Ali Valiyev",
        "avatar_url": "https://example.com/ali.png",
        "username": EMAIL,
    }
    return OAuthIdentity(**{**defaults, **overrides})


def telegram_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 987654321,
        "first_name": "Ali",
        "last_name": "Valiyev",
        "username": "ali",
        "photo_url": "https://t.me/i/userpic/320/ali.jpg",
        "auth_date": int(time.time()),
    }
    payload.update(overrides)
    payload.pop("hash", None)

    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if v is not None)
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    payload["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {**payload, **{k: v for k, v in overrides.items() if k == "hash"}}


async def google_sign_in(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, **overrides: Any):
    fake_identity(monkeypatch, google_identity(**overrides))
    state = (await client.get("/auth/oauth/google/authorize")).json()["state"]
    return await client.post("/auth/oauth/google/callback", json={"code": "code", "state": state})


# --- Discovery -------------------------------------------------------------


async def test_providers_lists_what_is_configured(client: AsyncClient):
    body = (await client.get("/auth/oauth/providers")).json()
    names = {provider["name"] for provider in body["providers"]}

    assert names == {"google", "github", "telegram"}
    telegram = next(p for p in body["providers"] if p["name"] == "telegram")
    assert telegram["supports_code_flow"] is False
    assert telegram["bot_username"] == "synora_login_bot"


async def test_providers_hides_an_unconfigured_one(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", None)
    body = (await client.get("/auth/oauth/providers")).json()

    assert "github" not in {provider["name"] for provider in body["providers"]}


async def test_unconfigured_provider_is_503(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "google_client_secret", None)
    response = await client.get("/auth/oauth/google/authorize")

    assert response.status_code == 503
    assert response.json()["code"] == "oauth_provider_unconfigured"


async def test_unknown_provider_is_404(client: AsyncClient):
    response = await client.get("/auth/oauth/facebook/authorize")

    assert response.status_code == 404
    assert response.json()["code"] == "oauth_provider_unknown"


# --- Authorize -------------------------------------------------------------


async def test_authorize_returns_a_consent_url(client: AsyncClient):
    body = (await client.get("/auth/oauth/google/authorize")).json()

    assert body["authorization_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-client-id" in body["authorization_url"]
    assert body["redirect_uri"] == REDIRECT_URI
    assert body["state"]


async def test_authorize_refuses_an_unlisted_redirect_uri(client: AsyncClient):
    response = await client.get(
        "/auth/oauth/google/authorize", params={"redirect_uri": "https://evil.example/steal"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_redirect_uri_not_allowed"


async def test_link_intent_needs_a_token(client: AsyncClient):
    response = await client.get("/auth/oauth/google/authorize", params={"intent": "link"})

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --- Callback --------------------------------------------------------------


async def test_callback_creates_an_account_and_signs_in(client: AsyncClient, monkeypatch):
    response = await google_sign_in(client, monkeypatch)

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["email"] == EMAIL
    assert body["user"]["is_verified"] is True
    assert body["user"]["has_password"] is False
    assert body["user"]["full_name"] == "Ali Valiyev"


async def test_second_sign_in_reuses_the_same_account(client: AsyncClient, monkeypatch):
    first = (await google_sign_in(client, monkeypatch)).json()
    second = (await google_sign_in(client, monkeypatch)).json()

    assert first["user"]["id"] == second["user"]["id"]


async def test_a_changed_provider_email_keeps_the_account(client: AsyncClient, monkeypatch):
    first = (await google_sign_in(client, monkeypatch)).json()
    second = (await google_sign_in(client, monkeypatch, email="new@example.com")).json()

    # Matched on the provider's account id, not the address.
    assert second["user"]["id"] == first["user"]["id"]


async def test_verified_provider_email_joins_the_password_account(client: AsyncClient, monkeypatch):
    code = (
        await client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    ).json()["dev_code"]
    verified = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})

    oauth = (await google_sign_in(client, monkeypatch)).json()

    assert oauth["user"]["id"] == verified.json()["user"]["id"]
    # The password still works — the provider was added, not substituted.
    assert oauth["user"]["has_password"] is True
    login = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200


async def test_claiming_an_unfinished_signup_drops_its_password(client: AsyncClient, monkeypatch):
    # Somebody registered this address but never proved they could read it.
    await client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})

    oauth = (await google_sign_in(client, monkeypatch)).json()
    assert oauth["user"]["has_password"] is False

    # So the password they chose must not be a way in.
    login = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 403
    assert login.json()["code"] == "password_login_unavailable"
    assert "Google" in login.json()["detail"]


async def test_unverified_provider_email_is_refused(client: AsyncClient, monkeypatch):
    response = await google_sign_in(client, monkeypatch, email_verified=False)

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_email_unverified"


async def test_callback_rejects_a_tampered_state(client: AsyncClient, monkeypatch):
    fake_identity(monkeypatch, google_identity())
    response = await client.post(
        "/auth/oauth/google/callback", json={"code": "code", "state": "not-a-state"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_state_invalid"


async def test_a_state_is_bound_to_its_provider(client: AsyncClient, monkeypatch):
    fake_identity(monkeypatch, google_identity(), provider=GitHubProvider)
    state = (await client.get("/auth/oauth/google/authorize")).json()["state"]

    response = await client.post(
        "/auth/oauth/github/callback", json={"code": "code", "state": state}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_state_invalid"


async def test_a_link_state_cannot_open_a_session(client: AsyncClient, monkeypatch):
    fake_identity(monkeypatch, google_identity())
    state = create_oauth_state("google", REDIRECT_URI, link_user_id="someone")

    response = await client.post(
        "/auth/oauth/google/callback", json={"code": "code", "state": state}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_state_invalid"


# --- Telegram --------------------------------------------------------------


async def test_telegram_login_creates_an_emailless_account(client: AsyncClient):
    response = await client.post("/auth/oauth/telegram/callback", json=telegram_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] is None
    assert body["user"]["has_password"] is False
    assert body["user"]["full_name"] == "Ali Valiyev"
    assert body["access_token"]


async def test_telegram_login_is_idempotent(client: AsyncClient):
    first = (await client.post("/auth/oauth/telegram/callback", json=telegram_payload())).json()
    second = (await client.post("/auth/oauth/telegram/callback", json=telegram_payload())).json()

    assert first["user"]["id"] == second["user"]["id"]


async def test_telegram_rejects_a_bad_signature(client: AsyncClient):
    payload = telegram_payload()
    payload["first_name"] = "Somebody Else"

    response = await client.post("/auth/oauth/telegram/callback", json=payload)

    assert response.status_code == 401
    assert response.json()["code"] == "telegram_signature_invalid"


async def test_telegram_rejects_a_stale_payload(client: AsyncClient):
    payload = telegram_payload(auth_date=int(time.time()) - settings.telegram_auth_ttl_seconds - 60)

    response = await client.post("/auth/oauth/telegram/callback", json=payload)

    assert response.status_code == 400
    assert response.json()["code"] == "telegram_auth_expired"


async def test_telegram_signs_extra_fields_too(client: AsyncClient):
    # A field we do not model still has to reach the digest.
    payload = telegram_payload(language_code="uz")

    response = await client.post("/auth/oauth/telegram/callback", json=payload)

    assert response.status_code == 200


# --- Linking ---------------------------------------------------------------


async def register_and_verify(client: AsyncClient) -> str:
    code = (
        await client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    ).json()["dev_code"]
    tokens = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})
    return tokens.json()["access_token"]


async def link_google(client: AsyncClient, monkeypatch, token: str, **overrides: Any):
    fake_identity(monkeypatch, google_identity(**overrides))
    auth = {"Authorization": f"Bearer {token}"}
    state = (
        await client.get(
            "/auth/oauth/google/authorize", params={"intent": "link"}, headers=auth
        )
    ).json()["state"]
    return await client.post(
        "/auth/oauth/google/link", json={"code": "code", "state": state}, headers=auth
    )


async def test_link_then_list(client: AsyncClient, monkeypatch):
    token = await register_and_verify(client)

    response = await link_google(client, monkeypatch, token, email="other@example.com")

    assert response.status_code == 200
    accounts = response.json()["accounts"]
    assert [account["provider"] for account in accounts] == ["google"]
    assert accounts[0]["provider_account_id"] == "google-123"
    # Serialised as UTC, not as a naive timestamp the client would read as local.
    assert accounts[0]["linked_at"].endswith("Z") or "+00:00" in accounts[0]["linked_at"]

    listed = await client.get(
        "/auth/oauth/accounts", headers={"Authorization": f"Bearer {token}"}
    )
    assert listed.json()["accounts"] == accounts


async def test_linking_someone_elses_provider_account_conflicts(client: AsyncClient, monkeypatch):
    # The Google account already signs in as its own Synora account.
    await google_sign_in(client, monkeypatch, email="taken@example.com")
    token = await register_and_verify(client)

    response = await link_google(client, monkeypatch, token, email="taken@example.com")

    assert response.status_code == 409
    assert response.json()["code"] == "oauth_account_already_linked"


async def test_a_second_google_account_conflicts(client: AsyncClient, monkeypatch):
    token = await register_and_verify(client)
    await link_google(client, monkeypatch, token, email="other@example.com")

    response = await link_google(
        client, monkeypatch, token, account_id="google-999", email="second@example.com"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "oauth_provider_already_linked"


async def test_a_sign_in_state_cannot_link(client: AsyncClient, monkeypatch):
    token = await register_and_verify(client)
    fake_identity(monkeypatch, google_identity(email="other@example.com"))
    state = (await client.get("/auth/oauth/google/authorize")).json()["state"]

    response = await client.post(
        "/auth/oauth/google/link",
        json={"code": "code", "state": state},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_state_invalid"


async def test_unlink(client: AsyncClient, monkeypatch):
    token = await register_and_verify(client)
    await link_google(client, monkeypatch, token, email="other@example.com")
    auth = {"Authorization": f"Bearer {token}"}

    response = await client.delete("/auth/oauth/google/link", headers=auth)

    assert response.status_code == 200
    assert (await client.get("/auth/oauth/accounts", headers=auth)).json()["accounts"] == []


async def test_unlink_refuses_to_lock_the_user_out(client: AsyncClient, monkeypatch):
    token = (await google_sign_in(client, monkeypatch)).json()["access_token"]

    response = await client.delete(
        "/auth/oauth/google/link", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "oauth_last_login_method"


async def test_unlink_needs_a_linked_provider(client: AsyncClient):
    token = await register_and_verify(client)

    response = await client.delete(
        "/auth/oauth/github/link", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "oauth_account_not_linked"


# --- Setting a first password ----------------------------------------------


async def test_a_provider_account_can_set_a_password(client: AsyncClient, monkeypatch):
    await google_sign_in(client, monkeypatch)

    code = (await client.post("/auth/forgot-password", json={"email": EMAIL})).json()["dev_code"]
    reset = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": code})
    token = reset.json()["reset_token"]

    done = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": "N3wStr0ngPassw0rd"},
    )
    assert done.status_code == 200

    login = await client.post(
        "/auth/login", json={"email": EMAIL, "password": "N3wStr0ngPassw0rd"}
    )
    assert login.status_code == 200
    assert login.json()["user"]["has_password"] is True
