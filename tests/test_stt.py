"""`POST /stt/transcribe`: the text out, and the money that moved behind it.

The mirror of `test_tts_api.py`, against the other gateway, and the interesting
cases are the ones TTS does not have. There the billable quantity is in the
request and the audio is the unknown; here the audio *is* the request and the
duration is what nobody on our side has measured. So the hold is an estimate
and the charge is not, and most of what follows is about the gap between them:

* the estimate is exact for WAV and deliberately high for everything else;
* the charge comes from upstream's `audio_seconds`, not from our estimate;
* an upstream that over-reports is clamped at the hold and flagged, rather than
  quietly emptying a wallet;
* and every way the call can fail hands the whole hold back, because nothing is
  charged for a transcription that did not happen.

Upstream is faked at the transport rather than mocked at the function, for the
reason `test_tts_api.py` gives: `stt_client` exposes `build_client()` as the
seam, so the real client, the real error mapping and the real multipart
encoding all run.

The numbers are the `price_book` fixture's: a minute to the unit, 1.2 credits
each, CEIL. So anything from one millisecond to sixty seconds costs 1.200000.
"""

from __future__ import annotations

import hashlib
import struct

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.ai_session import AiSession
from app.models.billing_enums import AiSessionStatus, BillingService, SessionEndReason
from app.models.stt_transcription import SttTranscription
from app.models.user import User
from app.services.ai import recording_store, stt_client, stt_service
from tests.conftest import auth, fund, register_and_verify

CREDIT = 1_000_000
MINUTE_MICROS = 1_200_000  # one priced unit


def wav(ms: int, *, sample_rate: int = 16_000) -> bytes:
    """A RIFF file of exactly `ms` milliseconds, 16-bit mono."""
    frames = sample_rate * ms // 1000
    data = b"\x00\x00" * frames
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


class FakeTranscriber:
    """Upstream, as far as `stt_client` can tell.

    Records what it was sent, so a test can assert on the request rather than
    only on the answer, and can be told to refuse with any status.
    """

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.status = 200
        self.audio_seconds = 1.0
        self.text = "Assalomu alaykum."
        self.body: dict | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == stt_client.PATH_TRANSCRIBE, request.url.path
        self.requests.append(request.content)
        if self.status != 200:
            return httpx.Response(
                self.status, json={"detail": "the decoder gave up"}
            )
        return httpx.Response(
            200,
            json=self.body
            if self.body is not None
            else {
                "text": self.text,
                "language": "uz",
                "audio_seconds": self.audio_seconds,
                "infer_seconds": 0.3,
                "rtf": 0.25,
            },
        )


@pytest.fixture
def kept(monkeypatch, tmp_path):
    """Keeping on, pointed at a tmp directory.

    The default is `data/recordings`, and a suite that wrote there would leave
    the developer's own store full of silence.
    """
    monkeypatch.setattr(settings, "recordings_enabled", True)
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path / "recordings"))


@pytest.fixture
async def upstream(monkeypatch, tmp_path):
    box = FakeTranscriber()
    monkeypatch.setattr(settings, "stt_base_url", "http://stt.test")
    monkeypatch.setattr(settings, "stt_api_key", "test-token")
    # Off unless a test asks for it with `kept`, so the billing cases below are
    # not also exercising the file store — and so none of them writes to the
    # real `data/recordings`.
    monkeypatch.setattr(settings, "recordings_enabled", False)
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path / "recordings"))
    monkeypatch.setattr(
        stt_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url="http://stt.test", transport=httpx.MockTransport(box.handle)
        ),
    )
    await stt_client.aclose_client()
    yield box
    await stt_client.aclose_client()


async def funded(client, session, email: str, *, paid: int = 20 * CREDIT) -> str:
    tokens = await register_and_verify(client, email=email)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    await fund(session, user.id, paid=paid)
    await session.commit()
    return tokens["access_token"]


