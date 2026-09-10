"""`GET /metrics`: who may read it, and whether the numbers are the billed ones.

The counters themselves are trivial — `prometheus_client` is not what needs
testing. What needs testing is everything around them:

* the endpoint refuses the wrong token and answers with the right one, because
  it is public the moment nginx proxies `location /`;
* a synthesis moves the character counter by exactly what the wallet moved by,
  so the money panel cannot drift from `usage_events` the way a metric derived
  from a second source always eventually does;
* a replay is counted as a replay, since it delivers audio and charges nothing
  and the naive counter would bill it twice;
* the streams-inflight gauge comes back to zero on both the happy path and the
  upstream-refusal one, which is the leak that would read as "syntheses that
  never end".

`app/core/metrics.py` holds a module-level registry, so these tests read the
exposition text and diff it rather than resetting anything. A metric's value
across a whole suite run is not knowable; the *delta* around one call is.
"""

from __future__ import annotations

import httpx
import pytest

from app.core import metrics
from app.core.config import settings
from app.services.ai import tts_client
from tests.conftest import auth, fund, register_and_verify

TEXT = "a" * 1_000
PRICE_MICROS = 250_000
AUDIO = b"ID3fake" + b"frame" * 20


def sample(body: str, name: str, **labels: str) -> float:
    """One number out of the exposition text, by name and labels.

    Parsing the text rather than reading `Counter._value` on purpose: the text
    is what Prometheus consumes, so a metric that is recorded but never
    exported — the mistake a second registry produces — fails here.
    """
    wanted = name
    if labels:
        inner = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        wanted = f"{name}{{{inner}}}"
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        if head == wanted:
            return float(value)
    return 0.0


class FakeSpeechBox:
    """The same seam `test_tts_api.py` uses: a transport, not a mock."""

    def __init__(self) -> None:
        self.status = 200

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "no"})
        return httpx.Response(
            200, headers={"content-type": "audio/mpeg"}, content=self._audio()
        )

    async def _audio(self):
        # A generator, not `content=AUDIO`. Bytes handed to `httpx.Response`
        # are a *read* body, and `aiter_raw()` over one yields nothing at all —
        # which reaches the assertions as a synthesis that delivered zero bytes
        # and was therefore free, i.e. as a plausible billing bug in code that
        # is fine.
        yield AUDIO


@pytest.fixture
async def speech_box(monkeypatch):
    box = FakeSpeechBox()
    monkeypatch.setattr(settings, "tts_base_url", "http://speech.test")
    monkeypatch.setattr(settings, "tts_api_key", "test-key")
    monkeypatch.setattr(
        tts_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url="http://speech.test",
            transport=httpx.MockTransport(box.handle),
        ),
    )
    await tts_client.aclose_client()
    yield box
    await tts_client.aclose_client()


@pytest.fixture
def open_metrics(monkeypatch):
    """No token, which is what a development box runs with."""
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "")
    monkeypatch.setattr(settings, "environment", "development")


# The `client` fixture is based at `/api/v1`, and `/metrics` deliberately is
# not: it is not part of the surface a customer's token reaches. So every
# request here is absolute.
METRICS_URL = "http://test/metrics"


async def scrape(client) -> str:
    response = await client.get(METRICS_URL)
    assert response.status_code == 200, response.text
    return response.text


# --- who may read it --------------------------------------------------------


