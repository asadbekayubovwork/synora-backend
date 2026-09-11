"""Realtime transcription: the transcript out, and the money behind a live socket.

This is the first session in the codebase that is *live* — held before the work
and settled after an unbounded amount of it — so the cases worth pinning down
are the ones a one-shot never has:

* the hold covers the ceiling, and the charge is what actually happened;
* two metrics are billed, and they are not the same number: VAD trims silence
  out of `stt_audio_ms`, and `session_ms` is what covers the GPU slot anyway;
* every way a socket can end — stop, hang-up, the cap, going quiet — still
  settles, because a live session whose credit never comes back is the failure
  this module is arranged around;
* a handshake upstream refuses charges nothing at all.

Both sockets are fakes. The client's side is a scripted queue of ASGI-shaped
messages and the upstream is a scripted list of events, which is what lets a
whole session run in milliseconds and lets a test say "the client vanished
here" precisely. The real sockets are exercised against the live service by
hand; what is checked here is the wallet.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionStatus,
    BillingService,
    SessionEndReason,
    UsageMetric,
)
from app.models.usage import UsageEvent, UsageEventItem
from app.services.ai import stt_stream_client, stt_stream_service
from app.services.billing import wallet_repo
from tests.conftest import fund, make_user

CREDIT = 1_000_000
AUDIO_PER_MINUTE = 1_200_000
SESSION_PER_MINUTE = 200_000


class FakeClient:
    """The caller's socket: a scripted queue in, a recorded list out."""

    def __init__(self, *messages: dict) -> None:
        self.incoming: list[dict] = list(messages)
        self.sent: list[dict] = []
        self.blocked = asyncio.Event()

    async def receive(self) -> dict:
        if self.incoming:
            return self.incoming.pop(0)
        # Nothing left to say and no disconnect scripted: hang, so the idle
        # timeout is what ends the session rather than an empty queue.
        await self.blocked.wait()
        return {"type": "websocket.disconnect"}

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


class FakeUpstream:
    """The transcription service's socket, scripted."""

    def __init__(self, *events: dict) -> None:
        self.events_to_send = list(events)
        self.audio_chunks: list[bytes] = []
        self.stopped = False
        self.started_with: dict | None = None

    async def send_audio(self, chunk: bytes) -> None:
        self.audio_chunks.append(chunk)

    async def stop(self) -> None:
        self.stopped = True

    async def events(self):
        # Drip-fed rather than returned at once, so `pump_events` really does
        # run concurrently with the audio pump.
        for event in self.events_to_send:
            await asyncio.sleep(0)
            yield event


def audio(chunk: bytes = b"\x00\x01" * 160) -> dict:
    return {"type": "websocket.receive", "bytes": chunk}


def stop() -> dict:
    return {"type": "websocket.receive", "text": json.dumps({"type": "stop"})}


def disconnect() -> dict:
    return {"type": "websocket.disconnect"}


def final(seq: int, seconds: float, text: str = "salom") -> dict:
    return {
        "type": "final",
        "seq": seq,
        "text": text,
        "language": "uz",
        "audio_seconds": seconds,
        "infer_seconds": 0.1,
    }


DONE = {"type": "done", "segments": 0}


@pytest.fixture
def upstream(monkeypatch):
    """Replaces the seam. Returns a setter the test calls with its script."""
    holder: dict[str, FakeUpstream] = {}

    def install(*events: dict) -> FakeUpstream:
        fake = FakeUpstream(*events)
        holder["fake"] = fake

        @asynccontextmanager
        async def fake_connect(*, language: str, sample_rate: int):
            fake.started_with = {"language": language, "sample_rate": sample_rate}
            yield fake

        monkeypatch.setattr(stt_stream_client, "connect", fake_connect)
        return fake

    monkeypatch.setattr(settings, "stt_base_url", "http://stt.test")
    monkeypatch.setattr(settings, "stt_api_key", "test-token")
    install(DONE)
    return install


async def a_user(session, *, paid: int = 100 * CREDIT):
    user = await make_user(session)
    await fund(session, user.id, paid=paid)
    await session.commit()
    return user


async def balance(session, user) -> tuple[int, int]:
    from app.services.billing import wallet_service

    snapshot = await wallet_service.get_balance(session, user.id)
    return snapshot.available_micros, snapshot.reserved_micros


# --- the ordinary session ---------------------------------------------------


