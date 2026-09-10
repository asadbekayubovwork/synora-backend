"""The broker, and the deployment that has not got one.

`RABBITMQ_URL` is empty for this whole suite, deliberately, exactly as
`REDIS_URL` is. Both are optional infrastructure whose absence has to be an
ordinary configuration rather than an outage, and the only way to keep that
claim honest is to run the no-broker path as the default one. A fallback
nobody exercises is a fallback that does not work.

So what is under test here is the contract `publish` makes rather than
RabbitMQ: **False means "not queued", it is a normal answer, and every caller
must have a path that still works.** For batch submission that path is to hand
the job to upstream inline on the request that created it — which is why the
last test in this file is an HTTP round trip rather than a unit test.

There is no test of a live connection, and there could not be a useful one
here: `aio-pika` is imported inside `RabbitBroker.__init__` so that it stays
optional in practice as well as in principle, and a box with `RABBITMQ_URL` set
and no wheel installed is a deploy mistake that must degrade rather than 500
every batch request. That branch is forced below through `sys.modules`, so it
behaves the same whether or not the package happens to be installed.

The worker's acknowledgement policy is tested here too, at the bottom, because
it is the other half of the same contract: what a consumer does when the broker
will not take a publish, and what it does with a message whose handler failed.
Both decisions are about the queue rather than about billing, and both are
reachable without a broker by driving `BatchWorker` directly — which is the only
way to reach them at all in a suite that has no RabbitMQ.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core import broker as broker_module
from app.core.broker import (
    RK_BATCH_POLL,
    RK_BATCH_SUBMIT,
    WORK_ROUTING_KEYS,
    Broker,
    NullBroker,
    close_broker,
    dead_letter_queue_name,
    delay_queue_name,
    get_broker,
    work_exchange_name,
    work_queue_name,
)
from app.core.config import settings
from app.models.billing_enums import TtsBatchJobState
from app.models.user import User
from app.services.ai import tts_batch_service, tts_client
from app.workers import tts_batch as worker_module
from tests.conftest import auth, fund, register_and_verify

PREFIX = "synora."
UPSTREAM_JOB_ID = "btch_91c0d3"


# --- the null broker --------------------------------------------------------


async def test_publishing_without_a_broker_says_no_rather_than_pretending():
    """`is False`, not falsy: the caller branches on this to decide whether to
    do the work itself, and a `None` from a method that forgot to return would
    read identically in an `if` while meaning something entirely different."""
    broker = NullBroker()

    assert await broker.publish(RK_BATCH_SUBMIT, {"job_id": "j1"}) is False
    assert await broker.publish(RK_BATCH_POLL, {"job_id": "j1"}, delay_seconds=10) is False


async def test_the_null_broker_admits_it_is_not_available():
    broker = NullBroker()

    assert broker.is_available is False
    assert isinstance(broker, Broker)
    assert await broker.close() is None


async def test_the_app_runs_on_the_null_broker_when_no_url_is_set(monkeypatch):
    monkeypatch.setattr(broker_module, "_broker", None)

    assert settings.has_broker is False
    assert isinstance(get_broker(), NullBroker)


async def test_the_broker_is_built_once(monkeypatch):
    """Lazily rather than in the lifespan, because the lifespan does not run
    under `ASGITransport` and a lifespan-built broker would be `None` in every
    test — the same reasoning as `get_cache()` and the outbound HTTP client."""
    monkeypatch.setattr(broker_module, "_broker", None)

    assert get_broker() is get_broker()


async def test_closing_it_lets_the_next_caller_build_a_fresh_one(monkeypatch):
    monkeypatch.setattr(broker_module, "_broker", None)
    first = get_broker()

    await close_broker()

    assert broker_module._broker is None
    assert get_broker() is not first


async def test_a_url_with_no_aio_pika_installed_degrades_instead_of_failing(
    monkeypatch, caplog
):
    """A deploy mistake, not a user-visible failure. Taking the path that is
    known to work beats 500ing every batch request — but it is an ERROR line,
    because nothing retries this away and somebody has to install the wheel."""
    # `None` in `sys.modules` makes the import statement raise, whether or not
    # the package is present on the box running this.
    monkeypatch.setitem(sys.modules, "aio_pika", None)
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://guest:guest@localhost/")
    monkeypatch.setattr(broker_module, "_broker", None)

    with caplog.at_level("ERROR", logger="synora.broker"):
        broker = get_broker()

    assert isinstance(broker, NullBroker)
    assert any("aio-pika" in record.message for record in caplog.records)


# --- the names both sides have to agree on ----------------------------------


async def test_a_delay_queue_is_named_after_the_key_it_feeds():
    """One waiting room per routing key, and it has to be.

    A delayed message is published through the default exchange, so the queue's
    name *is* its routing key, and what sends it onward afterwards is the
    queue's fixed `x-dead-letter-routing-key`. One shared `...delay.10` queue
    would therefore deliver delayed submits into the poll queue, silently.
    """
    submit = delay_queue_name(RK_BATCH_SUBMIT, 10, PREFIX)
    poll = delay_queue_name(RK_BATCH_POLL, 10, PREFIX)

    assert submit != poll
    assert submit == f"{PREFIX}{RK_BATCH_SUBMIT}.delay.10"
    assert delay_queue_name(RK_BATCH_POLL, 60, PREFIX) != poll


async def test_every_name_is_namespaced_because_the_box_hosts_other_projects():
    names = [
        work_exchange_name(PREFIX),
        dead_letter_queue_name(PREFIX),
        *(work_queue_name(key, PREFIX) for key in WORK_ROUTING_KEYS),
    ]

    assert all(name.startswith(PREFIX) for name in names)
    assert len(set(names)) == len(names)


# --- the caller's fallback, end to end --------------------------------------


@pytest.fixture
def upstream(monkeypatch) -> list[dict]:
    """A speech box that accepts a batch. Returns the bodies it was sent."""
    submitted: list[dict] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/v1/batch")
        submitted.append(json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "job_id": UPSTREAM_JOB_ID,
                "state": "pending",
                "total_items": 1,
                "completed_items": 0,
                "failed_items": 0,
                "error": None,
                "usage": {"characters": 0, "audio_seconds": 0},
            },
        )

    monkeypatch.setattr(settings, "tts_base_url", "https://speech.test")
    monkeypatch.setattr(settings, "tts_api_key", "sk_live_test")
    monkeypatch.setattr(
        tts_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url="https://speech.test", transport=httpx.MockTransport(handle)
        ),
    )
    monkeypatch.setattr(tts_client, "_client_instance", None)
    return submitted


async def test_the_batch_route_still_works_with_no_broker(
    client, session, price_book, upstream, monkeypatch
):
    """The whole point of the contract. `publish` returned False, so the
    request that created the job handed it to upstream itself and answered with
    a job that is already `submitted` — not one parked in `queued` waiting for
    a worker that does not exist."""
    monkeypatch.setattr(broker_module, "_broker", None)
    tokens = await register_and_verify(client)
    user_id = (
        await session.execute(select(User.id).where(User.email == "ali@example.com"))
    ).scalar_one()
    await fund(session, user_id, paid=1_000_000)
    await session.commit()

    response = await client.post(
        "/tts/batch",
        headers=auth(tokens["access_token"]),
        json={"items": [{"id": "one", "text": "a" * 1_000}]},
    )

    assert isinstance(get_broker(), NullBroker), "no queue was involved"
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == TtsBatchJobState.SUBMITTED.value
    assert body["upstream_job_id"] == UPSTREAM_JOB_ID
    assert len(upstream) == 1
    assert body["estimated_micros"] == 250_000


# --- what the worker does with a message it could not finish -----------------


class FakeMessage:
    """One delivery, remembering which acknowledgement it was given.

    The three outcomes are the whole policy and they are not interchangeable:
    `ack` means done, `nack(requeue=True)` means "hand it straight back", and
    `nack(requeue=False)` means the dead-letter queue — which for a poll message
    is the job's only future timer thrown away.
    """

    def __init__(self, payload: dict[str, Any], routing_key: str) -> None:
        self.body = json.dumps(payload).encode()
        self.routing_key = routing_key
        self.acked = False
        self.nacked: list[bool] = []

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = False) -> None:
        self.nacked.append(requeue)


async def test_a_database_blip_retries_the_poll_instead_of_destroying_it(monkeypatch):
    """Anything that is not an `AppError` is the world misbehaving, not a verdict.

    The message in hand *is* the job's only future timer — with a broker, the
    next poll exists only because this handler republishes it, and `expire_job`,
    the `TTS_BATCH_MAX_POLL_SECONDS` backstop, is reachable only from inside
    `refresh_job`. So dead-lettering a poll because Postgres dropped a
    connection for five seconds freezes that job's hold until a human reads the
    DLQ. Retrying a genuine bug in our own code instead costs three stack traces
    and then the same DLQ entry, which is by far the cheaper way to be wrong.
    """
    job_id = str(uuid.uuid4())
    scheduled: list[tuple[str, dict, int]] = []

    async def blew_up(session, requested_id):  # noqa: ARG001 - the failure is the point
        raise OperationalError("SELECT 1", {}, Exception("connection reset by peer"))

    async def record(message, routing_key, payload, delay):
        scheduled.append((routing_key, payload, delay))
        await message.ack()

    monkeypatch.setattr(tts_batch_service, "refresh_job", blew_up)
    worker = worker_module.BatchWorker()
    monkeypatch.setattr(worker, "_publish", record)
    message = FakeMessage({"job_id": job_id}, RK_BATCH_POLL)

    await worker._process(message, RK_BATCH_POLL)

    assert message.nacked == [], "the DLQ is for payloads, not for a database blip"
    (routing_key, payload, delay) = scheduled[0]
    assert routing_key == RK_BATCH_POLL
    assert payload == {"job_id": job_id, "attempt": 1}
    assert delay == worker_module.RETRY_DELAYS[0]


async def test_a_broker_that_refuses_publishes_is_not_spun_against(monkeypatch):
    """A broker that consumes happily while refusing publishes is a real state.

    A delay queue that already exists with different arguments answers the
    declaration with a 406 and closes the channel; consuming is unaffected. A
    bare `nack(requeue=True)` against that is a hot loop — one database round
    trip and one failed channel setup per turn, per prefetch slot, forever,
    burying its own cause under a million identical log lines. So the requeue is
    bounded and paced: at most `MAX_PUBLISH_FAILURES` of them, never faster than
    `RETRY_DELAYS[0]`, and then the dead-letter queue. That loses the timer but
    not the job — a read still advances it, and the reaper still frees the hold.

    `RETRY_DELAYS` is replaced with a zero first delay so the pacing is real
    code taking a real (empty) pause rather than ten seconds of test runtime.
    """
    monkeypatch.setattr(worker_module, "RETRY_DELAYS", (0, 60, 300))
    monkeypatch.setattr(broker_module, "_broker", NullBroker())
    worker = worker_module.BatchWorker()
    payload = {"job_id": str(uuid.uuid4())}

    messages = [FakeMessage(payload, RK_BATCH_POLL) for _ in range(3)]
    for message in messages:
        await worker._publish(message, RK_BATCH_POLL, payload, 10)

    assert [message.nacked for message in messages] == [[True], [True], [False]]
    assert not any(message.acked for message in messages)


async def test_one_publish_that_works_forgives_the_ones_before_it(monkeypatch):
    """The counter is consecutive failures, not lifetime ones.

    It lives on the worker rather than on a message precisely because a broker
    refusing publishes refuses everybody's, so the evidence worth acting on is
    "three in a row across two pauses" and not "three since this process
    started". A queue that recovers must not leave the next unlucky message one
    failure away from the dead-letter queue.
    """
    monkeypatch.setattr(worker_module, "RETRY_DELAYS", (0, 60, 300))
    monkeypatch.setattr(broker_module, "_broker", NullBroker())
    worker = worker_module.BatchWorker()
    payload = {"job_id": str(uuid.uuid4())}

    refused = FakeMessage(payload, RK_BATCH_POLL)
    await worker._publish(refused, RK_BATCH_POLL, payload, 10)
    assert worker._publish_failures == 1

    class WorkingBroker:
        async def publish(self, *args, **kwargs) -> bool:  # noqa: ARG002
            return True

    monkeypatch.setattr(broker_module, "_broker", WorkingBroker())
    accepted = FakeMessage(payload, RK_BATCH_POLL)
    await worker._publish(accepted, RK_BATCH_POLL, payload, 10)

    assert accepted.acked is True
    assert worker._publish_failures == 0


async def test_three_unlucky_jobs_do_not_spend_one_anothers_budget(monkeypatch):
    """The discard is per message, and it has to be.

    `MAX_PUBLISH_FAILURES` used to be counted on the worker, which is shared by
    every handler `RABBITMQ_PREFETCH` lets run at once. Three *different* jobs
    each hitting one isolated publish failure therefore dead-lettered the
    third — a job's only future timer discarded on its first failure, with no
    backoff of its own and nothing about that job to justify it. The evidence
    the counter was collecting ("the broker is refusing everyone") is real, but
    it is evidence about the broker rather than about this message, so it now
    decides how long the pause is and nothing else.

    Three messages, three job ids, one failure each: all three go back to the
    queue. The worker-wide streak still counts them, which is the assertion at
    the bottom — the circuit breaker did not have to be removed to stop it
    discarding.
    """
    monkeypatch.setattr(worker_module, "RETRY_DELAYS", (0, 60, 300))
    monkeypatch.setattr(broker_module, "_broker", NullBroker())
    worker = worker_module.BatchWorker()

    messages = []
    for _ in range(worker_module.MAX_PUBLISH_FAILURES):
        payload = {"job_id": str(uuid.uuid4())}
        message = FakeMessage(payload, RK_BATCH_POLL)
        messages.append(message)
        await worker._publish(message, RK_BATCH_POLL, payload, 10)

    assert [message.nacked for message in messages] == [[True], [True], [True]], (
        "an unrelated job's bad luck is not evidence about this one"
    )
    assert not any(message.acked for message in messages)
    assert worker._publish_failures == worker_module.MAX_PUBLISH_FAILURES


async def test_a_prefetch_window_failing_at_once_still_keeps_every_message(monkeypatch):
    """The same rule under the timing that produced it.

    With the default prefetch four handlers are delivered together, so a broker
    that starts refusing publishes takes all four inside one event-loop turn.
    On a shared counter the third was nacked `requeue=False` 0.3 seconds into
    an outage — before any of the four had waited a single second of the
    backoff the module docstring promises them.
    """
    monkeypatch.setattr(worker_module, "RETRY_DELAYS", (0, 60, 300))
    monkeypatch.setattr(broker_module, "_broker", NullBroker())
    worker = worker_module.BatchWorker()

    payloads = [
        {"job_id": str(uuid.uuid4())} for _ in range(settings.rabbitmq_prefetch)
    ]
    messages = [FakeMessage(payload, RK_BATCH_POLL) for payload in payloads]
    await asyncio.gather(
        *(
            worker._publish(message, RK_BATCH_POLL, payload, 10)
            for message, payload in zip(messages, payloads, strict=True)
        )
    )

    assert all(message.nacked == [True] for message in messages)


# --- the shutdown, which has to end ------------------------------------------


async def test_a_handler_that_will_not_finish_does_not_hold_the_shutdown_open(
    monkeypatch, caplog
):
    """An unbounded drain does not give the work more time; it guarantees a kill.

    `_drain` was a `gather` over the in-flight handlers with no deadline, and a
    handler blocked on `tts_client` holds its slot for `TTS_READ_TIMEOUT_SECONDS`
    — five minutes, per prefetch slot. Kubernetes' default grace period is
    thirty seconds and `docker stop`'s is ten, so the process was SIGKILLed
    mid-handler with the broker connection, the HTTP pool and the engine all
    dropped uncleanly, which is strictly worse than abandoning the message: an
    abandoned message was never acked, so the broker redelivers it, and both
    handlers are built for exactly that.

    The bound is asserted by `wait_for` here rather than by a stopwatch: an
    unbounded drain does not return late, it does not return.
    """
    monkeypatch.setattr(worker_module, "DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(worker_module, "CANCEL_GRACE_SECONDS", 0.05)
    worker = worker_module.BatchWorker()

    stuck = asyncio.create_task(asyncio.sleep(300))
    worker._in_flight.add(stuck)

    with caplog.at_level("ERROR", logger="synora.worker.tts_batch"):
        await asyncio.wait_for(worker._drain(), timeout=5)

    assert stuck.cancelled(), "abandoned means cancelled and unacked, not waited on"
    assert any(
        "abandoning 1 of 1" in record.getMessage() for record in caplog.records
    ), "an operator has to be told how many messages the next process gets back"


async def test_a_second_signal_during_the_drain_is_an_escape_hatch(monkeypatch):
    """The documented Ctrl-C route does not exist once the handlers are installed.

    `_install_signal_handlers` registers SIGINT through `loop.add_signal_handler`,
    which replaces Python's default SIGINT disposition, so Ctrl-C stops raising
    `KeyboardInterrupt` for as long as the worker runs and `main()`'s
    `except KeyboardInterrupt` is unreachable. An operator whose drain is stuck
    behind a 300-second upstream read was told "shutdown is already draining"
    and given no lever at all.

    The second signal is that lever. Cancelling is the clean abandonment —
    `_process` deliberately does not catch `CancelledError`, so the message is
    neither acked nor nacked and the broker redelivers it — and `run`'s
    `finally` still closes the connection behind it.
    """
    worker = worker_module.BatchWorker()
    running = asyncio.Event()

    async def wedged() -> None:
        running.set()
        await asyncio.sleep(300)

    task = asyncio.create_task(wedged())
    worker._in_flight.add(task)
    await running.wait()

    worker._request_stop(signal.SIGTERM)
    assert worker._stopping.is_set()
    assert not task.cancelled(), "the first signal drains rather than kills"

    worker._request_stop(signal.SIGTERM)
    assert worker._aborting.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task
