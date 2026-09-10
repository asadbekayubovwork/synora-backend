"""RabbitMQ, and the admission control that is the actual reason for it.

There is one GPU behind the TTS box. Concurrency against a single card is not
a throughput dial, it is a cliff: past the point where the model's working set
stops fitting, everything in flight gets slower together and the failures
arrive as timeouts rather than as a queue that is visibly long. So the batch
path does not call upstream when the request arrives — it enqueues, and
`RABBITMQ_PREFETCH` is the number of batch items allowed in flight against the
card at once. That number *is* the admission control. It is the knob, and it is
why this module exists. "Async is nicer" is not a reason for a broker; the
reason is that a handful of workers with a small prefetch is a load the card
can hold, and two hundred concurrent HTTP handlers is not.

Saying where the queue deliberately is **not** matters just as much:

- **The synthesis streaming path.** Upstream delivers first audio in about
  100 ms and our end-to-end budget for the first byte is 180 ms. A broker round
  trip plus a worker pickup spends most of that before any audio moves, and
  buys nothing back: the caller is already blocked on the answer, so queueing
  only relocates the waiting to somewhere it cannot be streamed from.
- **The ledger.** A hold, the debit that settles it and the usage event that
  justifies the debit commit inside one Postgres transaction. Handing any part
  of that to a worker converts an invariant the database enforces into a
  message that may be delivered twice or not at all, and no amount of
  idempotency-key discipline buys back the ability to answer "is the balance
  right?" by reading one row.

## Every caller needs a working no-broker path

This degrades exactly the way `app/core/cache.py` does. `publish()` returns
False when `RABBITMQ_URL` is empty and when the broker refused, timed out or
disappeared, and **the caller is required to have a path that still works**
— the same contract `NullCache` sets, for the same reason. For batch
submission that path is to submit inline: upstream's own `POST /v1/batch`
answers in milliseconds, so a broker that is merely absent must never become an
outage. `RABBITMQ_URL` is empty in the test suite, which is what keeps that
claim honest — a fallback nobody runs is a fallback that does not work.

## Delayed retry without the plugin

Polling a batch job means "ask again in ten seconds", and the delayed-message
exchange plugin is one more thing to install on the box, remember at the next
upgrade, and discover missing at 3am. The two primitives the broker already
ships with compose into a scheduler: a message whose `x-message-ttl` expires is
dead-lettered, so a queue nothing consumes, whose dead-letter target is the
work exchange, is a waiting room with a timer on it.

**The name of a waiting room has to encode everything that defines it.** A
queue outlives the deploy that declared it, and RabbitMQ answers a declaration
that disagrees with an existing queue by so much as one argument with a 406
PRECONDITION_FAILED that closes the channel — so a name that can outlive a
change to its own arguments is a permanent, self-inflicted publish outage.
`delay_queue_name` therefore folds the prefix, the routing key and the delay
into the name, and every argument `_delay_queue` passes is a pure function of
those three: the TTL is the delay, the dead-letter routing key is the routing
key, and the dead-letter exchange is derived from the prefix. Changing any
setting that could change an argument changes the name with it, so there is no
configuration that produces a 406 here. Adding an argument that is *not* a
function of the name — an `x-expires`, a max length, a different dead-letter
target — would be, and it would only show up on a broker that already has the
old queue. If you add one, change the name too.

That is also why the 406 is reported as itself rather than through `_degrade`.
The broker is up and taking messages; one waiting room is unusable, and an
operator sent to look at the network will not find it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.config import settings

if TYPE_CHECKING:  # pragma: no cover - types only, aio-pika stays optional
    from aio_pika.abc import (
        AbstractChannel,
        AbstractExchange,
        AbstractQueue,
        AbstractRobustConnection,
    )

logger = logging.getLogger("synora.broker")


class _NeverRaised(Exception):
    """Stands in for an aio-pika exception name this version does not have.

    Catching it is a no-op, which is the honest outcome when the class we
    wanted to single out has been renamed: the failure still lands in the
    blanket handler and still returns False, we just cannot say why it failed.
    Better than a hard `from aio_pika.exceptions import ...`, which would turn
    that rename into an ImportError that `get_broker()` reports as "aio-pika is
    not installed" — a different problem, sending the reader somewhere else.
    """


class _DelayQueueMisdeclared(Exception):
    """The broker holds one of our delay queues with different arguments.

    Its own type, rather than a bool threaded back through `_publish`, because
    `publish` has to tell it apart from every other failure: this one is not an
    outage and must not set `_degraded_since`, or the "RabbitMQ unavailable" /
    "accepting messages again" pair flaps once per message while the broker is
    perfectly healthy.
    """


@runtime_checkable
class Broker(Protocol):
    """The narrow surface the rest of the app is allowed to depend on."""

    @property
    def is_available(self) -> bool: ...

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        *,
        delay_seconds: int = 0,
    ) -> bool:
        """Hand one JSON message to the broker. True once it is durably held.

        False means "not queued" — no broker configured, or the publish failed
        — and is a normal answer, not an exception, because every caller has to
        cope with it anyway. `delay_seconds` at or below zero publishes now.
        """

    async def close(self) -> None: ...


class NullBroker:
    """No RabbitMQ. Every method is a no-op that admits it is a no-op.

    `publish` returns False rather than pretending, so the caller takes its
    inline path instead of enqueueing into nowhere and waiting forever for a
    worker that does not exist. This is the configuration the test suite runs
    in, and the one a single-box deployment runs in until the GPU needs
    protecting.
    """

    @property
    def is_available(self) -> bool:
        return False

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        *,
        delay_seconds: int = 0,
    ) -> bool:
        return False

    async def close(self) -> None:
        return None


class RabbitBroker:
    """RabbitMQ, with every failure downgraded to the `NullBroker` answer.

    A broker outage should read like the broker being absent, which is a state
    the app is already designed for and already tested in — not like a new
    failure mode nobody has thought about. The connection is robust and heals
    itself, so a failed publish deliberately does not tear it down; it just
    returns False and lets the caller do the work inline this once.
    """

    def __init__(self, url: str, prefix: str, publish_timeout_seconds: float) -> None:
        # Imported here so `aio-pika` stays an optional runtime dependency in
        # practice as well as in principle, and so a box that has RABBITMQ_URL
        # set but no wheel installed fails at `get_broker()` with an ImportError
        # naming the package, rather than six frames down inside a publish.
        import aio_pika
        import aio_pika.exceptions

        self._aio_pika = aio_pika
        # Resolved by name for the reason `_NeverRaised` gives. This is the
        # exception aio-pika raises for a 406 PRECONDITION_FAILED, which is how
        # the broker refuses a declaration that disagrees with a queue it
        # already has.
        self._precondition_failed: type[BaseException] = getattr(
            aio_pika.exceptions, "ChannelPreconditionFailed", _NeverRaised
        )
        self._url = url
        self._prefix = prefix
        self._publish_timeout = publish_timeout_seconds
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._topology: Topology | None = None
        # Which delay queues this channel has already declared. Purely a
        # round-trip saver; the server, not this set, is the source of truth.
        self._declared_delays: set[str] = set()
        # Delay queues the broker has refused with a 406, so that line is
        # logged once per name instead of once per message. The declaration is
        # still attempted every time: an operator who deletes the stale queue
        # should heal this process without having to restart it.
        self._misdeclared_delays: set[str] = set()
        # One connection attempt at a time. Without it, a burst of requests
        # arriving on a cold process opens a connection each.
        self._lock = asyncio.Lock()
        self._degraded_since: float | None = None

    @property
    def is_available(self) -> bool:
        return self._degraded_since is None

    def _degrade(self, operation: str, error: BaseException) -> None:
        # Logged once per outage rather than once per message: a broker that is
        # down is down for every message in the backlog, and drowning the log
        # is how the *next* problem goes unnoticed.
        if self._degraded_since is None:
            self._degraded_since = time.monotonic()
            logger.error("RabbitMQ unavailable during %s: %s", operation, error)

    def _recover(self) -> None:
        if self._degraded_since is not None:
            logger.info("RabbitMQ is accepting messages again")
            self._degraded_since = None

    async def _ensure_ready(self) -> tuple[AbstractChannel, Topology]:
        """The connection, channel and topology, built on first use."""
        channel, topology = self._channel, self._topology
        if channel is not None and topology is not None and not channel.is_closed:
            return channel, topology

        async with self._lock:
            # Re-checked inside the lock: whoever held it before us has almost
            # certainly just built the thing we were about to build.
            channel, topology = self._channel, self._topology
            if channel is not None and topology is not None and not channel.is_closed:
                return channel, topology

            if self._connection is None or self._connection.is_closed:
                self._connection = await self._aio_pika.connect_robust(
                    self._url, timeout=self._publish_timeout
                )
            # Publisher confirms: without them `publish` returns as soon as the
            # socket takes the bytes, which is a lie we would only discover
            # when the job never ran.
            channel = await self._connection.channel(publisher_confirms=True)
            topology = await declare_topology(channel, prefix=self._prefix)
            self._declared_delays.clear()
            self._channel, self._topology = channel, topology
            return channel, topology

    async def _delay_queue(
        self, channel: AbstractChannel, routing_key: str, delay_seconds: int
    ) -> str:
        """Declare the waiting room for `routing_key`, and return its name."""
        name = delay_queue_name(routing_key, delay_seconds, self._prefix)
        if name in self._declared_delays:
            return name
        try:
            await channel.declare_queue(
                name,
                durable=True,
                # Every one of these is a pure function of the three parts of
                # `name` — see the module docstring. That is what makes a 406
                # here impossible to reach by configuration, and it is a rule
                # rather than a coincidence: an argument that does not follow
                # it needs the name changed with it.
                arguments={
                    "x-message-ttl": delay_seconds * 1000,
                    # Nothing consumes this queue. Expiry is the delivery
                    # mechanism: the message dead-letters into the work exchange
                    # under the key it was always meant to travel under.
                    "x-dead-letter-exchange": work_exchange_name(self._prefix),
                    "x-dead-letter-routing-key": routing_key,
                    # No `x-expires`. An idle delay queue reaped by the server
                    # between our declaration cache and a publish would fail that
                    # publish, and the set of delays in use is small and bounded by
                    # the poll settings anyway.
                },
            )
        except self._precondition_failed as error:
            # 406: the broker already holds this queue with other arguments, and
            # the declaration has just closed our channel (`_ensure_ready`
            # rebuilds it on the next call). Publishing anyway is not an option
            # even though the name routes: the message would sit in a waiting
            # room with somebody else's TTL and dead-letter target, and come
            # back late, or under the wrong key, or never — which is worse than
            # not being scheduled, because nothing would report it.
            if name not in self._misdeclared_delays:
                self._misdeclared_delays.add(name)
                logger.error(
                    "Delay queue %s already exists with different arguments (%s). "
                    "Delayed publishes on %s fail until it is deleted or this "
                    "delay is renamed; the broker itself is healthy.",
                    name,
                    error,
                    routing_key,
                )
            raise _DelayQueueMisdeclared(name) from error
        self._declared_delays.add(name)
        return name

    async def _publish(self, routing_key: str, body: bytes, delay_seconds: int) -> None:
        channel, topology = await self._ensure_ready()
        message = self._aio_pika.Message(
            body,
            content_type="application/json",
            content_encoding="utf-8",
            # Persistent message + durable queue + publisher confirms is the
            # whole of "the broker has it". Any one of the three missing and an
            # ack means only that a process accepted some bytes.
            delivery_mode=self._aio_pika.DeliveryMode.PERSISTENT,
        )
        # `mandatory` is aio-pika's default, but it is spelled out because it is
        # load-bearing: it turns a routing key that matches no binding into a
        # returned message and a False from `publish`, instead of a message the
        # broker cheerfully accepts and then drops.
        if delay_seconds > 0:
            queue = await self._delay_queue(channel, routing_key, delay_seconds)
            # The default exchange routes by queue name, so a delayed message is
            # addressed to the waiting room rather than to the work exchange.
            # The TTL expiry is what routes it for real, later.
            await channel.default_exchange.publish(
                message, routing_key=queue, mandatory=True
            )
        else:
            await topology.work_exchange.publish(
                message, routing_key=routing_key, mandatory=True
            )

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        *,
        delay_seconds: int = 0,
    ) -> bool:
        # `ensure_ascii=False` because payloads carry user-supplied identifiers
        # and Cyrillic escapes triple the body for no benefit; the content
        # encoding says utf-8 and every consumer here is ours.
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            # One budget covering connect, declare and confirm together. A
            # broker that is wedged rather than down answers nothing at all
            # until TCP eventually gives up, and the caller on the other end of
            # this is a live HTTP request that cannot wait that long.
            await asyncio.wait_for(
                self._publish(routing_key, body, delay_seconds),
                timeout=self._publish_timeout,
            )
        except asyncio.TimeoutError:
            self._degrade(
                f"publish {routing_key}",
                TimeoutError(f"no confirm within {self._publish_timeout}s"),
            )
            return False
        except _DelayQueueMisdeclared:
            # Already logged, once, with the queue name and the remedy. Not
            # routed through `_degrade`, because this broker is neither down nor
            # degraded — `is_available` would start lying, and the recovery line
            # would print on the next immediate publish as though something had
            # been fixed. False is still the answer: the caller has a path that
            # works without us, which is the whole contract of this module.
            return False
        except Exception as error:  # noqa: BLE001 - any failure is "no broker"
            self._degrade(f"publish {routing_key}", error)
            return False
        self._recover()
        return True

    async def close(self) -> None:
        try:
            if self._connection is not None and not self._connection.is_closed:
                await self._connection.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("Closing RabbitMQ raised: %s", error)
        finally:
            self._connection = None
            self._channel = None
            self._topology = None
            self._declared_delays.clear()
            self._misdeclared_delays.clear()


@dataclass(frozen=True)
class Topology:
    """What `declare_topology` built, for whoever needs to consume from it.

    The publisher only wants `work_exchange`; the worker wants the queues. Both
    get them from the same call, which is the point.
    """

    prefix: str
    work_exchange: AbstractExchange
    dead_letter_exchange: AbstractExchange
    dead_letter_queue: AbstractQueue
    work_queues: dict[str, AbstractQueue]


async def declare_topology(
    channel: AbstractChannel, *, prefix: str | None = None
) -> Topology:
    """Declare every exchange, queue and binding, from one place.

    Both sides call this — the publisher when it builds a channel, the worker
    when it starts — because the alternative is a publisher declaring a queue
    with one set of arguments and a worker declaring the same queue with
    another, which RabbitMQ answers with a 406 that closes the channel under
    whichever side arrived second. That failure is easy to cause, ugly to read
    and impossible to reproduce locally once the queue already exists, so the
    only defence worth having is that there is nowhere for the two to disagree.
    Declaration is idempotent, so calling it from both costs a few round trips
    per connection and nothing else.

    Delay queues are absent on purpose: they depend on a delay the publisher
    picks at send time, nothing consumes them, and the worker has no reason to
    know they exist.
    """
    from aio_pika import ExchangeType

    namespace = settings.rabbitmq_prefix if prefix is None else prefix

    work = await channel.declare_exchange(
        work_exchange_name(namespace), ExchangeType.TOPIC, durable=True
    )
    # Direct rather than fanout: the work queues set no
    # `x-dead-letter-routing-key`, so a dead letter keeps the key it died
    # under, and the DLQ's bindings double as the list of what is allowed to
    # land in it. A stray key ending up unroutable is a better outcome than a
    # dead-letter queue that quietly accepts anything.
    dead_letters = await channel.declare_exchange(
        dead_letter_exchange_name(namespace), ExchangeType.DIRECT, durable=True
    )
    dead_letter_queue = await channel.declare_queue(
        dead_letter_queue_name(namespace), durable=True
    )

    work_queues: dict[str, AbstractQueue] = {}
    for routing_key in WORK_ROUTING_KEYS:
        queue = await channel.declare_queue(
            work_queue_name(routing_key, namespace),
            durable=True,
            arguments={"x-dead-letter-exchange": dead_letters.name},
        )
        await queue.bind(work, routing_key=routing_key)
        await dead_letter_queue.bind(dead_letters, routing_key=routing_key)
        work_queues[routing_key] = queue

    return Topology(
        prefix=namespace,
        work_exchange=work,
        dead_letter_exchange=dead_letters,
        dead_letter_queue=dead_letter_queue,
        work_queues=work_queues,
    )


_broker: Broker | None = None


def get_broker() -> Broker:
    """The process-wide broker, built on first use.

    Lazily, not in the lifespan, because `tests/conftest.py` runs the app
    through `ASGITransport` where the lifespan never fires — a
    lifespan-constructed broker would be `None` in every test. Same reasoning
    as `get_cache()` and the outbound HTTP clients.
    """
    global _broker
    if _broker is None:
        if settings.has_broker:
            try:
                _broker = RabbitBroker(
                    settings.rabbitmq_url,
                    settings.rabbitmq_prefix,
                    settings.rabbitmq_publish_timeout_seconds,
                )
            except ImportError as error:
                # RABBITMQ_URL set and aio-pika missing is a deploy mistake, not
                # a user-visible failure: log it at ERROR and take the path that
                # is known to work rather than 500ing every batch request.
                logger.error("RABBITMQ_URL is set but aio-pika is not installed: %s", error)
                _broker = NullBroker()
            else:
                logger.info(
                    "RabbitMQ enabled (prefix=%s, prefetch=%d)",
                    settings.rabbitmq_prefix,
                    settings.rabbitmq_prefetch,
                )
        else:
            _broker = NullBroker()
            logger.info("No RABBITMQ_URL: batch jobs run inline")
    return _broker


async def close_broker() -> None:
    global _broker
    if _broker is not None:
        await _broker.close()
        _broker = None


# --- exchange, queue and routing-key names, in one place -------------------
#
# Spelled out here rather than formatted at each call site, so that the
# publisher, the worker and whoever is reading the management UI at 3am all
# agree on what a thing is called. Routing keys are deliberately un-prefixed:
# the exchange they travel through is already namespaced, and repeating the
# prefix inside a key would only make the bindings harder to read.

RK_BATCH_SUBMIT = "tts.batch.submit"
RK_BATCH_POLL = "tts.batch.poll"

WORK_ROUTING_KEYS: tuple[str, ...] = (RK_BATCH_SUBMIT, RK_BATCH_POLL)


def work_exchange_name(prefix: str) -> str:
    return f"{prefix}tts"


def dead_letter_exchange_name(prefix: str) -> str:
    return f"{prefix}dlx"


def work_queue_name(routing_key: str, prefix: str) -> str:
    """`synora.tts.batch.submit` — the queue is named after what it carries."""
    return f"{prefix}{routing_key}"


def dead_letter_queue_name(prefix: str) -> str:
    return f"{prefix}tts.dlq"


def delay_queue_name(routing_key: str, delay_seconds: int, prefix: str) -> str:
    """`synora.tts.batch.poll.delay.10` — one waiting room per key and delay.

    The routing key is part of the name because it has to be. A delayed message
    is published through the default exchange, so its own routing key becomes
    the queue's name, and the only way the message can find its way back to the
    work exchange afterwards is the queue's fixed `x-dead-letter-routing-key`.
    Sharing one `…delay.10` queue across both work keys would therefore deliver
    delayed submits into the poll queue, silently. Naming it this way also
    sorts every delay queue next to the queue it feeds in the management UI.

    The delay is in the name for a second reason: a queue outlives the deploy
    that declared it, and RabbitMQ answers a re-declaration whose arguments
    differ with a 406 that closes the channel. Because the TTL is the delay and
    the delay is in the name, changing `TTS_BATCH_POLL_SECONDS` moves to a new
    queue rather than colliding with the old one. Every argument in
    `RabbitBroker._delay_queue` has to keep that property; one that cannot
    belongs in this name.
    """
    return f"{prefix}{routing_key}.delay.{delay_seconds}"
