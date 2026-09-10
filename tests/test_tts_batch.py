"""A corpus through the batch path, on a deployment with no queue.

`RABBITMQ_URL` is empty in this suite, exactly as `REDIS_URL` is, and for the
same reason: the fallback that nobody runs is the fallback that does not work.
So every test here goes through the branch `create_job` takes when
`broker.publish` returns False — the request submits the job to upstream
itself, inline, before it answers. `test_broker.py` owns the broker's own
contract; this file owns what the absence of one does to a job and a wallet.

The interesting property is that nothing about the *billing* changes with the
broker. The route and `app/workers/tts_batch.py` call the same three functions,
so a deployment cannot charge differently depending on whether RabbitMQ
happened to be running — which is the kind of divergence nobody notices until a
customer compares two invoices.

Two numbers do all the work below: the `price_book` fixture's 1000 characters
to the unit at a quarter of a credit, and a two-item job of 1000 characters
each. So a whole job is 500_000 micros and half a job is 250_000.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.db.base import as_utc, utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    AiSessionStatus,
    LedgerEntryKind,
    SessionEndReason,
    TtsBatchJobState,
)
from app.models.ledger import LedgerEntry
from app.models.tts_job import TtsBatchJob
from app.models.usage import UsageEvent
from app.models.user import User
from app.services.ai import tts_batch_service, tts_client
from app.services.billing import reconcile_service, session_service, wallet_repo
from tests.conftest import auth, fund, register_and_verify

CREDIT = 1_000_000
UNIT_MICROS = 250_000

ITEM_CHARACTERS = 1_000
ITEMS = [
    {"id": "chapter-01", "text": "a" * ITEM_CHARACTERS},
    {"id": "chapter-02", "text": "b" * ITEM_CHARACTERS},
]
JOB_CHARACTERS = 2 * ITEM_CHARACTERS
JOB_MICROS = 2 * UNIT_MICROS

UPSTREAM_JOB_ID = "btch_91c0d3"


class FakeBatchBox:
    """Upstream's batch surface, as a state machine a test can drive.

    It keeps a `state` and a `usage` that the test moves by hand, because what
    is under test is what *we* do at each transition — not upstream's
    scheduler. Every call is recorded, so "reading a list must not poll" and
    "a settled job is never polled again" are assertions about the call log
    rather than about timing.
    """

    def __init__(self) -> None:
        self.state = "pending"
        self.usage: dict[str, object] = {"characters": 0, "audio_seconds": 0}
        self.completed_items = 0
        self.failed_items = 0
        self.results: list[dict] = []
        self.submitted: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.cancel_reports_usage = True
        # Refusals, per surface. A busy GPU answers the submit with 429 and a
        # box that is down answers the status poll with 502, and the two are
        # separate knobs because the interesting cases are one at a time: a
        # submit that fails must not destroy the payload, and a poll that fails
        # must not stop the owner reading their own job.
        self.submit_status = 202
        self.status_status = 200
        # Run once, while the submit POST is in flight. The only way to
        # reproduce a cancel landing inside that window, which is the race
        # `submit_job`'s conditional claim exists for.
        self.on_submit = None

    # The test's own vocabulary for "upstream got on with it".
    def finish(self, *, characters: int, audio_seconds: float = 0, failed: int = 0) -> None:
        self.state = "succeeded"
        self.usage = {"characters": characters, "audio_seconds": audio_seconds}
        self.failed_items = failed
        self.completed_items = len(ITEMS) - failed

    def _job(self) -> dict:
        return {
            "job_id": UPSTREAM_JOB_ID,
            "state": self.state,
            "total_items": len(ITEMS),
            "completed_items": self.completed_items,
            "failed_items": self.failed_items,
            "voice_id": "",
            "quality": "high_fidelity",
            "format": "wav",
            "error": None,
            "created_at": "2026-09-07T12:34:56Z",
            "updated_at": "2026-09-07T12:35:56Z",
            "usage": self.usage,
        }

    async def _speech(self):
        yield b"ID3fake-header"

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))

        if request.method == "POST" and path == "/v1/tts/stream":
            # The streaming surface, present only so a test can spend one
            # idempotency key on both routes and watch what the other one's
            # session does. The audio itself is beside the point — but it has to
            # arrive as a stream, because `tts_client` opens this response with
            # `stream=True` and relays it with `aiter_raw()`, and a `Response`
            # built from plain bytes has already consumed its own stream.
            return httpx.Response(
                200, headers={"content-type": "audio/mpeg"}, content=self._speech()
            )

        if request.method == "POST" and path == "/v1/batch":
            self.submitted.append(json.loads(request.content))
            if self.on_submit is not None:
                # Once only: this is somebody else moving the row while our
                # POST is on the wire, not a standing behaviour of the box.
                hook, self.on_submit = self.on_submit, None
                await hook()
            if self.submit_status != 202:
                return httpx.Response(
                    self.submit_status, json={"detail": "the card is full"}
                )
            return httpx.Response(202, json=self._job())
        if request.method == "GET" and path == f"/v1/batch/{UPSTREAM_JOB_ID}":
            if self.status_status != 200:
                return httpx.Response(
                    self.status_status, json={"detail": "the box is down"}
                )
            return httpx.Response(200, json=self._job())
        if request.method == "GET" and path == f"/v1/batch/{UPSTREAM_JOB_ID}/results":
            return httpx.Response(
                200,
                json={"job_id": UPSTREAM_JOB_ID, "state": self.state, "results": self.results},
            )
        if request.method == "DELETE" and path == f"/v1/batch/{UPSTREAM_JOB_ID}":
            self.state = "cancelled"
            payload = self._job()
            if not self.cancel_reports_usage:
                # Upstream answering the DELETE with no counters. The service
                # is then expected to go and read them rather than settle at
                # zero, which would give away every cancelled job's work.
                payload.pop("usage")
            return httpx.Response(200, json=payload)

        return httpx.Response(404, json={"detail": "no such job"})

    def status_calls(self) -> int:
        return sum(
            1
            for method, path in self.calls
            if method == "GET" and path == f"/v1/batch/{UPSTREAM_JOB_ID}"
        )


@pytest.fixture
def upstream(monkeypatch) -> FakeBatchBox:
    box = FakeBatchBox()
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


@pytest.fixture
def poll_now(monkeypatch) -> None:
    """Make every poll due the moment it is asked for.

    `refresh_job` rate-limits itself to one upstream call per
    `TTS_BATCH_POLL_SECONDS`, which is what makes polling in a tight loop free.
    Left at ten seconds, half the tests below would be asserting on a clock
    instead of on a settlement.
    """
    monkeypatch.setattr(settings, "tts_batch_poll_seconds", 0)


# --- helpers ----------------------------------------------------------------


async def _funded(client, session, *, paid: int = 2 * CREDIT, email: str = "ali@example.com"):
    tokens = await register_and_verify(client, email=email)
    user_id = (
        await session.execute(select(User.id).where(User.email == email))
    ).scalar_one()
    snapshot = await fund(session, user_id, paid=paid)
    await session.commit()
    return tokens["access_token"], snapshot


async def _create(client, token: str, **body):
    return await client.post(
        "/tts/batch", headers=auth(token), json={"items": ITEMS, **body}
    )


async def _available(wallet_id) -> int:
    async with SessionLocal() as db:
        return (await wallet_repo.snapshot_by_id(db, wallet_id)).available_micros


async def _reserved(wallet_id) -> int:
    async with SessionLocal() as db:
        return (await wallet_repo.snapshot_by_id(db, wallet_id)).reserved_micros


async def _job_row(job_id) -> TtsBatchJob:
    async with SessionLocal() as db:
        return await db.get(TtsBatchJob, uuid.UUID(str(job_id)))


async def _ai_session(job_id) -> AiSession:
    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(str(job_id)))
        return await db.get(AiSession, job.ai_session_id)


async def _events() -> list[UsageEvent]:
    async with SessionLocal() as db:
        return list((await db.execute(select(UsageEvent))).scalars())


async def _jobs() -> list[TtsBatchJob]:
    """Every job row. For the cases where the response carries no id to look up."""
    async with SessionLocal() as db:
        return list((await db.execute(select(TtsBatchJob))).scalars())


async def _sessions() -> list[AiSession]:
    async with SessionLocal() as db:
        return list((await db.execute(select(AiSession))).scalars())


async def _requeue(job_id) -> None:
    """Put a job back into the state a lost submit leaves behind.

    `queued`, never handed over, payload intact — which is what a dead-lettered
    submit message, a broker restart or a worker killed before its ack all
    leave on the row. Every recovery path below starts from here, and so does
    the deadline that has to fire when no recovery ever comes.
    """
    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(str(job_id)))
        job.state = TtsBatchJobState.QUEUED
        job.upstream_job_id = None
        job.submitted_at = None
        job.next_poll_at = None
        job.items_json = json.dumps(ITEMS)
        await db.commit()


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


# --- create -----------------------------------------------------------------


async def test_creating_a_job_prices_the_whole_corpus_and_holds_for_it(
    client, session, price_book, upstream
):
    """The hold is placed from the character counts in the payload, so a batch
    the wallet cannot cover is refused before any GPU time is spent rather than
    an hour into the work."""
    token, wallet = await _funded(client, session)

    response = await _create(client, token)

    assert response.status_code == 202
    body = response.json()
    assert body["submitted_characters"] == JOB_CHARACTERS
    assert body["estimated_micros"] == JOB_MICROS
    assert body["reserved_micros"] == JOB_MICROS
    assert body["settled_micros"] == 0
    assert body["billed_characters"] == 0
    assert body["is_terminal"] is False
    assert await _reserved(wallet.wallet_id) == JOB_MICROS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS


async def test_with_no_broker_the_request_hands_the_job_over_itself(
    client, session, price_book, upstream
):
    """`publish` returned False, so the job does not sit in `queued` waiting
    for a worker that does not exist. Upstream's own POST answers in
    milliseconds — it enqueues on its side too — so a queue that is merely
    absent must not become an outage."""
    token, _ = await _funded(client, session)

    body = (await _create(client, token)).json()

    assert body["state"] == TtsBatchJobState.SUBMITTED.value
    assert body["upstream_job_id"] == UPSTREAM_JOB_ID
    (submitted,) = upstream.submitted
    assert [item["id"] for item in submitted["items"]] == [item["id"] for item in ITEMS]
    assert submitted["items"][0]["text"] == ITEMS[0]["text"], "the text, verbatim"
    # Our own job id travels as upstream's idempotency key, so a redelivered
    # queue message gets the original job back instead of synthesising — and
    # billing us for — the whole corpus a second time.
    assert submitted["idempotency_key"] == body["id"]


async def test_the_payload_is_dropped_once_upstream_has_it(
    client, session, price_book, upstream
):
    """`items_json` exists to make a lost queue message recoverable, and stops
    being worth its weight the moment upstream is holding the text — the row is
    read on every poll."""
    token, _ = await _funded(client, session)

    body = (await _create(client, token)).json()

    job = await _job_row(body["id"])
    assert job.items_json is None
    assert job.next_poll_at is not None, "the first poll is scheduled"


async def test_a_batch_the_wallet_cannot_cover_is_refused_before_submission(
    client, session, price_book, upstream
):
    token, wallet = await _funded(client, session, paid=100_000)

    response = await _create(client, token)

    assert response.status_code == 402
    assert response.json()["shortfallMicros"] == JOB_MICROS - 100_000
    assert upstream.submitted == [], "no GPU time is spent on unpayable work"
    assert await _reserved(wallet.wallet_id) == 0


async def test_the_same_idempotency_key_returns_the_job_it_already_created(
    client, session, price_book, upstream
):
    """One key, one corpus, one hold. Without this a retried POST is a second
    job holding a second time the credit for the same work."""
    token, wallet = await _funded(client, session)

    first = await _create(client, token, idempotency_key="book-42")
    second = await _create(client, token, idempotency_key="book-42")

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(upstream.submitted) == 1
    assert await _reserved(wallet.wallet_id) == JOB_MICROS


# --- poll and settle --------------------------------------------------------


async def test_polling_a_finished_job_settles_it_at_what_upstream_reported(
    client, session, price_book, upstream, poll_now
):
    """On a deployment with no worker, reading the job is the only thing that
    ever advances it — so the read is what charges the wallet."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS, audio_seconds=90.5)

    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.SUCCEEDED.value
    assert body["is_terminal"] is True
    assert body["billed_characters"] == JOB_CHARACTERS
    assert body["settled_micros"] == JOB_MICROS
    assert body["reserved_micros"] == 0
    assert body["completed_items"] == len(ITEMS)
    # Reported because it is worth knowing, and never priced: there is no
    # `tts`/`tts_audio_ms` row, and pricing one would raise.
    assert body["audio_ms"] == 90_500
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS
    assert await _reserved(wallet.wallet_id) == 0

    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.CLOSED
    assert ai_session.end_reason is SessionEndReason.COMPLETED
    assert ai_session.cum_tts_characters == JOB_CHARACTERS
    assert ai_session.cum_tts_audio_ms == 0, "never written to a priced column"


