from __future__ import annotations

from httpx import AsyncClient

PATHS = [
    "/api/v1/auth/register",
    "/api/v1/auth/verify-otp",
    "/api/v1/auth/resend-otp",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/me",
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
