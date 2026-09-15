from __future__ import annotations

from httpx import AsyncClient

EMAIL = "ali@example.com"
PASSWORD = "Str0ngPassw0rd"


async def register(client: AsyncClient, email: str = EMAIL, password: str = PASSWORD):
    return await client.post("/auth/register", json={"email": email, "password": password})


async def register_and_verify(client: AsyncClient, email: str = EMAIL, password: str = PASSWORD):
    code = (await register(client, email, password)).json()["dev_code"]
    return await client.post("/auth/verify-otp", json={"email": email, "code": code})


# --- Registration ----------------------------------------------------------


async def test_register_issues_a_code(client: AsyncClient):
    response = await register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["email"] == EMAIL
    assert len(body["dev_code"]) == 6


async def test_register_normalizes_the_email(client: AsyncClient):
    await register(client, "  ALI@Example.COM  ")
    login = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    # Found the account (403 unverified), rather than missing it (401).
    assert login.status_code == 403


async def test_register_rejects_a_short_password(client: AsyncClient):
    response = await register(client, password="short")
    assert response.status_code == 422


async def test_register_rejects_a_malformed_email(client: AsyncClient):
    response = await register(client, email="not-an-email")
    assert response.status_code == 422


async def test_unverified_signup_can_be_retried(client: AsyncClient):
    await register(client)
    second = await register(client, password="AnotherPassw0rd")

    assert second.status_code == 201

    # The retry's password is the one that counts.
    code = second.json()["dev_code"]
    verified = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})
    assert verified.status_code == 200

    login = await client.post("/auth/login", json={"email": EMAIL, "password": "AnotherPassw0rd"})
    assert login.status_code == 200


async def test_register_conflicts_once_verified(client: AsyncClient):
    await register_and_verify(client)
    response = await register(client)

    assert response.status_code == 409
    assert response.json()["code"] == "email_already_registered"


# --- Verify OTP ------------------------------------------------------------


async def test_verify_returns_tokens_and_the_user(client: AsyncClient):
    response = await register_and_verify(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == EMAIL
    assert body["user"]["is_verified"] is True


async def test_wrong_code_is_rejected(client: AsyncClient):
    code = (await register(client)).json()["dev_code"]
    wrong = "000000" if code != "000000" else "111111"

    response = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": wrong})

    assert response.status_code == 400
    assert response.json()["code"] == "otp_invalid"
    # The Nuxt pages read the message from here.
    assert response.json()["statusMessage"] == "That code is not correct. Please try again."


async def test_a_code_works_only_once(client: AsyncClient):
    code = (await register(client)).json()["dev_code"]
    await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})

    replay = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})

    assert replay.status_code == 400
    assert replay.json()["code"] == "email_already_verified"


async def test_attempts_are_capped(client: AsyncClient):
    code = (await register(client)).json()["dev_code"]
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(5):  # OTP_MAX_ATTEMPTS
        await client.post("/auth/verify-otp", json={"email": EMAIL, "code": wrong})

    # The cap is checked before the code is compared, so even the right code
    # is refused now.
    response = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": code})

    assert response.status_code == 429
    assert response.json()["code"] == "otp_too_many_attempts"


async def test_verify_without_a_pending_signup(client: AsyncClient):
    response = await client.post("/auth/verify-otp", json={"email": "nobody@example.com", "code": "123456"})

    assert response.status_code == 400
    assert response.json()["code"] == "otp_not_found"


async def test_verify_rejects_a_non_numeric_code(client: AsyncClient):
    await register(client)
    response = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": "abcdef"})

    assert response.status_code == 422


# --- Resend ----------------------------------------------------------------


async def test_resend_replaces_the_previous_code(client: AsyncClient):
    first = (await register(client)).json()["dev_code"]
    second = (await client.post("/auth/resend-otp", json={"email": EMAIL})).json()["dev_code"]

    stale = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": first})
    assert stale.status_code == 400

    fresh = await client.post("/auth/verify-otp", json={"email": EMAIL, "code": second})
    assert fresh.status_code == 200


async def test_resend_needs_a_pending_signup(client: AsyncClient):
    response = await client.post("/auth/resend-otp", json={"email": "nobody@example.com"})

    assert response.status_code == 400
    assert response.json()["code"] == "otp_not_found"


async def test_resend_is_refused_once_verified(client: AsyncClient):
    await register_and_verify(client)
    response = await client.post("/auth/resend-otp", json={"email": EMAIL})

    # Same answer as an unknown address — it must not confirm the account exists.
    assert response.status_code == 400
    assert response.json()["code"] == "otp_not_found"


# --- Login -----------------------------------------------------------------


async def test_login_succeeds_after_verification(client: AsyncClient):
    await register_and_verify(client)
    response = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == EMAIL


async def test_login_is_case_insensitive_on_the_email(client: AsyncClient):
    await register_and_verify(client)
    response = await client.post("/auth/login", json={"email": "ALI@EXAMPLE.COM", "password": PASSWORD})

    assert response.status_code == 200


async def test_login_with_a_wrong_password(client: AsyncClient):
    await register_and_verify(client)
    response = await client.post("/auth/login", json={"email": EMAIL, "password": "WrongPassw0rd"})

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