async def test_items_that_failed_are_not_billed_for(
    client, session, price_book, upstream, poll_now
):
    """We hold against the characters we counted and charge the ones upstream
    says it synthesised. Nobody pays for audio that was never produced, and the
    difference goes back to the wallet rather than to us."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=ITEM_CHARACTERS, failed=1)

    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["submitted_characters"] == JOB_CHARACTERS
    assert body["billed_characters"] == ITEM_CHARACTERS
    assert body["failed_items"] == 1
    assert body["settled_micros"] == UNIT_MICROS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - UNIT_MICROS


async def test_a_job_still_running_is_reported_and_left_alone(
    client, session, price_book, upstream, poll_now
):
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.state = "running"
    upstream.completed_items = 1

    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.RUNNING.value
    assert body["is_terminal"] is False
    assert body["completed_items"] == 1
    assert body["settled_micros"] == 0
    assert await _reserved(wallet.wallet_id) == JOB_MICROS, "the hold stands"


async def test_the_list_route_never_asks_upstream_anything(
    client, session, price_book, upstream, poll_now
):
    """A page of twenty-five jobs would otherwise be twenty-five upstream calls
    made on behalf of somebody who only wanted to see a list."""
    token, _ = await _funded(client, session)
    await _create(client, token)
    before = upstream.status_calls()

    body = (await client.get("/tts/batch", headers=auth(token))).json()

    assert len(body["jobs"]) == 1
    assert upstream.status_calls() == before


async def test_another_users_job_is_a_404_rather_than_a_403(
    client, session, price_book, upstream
):
    """"No such job" and "not yours" are the same sentence to anyone who should
    not be able to tell the difference; a 403 would confirm which ids exist."""
    owner, _ = await _funded(client, session, email="ali@example.com")
    created = (await _create(client, owner)).json()
    stranger, _ = await _funded(client, session, email="bek@example.com")

    response = await client.get(f"/tts/batch/{created['id']}", headers=auth(stranger))

    assert response.status_code == 404
    assert response.json()["code"] == "tts_batch_not_found"


# --- billed once, and only once ---------------------------------------------


async def test_a_settled_job_is_never_charged_a_second_time(
    client, session, price_book, upstream, poll_now
):
    """Every caller of `settle_job` reaches it from somewhere that can run
    twice — a redelivered poll, a cancel racing the poller, an impatient client
    refreshing. The job's own terminal state is the guard, and
    `uq_usage_events_session_sequence` is the one underneath it."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS)

    for _ in range(3):
        body = (
            await client.get(f"/tts/batch/{created['id']}", headers=auth(token))
        ).json()

    assert body["settled_micros"] == JOB_MICROS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS
    assert len(await _events()) == 1
    debits = await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT)
    assert len({entry.group_id for entry in debits}) == 1
    assert sum(entry.amount_micros for entry in debits) == -JOB_MICROS
    assert len(await _entries(wallet.wallet_id, LedgerEntryKind.RELEASE)) == 1


