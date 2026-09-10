"""What each way a synthesis can end does to the wallet.

Three rules, and every test here is one of them:

1. **A refusal costs nothing and leaves nothing behind.** A wallet that cannot
   cover the text is a `402` before the speech box is called at all, and the
   session it opened to find that out is closed on the way past — a `pending`
   row nobody will ever settle is a hold the reaper has to clean up and a row
   that reads as "in flight" forever.
2. **Nothing delivered, nothing charged.** Upstream failing before the first
   byte is our supplier's bad afternoon, not the user's, so the hold goes back
   in full.
3. **One byte delivered, the whole text charged.** By the time any audio is
   moving, every character has already been sent to the GPU. Hanging up saves
   no work, so it saves no money either.

And one rule about the bytes rather than the money, because it is the same
`finally` that enforces it: **a body that could not be finished is not ended
cleanly.** A truncated stream aborts, and under `httpx.ASGITransport` — which
re-raises whatever the app raised — that surfaces as the upstream exception
escaping the request call. Over a real socket it is the missing terminating
chunk, which is the only thing HTTP has that means "this body is incomplete".

A client hanging up is the one case that cannot be provoked over HTTP:
`httpx.ASGITransport` runs the app to completion and hands back a buffered
body, so there is never a moment at which the socket could go away. The
disconnect, replay-race and drain tests therefore drive
`tts_service.synthesize` directly and close the returned generator by hand,
which is exactly what Starlette does to a `StreamingResponse` body when a
client vanishes. Everything else here is a real request through the app.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.exceptions import BadGatewayError, ConflictError
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_SESSION_STATUSES,
    AiSessionStatus,
    BillingService,
    LedgerEntryKind,
    SessionEndReason,
    UsageMetric,
)
from app.models.ledger import LedgerEntry
from app.models.usage import UsageEvent
from app.models.user import User
from app.models.wallet import Wallet
from app.services.ai import tts_client, tts_service
from app.services.billing import reconcile_service, session_service, wallet_repo
from tests.conftest import auth, fund, make_user, register_and_verify

CREDIT = 1_000_000
TEXT = "a" * 1_000
PRICE_MICROS = 250_000

AUDIO_CHUNKS = (b"ID3fake-header", b"frame-one-frame-two", b"frame-three")
AUDIO = b"".join(AUDIO_CHUNKS)


class FakeSpeechBox:
    """Upstream, with both of its failure modes reachable.

    `status` refuses before a byte has moved, which is the case that still has
    a status code left to choose. `break_after` yields that many chunks and
    then drops the connection, which is the case that does not: our own `200`
    is already on the wire by then.

    `answer_when` holds the response back until a test says so, and `arrived`
    says the request is in flight. Together they reproduce the window the read
    timeout is 300 seconds wide for — the GPU queueing before it sends headers
    — which is where a cancellation has to find a hold already placed.
    """

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.status = 200
        self.break_after: int | None = None
        self.chunks: tuple[bytes, ...] = AUDIO_CHUNKS
        self.arrived = asyncio.Event()
        self.answer_when: asyncio.Event | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.arrived.set()
        if self.answer_when is not None:
            await self.answer_when.wait()
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "the card fell over"})
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", tts_client.HEADER_SAMPLE_RATE: "48000"},
            content=self._audio(),
        )

    async def _audio(self):
        for sent, chunk in enumerate(self.chunks, start=1):
            yield chunk
            if self.break_after is not None and sent >= self.break_after:
                raise httpx.ReadError("the connection went away mid-stream")


@pytest.fixture
def upstream(monkeypatch) -> FakeSpeechBox:
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
    monkeypatch.setattr(tts_client, "_client_instance", None)
    return box


# --- helpers ----------------------------------------------------------------


async def _funded(client, session, *, paid: int, email: str = "ali@example.com"):
    tokens = await register_and_verify(client, email=email)
    user_id = (
        await session.execute(select(User.id).where(User.email == email))
    ).scalar_one()
    snapshot = await fund(session, user_id, paid=paid)
    await session.commit()
    return tokens["access_token"], snapshot


async def _speak(client, token: str, *, text: str = TEXT):
    return await client.post("/tts/speech", headers=auth(token), json={"text": text})


async def _available(wallet_id) -> int:
    async with SessionLocal() as db:
        return (await wallet_repo.snapshot_by_id(db, wallet_id)).available_micros


async def _reserved(wallet_id) -> int:
    async with SessionLocal() as db:
        return (await wallet_repo.snapshot_by_id(db, wallet_id)).reserved_micros


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


# --- 402: the wallet says no ------------------------------------------------


async def test_too_little_credit_is_a_402_that_says_how_much_is_missing(
    client, session, price_book, upstream
):
    """`shortfallMicros` is what the top-up dialog is sized from, so it is a
    number and not a sentence. The whole text is priced up front, which is what
    makes it knowable before any work happens."""
    token, _ = await _funded(client, session, paid=100_000)

    response = await _speak(client, token)

    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "insufficient_balance"
    assert body["shortfallMicros"] == PRICE_MICROS - 100_000
    assert body["requiredMicros"] == PRICE_MICROS
    assert body["availableMicros"] == 100_000


async def test_a_refused_call_leaves_no_hold_behind(client, session, price_book, upstream):
    """A `402` that stranded credit would be the worst of both: the user is
    told they cannot afford it *and* some of what they have is locked away."""
    token, wallet = await _funded(client, session, paid=100_000)

    await _speak(client, token)

    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 100_000
    assert await _entries(wallet.wallet_id, LedgerEntryKind.HOLD) == []
    assert await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT) == []


async def test_a_refused_call_leaves_no_live_session(client, session, price_book, upstream):
    """The row survives the exception on purpose — a declined attempt is worth
    being able to read afterwards — but it survives it *terminal*. The reaper
    must never find a `pending` session that never started, and nobody reading
    the table should have to tell "declined" from "in flight"."""
    token, _ = await _funded(client, session, paid=100_000)

    await _speak(client, token)

    (row,) = await _sessions()
    assert row.status is AiSessionStatus.FAILED
    assert row.status in TERMINAL_SESSION_STATUSES
    assert row.end_reason is SessionEndReason.INSUFFICIENT_CREDIT
    assert row.error_code == "insufficient_balance"
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None


async def test_a_refused_call_never_reaches_the_speech_box(
    client, session, price_book, upstream
):
    """The hold is placed before the upstream request, not after it, so GPU
    time is never spent on work that cannot be paid for."""
    token, _ = await _funded(client, session, paid=100_000)

    await _speak(client, token)

    assert upstream.bodies == []


# --- upstream fails before the first byte -----------------------------------


@pytest.mark.parametrize(
    ("upstream_status", "status", "code"),
    [
        (502, 502, "tts_unreachable"),
        (500, 502, "tts_unreachable"),
        # Our credential, never the caller's problem: a 4xx here would ask a
        # user to fix something only we can fix.
        (401, 503, "tts_key_rejected"),
        # Upstream's own tenant quota. Relaying its 402 would open the top-up
        # dialog for a shortfall on our account.
        (402, 503, "tts_quota_exhausted"),
        (422, 400, "tts_rejected_input"),
    ],
)
async def test_an_upstream_refusal_before_any_audio_is_a_real_status_code(
    client, session, price_book, upstream, upstream_status, status, code
):
    """Nothing has been written to the socket yet, so the refusal can still be
    mapped. `tts_service` opens the upstream stream before the route builds a
    response for exactly this reason: after the first chunk there is no status
    left to choose."""
    token, _ = await _funded(client, session, paid=CREDIT)
    upstream.status = upstream_status

    response = await _speak(client, token)

    assert response.status_code == status
    assert response.json()["code"] == code


async def test_upstream_failing_before_the_first_byte_charges_nothing(
    client, session, price_book, upstream
):
    token, wallet = await _funded(client, session, paid=CREDIT)
    upstream.status = 502

    await _speak(client, token)

    assert await _available(wallet.wallet_id) == CREDIT
    assert await _reserved(wallet.wallet_id) == 0
    assert await _events() == []
    assert await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT) == []


async def test_upstream_failing_before_the_first_byte_abandons_the_session(
    client, session, price_book, upstream
):
    """`FAILED`, not `CLOSED`: the session charged nothing at all, and that
    distinction is what `end_reason` and `status` carry between them."""
    token, wallet = await _funded(client, session, paid=CREDIT)
    upstream.status = 502

    await _speak(client, token)

    (row,) = await _sessions()
    assert row.status is AiSessionStatus.FAILED
    assert row.end_reason is SessionEndReason.UPSTREAM_ERROR
    assert row.error_code == "tts_unreachable"
    assert row.settled_micros == 0
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None

    # The hold was placed and given back, which is the pair the reconciler
    # checks. One of each, netting to nothing.
    (hold,) = await _entries(wallet.wallet_id, LedgerEntryKind.HOLD)
    (release,) = await _entries(wallet.wallet_id, LedgerEntryKind.RELEASE)
    assert hold.amount_micros + release.amount_micros == 0


# --- upstream fails after the first byte ------------------------------------


async def test_a_stream_that_breaks_mid_body_is_aborted_rather_than_ended_cleanly(
    client, session, price_book, upstream
):
    """A truncated body must not look like a complete one.

    This test used to assert `200` with a short body and call that correct. It
    is not: a streamed response carries no `Content-Length`, and no mapping
    from characters to bytes exists for a compressed format, so a caller handed
    a cleanly terminated body has *nothing* to check the truncation against —
    they save half an mp3, are billed for all of it, and never find out.
    Aborting the response without its terminating chunk is the one signal HTTP
    has for "this body is incomplete", and every client already understands it.

    `httpx.ASGITransport` re-raises whatever the app raised, so here the abort
    shows up as upstream's own exception coming back out of the request call.
    Over a real socket it is uvicorn dropping the connection mid-chunk.
    """
    token, wallet = await _funded(client, session, paid=CREDIT)
    upstream.break_after = 1

    with pytest.raises(httpx.ReadError):
        await _speak(client, token)

    # And the money is unchanged by the abort: the `finally` still settles for
    # the whole text, because every character reached the GPU before any audio
    # came back. Billing is not what this fix moved.
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS

    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.UPSTREAM_ERROR
    assert row.settled_micros == PRICE_MICROS
    assert row.cum_tts_characters == len(TEXT)


# --- the client hangs up ----------------------------------------------------


async def _open_stream(
    session, *, text: str = TEXT, paid: int = CREDIT, idempotency_key: str | None = None
):
    """A synthesis driven at the service layer, with its body still in hand.

    The route hands this generator to Starlette, which closes it when the
    socket goes away. `httpx.ASGITransport` buffers the whole response and can
    therefore never produce that moment, so the test plays Starlette itself.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=paid)
    await session.commit()

    ticket, headers, body = await tts_service.synthesize(
        session,
        user,
        text=text,
        quality="balanced",
        audio_format="mp3",
        sample_rate=48_000,
        idempotency_key=idempotency_key,
    )
    return snapshot, ticket, headers, body