async def test_a_session_charges_for_the_audio_and_for_the_connection(
    session, price_book, upstream
):
    """Both metrics, and they are not the same number.

    Ninety seconds of speech across a socket that was open for less than a
    second: two started minutes of audio, one started minute of connection.
    """
    upstream(final(0, 60.0), final(1, 30.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), audio(), stop()), user, language="uz", sample_rate=16_000
    )

    assert outcome.segments == 2
    assert outcome.audio_ms == 90_000
    assert outcome.end_reason is SessionEndReason.STOP_REQUESTED
    # 2 × audio-minute + 1 × session-minute.
    assert outcome.price_micros == 2 * AUDIO_PER_MINUTE + SESSION_PER_MINUTE

    available, reserved = await balance(session, user)
    assert available == 100 * CREDIT - outcome.price_micros
    assert reserved == 0, "the ceiling hold has to come back"


async def test_the_hold_is_the_ceiling_and_comes_back(session, price_book, upstream):
    """A ten-minute cap reserves ten minutes of both metrics, up front."""
    ceiling = stt_stream_service.ceiling_quantities()
    assert ceiling[UsageMetric.SESSION_MS] == settings.stt_stream_max_seconds * 1000
    assert ceiling[UsageMetric.STT_AUDIO_MS] == settings.stt_stream_max_seconds * 1000

    upstream(final(0, 5.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )

    row = (
        await session.execute(
            select(AiSession).where(AiSession.id == outcome.ai_session_id)
        )
    ).scalar_one()
    assert row.status is AiSessionStatus.CLOSED
    assert row.reserved_micros == 0
    # Five seconds of speech: one started audio-minute plus the connection.
    assert outcome.price_micros == AUDIO_PER_MINUTE + SESSION_PER_MINUTE


async def test_a_connection_that_transcribed_nothing_still_pays_for_the_slot(
    session, price_book, upstream
):
    """The slot was held even though VAD closed no segment.

    And the floor is the rounding rather than a `min_charge`: CEIL to the
    started minute means the shortest possible session already costs one
    connection-minute, so a minimum below that would be configuration that
    looks load-bearing and never fires.
    """
    upstream(DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )

    assert outcome.segments == 0
    assert outcome.audio_ms == 0
    # No audio line at all, and one started minute of connection.
    assert outcome.price_micros == SESSION_PER_MINUTE


async def test_the_transcript_reaches_the_client_segment_by_segment(
    session, price_book, upstream
):
    upstream(
        {"type": "speech_started"},
        final(0, 2.0, "assalomu alaykum"),
        final(1, 3.0, "bugun havo yaxshi"),
        DONE,
    )
    user = await a_user(session)
    client = FakeClient(audio(), audio(), stop())

    await stt_stream_service.run(client, user, language="uz", sample_rate=16_000)

    assert [m["text"] for m in client.of_type("final")] == [
        "assalomu alaykum",
        "bugun havo yaxshi",
    ]
    # The barge-in trigger is relayed, because a caller's playback depends on it.
    assert client.of_type("speech_started")
    ready = client.of_type("ready")[0]
    assert ready["max_seconds"] == settings.stt_stream_max_seconds


async def test_the_audio_reaches_upstream_and_stop_is_always_sent(
    session, price_book, upstream
):
    """`stop` asks for the tail: segments already closed are paid-for transcript."""
    fake = upstream(final(0, 1.0), DONE)
    user = await a_user(session)

    await stt_stream_service.run(
        FakeClient(audio(b"aaaa"), audio(b"bbbb"), stop()),
        user,
        language="ru",
        sample_rate=8_000,
    )

    assert fake.audio_chunks == [b"aaaa", b"bbbb"]
    assert fake.stopped is True
    assert fake.started_with == {"language": "ru", "sample_rate": 8_000}


async def test_one_session_writes_one_usage_event_with_both_lines(
    session, price_book, upstream
):
    upstream(final(0, 45.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )

    event = (
        await session.execute(
            select(UsageEvent).where(UsageEvent.ai_session_id == outcome.ai_session_id)
        )
    ).scalar_one()
    items = (
        (
            await session.execute(
                select(UsageEventItem).where(
                    UsageEventItem.usage_event_id == event.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert {item.metric for item in items} == {
        UsageMetric.SESSION_MS,
        UsageMetric.STT_AUDIO_MS,
    }
    assert event.service is BillingService.STT


# --- every way it can end ---------------------------------------------------


async def test_the_session_is_recorded_as_a_live_one(session, price_book, upstream):
    """`AiSessionKind.REALTIME` has existed since the billing core and this is
    the first thing that writes it — without it a live socket is
    indistinguishable from a file upload in every report."""
    from app.models.billing_enums import AiSessionKind

    upstream(final(0, 3.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )

    row = (
        await session.execute(
            select(AiSession).where(AiSession.id == outcome.ai_session_id)
        )
    ).scalar_one()
    assert row.kind is AiSessionKind.REALTIME


async def test_the_inflight_gauge_comes_back_down(session, price_book, upstream):
    """Each open socket is a GPU slot held upstream; a leaked gauge reads as
    sessions that never end, which is the same shape as the bug it reveals."""
    from app.core import metrics

    def inflight() -> float:
        return metrics.stt_streams_inflight._value.get()

    before = inflight()
    upstream(final(0, 2.0), DONE)
    user = await a_user(session)

    await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )

    assert inflight() == before


async def test_a_client_that_hangs_up_is_still_settled(session, price_book, upstream):
    """The failure this module is arranged around: credit that never comes back."""
    upstream(final(0, 10.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), disconnect()), user, language="uz", sample_rate=16_000
    )

    assert outcome.end_reason is SessionEndReason.CLIENT_DISCONNECTED
    _, reserved = await balance(session, user)
    assert reserved == 0
    assert outcome.price_micros > 0


async def test_a_socket_that_goes_quiet_is_closed_and_settled(
    session, price_book, upstream, monkeypatch
):
    monkeypatch.setattr(settings, "stt_stream_idle_seconds", 0.05)
    upstream(final(0, 4.0), DONE)
    user = await a_user(session)

    # No disconnect scripted: the queue runs dry and the idle timer fires.
    outcome = await stt_stream_service.run(
        FakeClient(audio()), user, language="uz", sample_rate=16_000
    )

    assert outcome.end_reason is SessionEndReason.HEARTBEAT_TIMEOUT
    _, reserved = await balance(session, user)
    assert reserved == 0


async def test_a_session_past_the_cap_is_cut_off(
    session, price_book, upstream, monkeypatch
):
    monkeypatch.setattr(settings, "stt_stream_max_seconds", 0)
    upstream(final(0, 2.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), audio(), audio()), user, language="uz", sample_rate=16_000
    )

    assert outcome.end_reason is SessionEndReason.GRACE_EXHAUSTED
    _, reserved = await balance(session, user)
    assert reserved == 0


async def test_an_upstream_that_refuses_the_handshake_charges_nothing(
    session, price_book, upstream, monkeypatch
):
    from app.core.exceptions import ServiceUnavailableError

    @asynccontextmanager
    async def refuse(*, language: str, sample_rate: int):
        raise ServiceUnavailableError("no", code="stt_key_rejected")
        yield  # pragma: no cover

    monkeypatch.setattr(stt_stream_client, "connect", refuse)
    user = await a_user(session)
    before = await balance(session, user)

    with pytest.raises(ServiceUnavailableError):
        await stt_stream_service.run(
            FakeClient(audio()), user, language="uz", sample_rate=16_000
        )

    assert await balance(session, user) == before
    row = (
        await session.execute(
            select(AiSession).where(AiSession.service == BillingService.STT)
        )
    ).scalar_one()
    assert row.status is AiSessionStatus.FAILED
    assert row.reserved_micros == 0


async def test_too_little_credit_refuses_before_upstream_is_reached(
    session, price_book, upstream
):
    from app.core.exceptions import PaymentRequiredError

    fake = upstream(DONE)
    # Far less than the ceiling hold: ten minutes of both metrics.
    user = await a_user(session, paid=CREDIT)

    with pytest.raises(PaymentRequiredError):
        await stt_stream_service.run(
            FakeClient(audio()), user, language="uz", sample_rate=16_000
        )

    assert fake.audio_chunks == [], "no socket is opened on credit nobody has"


# --- the start message ------------------------------------------------------


def test_a_language_we_do_not_serve_is_refused():
    from app.core.exceptions import BadRequestError

    with pytest.raises(BadRequestError) as caught:
        stt_stream_service.validate_start({"type": "start", "language": "fr"})
    assert caught.value.code == "stt_language_unsupported"


def test_the_sample_rate_is_bounded_and_defaulted():
    assert stt_stream_service.validate_start({}) == (
        "uz",
        stt_stream_client.PREFERRED_SAMPLE_RATE,
    )
    assert stt_stream_service.validate_start({"sample_rate": 8_000})[1] == 8_000

    from app.core.exceptions import BadRequestError

    for bad in (7_999, 192_001, "many"):
        with pytest.raises(BadRequestError):
            stt_stream_service.validate_start({"sample_rate": bad})


async def test_the_done_message_carries_the_bill(session, price_book, upstream):
    upstream(final(0, 30.0), DONE)
    user = await a_user(session)

    outcome = await stt_stream_service.run(
        FakeClient(audio(), stop()), user, language="uz", sample_rate=16_000
    )
    message = stt_stream_service.done_message(outcome)

    assert message["type"] == "done"
    assert message["segments"] == 1
    assert message["audio_ms"] == 30_000
    assert message["price_micros"] == outcome.price_micros
    assert message["price"] == "1.400000"  # one audio minute + the connection
    assert message["ai_session_id"] == str(outcome.ai_session_id)
