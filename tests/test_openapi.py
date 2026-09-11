from __future__ import annotations

from httpx import AsyncClient

PATHS = [
    "/api/v1/auth/register",
    "/api/v1/auth/verify-otp",
    "/api/v1/auth/resend-otp",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/me",
    "/api/v1/auth/oauth/providers",
    "/api/v1/auth/oauth/accounts",
    "/api/v1/auth/oauth/{provider}/authorize",
    "/api/v1/auth/oauth/{provider}/callback",
    "/api/v1/auth/oauth/{provider}/link",
    "/api/v1/auth/oauth/telegram/callback",
    "/api/v1/auth/oauth/telegram/link",
    "/api/v1/wallet",
    "/api/v1/wallet/transactions",
    "/api/v1/tts/speech",
    "/api/v1/tts/estimate",
    "/api/v1/tts/voices",
    "/api/v1/tts/voices/{voice_id}",
    "/api/v1/tts/batch",
    "/api/v1/tts/batch/{job_id}",
    "/api/v1/tts/batch/{job_id}/results",
    "/api/v1/tts/recordings",
    "/api/v1/tts/recordings/{recording_id}",
    "/api/v1/tts/recordings/{recording_id}/audio",
    "/api/v1/usage",
    "/api/v1/admin/wallets/{user_id}",
    "/api/v1/admin/wallets/{user_id}/credits",
    "/api/v1/admin/wallets/{user_id}/freeze",
    "/api/v1/admin/wallets/{user_id}/unfreeze",
    "/api/v1/admin/reconcile",
    # Outside `api_prefix` on purpose, so nginx can allowlist the whole path.
    "/internal/v1/health",
    "/internal/v1/debug/echo-signature",
]


async def test_openapi_documents_every_endpoint(client: AsyncClient):
    schema = (await client.get("http://test/openapi.json")).json()

    for path in PATHS:
        assert path in schema["paths"], f"{path} is missing from the OpenAPI schema"


async def test_swagger_ui_is_served(client: AsyncClient):
    response = await client.get("http://test/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text


async def test_error_responses_are_documented(client: AsyncClient):
    schema = (await client.get("http://test/openapi.json")).json()
    login = schema["paths"]["/api/v1/auth/login"]["post"]["responses"]

    assert {"401", "403", "422", "429"} <= set(login)


async def test_the_internal_api_is_documented_rather_than_hidden(client: AsyncClient):
    """Auth is the control on `/internal/v1`, not obscurity.

    The other team should be reading the real schema, and a route that is not
    in it is a route nobody reviews.
    """
    schema = (await client.get("http://test/openapi.json")).json()
    health = schema["paths"]["/internal/v1/health"]["get"]

    assert health["tags"] == ["Internal"]
    assert {"401", "403", "404"} <= set(health["responses"])


async def test_admin_routes_document_their_refusals(client: AsyncClient):
    schema = (await client.get("http://test/openapi.json")).json()
    credits = schema["paths"]["/api/v1/admin/wallets/{user_id}/credits"]["post"]["responses"]

    assert {"400", "401", "403", "404", "409", "422"} <= set(credits)


async def test_the_two_idempotency_refusals_are_both_published(client: AsyncClient):
    """The half of a `409` that lives in the contract rather than in the code.

    An `Idempotency-Key` that has been spent and one that names a different
    request are both `409` and mean opposite things to a retry loop — "stop,
    you have already paid for this" against "send a fresh key for this text" —
    and for one release the page described only the second while the code
    answered it for both. An SDK author reading that documentation writes the
    loop that opens a second session and pays twice for one synthesis, which is
    the case the header exists to prevent. So both codes are named on the
    response, and the header says which one a spent key gets.
    """
    schema = (await client.get("http://test/openapi.json")).json()
    speech = schema["paths"]["/api/v1/tts/speech"]["post"]

    conflict = speech["responses"]["409"]["description"]
    assert "tts_idempotency_spent" in conflict
    assert "tts_idempotency_conflict" in conflict

    (header,) = [
        parameter
        for parameter in speech["parameters"]
        if parameter["name"] == "Idempotency-Key"
    ]
    assert "tts_idempotency_spent" in header["description"], (
        "the contract has to say a finished key is refused, not merely that a "
        "retry is safe"
    )