async def _replay(user_id, *, text: str = TEXT, idempotency_key: str):
    """The same call again from a second connection, as a retry would arrive.

    Its own `SessionLocal()` on purpose: a retry is a different request, and
    `open_oneshot` rolls its session back on the way to the replay branch —
    sharing the original's session would hide whether anything below depends on
    rows that rollback drops.
    """
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        return await tts_service.synthesize(
            db,
            user,
            text=text,
            quality="balanced",
            audio_format="mp3",
            sample_rate=48_000,
            idempotency_key=idempotency_key,
        )


async def test_hanging_up_after_the_first_chunk_bills_the_whole_text(
    session, price_book, upstream
):
    """The product decision, in one assertion. Every character was sent to the
    GPU before any audio came back, so a disconnect saves no work — and a
    refund for work already done would make the price depend on the network."""
    wallet, ticket, _headers, body = await _open_stream(session)

    first = await body.__anext__()
    await body.aclose()

    assert first == AUDIO_CHUNKS[0]
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS

    (row,) = await _sessions()
    assert row.id == ticket.ai_session_id
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.CLIENT_DISCONNECTED
    assert row.settled_micros == PRICE_MICROS
    assert row.cum_tts_characters == len(TEXT)
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None


async def test_hanging_up_settles_from_a_session_of_its_own(session, price_book, upstream):
    """The generator outlives the request, so the request-scoped session is
    already closed by the time the bill is written. That the charge lands at
    all is the assertion — it would not if `_finalise` reused the caller's
    session, and it would not if the shielded await were skippable in a
    cancelled task."""
    wallet, _ticket, _headers, body = await _open_stream(session)

    await body.__anext__()
    await session.close()
    await body.aclose()

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS


