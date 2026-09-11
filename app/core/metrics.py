"""What Prometheus scrapes, and the three rules that keep it cheap.

Every counter here is incremented from the code that already knows the answer —
`_finalise` knows how a stream ended, `settle_oneshot` knows what was debited —
rather than inferred later from logs or from the database. A metric derived from
a second source is a metric that disagrees with the first one, and the whole
argument for metering on this side of the gateway is that there is one count.

## Rule one: no unbounded labels

Never a user id, a session id, a job id, a voice id or a wallet id. Prometheus
keeps one time series per label combination forever, so a label whose values
grow with the customer base grows the server's memory with it, and the panel it
was added for could have been a log query. The labels below are all closed sets:
HTTP method, the route *template* (never the path, which carries ids), a status
code, an end reason, an error code, a service. `route` is the one that needs
watching — it comes from Starlette's matched route, so an unmatched request is
labelled `unmatched` rather than with whatever the caller typed.

## Rule two: money is counted in micros, as integers

`synora_credits_debited_micros_total` and not a float of credits, for the same
reason nothing else in this codebase divides money before it has to. Grafana
divides by a million in the panel; the wire stays exact.

## Rule three: this process is the only one counting

The registry is in-process and the service runs `--workers 1` (see
`deploy/synora-api.service`, and the SQLite comment in it). A second worker
would mean two registries, and Prometheus scraping one of them at random —
counters that halve and then double as the reverse proxy picks a socket. If
`WORKER_COUNT` ever goes up, this module needs
`prometheus_client.multiprocess` and a shared directory, and that is a bigger
change than adding a flag — so until then `settings.serves_metrics` switches
the endpoint off rather than let it produce plausible nonsense.

## Gauges that only the database knows

Held credit, live sessions and open batch jobs cannot be counted in-process:
they survive restarts and they are the sum of rows, not of events. So
`refresh_db_gauges` runs three aggregates at scrape time and the `/metrics`
route awaits it. Three `SELECT SUM(...)`s every fifteen seconds is nothing next
to what a single synthesis does, and the alternative — deriving held credit from
hold and release counters — is wrong the first time a process restarts mid
stream. `METRICS_DB_GAUGES=false` turns them off for a deployment that would
rather not pay even that.

Recording is unconditional; only exposure is gated, by
`settings.serves_metrics`. An incremented counter nobody scrapes costs a
dictionary lookup, and a switch that silences the call sites as well is a
switch that makes a metrics bug look like an application bug. Nothing in this
module can refuse a boot either: a deployment with no `METRICS_TOKEN` serves
customers exactly as before and answers 404 here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

logger = logging.getLogger("synora.metrics")

# Our own registry rather than the global default. The default is also where
# `prometheus_client`'s process and platform collectors register themselves,
# which we want, so those are added explicitly below — the point of owning the
# registry is that importing this module twice under a reloader, or importing
# some third-party library that registers metrics of its own, cannot collide
# with names we chose.
REGISTRY = CollectorRegistry(auto_describe=True)

try:  # pragma: no cover - platform dependent
    from prometheus_client import PLATFORM_COLLECTOR, PROCESS_COLLECTOR

    REGISTRY.register(PROCESS_COLLECTOR)
    REGISTRY.register(PLATFORM_COLLECTOR)
except Exception:  # noqa: BLE001 - no /proc on this platform, or already there
    # `process_*` needs /proc and is simply absent on macOS. Worth having in
    # production and not worth a boot failure anywhere else.
    logger.debug("process/platform collectors unavailable")


# --- HTTP -------------------------------------------------------------------

http_requests = Counter(
    "synora_http_requests_total",
    "HTTP requests completed, by route template and status.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
http_request_seconds = Histogram(
    "synora_http_request_seconds",
    "Wall-clock time to the status line. For a streamed body this is the\n"
    "time to the headers, which is the number the caller waits on.",
    ("method", "route"),
    # A JSON route that takes more than a second here is a bug; the buckets are
    # dense where the answers live. `/tts/speech` is deliberately not measured
    # by this histogram — see `observe_http`.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
http_requests_inflight = Gauge(
    "synora_http_requests_inflight",
    "Requests currently being served.",
    registry=REGISTRY,
)
api_errors = Counter(
    "synora_api_errors_total",
    "Refusals by their stable error code, as the client saw them.",
    ("code", "status"),
    registry=REGISTRY,
)


# --- one metered stream -----------------------------------------------------

tts_streams = Counter(
    "synora_tts_streams_total",
    "Finished syntheses, by how they ended and what was charged. `charge` is "
    "`billed` when audio moved, `free` when not a byte did — an upstream "
    "refusal before the first chunk — and `replay` for a request that "
    "borrowed a session an earlier one already paid for.",
    ("end_reason", "charge"),
    registry=REGISTRY,
)
tts_characters = Counter(
    "synora_tts_characters_total",
    "Characters charged for. The quantity the price book prices.",
    registry=REGISTRY,
)
tts_audio_bytes = Counter(
    "synora_tts_audio_bytes_total",
    "Audio bytes relayed to callers.",
    registry=REGISTRY,
)
tts_streams_inflight = Gauge(
    "synora_tts_streams_inflight",
    "Syntheses with an open upstream response right now.",
    registry=REGISTRY,
)
tts_stream_seconds = Histogram(
    "synora_tts_stream_seconds",
    "Whole relay, from upstream's headers to the last chunk.",
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)


# --- the speech box, from our side of the socket -----------------------------

upstream_seconds = Histogram(
    "synora_tts_upstream_seconds",
    "Time upstream took to answer, by operation. For `stream` this is the "
    "headers only, which is the number that decides time to first audio.",
    ("operation",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0),
    registry=REGISTRY,
)
upstream_errors = Counter(
    "synora_tts_upstream_errors_total",
    "Upstream failures by the code we mapped them to, not by its status.",
    ("operation", "code"),
    registry=REGISTRY,
)


# --- the transcription box ---------------------------------------------------
#
# Its own metrics rather than another `operation` label on the speech box's,
# because the two are different upstreams with different failure vocabularies
# and one dashboard panel showing both would be answering two questions at
# once. The money side needs no such split: `service` already tells `tts` from
# `stt` on every counter below.

stt_upstream_seconds = Histogram(
    "synora_stt_upstream_seconds",
    "Time the transcription service took to answer, upload included.",
    buckets=(0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)
stt_upstream_errors = Counter(
    "synora_stt_upstream_errors_total",
    "Transcription failures by the code we mapped them to, not by status.",
    ("code",),
    registry=REGISTRY,
)
stt_audio_seconds = Counter(
    "synora_stt_audio_seconds_total",
    "Seconds of audio transcribed and charged for, as upstream counted them.",
    registry=REGISTRY,
)


# --- money ------------------------------------------------------------------

sessions_settled = Counter(
    "synora_sessions_settled_total",
    "Sessions charged and closed.",
    ("service", "end_reason"),
    registry=REGISTRY,
)
sessions_abandoned = Counter(
    "synora_sessions_abandoned_total",
    "Sessions closed with the hold given back and nothing charged.",
    ("service", "end_reason"),
    registry=REGISTRY,
)
credits_debited = Counter(
    "synora_credits_debited_micros_total",
    "Micro-credits actually taken from wallets.",
    ("service",),
    registry=REGISTRY,
)
credits_writeoff = Counter(
    "synora_credits_writeoff_micros_total",
    "Micro-credits billed for and not collected. Never silently: every one of "
    "these has a `disputed` session behind it.",
    ("service",),
    registry=REGISTRY,
)
sessions_clamped = Counter(
    "synora_sessions_clamped_total",
    "Settlements clamped to the session ceiling — a reporting service that "
    "exceeded the budget it was quoted.",
    ("service",),
    registry=REGISTRY,
)


# --- batch ------------------------------------------------------------------

batch_jobs = Counter(
    "synora_tts_batch_jobs_total",
    "Batch jobs that reached a terminal state.",
    ("state",),
    registry=REGISTRY,
)
batch_characters = Counter(
    "synora_tts_batch_characters_total",
    "Characters billed across batch jobs, as upstream reported them.",
    registry=REGISTRY,
)


# --- reconciliation ---------------------------------------------------------

reconcile_runs = Counter(
    "synora_reconcile_runs_total",
    "Reconciliation passes.",
    registry=REGISTRY,
)
reconcile_swept = Counter(
    "synora_reconcile_swept_total",
    "Expired bonus buckets swept.",
    registry=REGISTRY,
)
reconcile_reaped = Counter(
    "synora_reconcile_reaped_total",
    "Stranded sessions reaped, each one a hold that came back late.",
    registry=REGISTRY,
)
reconcile_healed_micros = Counter(
    "synora_reconcile_healed_micros_total",
    "Reserved micro-credits handed back by healing.",
    registry=REGISTRY,
)
reconcile_diverged = Gauge(
    "synora_reconcile_diverged",
    "Wallets whose balance disagreed with their ledger on the last pass. "
    "The only gauge here that should be alerted on at any value above zero.",
    registry=REGISTRY,
)
reconcile_checked = Gauge(
    "synora_reconcile_checked",
    "Wallets examined on the last pass.",
    registry=REGISTRY,
)


# --- what only the database knows -------------------------------------------

wallet_reserved_micros = Gauge(
    "synora_wallet_reserved_micros",
    "Micro-credits held by sessions that have not settled. Should fall back to "
    "a floor of zero-ish between calls; a floor that climbs is a hold leak.",
    registry=REGISTRY,
)
wallet_balance_micros = Gauge(
    "synora_wallet_balance_micros",
    "Micro-credits sitting in wallets, by bucket.",
    ("bucket",),
    registry=REGISTRY,
)
ai_sessions_open = Gauge(
    "synora_ai_sessions_open",
    "Sessions in a non-terminal state, by status.",
    ("status",),
    registry=REGISTRY,
)
batch_jobs_open = Gauge(
    "synora_tts_batch_jobs_open",
    "Batch jobs still being polled, by state.",
    ("state",),
    registry=REGISTRY,
)


# --- call sites -------------------------------------------------------------
#
# Thin wrappers rather than touching the metric objects from the services. Two
# reasons: a call site reads as a sentence about the domain, and every label
# value is coerced to a string in one place, so a metric cannot acquire an
# `end_reason` of `SessionEndReason.COMPLETED` in one branch and `completed` in
# another and end up as two series that nobody thinks to add together.


def observe_http(*, method: str, route: str, status: int, seconds: float | None) -> None:
    """One finished request. `seconds` is None when no response ever started.

    Timing stops at the status line, not at the last byte. On `/tts/speech` the
    difference is the whole audio: measuring to the end would file however long
    the *client* took to read a five-minute stream under our own latency, and
    the p95 of this histogram would then track listening habits rather than the
    service. `synora_tts_stream_seconds` measures the relay, where that number
    means something.
    """
    http_requests.labels(method=method, route=route, status=str(status)).inc()
    if seconds is not None:
        http_request_seconds.labels(method=method, route=route).observe(seconds)


def record_api_error(*, code: str, status: int) -> None:
    api_errors.labels(code=code, status=str(status)).inc()


def record_stream(
    *,
    end_reason: str,
    characters: int,
    audio_bytes: int,
    replayed: bool = False,
) -> None:
    """A synthesis that is over, however it ended.

    The three quantities are deliberately not interchangeable. Audio bytes are
    bandwidth and are counted whenever they moved, replay or not. Characters
    are *revenue* and are counted only when this request is what produced the
    charge — a replay synthesises the audio again and is charged nothing, so
    adding its characters here would double the number the money panel divides
    by, and the dashboard would disagree with `usage_events` for the one reason
    hardest to spot later.
    """
    charge = "replay" if replayed else "billed" if audio_bytes else "free"
    tts_streams.labels(end_reason=end_reason, charge=charge).inc()
    if audio_bytes:
        tts_audio_bytes.inc(audio_bytes)
    if charge == "billed":
        tts_characters.inc(characters)


def observe_upstream(*, operation: str, seconds: float) -> None:
    upstream_seconds.labels(operation=operation).observe(seconds)


def record_upstream_error(*, operation: str, code: str) -> None:
    upstream_errors.labels(operation=operation, code=code).inc()


def observe_stt_upstream(*, seconds: float) -> None:
    stt_upstream_seconds.observe(seconds)


def record_stt_upstream_error(*, code: str) -> None:
    stt_upstream_errors.labels(code=code).inc()


def record_transcription(*, audio_seconds: float) -> None:
    """One charged transcription, in the unit the price book prices."""
    stt_audio_seconds.inc(audio_seconds)


def record_settlement(
    *,
    service: str,
    end_reason: str,
    debited_micros: int,
    writeoff_micros: int,
    clamped: bool,
) -> None:
    sessions_settled.labels(service=service, end_reason=end_reason).inc()
    if debited_micros:
        credits_debited.labels(service=service).inc(debited_micros)
    if writeoff_micros:
        credits_writeoff.labels(service=service).inc(writeoff_micros)
    if clamped:
        sessions_clamped.labels(service=service).inc()


def record_abandon(*, service: str, end_reason: str) -> None:
    sessions_abandoned.labels(service=service, end_reason=end_reason).inc()


def record_batch_job(*, state: str, characters: int) -> None:
    batch_jobs.labels(state=state).inc()
    if characters:
        batch_characters.inc(characters)


def record_reconcile(report: Mapping[str, int]) -> None:
    """One pass of `reconcile_all`, from the dict it already returns."""
    reconcile_runs.inc()
    reconcile_swept.inc(int(report.get("swept", 0)))
    reconcile_reaped.inc(int(report.get("reaped", 0)))
    reconcile_healed_micros.inc(int(report.get("healed_micros", 0)))
    reconcile_diverged.set(int(report.get("diverged", 0)))
    reconcile_checked.set(int(report.get("checked", 0)))


async def refresh_db_gauges() -> None:
    """Read the four things a counter cannot know. Never raises.

    Imported inside the function: `app.core` must not import `app.models` at
    module scope, or `app.db.base` and everything under it is dragged into
    Alembic's `env.py` and into any tool that only wanted the settings.

    A failure here is logged and swallowed rather than turned into a 500. The
    endpoint's job is to hand Prometheus the counters this process has been
    collecting all along; refusing to serve any of them because one aggregate
    could not run would blind the dashboard exactly when the database is the
    thing going wrong.
    """
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.models.ai_session import AiSession
    from app.models.billing_enums import (
        TERMINAL_BATCH_STATES,
        TERMINAL_SESSION_STATUSES,
        AiSessionStatus,
        TtsBatchJobState,
    )
    from app.models.tts_job import TtsBatchJob
    from app.models.wallet import Wallet

    try:
        async with SessionLocal() as session:
            totals = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(Wallet.reserved_micros), 0),
                        func.coalesce(func.sum(Wallet.paid_micros), 0),
                        func.coalesce(func.sum(Wallet.bonus_micros), 0),
                    )
                )
            ).one()
            wallet_reserved_micros.set(int(totals[0]))
            wallet_balance_micros.labels(bucket="paid").set(int(totals[1]))
            wallet_balance_micros.labels(bucket="bonus").set(int(totals[2]))

            # Grouped rather than counted per status in a loop: one round trip,
            # and a status nobody thought of still shows up.
            open_sessions = (
                await session.execute(
                    select(AiSession.status, func.count())
                    .where(AiSession.status.not_in(TERMINAL_SESSION_STATUSES))
                    .group_by(AiSession.status)
                )
            ).all()
            counted = {status.value: count for status, count in open_sessions}
            for status in AiSessionStatus:
                if status in TERMINAL_SESSION_STATUSES:
                    continue
                # Set every non-terminal status, including the ones at zero.
                # A gauge that simply stops being exported reads on a graph as
                # "no data", which is indistinguishable from a broken scrape.
                ai_sessions_open.labels(status=status.value).set(
                    counted.get(status.value, 0)
                )

            open_jobs = (
                await session.execute(
                    select(TtsBatchJob.state, func.count())
                    .where(TtsBatchJob.state.not_in(TERMINAL_BATCH_STATES))
                    .group_by(TtsBatchJob.state)
                )
            ).all()
            counted_jobs = {state.value: count for state, count in open_jobs}
            for state in TtsBatchJobState:
                if state in TERMINAL_BATCH_STATES:
                    continue
                batch_jobs_open.labels(state=state.value).set(
                    counted_jobs.get(state.value, 0)
                )
    except Exception:  # noqa: BLE001 - a scrape must not be able to 500
        logger.warning("metrics_db_gauges_failed", exc_info=True)


class MetricsMiddleware:
    """Count requests without getting between a stream and its client.

    Raw ASGI, deliberately not `@app.middleware("http")`. Starlette's
    `BaseHTTPMiddleware` runs the endpoint in its own task group and pipes the
    response body through a memory stream, which changes how a client
    disconnect reaches the body generator. On this app that generator's
    `finally` is where the money is: `tts_service._finalise` settles the call
    there, and the comment above the `yield b""` in `_body` is a note about how
    narrowly that path already works. A metric is not worth standing in it.

    So this class forwards `receive` and `send` untouched and reads two things
    off the way past: the status from `http.response.start`, and the clock.
    Nothing here can delay a chunk, and nothing here can swallow a cancel.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The scrape is not customer traffic, and counting it would put a
        # steady 4 requests a minute into every rate panel on the dashboard.
        if scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status: int | None = None
        elapsed: float | None = None

        async def send_wrapper(message) -> None:
            nonlocal status, elapsed
            if message["type"] == "http.response.start":
                status = message["status"]
                elapsed = time.perf_counter() - started
            await send(message)

        http_requests_inflight.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            http_requests_inflight.dec()
            observe_http(
                method=scope.get("method", "-"),
                # The matched route's template, so `/tts/batch/{job_id}` is one
                # series rather than one per job. Starlette writes it into the
                # scope during routing, which has happened by now for anything
                # that produced a response.
                route=_route_of(scope),
                # No `http.response.start` at all means the client vanished
                # before we answered. 499 is nginx's spelling of that, and
                # borrowing it keeps the panel honest — it is not a 200, and it
                # is not a 500 either.
                status=status if status is not None else 499,
                seconds=elapsed,
            )


def _route_of(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    # A 404, or a request that died before routing. Labelled as one series
    # rather than by the path the caller typed: a scanner probing a thousand
    # URLs would otherwise mint a thousand time series that never go away.
    return "unmatched"


def render() -> bytes:
    """The exposition text, for the route to return."""
    return _generate_latest(REGISTRY)
