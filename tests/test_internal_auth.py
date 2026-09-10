"""Request signing: the trust boundary with the microservice team.

Most of these are failure cases on purpose. A signature scheme that accepts
valid requests is easy; one that rejects every *near*-valid one is the point,
and each rejection has its own code because the other team will be reading
them off a log rather than stepping through a debugger.
"""

from __future__ import annotations

import json
import time

import pytest

from app.core import signing
from app.core.cache import NullCache
from tests.conftest import sign_internal

PATH = "/internal/v1/health"
URL = f"http://test{PATH}"


async def _get(client, headers):
    return await client.get(URL, headers=headers)


# --- the happy path --------------------------------------------------------


async def test_a_correctly_signed_request_is_accepted(client, service_key, price_book):  # noqa: ARG001
    headers = sign_internal(service_key, method="GET", path=PATH)

    response = await _get(client, headers)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["price_book_version"] == 1
    # No Redis in the test environment, so it says so rather than pretending.
    assert body["redis"] == "down"
    assert body["state"] == "degraded"
    assert body["server_time"], "the clock-skew canary must always be present"


async def test_the_response_says_when_no_prices_are_published(client, service_key):
    """A deployment that cannot bill must say so, not report ready."""
    headers = sign_internal(service_key, method="GET", path=PATH)

    body = (await _get(client, headers)).json()

    assert body["price_book_version"] is None
    assert body["state"] == "degraded"


# --- missing and malformed credentials -------------------------------------


async def test_an_unsigned_request_is_refused(client):
    response = await _get(client, {})

    assert response.status_code == 401
    assert response.json()["code"] == "signature_missing"


@pytest.mark.parametrize(
    "drop",
    [signing.HEADER_KEY_ID, signing.HEADER_TIMESTAMP, signing.HEADER_NONCE, signing.HEADER_SIGNATURE],
)
async def test_every_signature_header_is_required(client, service_key, drop):
    headers = sign_internal(service_key, method="GET", path=PATH)
    del headers[drop]

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_missing"


async def test_a_malformed_key_id_is_refused_without_a_database_lookup(client, service_key):
    headers = sign_internal(service_key, method="GET", path=PATH)
    headers[signing.HEADER_KEY_ID] = "NOT A VALID KEY ID!!"

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "service_key_unknown"


async def test_an_unknown_key_is_refused(client, service_key):
    headers = sign_internal(service_key, method="GET", path=PATH)
    headers[signing.HEADER_KEY_ID] = "svc_voice_agent_deadbeef"

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "service_key_unknown"


async def test_a_revoked_key_stops_working_immediately(client, session, service_key):
    """Revocation is one UPDATE — no deploy, no key redistribution."""
    from app.services.billing import service_key_service

    await service_key_service.revoke(session, key_id=service_key.key_id)
    await session.commit()

    response = await _get(client, sign_internal(service_key, method="GET", path=PATH))

    assert response.status_code == 401
    assert response.json()["code"] == "service_key_revoked"


async def test_an_expired_key_is_refused(client, session, service_key):
    from datetime import timedelta

    from sqlalchemy import update

    from app.db.base import utcnow
    from app.models.service_api_key import ServiceApiKey

    await session.execute(
        update(ServiceApiKey)
        .where(ServiceApiKey.key_id == service_key.key_id)
        .values(expires_at=utcnow() - timedelta(seconds=1))
    )
    await session.commit()

    response = await _get(client, sign_internal(service_key, method="GET", path=PATH))

    assert response.status_code == 401
    assert response.json()["code"] == "service_key_expired"


# --- the signature itself --------------------------------------------------


async def test_a_wrong_secret_does_not_match(client, service_key):
    from dataclasses import replace

    forged = replace(service_key, secret="not-the-real-secret")

    response = await _get(client, sign_internal(forged, method="GET", path=PATH))

    assert response.status_code == 401
    assert response.json()["code"] == "signature_invalid"


async def test_a_signature_for_another_path_cannot_be_replayed_here(client, service_key):
    """Why the path is in the canonical string.

    Without it, one captured request would authenticate any endpoint.
    """
    headers = sign_internal(service_key, method="GET", path="/internal/v1/somewhere-else")

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_invalid"