async def test_a_200_that_carries_no_audio_at_all_charges_nothing(
    client, session, price_book, upstream
):
    """Upstream answering `200` and then producing nothing is the same fact as
    upstream refusing: no audio arrived, so there is nothing to bill for. The
    settlement branches on delivered bytes rather than on the status precisely
    so that these two agree."""
    token, wallet = await _funded(client, session, paid=CREDIT)
    upstream.chunks = ()

    response = await _speak(client, token)

    assert response.status_code == 200
    assert response.content == b""
    assert await _available(wallet.wallet_id) == CREDIT
    assert await _reserved(wallet.wallet_id) == 0
    assert await _events() == []
    (row,) = await _sessions()
    assert row.status is AiSessionStatus.FAILED
    assert row.settled_micros == 0
    assert row.hold_released_at is not None


async def test_a_body_that_is_never_iterated_still_gives_the_hold_back(
    session, price_book, upstream
):
    """The disconnect that lands *before* Starlette pulls the first chunk.

    An async generator gets a finalizer on its first iteration and not before:
    `aclose()` on a never-started one is a no-op, and the loop's asyncgen hooks
    have never heard of it. Starlette reaches exactly that state when the
    client is already gone — `StreamingResponse` starts `stream_response` in a
    task group and awaits `listen_for_disconnect`, whose `receive()` returns
    the queued disconnect without suspending, so the cancel lands before the
    body's first step. Nothing would run the `finally`: the hold would sit on
    the wallet forever and the upstream response would hold its pooled
    connection until the pool ran dry.

    So `synthesize` pulls one empty chunk itself before handing the generator
    over, and the two assertions below are the two halves of that: the
    generator comes back *started*, and closing it without ever iterating it
    still settles. Every other test in this file calls `__anext__()` first,
    which is why this case survived so long.
    """
    wallet, ticket, _headers, body = await _open_stream(session)

    assert inspect.getasyncgenstate(body) == inspect.AGEN_SUSPENDED, (
        "handed over cold, its `finally` is unreachable"
    )

    await body.aclose()

    # Not one byte was delivered, so nothing is charged — but the hold is the
    # part that must not survive.
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == CREDIT
    (row,) = await _sessions()
    assert row.id == ticket.ai_session_id
    assert row.status in TERMINAL_SESSION_STATUSES
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None


