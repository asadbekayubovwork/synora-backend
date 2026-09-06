from __future__ import annotations

from httpx import AsyncClient

EMAIL = "ali@example.com"
PASSWORD = "Str0ngPassw0rd"
NEW_PASSWORD = "N3wStr0ngPassw0rd"


async def register_and_verify(client: AsyncClient, email: str = EMAIL, password: str = PASSWORD):
    code = (await client.post(
        "/auth/register", json={"email": email, "password": password}
    )).json()["dev_code"]
    return await client.post("/auth/verify-otp", json={"email": email, "code": code})


async def request_reset(client: AsyncClient, email: str = EMAIL) -> str | None:
    return (await client.post("/auth/forgot-password", json={"email": email})).json()["dev_code"]


async def reset_token_for(client: AsyncClient, email: str = EMAIL) -> str:
    code = await request_reset(client, email)
    response = await client.post("/auth/verify-reset-otp", json={"email": email, "code": code})
    return response.json()["reset_token"]


# --- Step 1: request a code ------------------------------------------------


async def test_a_reset_code_is_issued_for_a_real_account(client: AsyncClient):
    await register_and_verify(client)
    response = await client.post("/auth/forgot-password", json={"email": EMAIL})

    assert response.status_code == 200
    assert len(response.json()["dev_code"]) == 6


async def test_an_unknown_email_is_answered_identically(client: AsyncClient):
    await register_and_verify(client)

    known = await client.post("/auth/forgot-password", json={"email": EMAIL})
    unknown = await client.post("/auth/forgot-password", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    # No code exists to leak for an address with no account.
    assert unknown.json()["dev_code"] is None


async def test_an_unverified_signup_gets_no_reset_code(client: AsyncClient):
    await client.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    response = await client.post("/auth/forgot-password", json={"email": EMAIL})

    assert response.status_code == 200
    assert response.json()["dev_code"] is None


async def test_requesting_again_replaces_the_previous_code(client: AsyncClient):
    await register_and_verify(client)
    first = await request_reset(client)
    second = await request_reset(client)

    stale = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": first})
    assert stale.status_code == 400

    fresh = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": second})
    assert fresh.status_code == 200


# --- Step 2: verify the code -----------------------------------------------


async def test_a_correct_code_returns_a_reset_token(client: AsyncClient):
    await register_and_verify(client)
    code = await request_reset(client)

    response = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": code})

    assert response.status_code == 200
    assert response.json()["reset_token"]
    assert response.json()["expires_in"] == 15 * 60


async def test_a_wrong_reset_code_is_rejected(client: AsyncClient):
    await register_and_verify(client)
    code = await request_reset(client)
    wrong = "000000" if code != "000000" else "111111"

    response = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": wrong})

    assert response.status_code == 400
    assert response.json()["code"] == "otp_invalid"


async def test_the_registration_code_does_not_work_for_a_reset(client: AsyncClient):
    # Registration and reset codes live in separate purposes; one must not
    # stand in for the other.
    signup_code = (await client.post(
        "/auth/register", json={"email": EMAIL, "password": PASSWORD}
    )).json()["dev_code"]

    response = await client.post(
        "/auth/verify-reset-otp", json={"email": EMAIL, "code": signup_code}
    )

    assert response.status_code == 400


# --- Step 3: set the new password ------------------------------------------


async def test_the_password_is_changed(client: AsyncClient):
    await register_and_verify(client)
    token = await reset_token_for(client)

    response = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": NEW_PASSWORD},
    )

    assert response.status_code == 200

    assert (await client.post(
        "/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD}
    )).status_code == 200
    assert (await client.post(
        "/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )).status_code == 401


async def test_a_reset_token_works_only_once(client: AsyncClient):
    await register_and_verify(client)
    token = await reset_token_for(client)

    first = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": NEW_PASSWORD},
    )
    assert first.status_code == 200

    replay = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": "Y3tAnotherPassw0rd"},
    )

    assert replay.status_code == 400
    assert replay.json()["code"] == "reset_token_used"


async def test_a_garbage_reset_token_is_rejected(client: AsyncClient):
    await register_and_verify(client)

    response = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": "not-a-jwt", "password": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "reset_token_invalid"


async def test_an_access_token_cannot_stand_in_for_a_reset_token(client: AsyncClient):
    access = (await register_and_verify(client)).json()["access_token"]

    response = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": access, "password": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "reset_token_invalid"


async def test_a_reset_token_cannot_be_used_on_another_account(client: AsyncClient):
    await register_and_verify(client)
    await register_and_verify(client, "someone-else@example.com")

    token = await reset_token_for(client)

    response = await client.post(
        "/auth/reset-password",
        json={
            "email": "someone-else@example.com",
            "reset_token": token,
            "password": NEW_PASSWORD,
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "reset_token_invalid"


async def test_a_short_new_password_is_rejected(client: AsyncClient):
    await register_and_verify(client)
    token = await reset_token_for(client)

    response = await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": "short"},
    )

    assert response.status_code == 422


async def test_the_reset_code_is_spent_after_the_password_changes(client: AsyncClient):
    await register_and_verify(client)
    code = await request_reset(client)
    token = (await client.post(
        "/auth/verify-reset-otp", json={"email": EMAIL, "code": code}
    )).json()["reset_token"]
    await client.post(
        "/auth/reset-password",
        json={"email": EMAIL, "reset_token": token, "password": NEW_PASSWORD},
    )

    replay = await client.post("/auth/verify-reset-otp", json={"email": EMAIL, "code": code})

    assert replay.status_code == 400