async def test_unknown_email_is_indistinguishable_from_a_wrong_password(client: AsyncClient):
    await register_and_verify(client)

    unknown = await client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    wrong = await client.post("/auth/login", json={"email": EMAIL, "password": "WrongPassw0rd"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_unverified_account_cannot_log_in(client: AsyncClient):
    await register(client)
    response = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 403
    assert response.json()["code"] == "email_not_verified"


# --- Tokens ----------------------------------------------------------------


async def test_me_requires_a_token(client: AsyncClient):
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


async def test_me_rejects_a_garbage_token(client: AsyncClient):
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["code"] == "token_invalid"


async def test_me_returns_the_signed_in_user(client: AsyncClient):
    token = (await register_and_verify(client)).json()["access_token"]
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    # Offset-carrying, so clients don't read a UTC timestamp as local time.
    assert response.json()["created_at"].endswith(("Z", "+00:00"))


async def test_an_access_token_cannot_be_used_to_refresh(client: AsyncClient):
    access = (await register_and_verify(client)).json()["access_token"]
    response = await client.post("/auth/refresh", json={"refresh_token": access})

    assert response.status_code == 401
    assert response.json()["code"] == "token_invalid"


async def test_refresh_returns_a_usable_pair(client: AsyncClient):
    refresh_token = (await register_and_verify(client)).json()["refresh_token"]
    response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 200
    new_access = response.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


# --- bcrypt off the event loop ---------------------------------------------


async def test_hashing_a_password_does_not_stall_the_event_loop():
    """bcrypt is slow on purpose; the loop must not be the thing waiting.

    At the cost factor `gensalt()` picks, one hash measures ~220 ms. Spent
    inside an `async def` that is 220 ms in which this worker serves nobody —
    and `WORKER_COUNT` defaults to 1, so it is the whole API. Worse on login:
    the timing equaliser runs bcrypt for addresses that have no account, so an
    unauthenticated caller could hold the loop for as long as they kept
    guessing.

    The ticker is the assertion. It cannot advance while the loop is blocked,
    so a non-trivial tick count is proof the hash ran somewhere else.
    """
    import asyncio

    from app.core.security import hash_password_async, verify_password_async

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.02)          # let it settle
        ticks = 0
        digest = await hash_password_async(PASSWORD)
        assert await verify_password_async(PASSWORD, digest) is True
        assert await verify_password_async("wrong", digest) is False
    finally:
        beat.cancel()

    # Two hashes and a verify is ~600 ms of work; at 5 ms a tick that is well
    # over a hundred. Ten is the floor a heavily loaded CI box should still
    # clear, and a blocking implementation scores zero or one.
    assert ticks >= 10, f"event loop advanced only {ticks} times during bcrypt"


# --- the login throttle ----------------------------------------------------


async def test_login_is_throttled_per_address(client: AsyncClient, monkeypatch):
    """Wrong passwords cost the server ~220 ms of bcrypt each, and the endpoint
    runs it for unknown addresses too so that its answer cannot be used to
    discover which emails are registered. Without a budget that property is a
    free denial-of-service.
    """
    from app.core import throttle
    from app.core.cache import NullCache
    from app.core.config import settings

    counts: dict[str, int] = {}

    class Counting(NullCache):
        async def increment(self, key: str, ttl_seconds: int) -> int:
            counts[key] = counts.get(key, 0) + 1
            return counts[key]

    monkeypatch.setattr(throttle, "get_cache", Counting)
    monkeypatch.setattr(settings, "login_max_per_email", 3)
    monkeypatch.setattr(settings, "login_max_per_ip", 1000)

    await register_and_verify(client)

    for _ in range(3):
        wrong = await client.post(
            "/auth/login", json={"email": EMAIL, "password": "not-the-password"}
        )
        assert wrong.status_code == 401

    refused = await client.post(
        "/auth/login", json={"email": EMAIL, "password": "not-the-password"}
    )
    assert refused.status_code == 429
    assert refused.json()["code"] == "login_throttled"
    assert "Retry-After" in refused.headers

    # The budget is spent on the address, not on the password: the right one is
    # refused too, which is the point — an attacker who guesses correctly on
    # the next try still has to wait.
    assert (
        await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    ).status_code == 429


async def test_the_throttle_counts_before_the_password_is_checked(
    client: AsyncClient, monkeypatch
):
    """An address with no account must cost the same as one with.

    If the limiter ran after the lookup, an attacker could tell registered
    addresses from unregistered ones by which of them started answering 429 —
    reintroducing the enumeration oracle the endpoint is built to close.
    """
    from app.core import throttle
    from app.core.cache import NullCache
    from app.core.config import settings

    counts: dict[str, int] = {}

    class Counting(NullCache):
        async def increment(self, key: str, ttl_seconds: int) -> int:
            counts[key] = counts.get(key, 0) + 1
            return counts[key]

    monkeypatch.setattr(throttle, "get_cache", Counting)
    monkeypatch.setattr(settings, "login_max_per_email", 2)
    monkeypatch.setattr(settings, "login_max_per_ip", 1000)

    nobody = {"email": "ghost@example.com", "password": "whatever-it-is"}
    for _ in range(2):
        assert (await client.post("/auth/login", json=nobody)).status_code == 401

    refused = await client.post("/auth/login", json=nobody)
    assert refused.status_code == 429, "an unknown address must be throttled too"
