"""`POST /tts/speech`: the audio out, and the money that moved behind it.

Upstream is faked at the transport rather than mocked at the function, because
the ordering these tests exist to pin down is an HTTP ordering. `tts_client`
exposes `build_client()` as the seam for exactly this — there is no `respx` in
`requirements-dev.txt` and one fake does not justify a dev dependency — so an
`httpx.MockTransport` goes in there and every layer above it runs untouched:
the real client, the real error mapping, the real `aiter_raw()` relay.

What the fake cannot reproduce is a client hanging up mid-stream.
`httpx.ASGITransport` runs the app to completion and hands back a buffered
body, which is convenient here — settlement is provably finished before any
assertion runs — and useless for a disconnect. That case is driven at the
service layer in `test_tts_billing.py`, where the generator can be closed by
hand.

The numbers below are the `price_book` fixture's: 1000 characters to the unit,
a quarter of a credit each, CEIL. `test_tts_pricing.py` owns the arithmetic;
this file only ever spends it.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionStatus,
    LedgerEntryKind,
    SessionEndReason,
)
from app.models.ledger import LedgerEntry
from app.models.usage import UsageEvent
from app.models.user import User
from app.services.ai import tts_client
from app.services.billing import session_service, wallet_repo
from tests.conftest import auth, fund, register_and_verify

CREDIT = 1_000_000
UNIT_MICROS = 250_000

# One priced unit exactly, so every expectation is one multiplication.
TEXT = "a" * 1_000
PRICE_MICROS = UNIT_MICROS

# Three chunks rather than one, so a relay that buffered the body would still
# pass and a relay that dropped a chunk would not.
AUDIO_CHUNKS = (b"ID3fake-header", b"frame-one-frame-two", b"frame-three")
AUDIO = b"".join(AUDIO_CHUNKS)

UPSTREAM_SAMPLE_RATE = "48000"


class FakeSpeechBox:
    """Upstream, as far as `tts_client` can tell.

    Records the `SynthesizeBody` it was sent, so a test can assert on what we
    asked for rather than only on what came back, and can be told to refuse.
    """

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.chunks: tuple[bytes, ...] = AUDIO_CHUNKS
        self.status = 200

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tts/stream", request.url.path
        self.bodies.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "the card fell over"})
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/mpeg",
                # The only usage-ish header the streaming endpoint sends.
                tts_client.HEADER_SAMPLE_RATE: UPSTREAM_SAMPLE_RATE,
            },
            content=self._audio(),
        )

    async def _audio(self):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def upstream(monkeypatch) -> FakeSpeechBox:
    """A configured speech box that answers from memory.

    `settings.has_tts` is false in the suite's environment on purpose — an
    unconfigured deployment is its own test, below — so the two credentials are
    set here and the client factory is replaced with one that never opens a
    socket.
    """
    box = FakeSpeechBox()
    monkeypatch.setattr(settings, "tts_base_url", "https://speech.test")
    monkeypatch.setattr(settings, "tts_api_key", "sk_live_test")
    monkeypatch.setattr(
        tts_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url="https://speech.test", transport=httpx.MockTransport(box.handle)
        ),
    )
    # The client is a module-level singleton built on first use, so the fake
    # only takes effect if whatever is cached there is dropped first.
    monkeypatch.setattr(tts_client, "_client_instance", None)
    return box


# --- helpers ----------------------------------------------------------------


async def _funded(client, session, *, paid: int = CREDIT, email: str = "ali@example.com"):
    """A signed-in user with credit, committed. Returns the token and wallet."""
    tokens = await register_and_verify(client, email=email)
    user_id = (
        await session.execute(select(User.id).where(User.email == email))
    ).scalar_one()
    snapshot = await fund(session, user_id, paid=paid)
    await session.commit()
    return tokens["access_token"], snapshot


async def _speak(client, token: str, *, text: str = TEXT, **body):
    return await client.post(
        "/tts/speech", headers=auth(token), json={"text": text, **body}
    )


# Read back on a connection of their own. The settlement commits from a session
# the request never sees, so anything held by the test's own session is a
# snapshot from before the charge.


async def _available(wallet_id) -> int:
    async with SessionLocal() as db:
        return (await wallet_repo.snapshot_by_id(db, wallet_id)).available_micros


async def _sessions() -> list[AiSession]:
    async with SessionLocal() as db:
        return list((await db.execute(select(AiSession))).scalars())


async def _events() -> list[UsageEvent]:
    async with SessionLocal() as db:
        return list((await db.execute(select(UsageEvent))).scalars())


async def _entries(wallet_id, kind: LedgerEntryKind) -> list[LedgerEntry]:
    async with SessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.wallet_id == wallet_id, LedgerEntry.kind == kind
                    )
                )
            ).scalars()
        )


# --- the happy path ---------------------------------------------------------


async def test_the_audio_relayed_is_the_audio_upstream_sent(
    client, session, price_book, upstream
):
    token, _ = await _funded(client, session)

    response = await _speak(client, token)

    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"] == "audio/mpeg"


async def test_the_text_reaches_upstream_in_its_own_spelling(
    client, session, price_book, upstream
):
    """`format`, not `audio_format`, and empty strings where we send nulls.
    Upstream's model declares the optional fields `str = ""` and answers a 422
    for a null, so the translation has one home and this is its test."""
    token, _ = await _funded(client, session)

    await _speak(client, token, voice_id=None, style=None, audio_format="wav")

    (body,) = upstream.bodies
    assert body["text"] == TEXT
    assert body["format"] == "wav"
    assert body["voice_id"] == ""
    assert body["style"] == ""


async def test_upstreams_own_spelling_of_the_format_field_is_accepted(
    client, session, price_book, upstream
):
    """Sending `format` must not silently synthesise the default instead.

    Upstream, its OpenAI-compatible route and every example anyone will have
    read call this field `format`; we call it `audio_format` because `format`
    is a builtin and shadows it in a generated client. Pydantic drops an
    unknown key without a word, so before the alias existed `{"format": "wav"}`
    quietly produced mp3 and the caller found out by reading the bytes. Caught
    exactly that way, against the live box.
    """
    token, _ = await _funded(client, session)

    response = await _speak(client, token, format="wav")

    assert response.status_code == 200
    (body,) = upstream.bodies
    assert body["format"] == "wav"
    assert response.headers["content-type"] == "audio/wav"


async def test_the_published_field_name_is_still_the_canonical_one(client):
    """The alias adds a spelling; it must not replace the documented one.

    `AliasChoices` puts its first entry in the schema, and getting that order
    wrong would rename the field in every generated client without failing a
    single request-level test.
    """
    schema = client._transport.app.openapi()["components"]["schemas"]
    assert "audio_format" in schema["SynthesizeRequest"]["properties"]
    assert "audio_format" in schema["BatchCreateRequest"]["properties"]


async def test_the_price_is_final_before_the_first_byte(client, session, price_book, upstream):
    """The whole point of billing the full text: the bill can ride on the
    response headers, because it is known before any audio exists."""
    token, _ = await _funded(client, session)

    response = await _speak(client, token)

    assert response.headers["X-Synora-Characters"] == str(len(TEXT))
    assert response.headers["X-Synora-Price-Micros"] == str(PRICE_MICROS)
    assert response.headers["X-Synora-Price"] == "0.250000"
    assert response.headers["X-Synora-Sample-Rate"] == "48000"
    # Upstream's own echo, relayed verbatim.
    assert response.headers[tts_client.HEADER_SAMPLE_RATE] == UPSTREAM_SAMPLE_RATE

    (row,) = await _sessions()
    assert response.headers["X-Synora-Session-Id"] == str(row.id)


async def test_the_balance_falls_by_exactly_what_was_quoted(
    client, session, price_book, upstream
):
    token, wallet = await _funded(client, session)

    response = await _speak(client, token)

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    assert int(response.headers["X-Synora-Price-Micros"]) == PRICE_MICROS


async def test_one_call_settles_exactly_once(client, session, price_book, upstream):
    """`settle_oneshot` is called from a `finally`, and a `finally` runs more
    often than whoever wrote it expects. `uq_usage_events_session_sequence` is
    what makes the second run a no-op instead of a second charge."""
    token, _ = await _funded(client, session)

    await _speak(client, token)

    (event,) = await _events()
    assert event.sequence == 1
    assert event.price_micros == PRICE_MICROS
    assert event.debited_micros == PRICE_MICROS
    assert event.writeoff_micros == 0
    assert event.clamped is False


async def test_the_ledger_holds_one_debit_group_and_one_release(
    client, session, price_book, upstream
):
    """Hold, release, debit — in that order, and the release is its own step.

    Settling with `release_reserved_micros` on the debit would charge against a
    balance that still counted this session's own hold, so anything above
    `balance - hold` would fall into the grace write-off and be given away. The
    separate release is what makes `available >= charge` a theorem; see the
    long comment at that call site in `session_service`.
    """
    token, wallet = await _funded(client, session)

    await _speak(client, token)

    (hold,) = await _entries(wallet.wallet_id, LedgerEntryKind.HOLD)
    (release,) = await _entries(wallet.wallet_id, LedgerEntryKind.RELEASE)
    debits = await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT)

    assert hold.amount_micros == PRICE_MICROS
    assert release.amount_micros == -PRICE_MICROS
    assert len({entry.group_id for entry in debits}) == 1
    assert sum(entry.amount_micros for entry in debits) == -PRICE_MICROS

    # The thread back to the synthesis that was paid for. A hold and a release
    # name the session directly; a debit names the usage event that justifies
    # it, and the session is one hop further on — `wallet_repo.debit` carries
    # one reference per entry rather than both.
    (row,) = await _sessions()
    (event,) = await _events()
    assert {hold.ai_session_id, release.ai_session_id} == {row.id}
    assert {entry.usage_event_id for entry in debits} == {event.id}
    assert event.ai_session_id == row.id


async def test_a_balance_worth_exactly_one_call_pays_for_it_in_full(
    client, session, price_book, upstream
):
    """The case that tells the two settlement orderings apart.

    `wallet_repo.debit` takes `chargeable` from a snapshot read before its own
    UPDATE lands, and `available` there is already net of `reserved` — so
    folding the hold into the charge with `release_reserved_micros` would price
    this call against an `available` of zero. The whole charge would become a
    shortfall, and `billing_grace_micros` — five credits, twenty times this
    price — would absorb every micro of it. The wallet still reads zero
    afterwards either way, which is why this is worth a test: the free call
    looks exactly like the paid one until somebody totals up
    `lifetime_writeoff_micros`. Releasing first is what makes the credit
    provably there before it is spent.
    """
    token, wallet = await _funded(client, session, paid=PRICE_MICROS)

    response = await _speak(client, token)

    assert response.status_code == 200
    assert await _available(wallet.wallet_id) == 0

    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS
    # The assertion the whole test exists for: nothing was given away.
    assert event.writeoff_micros == 0


async def test_the_session_closes_with_its_hold_given_back(
    client, session, price_book, upstream
):
    token, _ = await _funded(client, session)

    await _speak(client, token)

    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.COMPLETED
    assert row.hold_released_at is not None
    assert row.reserved_micros == 0
    assert row.settled_micros == PRICE_MICROS
    assert row.cum_tts_characters == len(TEXT)
    assert row.last_sequence == 1
    assert row.disputed is False


async def test_audio_duration_is_never_written_to_a_priced_column(
    client, session, price_book, upstream
):
    """`cum_tts_audio_ms` has no price row, and `price_cumulative` raises for
    an unpriced metric carrying a quantity — out of the `finally` that settles
    a stream whose 200 has already gone out. Duration is a header and a log
    line, and that is all it is."""
    token, _ = await _funded(client, session)

    await _speak(client, token, audio_format="pcm")

    (row,) = await _sessions()
    assert row.cum_tts_audio_ms == 0


# --- replay -----------------------------------------------------------------


async def test_a_key_whose_synthesis_already_finished_is_a_409_not_free_audio(
    client, session, price_book, upstream
):
    """A spent key is refused, and this test used to assert the opposite.

    It read `(200, 200)` and called that "charged once and spoken twice", which
    is exactly the hole: the first call settles the session, so the second one
    finds it terminal, `settle_oneshot` rebuilds the first answer, and every
    byte of the second synthesis is delivered for nothing. Repeat forever and
    one paid call buys unlimited free GPU time — and the response even
    advertises the first call's price while doing it.

    The audio is not stored anywhere, so "return the original response" is not
    one of the available answers. Of the two that are left, a refusal is the one
    that cannot be farmed, and a client that genuinely wants the audio again can
    ask for it with a fresh key and be charged for it.

    The code is `tts_idempotency_spent` and not `tts_idempotency_conflict`,
    which this test asserted for one release. Both are `409` and they are
    opposite instructions to a retry loop: `conflict` means "that key was spent
    on something else, send a fresh one for this text", and a client that
    follows it here opens a second session and pays twice for the one request
    the `Idempotency-Key` header exists to make safe. `spent` means "this same
    request already finished and was charged, stop retrying".
    """
    token, wallet = await _funded(client, session)
    headers = {**auth(token), "Idempotency-Key": "retry-1"}

    first = await client.post("/tts/speech", headers=headers, json={"text": TEXT})
    second = await client.post("/tts/speech", headers=headers, json={"text": TEXT})

    assert first.status_code == 200
    assert first.content == AUDIO
    assert second.status_code == 409
    assert second.json()["code"] == "tts_idempotency_spent"
    # The assertion that costs real money: the GPU was not asked a second time.
    assert len(upstream.bodies) == 1
    assert len(await _sessions()) == 1, "no second session was opened either"
    assert len(await _events()) == 1
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS


async def test_a_key_replayed_with_different_text_is_refused_before_the_gpu_runs(
    client, session, price_book, upstream
):
    """An `Idempotency-Key` means "this request again", not "this key again".

    Nothing binds a key to what it was first spent on except this check, and
    without it the key becomes a bearer token for somebody else's paid session:
    send four times the text under a key that has already been settled and the
    whole thing is synthesised, relayed, and charged at zero. The refusal
    happens before `stream_speech` is opened, so the attempt costs no GPU time
    at all — which is what makes farming it pointless rather than merely
    unprofitable.
    """
    token, wallet = await _funded(client, session)
    headers = {**auth(token), "Idempotency-Key": "retry-1"}

    first = await client.post("/tts/speech", headers=headers, json={"text": TEXT})
    # Four priced units against the first call's one, so the two cannot land in
    # the same rounding bucket and the price check has something to see.
    second = await client.post(
        "/tts/speech", headers=headers, json={"text": "b" * (4 * len(TEXT))}
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "tts_idempotency_conflict"
    assert len(upstream.bodies) == 1, "the second text never reached the GPU"
    assert len(await _sessions()) == 1
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS


async def test_the_two_ways_a_key_is_refused_do_not_share_one_code(
    client, session, price_book, upstream
):
    """`409` twice, and a client has to be able to tell them apart.

    They ask for opposite things. A key spent on a *different* request is
    `tts_idempotency_conflict`: the fix is a fresh key, and taking it costs the
    caller a second session and a second charge, which is correct — it is a
    second request. The same request coming back after it finished is
    `tts_idempotency_spent`: the fix is to stop, and following the *other*
    code's advice here would charge a retry twice for one synthesis. One code
    meaning both is a code no retry loop can act on, and since `_finalise`
    settles the moment the body ends, "already finished" is the shape of
    nearly every real retry.

    Both are asserted in one test on purpose: the pair is the contract, and two
    tests could drift into agreeing with each other.
    """
    token, wallet = await _funded(client, session)
    headers = {**auth(token), "Idempotency-Key": "retry-1"}

    paid = await client.post("/tts/speech", headers=headers, json={"text": TEXT})
    same_request_again = await client.post(
        "/tts/speech", headers=headers, json={"text": TEXT}
    )
    other_text = await client.post(
        "/tts/speech", headers=headers, json={"text": "b" * (4 * len(TEXT))}
    )

    assert paid.status_code == 200
    assert same_request_again.status_code == 409
    assert same_request_again.json()["code"] == "tts_idempotency_spent"
    assert other_text.status_code == 409
    assert other_text.json()["code"] == "tts_idempotency_conflict"
    # Neither refusal reached the GPU, and neither of them was charged for.
    assert len(upstream.bodies) == 1
    assert len(await _sessions()) == 1
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS


async def test_a_key_at_the_length_the_contract_publishes_is_accepted(
    client, session, price_book, upstream
):
    """The published limit and the enforced one are the same number.

    They were two numbers once: the header documented 128 characters while
    `open_oneshot` measured the *stored* key — `{user_id}:{scope}:` prefix and
    all — against its own 128, so anything past 91 came back `400
    idempotency_key_too_long` naming a constraint that appeared nowhere in the
    contract. The clients it broke were precisely the ones who read our Swagger
    page and generated a key at the length it advertised.
    """
    token, _ = await _funded(client, session)
    key = "k" * session_service.MAX_CLIENT_IDEMPOTENCY_KEY

    response = await client.post(
        "/tts/speech",
        headers={**auth(token), "Idempotency-Key": key},
        json={"text": TEXT},
    )

    assert response.status_code == 200


async def test_two_calls_without_a_key_are_two_calls(client, session, price_book, upstream):
    """The other half of the same rule: no key means no deduplication, because
    a null key deduplicates nothing and pretending otherwise would merge two
    deliberate requests into one."""
    token, wallet = await _funded(client, session)

    await _speak(client, token)
    await _speak(client, token)

    assert len(await _sessions()) == 2
    assert await _available(wallet.wallet_id) == CREDIT - 2 * PRICE_MICROS


# --- the surface ------------------------------------------------------------


@pytest.mark.parametrize(
    ("audio_format", "content_type"),
    [
        ("mp3", "audio/mpeg"),
        ("wav", "audio/wav"),
        ("opus", "audio/ogg"),
        # Headerless samples: nothing decodes them without being told the rate,
        # so claiming an audio type no player can open would be worse than
        # admitting they are bytes. The rate is on `X-Synora-Sample-Rate`.
        ("pcm", "application/octet-stream"),
    ],
)
async def test_the_content_type_follows_the_requested_format(
    client, session, price_book, upstream, audio_format, content_type
):
    token, _ = await _funded(client, session)

    response = await _speak(client, token, audio_format=audio_format)

    assert response.headers["content-type"] == content_type


async def test_speech_needs_a_bearer_token(client, price_book, upstream):
    response = await client.post("/tts/speech", json={"text": TEXT})

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


async def test_text_past_the_ceiling_is_refused_before_the_wallet_is_touched(
    client, session, price_book, upstream
):
    """Refused by the validator, which is the cheap half of the trade
    `schemas/tts.py` describes: upstream would refuse it too, but only after a
    price had been quoted, a hold placed and a session opened."""
    token, wallet = await _funded(client, session)

    response = await _speak(client, token, text="x" * (settings.tts_max_characters + 1))

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert upstream.bodies == []
    assert await _sessions() == []
    assert await _available(wallet.wallet_id) == CREDIT


async def test_a_deployment_with_no_speech_box_answers_a_clean_503(client, session, price_book):
    """No `upstream` fixture, so `settings.has_tts` is false. The point of
    `require_configured()` is that this is one status code with a name on it,
    rather than a connection error surfacing six frames down as a 502 that
    looks like an upstream outage."""
    token, wallet = await _funded(client, session)

    response = await _speak(client, token)

    assert response.status_code == 503
    assert response.json()["code"] == "tts_not_configured"
    assert await _sessions() == []
    assert await _available(wallet.wallet_id) == CREDIT


async def test_the_estimate_quotes_what_the_stream_then_charges(
    client, session, price_book, upstream
):
    """One pricing call behind both, so a quote that disagrees with the charge
    would need a new price book published between the two requests — which is
    why the version id is on the estimate."""
    token, _ = await _funded(client, session)

    quoted = await client.post("/tts/estimate", headers=auth(token), json={"text": TEXT})
    spoken = await _speak(client, token)

    body = quoted.json()
    assert body["price_micros"] == PRICE_MICROS
    assert body["sufficient_credit"] is True
    assert body["shortfall_micros"] == 0
    assert uuid.UUID(body["price_book_version_id"]) == price_book.id
    assert int(spoken.headers["X-Synora-Price-Micros"]) == body["price_micros"]