async def test_a_terminal_job_is_not_polled_again(
    client, session, price_book, upstream, poll_now
):
    """The row leaves `ix_tts_batch_jobs_state_next_poll` for good when it
    settles, and reading it stops costing an upstream call."""
    token, _ = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS)
    await client.get(f"/tts/batch/{created['id']}", headers=auth(token))
    settled_after = upstream.status_calls()

    await client.get(f"/tts/batch/{created['id']}", headers=auth(token))

    assert upstream.status_calls() == settled_after
    job = await _job_row(created["id"])
    assert job.next_poll_at is None


async def test_settling_the_same_job_by_hand_changes_nothing(
    client, session, price_book, upstream, poll_now
):
    """The worker and the route call this same function, so it is reachable
    twice on any deployment where both exist. Whichever arrives second must
    find the job terminal and leave."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS)
    await client.get(f"/tts/batch/{created['id']}", headers=auth(token))

    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(created["id"]))
        again = await tts_batch_service.settle_job(
            db,
            job,
            state=TtsBatchJobState.SUCCEEDED,
            usage={"characters": JOB_CHARACTERS},
        )

    assert again.billed_characters == JOB_CHARACTERS
    assert len(await _events()) == 1
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS


# --- cancelling -------------------------------------------------------------


async def test_cancelling_settles_at_the_usage_reported_so_far(
    client, session, price_book, upstream
):
    """Not a refund. Work already done is billed for — half the corpus here —
    and only the rest of the hold goes back."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.usage = {"characters": ITEM_CHARACTERS, "audio_seconds": 45}
    upstream.completed_items = 1

    body = (await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.CANCELLED.value
    assert body["is_terminal"] is True
    assert body["billed_characters"] == ITEM_CHARACTERS
    assert body["settled_micros"] == UNIT_MICROS
    assert body["reserved_micros"] == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT - UNIT_MICROS

    ai_session = await _ai_session(created["id"])
    assert ai_session.end_reason is SessionEndReason.USER_CANCELLED


async def test_a_cancel_answered_without_counters_goes_and_reads_them(
    client, session, price_book, upstream
):
    """One extra read, because this is the number the charge is made from and
    defaulting it to zero would give away every cancelled job's work."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.cancel_reports_usage = False
    upstream.usage = {"characters": ITEM_CHARACTERS, "audio_seconds": 45}
    before = upstream.status_calls()

    body = (await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert upstream.status_calls() == before + 1
    assert body["billed_characters"] == ITEM_CHARACTERS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - UNIT_MICROS


async def test_cancelling_a_job_that_produced_nothing_charges_nothing(
    client, session, price_book, upstream
):
    """A zero charge moves no money, so it writes no ledger entry at all —
    which is exactly what a null `ledger_group_id` on the usage event
    documents."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()

    body = (await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["billed_characters"] == 0
    assert body["settled_micros"] == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT
    assert await _reserved(wallet.wallet_id) == 0
    assert await _entries(wallet.wallet_id, LedgerEntryKind.DEBIT) == []
    assert len(await _entries(wallet.wallet_id, LedgerEntryKind.RELEASE)) == 1


async def test_cancelling_a_settled_job_returns_it_untouched(
    client, session, price_book, upstream, poll_now
):
    """A cancel racing the poller is safe, which is why it is not an error."""
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS)
    await client.get(f"/tts/batch/{created['id']}", headers=auth(token))

    response = await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))

    assert response.status_code == 200
    assert response.json()["state"] == TtsBatchJobState.SUCCEEDED.value
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS
    assert len(await _events()) == 1