async def test_the_exposition_is_prometheus_text_not_json(client, open_metrics):
    response = await client.get(METRICS_URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "synora_http_requests_total" in response.text


async def test_a_token_is_required_once_one_is_configured(client, monkeypatch):
    """Configured, it is enforced — in development too, where none is needed."""
    monkeypatch.setattr(settings, "metrics_token", "s3cret-scrape-token")

    assert (await client.get(METRICS_URL)).status_code == 401
    wrong = await client.get(METRICS_URL, headers=auth("nope"))
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "metrics_unauthorized"

    right = await client.get(METRICS_URL, headers=auth("s3cret-scrape-token"))
    assert right.status_code == 200


async def test_a_disabled_endpoint_looks_like_one_that_was_never_built(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "metrics_enabled", False)

    response = await client.get(METRICS_URL)

    # 404 rather than 503: a probe learns nothing from the difference.
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_without_a_token_outside_development_it_is_simply_absent(
    client, monkeypatch
):
    """The case that decides whether a release survives.

    `METRICS_TOKEN` is a new variable, so no deployed `.env` has one. The
    first version of this refused to boot without it, which would have failed
    a production release that changed nothing else — an observability feature
    taking down the API it observes. Missing configuration turns the endpoint
    off instead, and the startup log says why.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "metrics_token", "")
    monkeypatch.setattr(settings, "metrics_enabled", True)

    response = await client.get(METRICS_URL)

    assert response.status_code == 404
    assert "not served" in settings.metrics_status

    # ...and the rest of the API is untouched by that, which is the point.
    assert (await client.get("http://test/health")).status_code == 200


async def test_a_second_worker_turns_it_off_rather_than_reporting_a_fraction(
    client, monkeypatch
):
    """One registry per process, and Prometheus scrapes whichever one it gets."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "metrics_token", "s3cret-scrape-token")
    monkeypatch.setattr(settings, "worker_count", 2)

    response = await client.get(METRICS_URL, headers=auth("s3cret-scrape-token"))

    assert response.status_code == 404
    assert "WORKER_COUNT" in settings.metrics_status


async def test_a_token_outside_development_is_all_it_takes(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "metrics_token", "s3cret-scrape-token")
    monkeypatch.setattr(settings, "metrics_enabled", True)

    assert (await client.get(METRICS_URL)).status_code == 401
    right = await client.get(METRICS_URL, headers=auth("s3cret-scrape-token"))
    assert right.status_code == 200
    assert "synora_http_requests_total" in right.text


async def test_the_scrape_does_not_count_itself(client, open_metrics):
    before = sample(
        await scrape(client), "synora_http_requests_total",
        method="GET", route="/metrics", status="200",
    )
    await scrape(client)

    after = sample(
        await scrape(client), "synora_http_requests_total",
        method="GET", route="/metrics", status="200",
    )
    assert before == after == 0


# --- the numbers ------------------------------------------------------------


async def test_a_route_is_counted_under_its_template_not_its_path(
    client, open_metrics
):
    before = sample(
        await scrape(client), "synora_http_requests_total",
        method="POST", route="/api/v1/auth/login", status="401",
    )

    await client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})

    after = sample(
        await scrape(client), "synora_http_requests_total",
        method="POST", route="/api/v1/auth/login", status="401",
    )
    assert after == before + 1


async def test_a_refusal_is_counted_by_its_code_not_only_its_status(
    client, open_metrics, session, price_book
):
    tokens = await register_and_verify(client, email="broke@example.com")
    body = await scrape(client)
    before = sample(body, "synora_api_errors_total", code="tts_not_configured", status="503")

    # No TTS configured in this test, so every /tts route answers 503 with that
    # code — the case a status-only counter cannot tell from a rejected key.
    await client.post("/tts/estimate", json={"text": "salom"}, headers=auth(tokens["access_token"]))

    after = sample(
        await scrape(client), "synora_api_errors_total",
        code="tts_not_configured", status="503",
    )
    assert after == before + 1


async def test_a_synthesis_counts_the_characters_the_wallet_paid_for(
    client, session, price_book, speech_box, open_metrics
):
    tokens = await register_and_verify(client, email="metered@example.com")
    from sqlalchemy import select

    from app.models.user import User

    user = (
        await session.execute(select(User).where(User.email == "metered@example.com"))
    ).scalar_one()
    await fund(session, user.id, paid=10 * PRICE_MICROS)
    await session.commit()

    body = await scrape(client)
    characters_before = sample(body, "synora_tts_characters_total")
    debited_before = sample(body, "synora_credits_debited_micros_total", service="tts")
    streams_before = sample(
        body, "synora_tts_streams_total", charge="billed", end_reason="completed"
    )

    response = await client.post(
        "/tts/speech",
        json={"text": TEXT, "audio_format": "mp3"},
        headers=auth(tokens["access_token"]),
    )
    assert response.status_code == 200

    body = await scrape(client)
    assert sample(body, "synora_tts_characters_total") == characters_before + 1_000
    # The same micros the ledger moved, from the code that moved them.
    assert (
        sample(body, "synora_credits_debited_micros_total", service="tts")
        == debited_before + PRICE_MICROS
    )
    assert (
        sample(body, "synora_tts_streams_total", charge="billed", end_reason="completed")
        == streams_before + 1
    )
    assert sample(body, "synora_tts_audio_bytes_total") >= len(AUDIO)
    # Back to zero: every path that opens an upstream response decrements it.
    assert sample(body, "synora_tts_streams_inflight") == 0