async def post(client, token: str, audio: bytes, **extra):
    headers = auth(token)
    headers.update(extra.pop("headers", {}))
    return await client.post(
        "/stt/transcribe",
        files={"file": ("clip.wav", audio, "audio/wav")},
        data={"language": extra.pop("language", "uz")},
        headers=headers,
    )


async def balance(client, token: str) -> dict:
    return (await client.get("/wallet", headers=auth(token))).json()


# --- the reading of the file ------------------------------------------------


def test_a_wav_header_gives_its_duration_exactly():
    for ms in (250, 1_000, 1_280, 61_000):
        assert stt_service.wav_duration_ms(wav(ms)) == ms


def test_a_wav_with_a_chunk_in_front_of_the_audio_still_reads():
    """Phones and ffmpeg both write `LIST`/`INFO` before `data`."""
    original = wav(1_000)
    head, tail = original[:12], original[12:]
    padding = b"LIST" + struct.pack("<I", 4) + b"INFO"

    assert stt_service.wav_duration_ms(head + padding + tail) == 1_000


def test_anything_that_is_not_a_riff_file_is_estimated_from_its_size(monkeypatch):
    monkeypatch.setattr(settings, "stt_assumed_bytes_per_second", 8_000)

    assert stt_service.wav_duration_ms(b"ID3" + b"x" * 100) is None
    # Deliberately high: 8 kB/s is 64 kbps, and speech is usually 128. The hold
    # has to cover the charge, never the other way round.
    assert stt_service.estimate_ms(b"x" * 16_000) == 2_000


# --- the ordinary call ------------------------------------------------------


async def test_a_transcription_charges_for_the_duration_upstream_measured(
    client, session, price_book, upstream
):
    token = await funded(client, session, "hears@example.com")
    upstream.audio_seconds = 90.0  # two started minutes

    response = await post(client, token, wav(90_000))

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Assalomu alaykum."
    assert body["audio_ms"] == 90_000
    assert body["price_micros"] == 2 * MINUTE_MICROS
    assert body["price"] == "2.400000"

    after = await balance(client, token)
    assert after["available_micros"] == 20 * CREDIT - 2 * MINUTE_MICROS
    assert after["reserved_micros"] == 0


async def test_the_price_is_on_the_headers_as_well_as_in_the_body(
    client, session, price_book, upstream
):
    """A browser reads these without deserialising, which is why CORS exposes them."""
    token = await funded(client, session, "headers@example.com")

    response = await post(client, token, wav(1_000))

    assert response.headers["X-Synora-Price"] == "1.200000"
    assert response.headers["X-Synora-Price-Micros"] == str(MINUTE_MICROS)
    assert response.headers["X-Synora-Audio-Ms"] == "1000"
    assert response.headers["X-Synora-Session-Id"] == response.json()["ai_session_id"]


async def test_the_language_reaches_upstream_in_the_form_body(
    client, session, price_book, upstream
):
    token = await funded(client, session, "russian@example.com")

    await post(client, token, wav(1_000), language="ru")

    sent = upstream.requests[-1]
    assert b'name="language"' in sent
    assert b"ru" in sent
    assert b'name="file"' in sent


async def test_the_charge_is_upstreams_count_and_not_our_estimate(
    client, session, price_book, upstream
):
    """The gap this whole design is about.

    Three minutes of WAV is held for, upstream says it was sixty-one seconds,
    and the bill is two started minutes rather than three.
    """
    token = await funded(client, session, "generous@example.com")
    upstream.audio_seconds = 61.0

    response = await post(client, token, wav(180_000))

    assert response.json()["price_micros"] == 2 * MINUTE_MICROS
    after = await balance(client, token)
    assert after["available_micros"] == 20 * CREDIT - 2 * MINUTE_MICROS
    # The rest of the hold came back rather than being kept.
    assert after["reserved_micros"] == 0