# --- results ----------------------------------------------------------------


async def test_results_report_our_own_state_beside_upstreams_items(
    client, session, price_book, upstream, poll_now
):
    """`state` comes from our row rather than from the results payload, so
    results and state cannot disagree about a job that settled between two
    reads — ours is the one the settlement branched on."""
    token, _ = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.finish(characters=JOB_CHARACTERS)
    upstream.results = [
        {"id": "chapter-01", "ok": True, "path": "/var/lib/tts/one.wav",
         "characters": ITEM_CHARACTERS, "audio_seconds": 45.0, "retries": 0},
        {"id": "chapter-02", "ok": False, "error": "the voice went away", "characters": 0},
    ]

    body = (
        await client.get(f"/tts/batch/{created['id']}/results", headers=auth(token))
    ).json()

    assert body["job_id"] == created["id"], "our id, not upstream's"
    assert body["state"] == TtsBatchJobState.SUCCEEDED.value
    assert [item["id"] for item in body["results"]] == ["chapter-01", "chapter-02"]
    assert body["results"][1]["ok"] is False


async def test_a_cancelled_job_that_was_never_billed_keeps_its_clips_to_itself(
    client, session, price_book, upstream
):
    """The delivery route nobody thinks of as one.

    `DELETE /tts/batch/{id}` on a job billed nothing settles at zero characters
    and hands the whole hold back, exactly as the cancel contract promises — and
    the GPU has usually been rendering the corpus the entire time the DELETE was
    in flight. This route used to key off `upstream_job_id` alone and ignore
    state entirely, so every clip finished before the cancel landed came back
    off that same id for nothing. Free synthesis through what looks like a
    metadata endpoint.

    So it asks the question `POST /tts/speech` asks before it opens a socket:
    has this been paid for. The refusal is a `409` with a code of its own rather
    than a `404`, because the job exists and the caller is entitled to know what
    happened to it — just not to its audio.

    The last assertion is the one that would still be worth having if the status
    code changed: refusing after fetching the clips is a route that has already
    lost them to a proxy log and a response buffer.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    # What the card rendered while the DELETE was on the wire.
    upstream.results = [
        {"id": "chapter-01", "ok": True, "path": "/var/lib/tts/one.wav",
         "characters": ITEM_CHARACTERS, "audio_seconds": 45.0, "retries": 0},
    ]

    cancelled = (
        await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))
    ).json()
    assert cancelled["billed_characters"] == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT, "the hold went back in full"

    response = await client.get(
        f"/tts/batch/{created['id']}/results", headers=auth(token)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "tts_batch_results_unbilled"
    assert ("GET", f"/v1/batch/{UPSTREAM_JOB_ID}/results") not in upstream.calls, (
        "refused before the clips were fetched, not after"
    )


async def test_a_cancelled_job_billed_for_what_it_rendered_still_hands_them_over(
    client, session, price_book, upstream
):
    """The other half, and the reason the test above is not `state is cancelled`.

    Same route, same cancel, one difference: upstream reported a chapter's worth
    of characters before the DELETE landed, so half the corpus was charged for.
    Anything above zero means this audio was paid for as far as it got — a
    partial failure, or a job that failed after being billed for the items that
    did run — and the clips behind that charge are owed to the caller. Billed,
    not finished, is what the condition reads.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.usage = {"characters": ITEM_CHARACTERS, "audio_seconds": 45}
    upstream.completed_items = 1
    upstream.results = [
        {"id": "chapter-01", "ok": True, "path": "/var/lib/tts/one.wav",
         "characters": ITEM_CHARACTERS, "audio_seconds": 45.0, "retries": 0},
    ]

    cancelled = (
        await client.delete(f"/tts/batch/{created['id']}", headers=auth(token))
    ).json()
    assert cancelled["billed_characters"] == ITEM_CHARACTERS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - UNIT_MICROS

    response = await client.get(
        f"/tts/batch/{created['id']}/results", headers=auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == TtsBatchJobState.CANCELLED.value
    assert [item["id"] for item in body["results"]] == ["chapter-01"]


# --- recovery ---------------------------------------------------------------


async def test_reading_a_job_upstream_never_received_hands_it_over_again(
    client, session, price_book, upstream, poll_now
):
    """The reason `items_json` is on the row at all.

    A job stuck in `queued` with its payload intact is a submit that was lost —
    a broker restart, a worker killed before its ack, or a crash between the
    commit and the inline submit. Polling one resubmits it, and the upstream
    idempotency key makes that free even if the original submit did land: the
    same key returns the same job rather than synthesising the corpus twice.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(created["id"]))
        job.state = TtsBatchJobState.QUEUED
        job.upstream_job_id = None
        job.items_json = json.dumps(ITEMS)
        await db.commit()
    upstream.submitted.clear()

    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.SUBMITTED.value
    assert body["upstream_job_id"] == UPSTREAM_JOB_ID
    (resubmitted,) = upstream.submitted
    assert resubmitted["idempotency_key"] == created["id"], "the same key as the first try"
    assert await _reserved(wallet.wallet_id) == JOB_MICROS, "one hold, not two"


async def test_a_submit_that_comes_back_finished_is_settled_on_the_spot(
    client, session, price_book, upstream
):
    """A redelivered submit can be answered by a job that has already run.

    Our own job id travels as upstream's idempotency key precisely so that a
    second submit returns the *original* job — and by the time it does, that job
    may well have finished. Writing that terminal state down and waiting for a
    poll is the end of the job: `refresh_job` returns immediately for anything
    terminal and the worker acks, so nothing ever polls it again. The hold would
    stand forever and GPU time upstream really spent would be billed at zero.
    So a terminal answer to a *submit* is settled where it arrives.
    """
    token, wallet = await _funded(client, session)
    upstream.finish(characters=JOB_CHARACTERS, audio_seconds=90.5)

    body = (await _create(client, token)).json()

    assert body["state"] == TtsBatchJobState.SUCCEEDED.value
    assert body["is_terminal"] is True
    assert body["billed_characters"] == JOB_CHARACTERS
    assert body["settled_micros"] == JOB_MICROS
    assert body["reserved_micros"] == 0
    assert body["audio_ms"] == 90_500
    # The two numbers the stranded-hold bug showed up as: nothing reserved, and
    # the work actually charged for.
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS
    assert len(await _events()) == 1

    ai_session = await _ai_session(body["id"])
    assert ai_session.status is AiSessionStatus.CLOSED
    assert ai_session.hold_released_at is not None

    job = await _job_row(body["id"])
    assert job.next_poll_at is None, "nothing was ever going to poll it again"


async def test_a_job_upstream_never_accepted_is_expired_at_its_deadline(
    client, session, price_book, upstream, monkeypatch
):
    """`TTS_BATCH_MAX_POLL_SECONDS` measured from `created_at`, not `submitted_at`.

    Keyed off `submitted_at` the deadline answers False forever for the one job
    that needs it most: a submit dead-lettered after three upstream 502s leaves
    the row `queued` with no `submitted_at` at all, and nothing else in this
    codebase releases its hold. Reading the job did not help either, because the
    `queued` branch resubmitted before the expiry was ever checked — so every
    read was another failing POST while the credit stayed frozen.

    The deadline therefore starts when the credit was taken, and it is checked
    above the resubmit. `upstream.submitted` staying empty is that ordering.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    await _requeue(created["id"])
    upstream.submitted.clear()
    # The whole point of the row: there is no `submitted_at` to measure from,
    # so a deadline keyed off it can never fire.
    assert (await _job_row(created["id"])).submitted_at is None
    # Everything created before now is past its deadline.
    monkeypatch.setattr(settings, "tts_batch_max_poll_seconds", 0)

    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.EXPIRED.value
    assert body["is_terminal"] is True
    assert body["settled_micros"] == 0, "upstream never saw it, so nobody owes for it"
    assert body["reserved_micros"] == 0
    assert upstream.submitted == [], "the deadline is read before the resubmit"
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT

    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.CLOSED
    assert ai_session.end_reason is SessionEndReason.TIMEOUT
    assert ai_session.hold_released_at is not None
    # Settled on incomplete information, which is the other reason to dispute
    # one: support has to be able to find these.
    assert ai_session.disputed is True


# --- one deadline, one clock ------------------------------------------------


async def _expire_session_now(job_id) -> None:
    """Put a job's session past its deadline, without waiting six hours.

    The window this reproduces is not exotic: `create_job` opened the session
    at one moment and the job's own clock starts when upstream accepted it, so
    every batch has a stretch in which the session is due and the job is not.
    """
    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(str(job_id)))
        await db.execute(
            update(AiSession)
            .where(AiSession.id == job.ai_session_id)
            .values(expires_at=utcnow() - timedelta(minutes=1))
        )
        await db.commit()


