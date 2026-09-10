"""The batch worker: `python -m app.workers.tts_batch`.

It consumes two queues and calls two functions. All of the billing lives in
`app/services/ai/tts_batch_service.py`, which the API calls too, so a job costs
the same whether this process is running or not — the alternative is an invoice
that depends on which deployment topology happened to be up, and nobody would
find that bug from the invoice.

## Why it refuses to start without RABBITMQ_URL

Because the API is correct without it. With no broker, `create_job` submits the
batch on the request that created it and polls it when someone reads it, so a
worker started against an unconfigured broker would not be degraded — it would
be a second, silent way to run the same code, connected to nothing. Failing
loudly at startup is the only honest answer, and the message says where the
work actually goes instead.

## Acknowledgement policy, which is the whole of the design

- **Success** acks. If the job is still running, the next poll is published as
  a delayed message *before* the ack, so a broker that refuses the publish
  leaves the current message requeued rather than dropping the job's only
  remaining timer.
- **A permanent failure** — upstream refused the payload, the job id is not a
  job, the message is not JSON — nacks without requeue, which is what the
  `x-dead-letter-exchange` on both work queues is for. Requeueing it would
  redeliver the same bytes to the same validator forever, and the DLQ is where
  a human can see that it happened.
- **A transient failure** — upstream unreachable, saturated, or briefly
  unconfigured — is re-published with a delay and a bounded attempt count. Not
  requeued: an immediate redelivery of a message whose failure was "the GPU is
  busy" is precisely the load that made it busy.
- **Anything else that escapes the service call** — an `OperationalError` from
  a database that blinked, a pool timeout, a connection reset mid-statement —
  is transient too and takes that same path. The classification is deliberately
  "an `AppError` the service raised as permanent is permanent, everything else
  is the world misbehaving", not the other way round, and with a broker the
  asymmetry is not close. The message in hand *is* the job's only future timer:
  the next poll exists only because this handler republishes it, and
  `expire_job` — the `TTS_BATCH_MAX_POLL_SECONDS` backstop — is reachable only
  from inside `refresh_job`. Dead-lettering a poll because Postgres hiccuped
  for five seconds therefore freezes that job's hold until a human reads the
  DLQ. Retrying a genuine bug in our code costs three stack traces and then the
  same DLQ entry, which is by far the cheaper mistake.
- **A follow-up publish that fails** hands the current message back to the
  broker, on a budget that belongs to *that message*: at most
  `MAX_PUBLISH_FAILURES` failures of its own, each one preceded by a pause, and
  only then the dead-letter queue. A broker that consumes happily while
  refusing publishes is a real state, not a hypothetical: a delay queue that
  already exists with different arguments answers the declaration with a 406
  and closes the channel, and consuming is unaffected. Bare `requeue=True`
  against that is a hot loop spinning as fast as Postgres will answer, once per
  prefetch slot, forever — hence the pause. What must *not* be shared is the
  count that decides the discard. `RABBITMQ_PREFETCH` handlers run
  concurrently, so a worker-wide counter is spent inside a single event-loop
  turn and the third message is dead-lettered before any of them has waited
  once — a job's only future timer thrown away 0.3 seconds into an outage that
  had not yet cost anyone a single retry.
- **The worker-wide streak is a circuit breaker instead.** Past
  `publish_outage_streak()` consecutive failures — one more than there are
  prefetch slots — the broker is refusing everyone rather than this message
  being unlucky, so every failing handler then holds its slot for
  `RETRY_DELAYS[-1]` instead of `RETRY_DELAYS[0]`.
  Prefetch counts *unacked* messages, so slots parked in that pause are slots
  the broker cannot refill: consumption stops for as long as the outage lasts,
  without cancelling a consumer, and resumes on the first publish that works.
  Discarding stays the message's own decision, and it loses that job's timer
  but not the job — `refresh_job` advances it the moment anyone reads it, and
  `POST /admin/reconcile` reaps the hold once `expires_at` passes. Both of
  those need a user or a human, which is exactly why the budget is three
  failures of this message and not one of somebody else's.

The attempt counter deliberately rides on the message rather than on the row,
so it counts *consecutive* failures — a poll that succeeds publishes a fresh
message with no attempts on it, and a job that is merely slow never exhausts a
budget meant for a job that is broken. The backstop for a job that never
finishes is not this counter at all: it is `TTS_BATCH_MAX_POLL_SECONDS`, which
`refresh_job` turns into a settlement.

## Shutting down, which is bounded on purpose

A signal cancels the consumers and then waits `DRAIN_TIMEOUT_SECONDS` for the
handlers already running. The bound is not a courtesy: a handler blocked on
`tts_client` holds its prefetch slot for up to `TTS_READ_TIMEOUT_SECONDS`
(300s), and Kubernetes' default `terminationGracePeriodSeconds` is 30 — so an
unbounded drain does not buy the work more time to finish, it guarantees the
SIGKILL lands mid-handler with the broker connection, the upstream HTTP pool
and the database engine all dropped uncleanly. Whatever is still running when
the bound expires is cancelled and counted in the log. Those messages were
never acked, so the broker redelivers them, and both handlers are built for
that: `submit_job` sends our own job id upstream as an idempotency key, and
`refresh_job` is a poll.

A second signal is the escape hatch, because the documented one does not exist
here. `loop.add_signal_handler` replaces Python's default SIGINT handler, so
Ctrl-C stops raising `KeyboardInterrupt` the moment this worker starts; a
second signal therefore has to do the cancelling itself, and a third restores
the default disposition and re-raises it, which is the operator asking for the
kill by hand and getting it.

Every message gets its own `SessionLocal()`, opened and closed inside the
handler, for the reason `app/db/session.py` gives: a session is a
request-shaped thing, and a long-lived one shared across messages would hold a
transaction open across upstream I/O it has no business waiting on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import uuid
from typing import TYPE_CHECKING, Any

from app.core.broker import (
    RK_BATCH_POLL,
    RK_BATCH_SUBMIT,
    close_broker,
    declare_topology,
    get_broker,
)
from app.core.config import settings
from app.core.exceptions import AppError
from app.db.session import SessionLocal, close_db
from app.services.ai import tts_batch_service, tts_client

if TYPE_CHECKING:  # pragma: no cover - types only, aio-pika is imported late
    from aio_pika.abc import AbstractIncomingMessage

logger = logging.getLogger("synora.worker.tts_batch")

# Backoff for a message whose handler failed transiently, and the attempt
# budget, which is the length of this tuple. Every distinct delay is a durable
# queue on the broker — see `broker.delay_queue_name` — so the set is small and
# fixed on purpose rather than computed from an exponent.
RETRY_DELAYS: tuple[int, ...] = (10, 60, 300)

# How many times *one message* may fail to schedule its follow-up before it is
# dead-lettered instead of requeued. Per message, and emphatically not per
# worker: `RABBITMQ_PREFETCH` handlers run concurrently, so a counter they share
# is spent by whichever messages happen to be in flight when the broker starts
# refusing, and the third of them is discarded in the same event-loop turn as
# the first — a job's only future timer thrown away before anything had waited
# even once. Three, because every failure here costs a pause first, so three is
# the shortest streak that is evidence about this message rather than about one
# bad moment on the broker.
MAX_PUBLISH_FAILURES = 3

# How long the shutdown drain waits for handlers that are already running.
# Kubernetes' default `terminationGracePeriodSeconds` is 30 and `docker stop`'s
# is 10, while a handler blocked on `tts_client` holds its slot for up to
# `TTS_READ_TIMEOUT_SECONDS` (300s). An unbounded drain therefore does not give
# the work more time — it guarantees a SIGKILL mid-handler, with the broker
# connection, the HTTP pool and the engine all dropped uncleanly. Twenty seconds
# leaves the rest of a default grace period for those three closes; the API side
# bounds its own drain at 30 for the same reason, in
# `tts_service.DRAIN_TIMEOUT_SECONDS`.
DRAIN_TIMEOUT_SECONDS = 20.0

# How long the cancellations that follow that bound get to unwind before
# `_serve`'s `finally` closes the connection and disposes the engine underneath
# them. Long enough for a `finally` and an `aclose`, short enough to disappear
# next to the bound above.
CANCEL_GRACE_SECONDS = 2.0

# Statuses that mean "ask again later" rather than "this will never work".
# 429 is upstream telling us the card is full, which is the one 4xx worth
# retrying; everything else below 500 is about the payload and will not change.
TRANSIENT_STATUSES = frozenset({429})


def publish_outage_streak() -> int:
    """Consecutive failures that mean the broker is refusing *everyone*.

    One more than the prefetch window, because that is the shortest streak that
    cannot be a coincidence: only `RABBITMQ_PREFETCH` messages are in flight and
    any success resets the count, so a streak longer than the window means some
    slot failed twice — and a slot only comes round again after a
    `RETRY_DELAYS[0]` pause. So the threshold reads "every slot has failed, and
    at least one of them has failed again after waiting", which a network blip
    does not produce and a misdeclared delay queue produces immediately.

    Reaching it stretches the pause in `_publish` from `RETRY_DELAYS[0]` to
    `RETRY_DELAYS[-1]`, and that is this worker's circuit breaker: prefetch
    counts *unacked* messages, so every slot parked in that pause is a slot the
    broker cannot refill, and consumption stops without anyone cancelling a
    consumer. The streak resets on the first publish that works, so a broker
    that recovers is consuming again one message later. Deliberately *not* the
    thing that discards anything — that is the per-message budget above.
    """
    return settings.rabbitmq_prefetch + 1


class BatchWorker:
    """One consumer process. Owns its connection and its shutdown."""

    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        # Set by a *second* signal, which is the escape hatch: see
        # `_request_stop`. Separate from `_stopping` because the two mean
        # different things to a handler — "finish what you are doing" and
        # "you are being cancelled".
        self._aborting = asyncio.Event()
        # The handlers currently in flight. `queue.consume` runs each callback
        # as its own task, so shutdown has to wait for them explicitly: a
        # message killed between the upstream call and the ack is a job whose
        # submission is repeated, and one killed mid-settlement is a hold
        # nothing releases until the reaper. Waiting is therefore worth
        # `DRAIN_TIMEOUT_SECONDS` and not more — see `_drain`, which spends that
        # budget and then abandons what is left rather than being SIGKILLed
        # holding the same messages plus three open resources.
        self._in_flight: set[asyncio.Task[Any]] = set()
        # Consecutive failed follow-up publishes across every handler, reset by
        # the first that succeeds. The circuit breaker, and nothing else: see
        # `publish_outage_streak()`.
        self._publish_failures = 0
        # Per-message publish budgets — `_budget_key(...)` -> failures so far.
        # An entry exists only while some message's publishes are failing, and
        # is dropped the moment one works, the message is discarded, or the job
        # reaches a terminal state, so this maps the jobs currently in trouble
        # rather than every job the process has seen.
        self._message_failures: dict[tuple[str, str], int] = {}

    # --- message handling ---------------------------------------------------

    def _decode(self, message: AbstractIncomingMessage) -> tuple[uuid.UUID, int] | None:
        """The job id and the attempt count, or None if the message is garbage."""
        try:
            body = json.loads(message.body)
            return uuid.UUID(str(body["job_id"])), int(body.get("attempt", 0))
        except (ValueError, TypeError, KeyError) as error:
            logger.error("Unreadable message on %s: %s", message.routing_key, error)
            return None

    async def _reject(self, message: AbstractIncomingMessage) -> None:
        """Dead-letter this message. Nothing here will make it work next time."""
        await message.nack(requeue=False)

    @staticmethod
    def _budget_key(routing_key: str, payload: dict[str, Any]) -> tuple[str, str]:
        """What "this message" means for the publish budget in `_publish`.

        Not the delivery. A message handed back with `requeue=True` comes round
        again as a different `AbstractIncomingMessage` carrying the same bytes,
        so a budget kept per delivery would reset on every redelivery and never
        run out — and the `attempt` counter in the body cannot help either,
        because a requeue republishes nothing and so cannot increment it. The
        job plus the follow-up we are trying to schedule for it is stable across
        redeliveries and is what an operator reading the DLQ would call "this
        message".

        The consequence worth stating: the budget is per worker *process*, so a
        message redelivered to a different worker starts again there. That errs
        toward keeping the message, which is the right side to err on when the
        thing being spent is a job's only future timer.
        """
        return routing_key, str(payload.get("job_id"))

    def _forget_budget(self, job_id: uuid.UUID) -> None:
        """This job has no more messages coming. Drop whatever budget it had."""
        for routing_key in (RK_BATCH_SUBMIT, RK_BATCH_POLL):
            self._message_failures.pop((routing_key, str(job_id)), None)

    async def _publish(
        self,
        message: AbstractIncomingMessage,
        routing_key: str,
        payload: dict[str, Any],
        delay: int,
    ) -> None:
        """Schedule a follow-up, then ack. Hand this message back if it fails.

        Published through `get_broker()` rather than the consumer's own channel,
        so the delay-queue machinery has exactly one implementation. It opens a
        second connection, which costs one socket and buys the guarantee that a
        publisher and a consumer cannot declare the same queue differently.

        Two counters, deliberately separate, because they answer different
        questions. `_message_failures[key]` is *this* message's budget and is the
        only thing allowed to discard it. `self._publish_failures` is the streak
        across every concurrent handler, and all it does is decide how long the
        pause is — the circuit breaker described at the top of this module. A
        shared counter that discards is the bug this split exists to prevent:
        with `RABBITMQ_PREFETCH` handlers failing inside one event-loop turn it
        dead-letters the third job's only timer before any of them has waited.
        """
        # This process consumes its own retries and polls, so a publish that
        # returns False is the broker refusing us while still feeding us —
        # a state it can hold indefinitely, because a delay queue declared with
        # different arguments 406s every publish while consuming carries on. So
        # the message goes back, but slowly and not forever.
        key = self._budget_key(routing_key, payload)
        if await get_broker().publish(routing_key, payload, delay_seconds=delay):
            self._publish_failures = 0
            self._message_failures.pop(key, None)
            await message.ack()
            return

        # Read the streak *before* counting this failure into it, so the handler
        # that trips the breaker still pays the short pause and only the ones
        # that fail after it pay the long one. Otherwise the first outage would
        # park every slot for `RETRY_DELAYS[-1]` on the strength of evidence
        # that arrived in the same turn as the decision.
        outage = self._publish_failures >= publish_outage_streak()
        self._publish_failures += 1
        failures = self._message_failures.get(key, 0) + 1
        self._message_failures[key] = failures
        discard = failures >= MAX_PUBLISH_FAILURES
        pause = RETRY_DELAYS[-1] if outage else RETRY_DELAYS[0]

        logger.error(
            "Could not schedule %s for job=%s (failure %d of %d for this message, "
            "%d in a row for this worker); pausing %ds, then %s",
            routing_key,
            payload.get("job_id"),
            failures,
            MAX_PUBLISH_FAILURES,
            self._publish_failures,
            pause,
            "dead-lettering it (a read still advances the job)"
            if discard
            else "requeueing it",
        )
        # The pause happens before the decision and on every branch, so nothing
        # is ever discarded that has not held still for at least
        # `RETRY_DELAYS[0]` first. Sleeping in a handler holds a prefetch slot,
        # which is the one thing this worker is otherwise careful never to do.
        # Here it is the point: we have just failed to schedule anything, so the
        # only backpressure left is to stop asking for a moment, and an unpaused
        # `requeue=True` costs a database round trip and a failed channel setup
        # per turn per slot. Waiting on the shutdown event rather than sleeping,
        # so a SIGTERM mid-backoff does not add ten seconds — or, in an outage,
        # five minutes — to every drain.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=pause)

        if discard and self._stopping.is_set():
            # Shutdown cut the pause short, so this message never got the wait
            # its last failure was supposed to buy, and the broker it could not
            # reach may well be reachable by the process that takes over. Hand
            # it back instead: a fresh budget somewhere else beats spending the
            # last of this one on a pause that did not happen.
            logger.warning(
                "Shutting down mid-backoff; requeueing %s for job=%s rather than "
                "spending its last publish attempt here",
                routing_key,
                payload.get("job_id"),
            )
        elif discard:
            # Another requeue would only be redelivered into the same refusal.
            # Dead-lettering loses this job's timer but not the job: reading it
            # through `GET /tts/batch/{job_id}` runs `refresh_job` on the spot,
            # and `POST /admin/reconcile` releases the hold once `expires_at`
            # passes. A DLQ entry a human can see beats a loop that buries its
            # own cause under a million identical log lines.
            self._message_failures.pop(key, None)
            await self._reject(message)
            return

        await message.nack(requeue=True)

    async def _retry(
        self,
        message: AbstractIncomingMessage,
        routing_key: str,
        job_id: uuid.UUID,
        attempt: int,
        reason: str,
    ) -> None:
        if attempt >= len(RETRY_DELAYS):
            logger.error(
                "Giving up on %s for job=%s after %d attempts (%s), dead-lettering",
                routing_key,
                job_id,
                attempt,
                reason,
            )
            self._forget_budget(job_id)
            await self._reject(message)
            return

        delay = RETRY_DELAYS[attempt]
        logger.warning(
            "Retrying %s for job=%s in %ds (attempt %d, %s)",
            routing_key,
            job_id,
            delay,
            attempt + 1,
            reason,
        )
        await self._publish(
            message,
            routing_key,
            {"job_id": str(job_id), "attempt": attempt + 1},
            delay,
        )

    async def _process(self, message: AbstractIncomingMessage, routing_key: str) -> None:
        decoded = self._decode(message)
        if decoded is None:
            await self._reject(message)
            return
        job_id, attempt = decoded

        try:
            # One session per message, opened here and closed before the ack.
            async with SessionLocal() as session:
                if routing_key == RK_BATCH_SUBMIT:
                    job = await tts_batch_service.submit_job(session, job_id)
                else:
                    job = await tts_batch_service.refresh_job(session, job_id)
                terminal = job.is_terminal
        except AppError as error:
            if error.status_code >= 500 or error.status_code in TRANSIENT_STATUSES:
                await self._retry(message, routing_key, job_id, attempt, error.code)
                return
            # The service has already settled the job and released the hold on
            # every permanent branch it has; the DLQ entry is the record that a
            # human should look at why.
            logger.error(
                "Permanent failure on %s for job=%s (%s), dead-lettering",
                routing_key,
                job_id,
                error.code,
            )
            self._forget_budget(job_id)
            await self._reject(message)
            return
        except Exception as error:  # noqa: BLE001 - deliberately the wide net
            # Not an `AppError`, so nothing classified it: a dropped connection,
            # a deadlock, a pool timeout — the database blinking rather than a
            # payload we are never going to accept. It retries, because letting
            # it fall through to `_handle` destroys the job's only future timer
            # (see the acknowledgement policy at the top of this module), and a
            # bug in our code that is genuinely deterministic still reaches the
            # DLQ three attempts later with three stack traces to read.
            # `CancelledError` is a `BaseException` and is deliberately not
            # caught here: a message cancelled during the drain is neither acked
            # nor nacked, so the broker redelivers it to the next process.
            logger.exception(
                "Transient failure on %s for job=%s (%s), retrying",
                routing_key,
                job_id,
                type(error).__name__,
            )
            await self._retry(
                message, routing_key, job_id, attempt, type(error).__name__
            )
            return

        if terminal:
            self._forget_budget(job_id)
            await message.ack()
            return

        # Not finished. The next poll is a delayed message rather than a sleep,
        # because a sleeping handler holds one of the `RABBITMQ_PREFETCH` slots
        # that are the only thing standing between the queue and the GPU.
        await self._publish(
            message,
            RK_BATCH_POLL,
            {"job_id": str(job_id)},
            settings.tts_batch_poll_seconds,
        )

    async def _handle(self, message: AbstractIncomingMessage, routing_key: str) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)
        try:
            await self._process(message, routing_key)
        except Exception:
            # `_process` now classifies everything the service can raise, so
            # what reaches here comes from the acknowledgement itself — a nack
            # on a channel the broker has already closed is the usual one. No
            # state of the world makes those work on a redelivery, so the
            # message goes to the DLQ with a stack trace next to it rather than
            # round the loop again.
            logger.exception("Unhandled failure on %s", routing_key)
            await self._reject(message)
        finally:
            if task is not None:
                self._in_flight.discard(task)

    # --- lifecycle ----------------------------------------------------------

    def _request_stop(self, sig: signal.Signals) -> None:
        """Drain, then abandon, then die — one step per signal.

        A repeat signal has to *do* something here, because the usual way out is
        gone: `_install_signal_handlers` registers SIGINT through
        `loop.add_signal_handler`, which replaces Python's default SIGINT
        handler, so Ctrl-C stops raising `KeyboardInterrupt` for as long as this
        worker runs. An operator whose drain is stuck behind a 300-second
        upstream read has no other lever, and "shutdown is already draining"
        told them so without offering one.

        The second signal cancels the handlers rather than killing the process,
        because cancelling is the *clean* abandonment: `_process` deliberately
        does not catch `CancelledError`, so a cancelled message is neither acked
        nor nacked and the broker redelivers it, while `run`'s `finally` still
        closes the connection and `_serve`'s still closes the HTTP pool and the
        engine. The third restores the signal's default disposition and re-raises
        it, which is exactly the kill the operator would have got had this
        handler never been installed.
        """
        if not self._stopping.is_set():
            logger.info("Received %s, finishing in-flight messages", sig.name)
            self._stopping.set()
            return

        if not self._aborting.is_set():
            self._aborting.set()
            in_flight = tuple(self._in_flight)
            logger.warning(
                "Received %s during the drain; cancelling %d in-flight message(s). "
                "They are unacked, so the broker redelivers them",
                sig.name,
                len(in_flight),
            )
            for task in in_flight:
                task.cancel()
            return

        logger.warning(
            "Received %s a third time; taking the default disposition now and "
            "leaving the broker to redeliver everything still unacked",
            sig.name,
        )
        # Reset before raising, or this handler catches its own signal and the
        # escape hatch becomes a loop. Signal handlers run on the main thread,
        # which is the only thread `signal.signal` may be called from.
        signal.signal(sig, signal.SIG_DFL)
        signal.raise_signal(sig)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop, sig)
            except NotImplementedError:  # pragma: no cover - not a POSIX loop
                signal.signal(
                    sig,
                    lambda number, _frame: self._request_stop(signal.Signals(number)),
                )

    async def run(self) -> None:
        # Deferred for the reason `RabbitBroker.__init__` defers it: the wheel
        # is optional for the API, so a missing one has to name itself here
        # rather than at an import six frames down.
        try:
            import aio_pika
        except ImportError as error:  # pragma: no cover - deployment mistake
            raise SystemExit(
                "aio-pika is not installed, so this worker cannot consume anything. "
                "Install it with `pip install -r requirements.txt`."
            ) from error

        self._install_signal_handlers()

        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        try:
            channel = await connection.channel()
            # The admission control, restated where it takes effect: this many
            # batch items are in flight against the card at once, and raising it
            # moves the queue into the GPU's own scheduler where nothing can
            # reorder, delay or cancel it.
            await channel.set_qos(prefetch_count=settings.rabbitmq_prefetch)
            # The same declaration the publisher makes, from the same function,
            # so the two cannot disagree and close each other's channels.
            topology = await declare_topology(channel, prefix=settings.rabbitmq_prefix)

            consumers = {
                key: await topology.work_queues[key].consume(
                    lambda message, key=key: self._handle(message, key)
                )
                for key in (RK_BATCH_SUBMIT, RK_BATCH_POLL)
            }
            logger.info(
                "Consuming %s and %s (prefix=%s, prefetch=%d)",
                RK_BATCH_SUBMIT,
                RK_BATCH_POLL,
                settings.rabbitmq_prefix,
                settings.rabbitmq_prefetch,
            )

            await self._stopping.wait()

            # Stop taking new work first, then let what is already running
            # finish. In the other order the drain never ends, because the
            # broker keeps refilling the prefetch window while we wait.
            for key, tag in consumers.items():
                await topology.work_queues[key].cancel(tag)
            await self._drain()
        finally:
            await connection.close()
        logger.info("Worker stopped")

    async def _drain(self) -> None:
        """Wait `DRAIN_TIMEOUT_SECONDS` for the handlers already running.

        `asyncio.wait` rather than `asyncio.gather`, because gather's timeout
        story is to cancel the whole group: what is wanted here is to *stop
        waiting*, and then to cancel deliberately and say how many. The count is
        the point of the log line — every abandoned handler is a message that
        was never acked, so the broker redelivers it, and both handlers are
        built for that (`submit_job` sends our own job id upstream as an
        idempotency key, and `refresh_job` is a poll). An operator reading
        "abandoned 3" knows three jobs will be picked up again elsewhere, not
        that three jobs were lost.

        The cancellations get `CANCEL_GRACE_SECONDS` to unwind before `run`'s
        `finally` closes the connection and `_serve`'s disposes the engine, so a
        handler's own `finally` — closing its `SessionLocal`, releasing its
        connection — runs against a database that is still there.
        """
        # Snapshot and emptiness check together, in one turn of the loop, so
        # there is no window in which `asyncio.wait` can be handed nothing.
        in_flight = tuple(self._in_flight)
        if not in_flight:
            return

        logger.info("Draining %d in-flight message(s)", len(in_flight))
        done, abandoned = await asyncio.wait(in_flight, timeout=DRAIN_TIMEOUT_SECONDS)
        self._log_handler_failures(done)
        if not abandoned:
            return

        logger.error(
            "Drain timed out after %.0fs; abandoning %d of %d in-flight message(s). "
            "They were never acked, so the broker redelivers them to the next "
            "process, which is the safe outcome — the alternative is the "
            "orchestrator's SIGKILL landing in the same place with the broker "
            "connection, the HTTP pool and the engine all dropped uncleanly",
            DRAIN_TIMEOUT_SECONDS,
            len(abandoned),
            len(in_flight),
        )
        for task in abandoned:
            task.cancel()
        unwound, _ = await asyncio.wait(abandoned, timeout=CANCEL_GRACE_SECONDS)
        self._log_handler_failures(unwound)

    @staticmethod
    def _log_handler_failures(tasks: set[asyncio.Task[Any]]) -> None:
        """Name what the handlers raised, and retrieve it while doing so.

        `asyncio.wait` — unlike the `gather(..., return_exceptions=True)` this
        replaced — leaves a task's exception unretrieved, which surfaces much
        later as a warning at garbage-collection time with no context attached.
        `_handle` already catches everything a handler can raise, so anything
        reaching here came from the acknowledgement itself — a nack on a channel
        the broker has closed underneath the drain is the usual one — and is
        worth exactly one line saying so.
        """
        for task in tasks:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                logger.error("A handler raised during the drain: %r", error)


async def _serve() -> None:
    worker = BatchWorker()
    try:
        await worker.run()
    finally:
        # The publisher connection, the upstream HTTP pool and the database
        # engine, all built lazily on first use and all owned by this process.
        await close_broker()
        await tts_client.aclose_client()
        await close_db()


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not settings.has_broker:
        raise SystemExit(
            "RABBITMQ_URL is not set, so there is nothing for this worker to consume. "
            "That is a supported configuration and not a mistake: with no broker the "
            "API submits each batch job inline on the request that creates it and "
            "refreshes it when someone reads it, so this process is genuinely "
            "optional. Set RABBITMQ_URL to move that work off the request path and "
            "behind RABBITMQ_PREFETCH, which is the admission control in front of the "
            "GPU."
        )
    if not settings.has_tts:
        raise SystemExit(
            "TTS_BASE_URL and TTS_API_KEY are not both set, so every job this worker "
            "picked up would fail at its first upstream call. Configure them, or run "
            "without this process."
        )

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C before the loop starts
        # Only reachable in the window before `_install_signal_handlers` runs,
        # or on a loop where `add_signal_handler` raised `NotImplementedError`.
        # Once the worker is consuming, SIGINT arrives as `_request_stop` and
        # never as this exception — which is why the escape hatch for a second
        # Ctrl-C lives there rather than here.
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