async def test_an_upstream_that_over_reports_is_clamped_and_flagged(
    client, session, price_book, upstream
):
    """A service reporting ten minutes for a ten-second clip is a bug, not a bill."""
    token = await funded(client, session, "overreport@example.com")
    upstream.audio_seconds = 600.0

    response = await post(client, token, wav(10_000))

    assert response.status_code == 200
    # Charged at the ceiling the hold set, not at what upstream claimed.
    assert response.json()["price_micros"] == MINUTE_MICROS

    row = (
        await session.execute(
            select(AiSession).where(AiSession.service == BillingService.STT)
        )
    ).scalar_one()
    assert row.disputed is True, "a clamped settlement has to be findable"
    assert row.status is AiSessionStatus.CLOSED


async def test_one_call_writes_one_usage_line(client, session, price_book, upstream):
    token = await funded(client, session, "usage@example.com")
    upstream.audio_seconds = 30.0

    await post(client, token, wav(30_000))

    usage = (await client.get("/usage", headers=auth(token))).json()
    (line,) = usage["lines"]
    assert line["service"] == "stt"
    assert line["metric"] == "stt_audio_ms"
    assert line["quantity"] == 30_000
    assert line["price"] == "1.200000"


# --- nothing is charged for work that did not happen ------------------------


async def test_audio_upstream_cannot_decode_charges_nothing(
    client, session, price_book, upstream
):
    token = await funded(client, session, "garbled@example.com")
    upstream.status = 400

    response = await post(client, token, wav(5_000))

    assert response.status_code == 400
    assert response.json()["code"] == "stt_rejected_input"
    # Upstream's own words about the caller's own file, relayed.
    assert "decoder" in response.json()["detail"]

    after = await balance(client, token)
    assert after["available_micros"] == 20 * CREDIT
    assert after["reserved_micros"] == 0


async def test_our_own_token_being_refused_is_never_the_callers_401(
    client, session, price_book, upstream
):
    token = await funded(client, session, "ourfault@example.com")
    upstream.status = 401

    response = await post(client, token, wav(5_000))

    # A 401 relayed here would tell a user who mistyped nothing to check their
    # own credentials. It is our key, so it is a 503.
    assert response.status_code == 503
    assert response.json()["code"] == "stt_key_rejected"
    assert (await balance(client, token))["reserved_micros"] == 0


async def test_a_model_still_loading_says_when_to_come_back(
    client, session, price_book, upstream
):
    token = await funded(client, session, "warming@example.com")
    upstream.status = 503

    response = await post(client, token, wav(5_000))

    assert response.status_code == 503
    assert response.json()["code"] == "stt_not_ready"
    # The one 503 here that is worth retrying, so it is the only one with a
    # number on it.
    assert int(response.headers["Retry-After"]) > 0
    assert (await balance(client, token))["reserved_micros"] == 0


async def test_a_failed_call_leaves_a_session_that_says_why(
    client, session, price_book, upstream
):
    token = await funded(client, session, "diagnosable@example.com")
    upstream.status = 400

    await post(client, token, wav(5_000))

    row = (
        await session.execute(
            select(AiSession).where(AiSession.service == BillingService.STT)
        )
    ).scalar_one()
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.UPSTREAM_ERROR
    # The code the caller was shown, so a table of failures can be acted on:
    # the caller's audio and our own token are different problems.
    assert row.error_code == "stt_rejected_input"
    assert row.reserved_micros == 0


async def test_an_unreadable_2xx_is_a_502_rather_than_an_empty_transcript(
    client, session, price_book, upstream
):
    token = await funded(client, session, "nonsense@example.com")
    upstream.body = {"something": "else"}

    response = await post(client, token, wav(5_000))

    assert response.status_code == 502
    assert response.json()["code"] == "stt_unreadable"
    assert (await balance(client, token))["reserved_micros"] == 0


# --- refused before the wallet is touched -----------------------------------