async def test_a_reconcile_pass_leaves_a_live_batchs_session_alone(
    client, session, price_book, upstream, poll_now
):
    """The reaper systematically beat `expire_job` to every long batch.

    A batch's session deadline was measured from `open_oneshot` and the job's
    from `submitted_at`, which is always later — by the broker hop, by the
    retry backoff, by hours when `refresh_job` resubmits a job upstream
    refused. In that window a reconcile pass found a non-terminal session past
    its deadline and abandoned it: the hold released in full, the row `FAILED`.
    The job was untouched, so it kept being polled, upstream rendered the whole
    corpus, and `settle_job` found the session already terminal, rebuilt a zero
    and stamped the job succeeded with `billed_characters` populated and
    nothing charged. Reproduced end to end: wallet back at its opening balance,
    `settled_micros=0`, and no usage event at all.

    So the reaper stands off any session a live job points at, and the second
    half of this test is the assertion that matters — the job that survived the
    pass is still billable when it finishes.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    await _expire_session_now(created["id"])

    async with SessionLocal() as db:
        assert await reconcile_service.reap_expired_sessions(db) == 0, (
            "the batch lifecycle owns this session; expire_job ends it"
        )

    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.ACTIVE
    assert ai_session.reserved_micros == JOB_MICROS
    assert await _reserved(wallet.wallet_id) == JOB_MICROS

    upstream.finish(characters=JOB_CHARACTERS)
    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.SUCCEEDED.value
    assert body["billed_characters"] == JOB_CHARACTERS
    assert body["settled_micros"] == JOB_MICROS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS
    (event,) = await _events()
    assert event.debited_micros == JOB_MICROS


async def test_the_submit_moves_the_sessions_deadline_onto_the_jobs_clock(
    client, session, price_book, upstream
):
    """The two clocks made one, in the transaction that stamps `submitted_at`.

    Standing off a live job keeps the reaper away while there *is* a job; this
    is what keeps the backstop meaningful when there is not. A batch whose row
    has gone still has to reach a deadline rather than hold credit forever, and
    it can only do that if `expires_at` and `_has_expired` agree about when
    that is.

    The session is dragged into the past first because that is the shape the
    bug left behind: a submit that arrives late — dead-lettered, backed off,
    or resubmitted hours later by `refresh_job` — used to inherit a deadline
    that had already passed while the job's own had not even started.
    """
    token, _wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    job_id = uuid.UUID(created["id"])
    await _requeue(job_id)
    await _expire_session_now(job_id)

    async with SessionLocal() as db:
        await tts_batch_service.submit_job(db, job_id)

    job = await _job_row(job_id)
    ai_session = await _ai_session(job_id)
    assert job.submitted_at is not None
    # The same `now` writes both, so this is an equality rather than a window.
    assert as_utc(ai_session.expires_at) == as_utc(job.submitted_at) + timedelta(
        seconds=settings.tts_batch_max_poll_seconds
    )
    assert as_utc(ai_session.expires_at) > utcnow(), (
        "a submit that inherits a deadline already in the past is the bug"
    )


# --- the sweeper: what standing off costs ------------------------------------


async def _strand_job(job_id) -> None:
    """Age a job past `TTS_BATCH_MAX_POLL_SECONDS`, without waiting six hours.

    Both timestamps, because `_deadline_of` measures from `submitted_at` when
    there is one and from `created_at` when there is not; editing one column
    would leave the deadline where it was on half the job shapes this pass
    exists for.
    """
    long_ago = utcnow() - timedelta(seconds=settings.tts_batch_max_poll_seconds + 60)
    async with SessionLocal() as db:
        job = await db.get(TtsBatchJob, uuid.UUID(str(job_id)))
        job.created_at = long_ago
        if job.submitted_at is not None:
            job.submitted_at = long_ago
        await db.commit()


async def test_a_job_nobody_polls_is_swept_and_gives_its_hold_back(
    client, session, price_book, upstream
):
    """The credit that was frozen forever, and the pass that gives it back.

    `expire_job` is reachable from `refresh_job` alone, which runs on a worker's
    poll message or on a read of the job. This deployment has no broker, so the
    only thing that ever advances a job is its owner reading it — and an owner
    who stops reading is not a rare event. A submit that was dead-lettered after
    three upstream 502s leaves the same row: non-terminal, hold in place, and
    nothing scheduled that will ever look at it again.

    The first half of this test is why no existing pass could rescue it. The
    reaper is *deliberately* forbidden to touch a session a live job points at —
    that exclusion is what stopped it closing healthy batches out from under
    their poller — so it answers zero here, correctly, and the hold stays.
    `heal_reserved` only stamps sessions that are already terminal and
    `verify_wallet` counts a live session's hold as legitimate, so the drift
    reads zero and nothing even alarms. The money was frozen with every audit
    green.

    Standing off therefore obliges the batch side to bring its own backstop, and
    `sweep_stale_jobs` is that obligation being met. It finishes the job through
    the paths that already exist rather than writing state of its own, which is
    why the assertions below are the ordinary expiry assertions: hold back in
    full, `EXPIRED`, session closed on a timeout and flagged for review because
    it settled on incomplete information.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    await _strand_job(created["id"])
    await _expire_session_now(created["id"])

    async with SessionLocal() as db:
        assert await reconcile_service.reap_expired_sessions(db) == 0, (
            "the reaper stands off a live job's session, so it cannot be the backstop"
        )
    assert await _reserved(wallet.wallet_id) == JOB_MICROS, "still frozen"

    async with SessionLocal() as db:
        assert await tts_batch_service.sweep_stale_jobs(db) == 1

    job = await _job_row(created["id"])
    assert job.state is TtsBatchJobState.EXPIRED
    assert job.is_terminal
    assert job.billed_characters == 0, "upstream rendered nothing it admitted to"
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT

    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.CLOSED
    assert ai_session.end_reason is SessionEndReason.TIMEOUT
    assert ai_session.hold_released_at is not None
    assert ai_session.disputed is True