async def test_a_replay_delivers_audio_and_counts_no_revenue(
    client, session, price_book, speech_box, open_metrics
):
    """The one case where bytes moved and nothing was charged.

    A counter that derived `billed` from the byte count would add this
    request's characters to the revenue panel, and the panel would then read
    double what `usage_events` says for every client that retries.
    """
    tokens = await register_and_verify(client, email="replay@example.com")
    from sqlalchemy import select

    from app.models.user import User

    user = (
        await session.execute(select(User).where(User.email == "replay@example.com"))
    ).scalar_one()
    await fund(session, user.id, paid=10 * PRICE_MICROS)
    await session.commit()

    headers = {**auth(tokens["access_token"]), "Idempotency-Key": "one-key"}
    first = await client.post(
        "/tts/speech", json={"text": TEXT, "audio_format": "mp3"}, headers=headers
    )
    assert first.status_code == 200

    body = await scrape(client)
    characters_before = sample(body, "synora_tts_characters_total")
    debited_before = sample(body, "synora_credits_debited_micros_total", service="tts")
    replays_before = sample(
        body, "synora_tts_streams_total", charge="replay", end_reason="completed"
    )

    # The same key on the same request. Whether this is a 200 or a 409 is
    # `test_tts_api.py`'s question; either way it must not bill twice.
    second = await client.post(
        "/tts/speech", json={"text": TEXT, "audio_format": "mp3"}, headers=headers
    )

    body = await scrape(client)
    assert sample(body, "synora_tts_characters_total") == characters_before
    assert (
        sample(body, "synora_credits_debited_micros_total", service="tts")
        == debited_before
    )
    if second.status_code == 200:
        assert (
            sample(
                body, "synora_tts_streams_total", charge="replay", end_reason="completed"
            )
            == replays_before + 1
        )
    assert sample(body, "synora_tts_streams_inflight") == 0


async def test_an_upstream_refusal_is_free_and_leaves_no_inflight_stream(
    client, session, price_book, speech_box, open_metrics
):
    tokens = await register_and_verify(client, email="refused@example.com")
    from sqlalchemy import select

    from app.models.user import User

    user = (
        await session.execute(select(User).where(User.email == "refused@example.com"))
    ).scalar_one()
    await fund(session, user.id, paid=10 * PRICE_MICROS)
    await session.commit()

    speech_box.status = 500
    body = await scrape(client)
    characters_before = sample(body, "synora_tts_characters_total")
    errors_before = sample(
        body, "synora_tts_upstream_errors_total", operation="stream", code="tts_unreachable"
    )

    response = await client.post(
        "/tts/speech",
        json={"text": TEXT, "audio_format": "mp3"},
        headers=auth(tokens["access_token"]),
    )
    assert response.status_code == 502

    body = await scrape(client)
    # Not a character was billed, and the upstream failure is on the record
    # under the code we mapped it to rather than under its status.
    assert sample(body, "synora_tts_characters_total") == characters_before
    assert (
        sample(
            body, "synora_tts_upstream_errors_total", operation="stream", code="tts_unreachable"
        )
        == errors_before + 1
    )
    assert (
        sample(body, "synora_tts_streams_total", charge="free", end_reason="upstream_error")
        >= 1
    )
    assert sample(body, "synora_tts_streams_inflight") == 0


# --- the gauges only the database knows -------------------------------------


async def test_held_credit_is_read_from_the_wallets_not_from_counters(
    client, session, wallet, open_metrics
):
    """`refresh_db_gauges` runs at scrape time, so a fresh row shows up at once."""
    from app.services.billing import wallet_repo

    await wallet_repo.place_hold(
        session,
        wallet_id=wallet.wallet_id,
        amount_micros=PRICE_MICROS,
        idempotency_key="gauge-hold",
    )
    await session.commit()

    body = await scrape(client)

    assert sample(body, "synora_wallet_reserved_micros") == PRICE_MICROS
    assert sample(body, "synora_wallet_balance_micros", bucket="paid") == 1_000_000


async def test_a_gauge_that_cannot_be_read_does_not_take_the_scrape_down(
    client, open_metrics, monkeypatch
):
    """The database is the thing most likely to be wrong when you look.

    A scrape that 500s because one aggregate failed blinds every other counter
    in the process at exactly the wrong moment.
    """

    async def explode() -> None:
        raise RuntimeError("no database today")

    monkeypatch.setattr(metrics, "refresh_db_gauges", explode)

    response = await client.get(METRICS_URL)

    assert response.status_code == 200
    assert "synora_http_requests_total" in response.text