async def test_an_unsupported_language_never_opens_a_session(
    client, session, price_book, upstream
):
    token = await funded(client, session, "french@example.com")

    response = await post(client, token, wav(1_000), language="fr")

    assert response.status_code == 400
    assert response.json()["code"] == "stt_language_unsupported"
    assert upstream.requests == [], "it must not reach the GPU"
    assert (await session.execute(select(AiSession))).scalars().all() == []


async def test_audio_past_the_length_ceiling_is_refused_before_the_hold(
    client, session, price_book, upstream, monkeypatch
):
    monkeypatch.setattr(settings, "stt_max_audio_seconds", 60)
    token = await funded(client, session, "epic@example.com")

    response = await post(client, token, wav(120_000))

    assert response.status_code == 400
    assert response.json()["code"] == "stt_audio_too_long"
    assert (await session.execute(select(AiSession))).scalars().all() == []


async def test_an_upload_past_the_size_ceiling_is_refused_before_the_hold(
    client, session, price_book, upstream, monkeypatch
):
    monkeypatch.setattr(settings, "stt_max_audio_bytes", 1_000)
    token = await funded(client, session, "huge@example.com")

    response = await post(client, token, wav(5_000))

    assert response.status_code == 400
    assert response.json()["code"] == "stt_audio_too_large"
    assert upstream.requests == []


async def test_an_over_large_wav_is_told_why_it_is_over_large(
    client, session, price_book, upstream, monkeypatch
):
    """The wall, turned into an instruction.

    Five minutes of speech recorded as uncompressed 48 kHz WAV is 27 MB and the
    same speech as mp3 is two — and a transcription model resamples to 16 kHz
    before it looks at anything, so the big file bought nothing. "27 MB, the
    limit is 25" is a true message that diagnoses none of that; the header is
    right there and says how long the audio is, so the refusal can.
    """
    monkeypatch.setattr(settings, "stt_max_audio_bytes", 1_000_000)
    token = await funded(client, session, "uncompressed@example.com")

    response = await post(client, token, wav(60_000, sample_rate=48_000))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert response.json()["code"] == "stt_audio_too_large"
    assert "60 seconds of uncompressed audio" in detail
    assert "mp3" in detail
    assert "16 kHz" in detail


async def test_an_over_large_file_we_cannot_read_is_not_told_a_guess(
    client, session, price_book, upstream, monkeypatch
):
    """No header, no diagnosis.

    The duration of a compressed file is an over-estimate on our side, and a
    refusal that announced "this is about fifty minutes" for a thirty-minute
    recording would be confidently wrong. Size is the number we can defend.
    """
    monkeypatch.setattr(settings, "stt_max_audio_bytes", 1_000)
    token = await funded(client, session, "opaque@example.com")

    response = await post(client, token, b"ID3" + b"\x00" * 5_000)

    detail = response.json()["detail"]
    assert response.json()["code"] == "stt_audio_too_large"
    assert "uncompressed" not in detail
    assert "seconds" not in detail


async def test_a_long_wav_is_refused_on_its_real_duration_not_its_size(
    client, session, price_book, upstream, monkeypatch
):
    """Duration first when it is known, because "split it" is the useful fix."""
    monkeypatch.setattr(settings, "stt_max_audio_seconds", 60)
    monkeypatch.setattr(settings, "stt_max_audio_bytes", 1_000)
    token = await funded(client, session, "longandbig@example.com")

    response = await post(client, token, wav(120_000))

    assert response.json()["code"] == "stt_audio_too_long"
    # Exact, so no hedging word: the header said 120 seconds.
    assert "about" not in response.json()["detail"]


async def test_an_empty_upload_is_refused(client, session, price_book, upstream):
    token = await funded(client, session, "silent@example.com")

    response = await post(client, token, b"")

    assert response.status_code == 400
    assert response.json()["code"] == "stt_audio_empty"