async def test_the_sweep_leaves_a_job_still_inside_its_deadline_alone(
    client, session, price_book, upstream
):
    """The control, and the reason the scan reads a deadline rather than a state.

    A sweeper that finished every non-terminal job would settle live batches at
    whatever was last known about them — the same mistake the reaper made from
    the other side, and a worse one here, because a batch is allowed six hours
    and spends most of them looking exactly like a job nobody is polling.

    `status_calls` is the sharper half. A job inside its deadline is skipped on
    the scan's own columns, before it is loaded and before upstream is asked
    anything, so a pass over a busy deployment costs one query rather than one
    round trip per live job.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    before = upstream.status_calls()

    async with SessionLocal() as db:
        assert await tts_batch_service.sweep_stale_jobs(db) == 0

    assert upstream.status_calls() == before, "not even read, let alone settled"
    job = await _job_row(created["id"])
    assert job.state is TtsBatchJobState.SUBMITTED
    assert not job.is_terminal
    assert await _reserved(wallet.wallet_id) == JOB_MICROS, "a running job keeps its hold"
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS

    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.ACTIVE
    assert ai_session.hold_released_at is None


async def test_a_cancel_landing_during_the_submit_is_not_overwritten(
    client, session, price_book, upstream
):
    """A blind `state = submitted` after the POST resurrects a cancelled job.

    Cancelling a queued job is a documented thing for a user to do, and the row
    is read *before* the upstream call and written after it with a network round
    trip in between. In that window `cancel_job` settles the session at zero and
    releases the whole hold; a blind write then puts the job back to `submitted`,
    so it is polled again, upstream synthesises the entire corpus, and the
    eventual settlement finds the session already terminal and charges nothing.
    The user gets the batch and pays for none of it.

    The claim is conditional on the job still being `queued`, so the loser of
    that race is the submit — and since upstream has accepted work we can no
    longer bill for, the GPU is told to stop.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    job_id = uuid.UUID(created["id"])
    await _requeue(job_id)

    async def cancel_midflight() -> None:
        # The user's DELETE, on its own connection, exactly as the route would
        # run it: `upstream_job_id` is still null, so it settles at zero and
        # hands the whole hold back.
        async with SessionLocal() as db:
            await tts_batch_service.cancel_job(db, await db.get(TtsBatchJob, job_id))

    upstream.on_submit = cancel_midflight

    async with SessionLocal() as db:
        await tts_batch_service.submit_job(db, job_id)

    job = await _job_row(job_id)
    assert job.state is TtsBatchJobState.CANCELLED, "the cancel is the truth here"
    assert job.is_terminal
    assert ("DELETE", f"/v1/batch/{UPSTREAM_JOB_ID}") in upstream.calls, (
        "upstream accepted work nobody can be billed for; stop the card"
    )
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT

    ai_session = await _ai_session(job_id)
    assert ai_session.status is AiSessionStatus.CLOSED
    assert ai_session.end_reason is SessionEndReason.USER_CANCELLED