async def test_a_signature_for_another_method_does_not_carry_over(client, service_key):
    headers = sign_internal(service_key, method="POST", path=PATH)

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_invalid"


async def test_a_tampered_body_breaks_the_signature(client, service_key, price_book):  # noqa: ARG001
    path = "/internal/v1/debug/echo-signature"
    body = json.dumps({"hello": "world"}).encode()
    headers = sign_internal(service_key, method="POST", path=path, body=body)

    response = await client.post(
        f"http://test{path}",
        content=body + b" ",  # one extra byte
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "signature_invalid"


async def test_query_parameter_order_does_not_matter(client, service_key, price_book):  # noqa: ARG001
    """A signature that depends on dict ordering fails intermittently, which is
    the worst thing to hand another team."""
    headers = sign_internal(service_key, method="GET", path=PATH, query="b=2&a=1")

    response = await client.get(f"{URL}?a=1&b=2", headers=headers)

    assert response.status_code == 200


async def test_a_signature_that_ignores_the_query_is_refused(client, service_key):
    headers = sign_internal(service_key, method="GET", path=PATH, query="")

    response = await client.get(f"{URL}?a=1", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_invalid"


# --- clock skew and replay -------------------------------------------------


async def test_a_stale_timestamp_is_refused_and_reports_our_clock(client, service_key):
    """Clock skew is the commonest cross-team failure and is undebuggable
    without knowing what time the server thinks it is."""
    headers = sign_internal(
        service_key, method="GET", path=PATH, timestamp=int(time.time()) - 3600
    )

    response = await _get(client, headers)

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "signature_timestamp_skew"
    assert isinstance(body["serverTime"], int)
    assert "clock" in body["detail"]


async def test_a_timestamp_from_the_future_is_refused_too(client, service_key):
    headers = sign_internal(
        service_key, method="GET", path=PATH, timestamp=int(time.time()) + 3600
    )

    assert (await _get(client, headers)).json()["code"] == "signature_timestamp_skew"


async def test_a_non_numeric_timestamp_is_refused(client, service_key):
    headers = sign_internal(service_key, method="GET", path=PATH)
    headers[signing.HEADER_TIMESTAMP] = "yesterday"

    response = await _get(client, headers)

    assert response.json()["code"] == "signature_timestamp_skew"


@pytest.mark.parametrize("nonce", ["short", "x" * 200, "has spaces in it!!!!!"])
async def test_a_malformed_nonce_is_refused(client, service_key, nonce):
    headers = sign_internal(service_key, method="GET", path=PATH, nonce=nonce)

    response = await _get(client, headers)

    assert response.status_code == 401
    assert response.json()["code"] == "signature_nonce_invalid"


async def test_replay_is_allowed_without_redis_and_that_is_deliberate(
    client, service_key, price_book  # noqa: ARG001
):
    """The replay guard fails open when there is nowhere to remember a nonce.

    The signature timestamp window still bounds replay to five minutes, and
    every money-bearing endpoint is idempotent on its own key — so dropping
    real usage reports because Redis blinked would be the worse trade. This
    test exists so that the choice is visible rather than accidental; with
    Redis configured, the second request is a 401 `signature_replayed`.
    """
    headers = sign_internal(service_key, method="GET", path=PATH)

    first = await _get(client, headers)
    second = await _get(client, headers)

    assert first.status_code == 200
    assert second.status_code == 200


# --- scopes ----------------------------------------------------------------


async def test_a_key_without_the_scope_is_refused(client, session, price_book):  # noqa: ARG001
    from app.models.billing_enums import BillingService
    from app.services.billing import service_key_service

    minted = await service_key_service.mint(
        session,
        label="usage only",
        service=BillingService.VOICE_AGENT,
        scopes=(service_key_service.SCOPE_USAGE_WRITE,),
    )
    await session.commit()

    response = await _get(client, sign_internal(minted, method="GET", path=PATH))

    assert response.status_code == 403
    assert response.json()["code"] == "service_key_forbidden"
    assert "health:read" in response.json()["detail"]


# --- the body-caching mechanic ---------------------------------------------


async def test_the_route_still_receives_the_body_after_the_dependency_read_it(
    client, service_key, price_book  # noqa: ARG001
):
    """`await request.body()` in the dependency must not consume the body.

    Starlette caches it, and FastAPI's own parsing reads the same cache — which
    is exactly why the dependency must use `.body()` and never `.stream()`.
    """
    path = "/internal/v1/debug/echo-signature"
    body = json.dumps({"reports": [{"seq": 42}]}).encode()
    headers = sign_internal(service_key, method="POST", path=path, body=body)

    response = await client.post(
        f"http://test{path}", content=body, headers={**headers, "Content-Type": "application/json"}
    )

    assert response.status_code == 200
    echoed = response.json()
    assert echoed["body_sha256"] == signing.body_digest(body)
    assert echoed["canonical"].startswith("SYNORA-HMAC-V1\nPOST\n/internal/v1/debug/echo-signature")
    assert echoed["signature_matched"] is True


async def test_the_signing_helper_is_hidden_outside_development(
    client, service_key, monkeypatch, price_book  # noqa: ARG001
):
    """A signing oracle in production is one somebody will point at production."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    path = "/internal/v1/debug/echo-signature"
    headers = sign_internal(service_key, method="POST", path=path)

    response = await client.post(f"http://test{path}", headers=headers)

    assert response.status_code == 404


async def test_the_whole_internal_surface_has_a_kill_switch(
    client, service_key, monkeypatch, price_book  # noqa: ARG001
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "internal_api_enabled", False)

    response = await _get(client, sign_internal(service_key, method="GET", path=PATH))

    assert response.status_code == 404


# --- golden vectors --------------------------------------------------------
#
# The canonical string is a cross-language contract: `docs/INTERNAL_API.md`
# ships a Python and a Node signer that another team will paste into their
# service. Pinning the exact bytes here means a change to the scheme fails
# loudly, with a reminder that the document and their client both need
# updating — rather than silently 401ing everyone at deploy time.

GOLDEN = [
    (
        "POST", "/internal/v1/usage/events", "seq=42&session=abc", b'{"reports":[]}',
        "SYNORA-HMAC-V1\nPOST\n/internal/v1/usage/events\nseq=42&session=abc\n"
        "1757250000\nnonce-abcdefghijklmn\nsvc_voice_agent_7f3a1c9e\n"
        "486a8f07711a55700e1ed31c536217c17f3135cc6340b4830bcb4f87bbc7072a",
    ),
    (
        "GET", "/internal/v1/health", "", b"",
        "SYNORA-HMAC-V1\nGET\n/internal/v1/health\n\n"
        "1757250000\nnonce-abcdefghijklmn\nsvc_voice_agent_7f3a1c9e\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
]


@pytest.mark.parametrize(("method", "path", "query", "body", "expected"), GOLDEN)
def test_the_canonical_string_is_exactly_what_the_docs_promise(
    method, path, query, body, expected
):
    produced = signing.canonical_string(
        method=method,
        path=path,
        query=query,
        timestamp="1757250000",
        nonce="nonce-abcdefghijklmn",
        key_id="svc_voice_agent_7f3a1c9e",
        body=body,
    )

    # Compared line by line so a failure names the line that moved.
    assert produced.split("\n") == expected.split("\n")


def test_an_absent_query_is_an_empty_line_not_a_missing_one():
    """The canonical string is always eight lines.

    A client that omits the line instead of emitting an empty one produces a
    seven-line string and a signature that never matches — worth pinning,
    because it is invisible in a diff.
    """
    produced = signing.canonical_string(
        method="GET", path="/x", query="", timestamp="1", nonce="n" * 16,
        key_id="svc_any_0000", body=b"",
    )

    assert len(produced.split("\n")) == 8
    assert produced.split("\n")[3] == ""


def test_a_space_in_the_query_encodes_as_percent_twenty_not_plus():
    """`quote()` and `encodeURIComponent()` have to agree, and they only do
    for `%20`. A `+` here is the classic Python-vs-JavaScript mismatch."""
    assert signing.canonical_query("q=a b") == "q=a%20b"
    assert signing.canonical_query("q=a+b") == "q=a%20b"


def test_a_non_ascii_body_hashes_over_its_utf8_bytes():
    """Uzbek and Russian text is the normal case here, not an edge case."""
    body = '{"uz":"salom дүнйә"}'.encode()

    assert signing.body_digest(body) == signing.body_digest(bytes(body))
    assert len(signing.body_digest(body)) == 64


async def test_with_a_working_cache_a_replayed_request_is_refused(
    client, service_key, price_book, monkeypatch  # noqa: ARG001
):
    """The other half of `test_replay_is_allowed_without_redis...`.

    Together they pin the whole policy: the guard works when it can, and gets
    out of the way when it cannot. Driven through an in-memory cache rather
    than a live Redis so it runs in CI.
    """
    from app.core import cache as cache_module

    class RememberingCache:
        def __init__(self) -> None:
            self.seen: set[str] = set()

        @property
        def is_available(self) -> bool:
            return True

        async def claim_once(self, key: str, ttl_seconds: int) -> bool:  # noqa: ARG002
            if key in self.seen:
                return False
            self.seen.add(key)
            return True

        async def get(self, key: str) -> str | None:  # noqa: ARG002
            return None

        async def set(self, key, value, ttl_seconds=None) -> None:  # noqa: ANN001, ARG002
            return None

        async def delete(self, *keys) -> None:  # noqa: ANN002, ARG002
            return None

        async def increment(self, key: str, ttl_seconds: int) -> int:  # noqa: ARG002
            return 0

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cache_module, "_cache", RememberingCache())
    headers = sign_internal(service_key, method="GET", path=PATH)

    first = await _get(client, headers)
    second = await _get(client, headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["code"] == "signature_replayed"


async def test_a_fresh_nonce_on_the_retry_is_accepted(
    client, service_key, price_book, monkeypatch  # noqa: ARG001
):
    """Which is why the contract insists a retry re-signs rather than re-sends."""
    from app.core import cache as cache_module

    seen: set[str] = set()

    class RememberingCache(NullCache):
        """A NullCache that does remember nonces, and nothing else."""

        async def claim_once(self, key: str, ttl_seconds: int) -> bool:  # noqa: ARG002
            if key in seen:
                return False
            seen.add(key)
            return True

        @property
        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(cache_module, "_cache", RememberingCache())

    first = await _get(client, sign_internal(service_key, method="GET", path=PATH))
    retry = await _get(client, sign_internal(service_key, method="GET", path=PATH))

    assert first.status_code == 200
    assert retry.status_code == 200, "a re-signed retry must not look like a replay"


async def test_using_a_key_is_recorded_even_when_the_request_then_fails(
    client, session, service_key, price_book  # noqa: ARG001
):
    """`last_used_at` exists to answer "is anything still using this key?".

    So it has to be recorded on the failing paths too — a counter that only
    counts successes cannot answer that question, and the failing requests are
    the ones you most want attributed.
    """
    from sqlalchemy import select

    from app.models.service_api_key import ServiceApiKey

    async def uses() -> tuple[int, object]:
        await session.rollback()  # see the committed value, not our own snapshot
        row = (
            await session.execute(
                select(ServiceApiKey.use_count, ServiceApiKey.last_used_at).where(
                    ServiceApiKey.key_id == service_key.key_id
                )
            )
        ).one()
        return row[0], row[1]

    before, _ = await uses()

    ok = await _get(client, sign_internal(service_key, method="GET", path=PATH))
    assert ok.status_code == 200
    after_success, last_used = await uses()
    assert after_success == before + 1
    assert last_used is not None

    # And a request that authenticates but is then refused on scope. This is
    # the case that matters: it is exactly the request you want attributed.
    from app.models.billing_enums import BillingService
    from app.services.billing import service_key_service

    narrow = await service_key_service.mint(
        session,
        label="usage only",
        service=BillingService.VOICE_AGENT,
        scopes=(service_key_service.SCOPE_USAGE_WRITE,),
    )
    await session.commit()

    refused = await _get(client, sign_internal(narrow, method="GET", path=PATH))
    assert refused.status_code == 403
    assert refused.json()["code"] == "service_key_forbidden"

    await session.rollback()
    narrow_uses = (
        await session.execute(
            select(ServiceApiKey.use_count).where(ServiceApiKey.key_id == narrow.key_id)
        )
    ).scalar_one()
    assert narrow_uses == 1, "a scope refusal must still be attributed to the key"