async def test_too_little_credit_is_a_402_that_says_how_much_is_missing(
    client, session, price_book, upstream
):
    # Half a unit, against a call that needs a whole one.
    token = await funded(client, session, "broke@example.com", paid=MINUTE_MICROS // 2)

    response = await post(client, token, wav(30_000))

    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "insufficient_balance"
    assert body["shortfallMicros"] == MINUTE_MICROS - MINUTE_MICROS // 2
    assert upstream.requests == [], "nothing is transcribed on credit nobody has"


async def test_transcription_needs_a_bearer_token(client, price_book, upstream):
    response = await client.post(
        "/stt/transcribe",
        files={"file": ("clip.wav", wav(1_000), "audio/wav")},
        data={"language": "uz"},
    )

    assert response.status_code in (401, 403)


async def test_a_deployment_with_no_transcription_service_answers_a_clean_503(
    client, session, price_book, monkeypatch
):
    monkeypatch.setattr(settings, "stt_base_url", "")
    monkeypatch.setattr(settings, "stt_api_key", "")
    token = await funded(client, session, "unconfigured@example.com")

    response = await post(client, token, wav(1_000))

    assert response.status_code == 503
    assert response.json()["code"] == "stt_not_configured"


# --- retries ----------------------------------------------------------------


async def test_a_spent_idempotency_key_is_refused_rather_than_charged_twice(
    client, session, price_book, upstream
):
    """There is no transcript to hand back, so the honest answer is a refusal."""
    token = await funded(client, session, "retry@example.com")
    headers = {"Idempotency-Key": "one-key"}

    first = await post(client, token, wav(5_000), headers=headers)
    assert first.status_code == 200

    second = await post(client, token, wav(5_000), headers=headers)

    assert second.status_code == 409
    assert second.json()["code"] == "stt_idempotency_spent"
    # One charge, not two.
    after = await balance(client, token)
    assert after["available_micros"] == 20 * CREDIT - MINUTE_MICROS
    assert after["reserved_micros"] == 0


async def test_two_calls_without_a_key_are_two_calls(
    client, session, price_book, upstream
):
    token = await funded(client, session, "twice@example.com")

    assert (await post(client, token, wav(5_000))).status_code == 200
    assert (await post(client, token, wav(5_000))).status_code == 200

    after = await balance(client, token)
    assert after["available_micros"] == 20 * CREDIT - 2 * MINUTE_MICROS


# --- what is kept -----------------------------------------------------------


async def test_a_transcription_is_kept_with_the_audio_that_produced_it(
    client, session, price_book, upstream, kept
):
    token = await funded(client, session, "keeper@example.com")
    upstream.text = "Bugun havo juda yaxshi."
    audio = wav(2_000)

    response = await post(client, token, audio)
    assert response.status_code == 200

    rows = (await client.get("/stt/transcriptions", headers=auth(token))).json()[
        "transcriptions"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "Bugun havo juda yaxshi."
    assert row["language"] == "uz"
    assert row["audio_bytes"] == len(audio)
    assert row["filename"] == "clip.wav"
    assert row["sha256"] == hashlib.sha256(audio).hexdigest()
    assert row["ai_session_id"] == response.json()["ai_session_id"]

    played = await client.get(
        f"/stt/transcriptions/{row['id']}/audio", headers=auth(token)
    )
    assert played.status_code == 200
    assert played.content == audio


async def test_a_refused_transcription_keeps_nothing(
    client, session, price_book, upstream, kept
):
    token = await funded(client, session, "refused@example.com")
    upstream.status = 400

    await post(client, token, wav(2_000))

    assert (await client.get("/stt/transcriptions", headers=auth(token))).json()[
        "transcriptions"
    ] == []
    assert list(recording_store.root().rglob("*.wav")) == []


async def test_keeping_can_be_switched_off(
    client, session, price_book, upstream, kept, monkeypatch
):
    monkeypatch.setattr(settings, "recordings_enabled", False)
    token = await funded(client, session, "optout@example.com")

    response = await post(client, token, wav(2_000))

    # The transcript still comes back; only the keepsake is gone.
    assert response.status_code == 200
    assert response.json()["text"]
    assert (await client.get("/stt/transcriptions", headers=auth(token))).json()[
        "transcriptions"
    ] == []


async def test_deleting_removes_the_row_and_the_audio(
    client, session, price_book, upstream, kept
):
    token = await funded(client, session, "eraser@example.com")
    await post(client, token, wav(2_000))
    row = (await client.get("/stt/transcriptions", headers=auth(token))).json()[
        "transcriptions"
    ][0]

    assert (
        await client.delete(f"/stt/transcriptions/{row['id']}", headers=auth(token))
    ).status_code == 200

    assert (await client.get("/stt/transcriptions", headers=auth(token))).json()[
        "transcriptions"
    ] == []
    assert list(recording_store.root().rglob("*.wav")) == []
    # The charge is untouched: what was said is erased, not that it was paid for.
    usage = (await client.get("/usage", headers=auth(token))).json()
    assert usage["lines"][0]["quantity"] == 1_000


async def test_another_accounts_transcription_is_a_404(
    client, session, price_book, upstream, kept
):
    ali = await funded(client, session, "ali@example.com")
    vali = await funded(client, session, "vali@example.com")
    await post(client, ali, wav(2_000))
    row = (await client.get("/stt/transcriptions", headers=auth(ali))).json()[
        "transcriptions"
    ][0]

    for method, path in (
        ("get", f"/stt/transcriptions/{row['id']}"),
        ("get", f"/stt/transcriptions/{row['id']}/audio"),
        ("delete", f"/stt/transcriptions/{row['id']}"),
    ):
        response = await getattr(client, method)(path, headers=auth(vali))
        assert response.status_code == 404, (method, path)
        assert response.json()["code"] == "transcription_not_found"


async def test_a_synthesis_and_its_own_transcription_share_one_file(
    client, session, price_book, upstream, kept
):
    """The case content addressing invites, across two tables.

    Transcribing audio this account synthesised is the obvious way to exercise
    both gateways — it is what the dev UI's "read the last synthesis" button
    does — and the uploaded bytes are the produced bytes. So one file ends up
    with a row in `tts_recordings` and a row in `stt_transcriptions`, and
    neither delete may unlink it while the other still points at it.
    """
    from app.services.ai import tts_recording_service

    token = await funded(client, session, "roundtrip@example.com")
    audio = wav(2_000)

    await post(client, token, audio)
    transcription = (
        await client.get("/stt/transcriptions", headers=auth(token))
    ).json()["transcriptions"][0]

    # The speech side's row for the same bytes. Written through the service
    # rather than by synthesising here, because what is under test is two
    # tables naming one key — not how the second one got there.
    user = (
        await session.execute(
            select(User).where(User.email == "roundtrip@example.com")
        )
    ).scalar_one()
    stored = (
        await session.execute(
            select(SttTranscription).where(SttTranscription.user_id == user.id)
        )
    ).scalar_one()
    await tts_recording_service.save(
        session,
        user_id=user.id,
        ai_session_id=stored.ai_session_id,
        body="Bugun havo juda yaxshi.",
        voice_id=None,
        quality="balanced",
        audio_format="wav",
        sample_rate=16_000,
        style=None,
        characters=23,
        audio_bytes=len(audio),
        audio_ms=2_000,
        storage_key=stored.storage_key,
        sha256=stored.sha256,
    )

    recording = (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ][0]
    assert recording["sha256"] == transcription["sha256"]
    assert len(list(recording_store.root().rglob("*.wav"))) == 1, "one file, two rows"

    # Deleting the recording must not empty the transcription's audio.
    await client.delete(f"/tts/recordings/{recording['id']}", headers=auth(token))

    played = await client.get(
        f"/stt/transcriptions/{transcription['id']}/audio", headers=auth(token)
    )
    assert played.status_code == 200
    assert played.content == audio

    # ...and once both rows are gone, so is the file.
    await client.delete(
        f"/stt/transcriptions/{transcription['id']}", headers=auth(token)
    )
    assert list(recording_store.root().rglob("*.wav")) == []