async def test_a_submit_landing_during_the_cancel_is_not_overwritten_either(
    client, session, price_book, upstream, monkeypatch, poll_now
):
    """The same race read from the other end, which the conditional claim missed.

    `submit_job` learned to write its state only if the row is still `queued`.
    `settle_job` kept writing blind, so the cancel that *lost* that claim still
    stamped `cancelled`, `billed_characters=0`, `items_json=NULL` over the
    submit that won it. The row then said cancelled while `upstream_job_id`
    named a live job: nothing polls a terminal row, so no DELETE was ever sent,
    the card rendered the whole corpus, `GET /tts/batch/{id}/results` handed
    every clip back off that same id, and the job was billed zero.

    The window is the settlement itself — several round trips and a commit
    between the state `settle_job` reads at entry and the state it writes — so
    the submit is fired from inside `settle_oneshot` here. `cancel_job` reached
    it with `upstream_job_id` still null, which is exactly why it skipped the
    upstream DELETE on its way in.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token)).json()
    job_id = uuid.UUID(created["id"])
    await _requeue(job_id)

    real_settle = session_service.settle_oneshot
    submitted: list[bool] = []

    async def submit_midsettle(*args, **kwargs):
        if not submitted:
            # Once only: this is the worker's message landing in the window,
            # not a standing behaviour of the settlement.
            submitted.append(True)
            async with SessionLocal() as db:
                await tts_batch_service.submit_job(db, job_id)
        return await real_settle(*args, **kwargs)

    monkeypatch.setattr(session_service, "settle_oneshot", submit_midsettle)

    async with SessionLocal() as db:
        await tts_batch_service.cancel_job(db, await db.get(TtsBatchJob, job_id))

    job = await _job_row(job_id)
    assert job.upstream_job_id == UPSTREAM_JOB_ID, "the submit won the claim"
    assert job.state is not TtsBatchJobState.CANCELLED, (
        "a cancelled row pointing at a live upstream job is the lost update"
    )
    assert ("DELETE", f"/v1/batch/{UPSTREAM_JOB_ID}") in upstream.calls, (
        "the hold is already released, so the card has to be stopped"
    )
    assert await _reserved(wallet.wallet_id) == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT

    # And it converges: the next read finds upstream cancelled and stamps the
    # row terminal, without charging the settled session a second time.
    body = (await client.get(f"/tts/batch/{created['id']}", headers=auth(token))).json()
    assert body["state"] == TtsBatchJobState.CANCELLED.value
    assert body["settled_micros"] == 0
    assert await _available(wallet.wallet_id) == 2 * CREDIT


async def test_a_busy_speech_service_does_not_cost_the_caller_their_corpus(
    client, session, price_book, upstream
):
    """A 429 on the inline submit leaves a job that can still be recovered.

    Failing the job here would call `settle_job`, which clears `items_json` —
    and nobody else has a copy of up to `TTS_BATCH_MAX_CHARACTERS` of text. "The
    GPU is busy, try again" would become "upload it all again", for a refusal
    that is asking to be retried. The row stays `queued` with its payload, which
    is exactly the state `refresh_job` resubmits from, and the caller's next read
    is the retry. The status still reaches the caller, `Retry-After` and all.
    """
    token, wallet = await _funded(client, session)
    upstream.submit_status = 429

    response = await _create(client, token)

    assert response.status_code == 429
    assert response.json()["code"] == "tts_busy"

    (job,) = await _jobs()
    assert job.state is TtsBatchJobState.QUEUED
    assert job.items_json is not None, "the only copy of the caller's corpus"
    assert await _reserved(wallet.wallet_id) == JOB_MICROS, "still priced and held"

    # And the next read is the retry, with no second hold and no re-upload.
    upstream.submit_status = 202
    body = (await client.get(f"/tts/batch/{job.id}", headers=auth(token))).json()

    assert body["state"] == TtsBatchJobState.SUBMITTED.value
    assert body["upstream_job_id"] == UPSTREAM_JOB_ID
    assert await _reserved(wallet.wallet_id) == JOB_MICROS


async def test_an_unreachable_speech_service_still_shows_the_job(
    client, session, price_book, upstream, poll_now
):
    """Advancing a job and reading one are two different things.

    A read is what advances a job where there is no worker, so `refresh_job`
    makes an upstream call — and an upstream call fails. It used to fail the read
    with it: while the speech service was unreachable, `502` was the only answer
    this route had, even though every field the caller came for is in our own
    database. An outage that stops a job progressing must not also stop its owner
    from looking at it.
    """
    token, _ = await _funded(client, session)
    created = (await _create(client, token)).json()
    upstream.status_status = 502

    response = await client.get(f"/tts/batch/{created['id']}", headers=auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == TtsBatchJobState.SUBMITTED.value, "the row as it stands"
    assert body["submitted_characters"] == JOB_CHARACTERS
    assert body["estimated_micros"] == JOB_MICROS


# --- one key, one surface ----------------------------------------------------


async def test_a_speech_call_cannot_settle_a_running_batchs_session(
    client, session, price_book, upstream
):
    """The idempotency key is scoped to the route as well as to the user.

    `uq_ai_sessions_idempotency_key` is global and cannot tell one kind of call
    from another, so with only the user on the front of the key a one-character
    `POST /tts/speech` sent under a running batch's key resolved to the *batch's*
    session: it settled it for one character, handed back the batch's entire
    hold, and the batch's own settlement later found nothing left to charge. Half
    a million characters of GPU time, billed at a quarter of a credit.
    """
    token, wallet = await _funded(client, session)
    created = (await _create(client, token, idempotency_key="book-42")).json()

    spoken = await client.post(
        "/tts/speech",
        headers={**auth(token), "Idempotency-Key": "book-42"},
        json={"text": "a"},
    )

    assert spoken.status_code == 200
    assert spoken.headers["X-Synora-Session-Id"] != created["ai_session_id"], (
        "the speech call opened a session of its own"
    )
    assert len(await _sessions()) == 2

    # The batch is untouched: still live, still holding, still owed for.
    ai_session = await _ai_session(created["id"])
    assert ai_session.status is AiSessionStatus.ACTIVE
    assert ai_session.reserved_micros == JOB_MICROS
    assert await _reserved(wallet.wallet_id) == JOB_MICROS
    assert await _available(wallet.wallet_id) == 2 * CREDIT - JOB_MICROS - UNIT_MICROS


async def test_a_batch_may_reuse_a_key_a_speech_call_has_spent(
    client, session, price_book, upstream
):
    """The same rule from the other side, where it reads as a false conflict.

    Unscoped, a key first spent on `/tts/speech` resolved to that session here
    too — and `create_job` refuses to attach a job to a session that has none,
    so the caller got a `409` for a key that was never theirs to collide with.
    Two different routes are never the same request.
    """
    token, wallet = await _funded(client, session)
    spoken = await client.post(
        "/tts/speech",
        headers={**auth(token), "Idempotency-Key": "shared"},
        json={"text": "a" * ITEM_CHARACTERS},
    )
    assert spoken.status_code == 200

    response = await _create(client, token, idempotency_key="shared")

    assert response.status_code == 202
    body = response.json()
    assert body["ai_session_id"] != spoken.headers["X-Synora-Session-Id"]
    assert body["reserved_micros"] == JOB_MICROS
    assert await _reserved(wallet.wallet_id) == JOB_MICROS


async def test_a_key_at_the_length_the_schema_publishes_is_accepted(
    client, session, price_book, upstream
):
    """The published ceiling and the enforced one are one number.

    They were two: the field advertised 128 while `open_oneshot` measured the
    stored key — user id, scope and colons included — against its own 128, so
    anything past 91 characters came back `400 idempotency_key_too_long`, naming
    a constraint that appeared nowhere in the contract. How much headroom the
    prefix needs is `session_service`'s business, and the schema repeats its
    answer rather than keeping a second one.
    """
    token, _ = await _funded(client, session)

    response = await _create(
        client, token, idempotency_key="k" * session_service.MAX_CLIENT_IDEMPOTENCY_KEY
    )

    assert response.status_code == 202