# --- the reaper against a stream that is still moving ------------------------


async def _expire_now(ai_session_id) -> None:
    """Move a session's deadline into the past, on a connection of its own.

    What ten minutes of a slow download does to `expires_at`, without spending
    ten minutes. The reaper runs in another process, so this is written and
    committed from a session the stream does not share.
    """
    async with SessionLocal() as db:
        await db.execute(
            update(AiSession)
            .where(AiSession.id == ai_session_id)
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()


async def _reap() -> int:
    async with SessionLocal() as db:
        return await reconcile_service.reap_expired_sessions(db)


async def test_a_stream_still_delivering_audio_survives_a_reconcile_pass(
    session, price_book, upstream, monkeypatch
):
    """The reaper ate live streams, and this is the test that stops it.

    `DEFAULT_ONESHOT_TTL_SECONDS` is ten minutes and nothing bounds how long a
    `/tts/speech` body takes to drain: `aiter_raw` reads only when the
    generator is pulled, so a phone on a throttled connection or a paused
    `<audio>` element holds the stream open indefinitely without ever tripping
    the 300-second read timeout. A reaper acting on the deadline alone released
    the hold and stamped the row `FAILED` while the audio was still going out;
    the stream then finished into a session already terminal, the settlement
    rebuilt a zero, and the whole synthesis was free with nothing on the row to
    say it had ever been billable — wallet whole, no usage event, no evidence.

    So the body stamps progress as bytes move and the reaper asks for silence
    as well as for the deadline. The interval is dropped to zero here because
    the real one is thirty seconds of wall clock: what is under test is that a
    mark is written at all and that the reaper reads it, not the arithmetic
    that spaces them, which `PROGRESS_TOUCH_INTERVAL_SECONDS` owns.
    """
    monkeypatch.setattr(tts_service, "PROGRESS_TOUCH_INTERVAL_SECONDS", 0)
    wallet, ticket, _headers, body = await _open_stream(session)

    assert await body.__anext__() == AUDIO_CHUNKS[0]
    await _expire_now(ticket.ai_session_id)

    assert await _reap() == 0, "a stream in the middle of delivering is not dead"

    async for _chunk in body:
        pass

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.COMPLETED
    assert row.settled_micros == PRICE_MICROS
    assert row.heartbeat_count >= 1, "the evidence the reaper stood off for"
    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS


async def test_a_stream_that_went_quiet_past_its_deadline_is_reaped(
    session, price_book, upstream, monkeypatch
):
    """The control, and the trade it names.

    A freshness test that never fires is not a freshness test: the same live
    stream, with its last progress mark pushed back beyond the grace period, is
    what a process killed mid-stream leaves behind and it has to be reaped.

    The second half is the price of being wrong in that direction, and it is no
    longer free synthesis. The hold used to come back and the audio that
    finished afterwards was charged nothing; now a claimed session is settled at
    the price it was opened for, so the cost of reaping a stream that was in
    fact still alive is a charge raised on a deadline instead of on a report —
    which is why the row carries `disputed` and why the grace period is fifteen
    minutes rather than one. A charge raised early is refundable; a charge
    skipped leaves nothing to refund and nothing to notice.
    """
    monkeypatch.setattr(tts_service, "PROGRESS_TOUCH_INTERVAL_SECONDS", 0)
    wallet, ticket, _headers, body = await _open_stream(session)
    await body.__anext__()

    async with SessionLocal() as db:
        await db.execute(
            update(AiSession)
            .where(AiSession.id == ticket.ai_session_id)
            .values(
                expires_at=utcnow() - timedelta(minutes=1),
                last_heartbeat_at=utcnow()
                - timedelta(seconds=reconcile_service.PROGRESS_GRACE_SECONDS + 60),
            )
        )
        await db.commit()

    assert await _reap() == 1

    (row,) = await _sessions()
    # `CLOSED`, not `FAILED`: this session settled. `FAILED` is reserved for the
    # rows that charged nothing at all, and reading one here would say the call
    # died owing nothing when it in fact died owing everything.
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.TIMEOUT
    assert row.error_code == "session_reaped"
    assert row.settled_micros == PRICE_MICROS
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None
    assert row.disputed is True

    # The audio that arrives after the reap was already paid for, and settling
    # a terminal session replays that answer rather than charging again.
    async for _chunk in body:
        pass
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    assert len(await _events()) == 1


async def test_a_stream_reaped_before_it_ever_reported_is_billed_the_estimate(
    session, price_book, upstream
):
    """The free synthesis the reaper handed out, and the charge that closes it.

    `_body` stamps progress only when a chunk is pulled *and* thirty seconds of
    wall clock have gone by, so a client that opens `POST /tts/speech`, takes
    the headers and then stops reading reaches `expires_at` with
    `last_heartbeat_at` still null — and a null mark is reapable on the deadline
    alone, which is the shape every genuinely stranded hold has. No monkeypatch
    below for exactly that reason: one chunk under the real interval writes no
    mark at all, which is the case.

    Reaping that row is right. Abandoning it was not. The hold went back, the
    row went terminal, and the client then drained the body it had been sitting
    on into a session `settle_oneshot` rebuilt a zero for: a thousand characters
    delivered, wallet untouched, no usage event to notice it by. Reproduced end
    to end, and the reason a claimed session now goes to
    `session_service.settle_at_estimate` instead.

    The trade that function names, restated in the assertions: this bills work
    we believe was delivered rather than work we watched being delivered, so it
    can be wrong. It is wrong in the only survivable direction — a refund is a
    ledger entry and a zero is silence — and `disputed` is the single predicate
    support needs to list every call billed on a deadline.
    """
    wallet, ticket, _headers, body = await _open_stream(session)

    assert await body.__anext__() == AUDIO_CHUNKS[0]
    (opened,) = await _sessions()
    assert opened.last_heartbeat_at is None, "nothing reported: the reaper's case"
    assert opened.claimed_at is not None, "and the work was started: the charge's case"
    await _expire_now(ticket.ai_session_id)

    assert await _reap() == 1

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    assert await _reserved(wallet.wallet_id) == 0
    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.TIMEOUT
    assert row.error_code == "session_reaped"
    assert row.settled_micros == PRICE_MICROS, "the price the hold was placed for"
    assert row.hold_released_at is not None
    assert row.disputed is True
    # The evidence of why the row is here at all, left exactly as the reap
    # found it: a null mark says the stream never reported a single byte.
    assert row.last_heartbeat_at is None
    (event,) = await _events()
    assert event.price_micros == PRICE_MICROS
    assert event.debited_micros == PRICE_MICROS
    assert event.ai_session_id == ticket.ai_session_id

    # And the door the whole bug came through: the stalled client drains the
    # body it was holding open, and the settlement finds a session that has
    # already been billed rather than one it can settle to zero.
    async for _chunk in body:
        pass
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    assert len(await _events()) == 1, "settled once, on a deadline"


# --- a replayed key borrows a session, it does not own one -------------------


async def test_a_failed_replay_does_not_void_the_stream_it_borrowed(
    session, price_book, upstream
):
    """The retry the `Idempotency-Key` header exists for, arriving mid-stream.

    `open_oneshot` hands back the session the key already opened, and a
    borrower does not get to close that account. When the borrower's own
    upstream call failed, the recovery path abandoned the *shared* session:
    the original's hold was released and its row stamped terminal, so its own
    settlement later found nothing to charge and the caller received the whole
    synthesis for free. One `429 tts_busy` from a saturated GPU makes this the
    ordinary case rather than an exotic one.
    """
    wallet, ticket, _headers, body = await _open_stream(
        session, idempotency_key="retry-1"
    )
    first = await body.__anext__()

    # The retry lands while the original is still on the wire, and fails.
    upstream.status = 502
    with pytest.raises(BadGatewayError):
        await _replay(ticket.user_id, idempotency_key="retry-1")

    # The original finishes normally and is charged in full.
    async for _chunk in body:
        pass

    assert first == AUDIO_CHUNKS[0]
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS
    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED
    assert row.end_reason is SessionEndReason.COMPLETED
    assert row.settled_micros == PRICE_MICROS


async def test_a_replay_priced_differently_is_refused_while_the_original_runs(
    session, price_book, upstream
):
    """The race variant of a key reused for different text, which is the
    expensive one.

    Fire four thousand characters and one character concurrently under a single
    key and whichever settles first settles the *shared* session at its own
    character count — so four thousand characters of GPU time are billed as
    one. The replay is therefore checked against the session it names before
    any audio is asked for, and a mismatch is a refusal rather than a second
    rendering.

    Two texts four rounding buckets apart, which is the case the *price*
    comparison this guard used to make could still see. The test below is the
    one it could not: same bucket, same price, different text.
    """
    wallet, ticket, _headers, body = await _open_stream(
        session, text="a" * 4_000, idempotency_key="retry-1"
    )
    await body.__anext__()

    with pytest.raises(ConflictError) as raised:
        await _replay(ticket.user_id, text="a", idempotency_key="retry-1")

    assert raised.value.code == "tts_idempotency_conflict"
    assert len(upstream.bodies) == 1, "the cheap replay never reached the GPU"

    async for _chunk in body:
        pass

    assert await _available(wallet.wallet_id) == CREDIT - 4 * PRICE_MICROS
    (row,) = await _sessions()
    assert row.settled_micros == 4 * PRICE_MICROS
    assert row.cum_tts_characters == 4_000


async def test_a_replay_inside_the_originals_price_bucket_is_refused_too(
    session, price_book, upstream
):
    """The hole a price comparison cannot close, and the reason for the digest.

    The price book charges per thousand characters with CEIL rounding, so every
    text from one character to a thousand quotes 250000 micros. A guard that
    asks "does this replay cost the same?" therefore says yes to a thousand
    different requests, and its other test — "has the original finished?" — is
    the caller's to control: the session goes terminal in `_body`'s `finally`,
    which runs when the generator is exhausted, which happens as fast as the
    client reads. Pay for one character, stop reading that stream, and replay
    the key with any text up to a thousand characters, forever. Reproduced at a
    thousand characters a time against a single paid character, with no
    settlement and no second session to show for it.

    So the key is bound to a digest of the request instead. One character and a
    thousand characters of different text are the same price and are not the
    same request, and the refusal here happens with the original still ACTIVE —
    which is exactly the state the terminal check cannot speak for.
    """
    wallet, ticket, _headers, body = await _open_stream(
        session, text="a", idempotency_key="retry-1"
    )
    # One chunk and no more: the original is mid-stream, so its session is
    # ACTIVE and every gate except the digest is open.
    await body.__anext__()

    with pytest.raises(ConflictError) as raised:
        await _replay(ticket.user_id, text="b" * 1_000, idempotency_key="retry-1")

    assert raised.value.code == "tts_idempotency_conflict"
    assert len(upstream.bodies) == 1, "a thousand free characters never reached the GPU"

    async for _chunk in body:
        pass

    # One character, one priced unit, charged once — and nothing was delivered
    # against the same session for free.
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (row,) = await _sessions()
    assert row.cum_tts_characters == 1
    assert row.settled_micros == PRICE_MICROS


# --- the settlement outlives the request, and the process ---------------------


async def test_a_settlement_in_flight_at_shutdown_is_waited_for(
    session, price_book, upstream, monkeypatch
):
    """`asyncio.shield` alone loses the charge; the strong reference is the fix.

    The shield protects the settlement from a cancellation reaching the awaiter
    — a client disconnect — but it does nothing to keep that coroutine alive:
    the shielded task is referenced only by the frame the cancellation is
    unwinding. A SIGTERM therefore leaves the settlement as an orphan and the
    loop closes under it, so the charge is lost and the hold is stranded until
    the reaper finds it. Every one is held in `_PENDING_SETTLEMENTS` and joined
    by `drain_settlements()` from the lifespan shutdown, which is what the two
    assertions below pin: it is *in* the set while it runs, and the drain is
    what finishes it.
    """
    wallet, _ticket, _headers, body = await _open_stream(session)
    await body.__anext__()

    running = asyncio.Event()
    finish = asyncio.Event()
    real_settle = session_service.settle_oneshot

    async def gated_settle(*args, **kwargs):
        running.set()
        await finish.wait()
        return await real_settle(*args, **kwargs)

    monkeypatch.setattr(session_service, "settle_oneshot", gated_settle)

    # The client hangs up. `aclose()` waits on the shield, so the settlement is
    # in flight and unfinished at exactly the moment a shutdown would arrive.
    closing = asyncio.create_task(body.aclose())
    await running.wait()
    assert len(tts_service._PENDING_SETTLEMENTS) == 1, "an orphan nobody can join"

    finish.set()
    assert await tts_service.drain_settlements() == 1
    await closing

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS
    assert tts_service._PENDING_SETTLEMENTS == set()


async def test_the_drain_gives_up_on_a_slow_settlement_without_killing_it(caplog):
    """A bound that stops us *waiting*, not one that stops the work.

    The timeout used to be `asyncio.timeout` around a `gather`, and a
    timing-out gather is cancelled: `_GatheringFuture.cancel` forwards to every
    child, so the bound written to stop the shutdown hanging instead killed the
    settlements mid-transaction, inside the very shield that exists to make
    them uninterruptible. `CancelledError` is a `BaseException`, so
    `_finalise`'s `except Exception` never saw it — no rollback, and no
    `tts_settle_failed` line naming the session whose hold was now stranded.

    The diagnostic lied about it too, which is the second assertion here.
    `gather` returns only once its children have finished being cancelled, so
    every task was `done()` and the operator was told `unfinished=0` however
    many had just been killed. `asyncio.wait` observes instead of owning: the
    count is real and the task is still running when the drain returns.

    No database and no stream: `_tracked_shield` is the seam every settlement
    goes through, and what is under test is what the drain does to a task, not
    what the task was doing.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    outcome: list[str] = []

    async def settlement() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            outcome.append("killed")
            raise
        outcome.append("committed")

    awaiting = asyncio.create_task(tts_service._tracked_shield(settlement()))
    await started.wait()

    with caplog.at_level("ERROR", logger="synora.tts"):
        # Short enough that the test is not a stopwatch, long enough that the
        # loop has certainly had its turn.
        assert await tts_service.drain_settlements(timeout=0.05) == 1

    assert outcome == [], "the drain stopped waiting; it did not stop the work"
    assert any(
        "tts_drain_settlements_timeout unfinished=1" in record.getMessage()
        for record in caplog.records
    ), "an operator told unfinished=0 has no idea a hold was stranded"

    # The rest of the shutdown is what it was left to finish in, and it does.
    release.set()
    await awaiting
    assert outcome == ["committed"]
    assert tts_service._PENDING_SETTLEMENTS == set()


async def test_a_cancel_while_upstream_is_thinking_still_releases_the_hold(
    session, price_book, upstream
):
    """The pre-first-byte recovery path is shielded too, and needs to be.

    `stream_speech` can sit for `TTS_READ_TIMEOUT_SECONDS` — five minutes —
    waiting for a queueing GPU to send its headers, and the hold is already
    committed and durable for that whole window. A shutdown or an outer cancel
    scope firing there raises `CancelledError` inside `client.send`, and an
    unshielded `await` in a cancelled task re-raises at its first suspension:
    the database write that gives the credit back. The recovery would never
    run, and the credit would be frozen until the reaper's deadline.
    """
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=CREDIT)
    await session.commit()

    upstream.answer_when = asyncio.Event()  # the GPU is queueing
    call = asyncio.create_task(
        tts_service.synthesize(
            session,
            user,
            text=TEXT,
            quality="balanced",
            audio_format="mp3",
            sample_rate=48_000,
        )
    )
    # Only once the request is on the wire is the hold definitely placed and
    # the cancellation definitely landing inside `client.send`.
    await upstream.arrived.wait()
    assert await _reserved(snapshot.wallet_id) == PRICE_MICROS

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    # Joined rather than counted: the recovery is tracked, so the shutdown can
    # wait for it, and whether it had already finished on its own by now is a
    # scheduling detail this test has no business asserting on. What it does
    # assert is that it ran at all, which an unshielded await would not have.
    await tts_service.drain_settlements()
    assert await _reserved(snapshot.wallet_id) == 0
    assert await _available(snapshot.wallet_id) == CREDIT
    (row,) = await _sessions()
    assert row.status in TERMINAL_SESSION_STATUSES
    assert row.end_reason is SessionEndReason.CLIENT_DISCONNECTED
    assert row.hold_released_at is not None


async def test_a_settlement_that_dies_on_a_dead_database_leaves_a_reapable_row(
    session, price_book, upstream, monkeypatch, caplog
):
    """`_finalise` is documented "never raises", and the rollback is why it wasn't.

    The commonest reason to be in that exception handler at all is that the
    database went away — and rolling back over the same dead connection raises
    again, out of a function nobody is left to catch, through the shield, into a
    generator's `finally`. That escape took the log line with it: the one record
    naming the session whose hold is now stranded, which is the only thread
    anybody has to pull.

    Nothing in this function can save the charge, but it is not lost either: the
    row is left visibly unsettled and the deadline is what finishes it — at the
    estimate, because a stream that delivered a chunk committed the work whether
    or not anything survived to report it. The second half of this test is that
    hand-off actually working, and it is the whole reason the row is left
    looking live rather than quietly closed.
    """
    wallet, ticket, _headers, body = await _open_stream(session)
    await body.__anext__()

    class DeadConnection:
        """A pool handing out a connection to a database that has gone."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> bool:
            return False

        async def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("server closed"))

        async def rollback(self) -> None:
            # The second failure, and the one that used to escape: the same
            # dead connection cannot roll anything back either.
            raise OperationalError("ROLLBACK", {}, Exception("server closed"))

    monkeypatch.setattr(tts_service, "SessionLocal", DeadConnection)

    with caplog.at_level("INFO", logger="synora.tts"):
        await body.aclose()  # must not raise: this is the response ending

    assert any(
        record.getMessage().startswith(f"tts_stream session={ticket.ai_session_id}")
        for record in caplog.records
    ), "the only record naming the stranded session"

    # Nothing settled, so the hold is still on the wallet and the row still
    # reads as live — which is exactly what the deadline is for.
    assert await _reserved(wallet.wallet_id) == PRICE_MICROS
    (row,) = await _sessions()
    assert row.status is AiSessionStatus.ACTIVE
    assert row.hold_released_at is None

    async with SessionLocal() as db:
        await db.execute(
            update(AiSession)
            .where(AiSession.id == ticket.ai_session_id)
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()
        assert await reconcile_service.reap_expired_sessions(db) == 1

    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    (event,) = await _events()
    assert event.debited_micros == PRICE_MICROS, (
        "the charge the dead connection dropped, recovered on the deadline"
    )


# --- the wallet cannot pay by the time the work is done ----------------------


async def test_a_settlement_the_wallet_cannot_cover_still_ends_the_session(
    session, price_book
):
    """Past the hold release there is no way back, and no way to stop half way.

    `settle_oneshot` hands the hold back *before* it charges, which is what
    makes `available >= charge` a theorem — but only while the buckets do not
    move underneath it. They can: a bonus that was there when the hold was
    placed can lapse during the six hours a batch is allowed to run, and then
    `debit` answers `PaymentRequiredError`. Letting that escape rolls back the
    release, the usage event and the terminal stamp together, leaving the
    session `ACTIVE` holding the full price — and stably so, because every
    later attempt reaches the same failing `debit`, while the reconciler counts
    a live session's hold as legitimately held and reports no drift at all.

    So the released hold is treated as proof that the credit was committed to
    this session: collect what is actually there, write off the rest, and reach
    a terminal state whatever the balance now says. `disputed` is how support
    finds it, because a write-off of this shape is not the ordinary grace path.
    """
    # Above `billing_grace_micros`, or the shortfall would be absorbed silently
    # and this would be testing the grace path instead.
    characters = 24_000
    price = 6_000_000
    assert price > settings.billing_grace_micros

    user = await make_user(session)
    snapshot = await fund(session, user.id, bonus=price, bonus_days=1)
    await session.commit()

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
        scope="batch",
    )
    assert ticket.reserved_micros == price

    # The bonus lapses while the batch is running.
    async with SessionLocal() as db:
        await db.execute(
            update(Wallet)
            .where(Wallet.id == snapshot.wallet_id)
            .values(bonus_expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()

    # A session of its own, as the worker settling this job would have.
    async with SessionLocal() as db:
        settlement = await session_service.settle_oneshot(
            db,
            ai_session_id=ticket.ai_session_id,
            quantities={UsageMetric.TTS_CHARACTERS: characters},
            end_reason=SessionEndReason.COMPLETED,
        )

    assert settlement.price_micros == price
    assert settlement.debited_micros == 0, "there was nothing left to collect"
    assert settlement.writeoff_micros == price

    (row,) = await _sessions()
    assert row.status is AiSessionStatus.CLOSED, "terminal whatever the balance said"
    assert row.reserved_micros == 0
    assert row.hold_released_at is not None
    assert row.disputed is True, "support has to be able to find this one"
    # The customer's credit is not frozen behind a settlement that cannot land.
    assert await _reserved(snapshot.wallet_id) == 0


# --- settling twice ---------------------------------------------------------


async def test_settling_a_second_time_replays_instead_of_charging_again(
    client, session, price_book, upstream
):
    """`settle_oneshot` is called out of a `finally`, and a `finally` runs
    twice more often than anyone expects: a disconnect, an exception on the way
    out, a generator closed by two different owners. The second call has to
    return the first call's answer rather than a second charge."""
    token, wallet = await _funded(client, session, paid=CREDIT)
    await _speak(client, token)
    (row,) = await _sessions()

    async with SessionLocal() as db:
        again = await session_service.settle_oneshot(
            db,
            ai_session_id=row.id,
            quantities={UsageMetric.TTS_CHARACTERS: len(TEXT)},
            end_reason=SessionEndReason.COMPLETED,
        )

    assert again.replayed is True
    assert again.price_micros == PRICE_MICROS, "the first answer, rebuilt from the row"
    assert again.debited_micros == PRICE_MICROS
    assert len(await _events()) == 1
    assert len(await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT)) == 1
    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS


async def test_abandoning_a_settled_session_is_not_an_error_and_refunds_nothing(
    client, session, price_book, upstream
):
    """The other half of the same `finally`: whichever of settle and abandon
    runs second has to be a no-op. Releasing a hold that was already handed
    back would credit the wallet for money it never lost."""
    token, wallet = await _funded(client, session, paid=CREDIT)
    await _speak(client, token)
    (row,) = await _sessions()

    async with SessionLocal() as db:
        await session_service.abandon_oneshot(
            db,
            ai_session_id=row.id,
            end_reason=SessionEndReason.UPSTREAM_ERROR,
            error_code="tts_unreachable",
        )

    assert await _available(wallet.wallet_id) == CREDIT - PRICE_MICROS
    assert len(await _entries(wallet.wallet_id, LedgerEntryKind.RELEASE)) == 1
    after = (await _sessions())[0]
    assert after.status is AiSessionStatus.CLOSED
    assert after.end_reason is SessionEndReason.COMPLETED
