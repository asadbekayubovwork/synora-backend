"""Batch synthesis: one hold, one upstream job, one settlement, two callers.

Everything here is called from two places — the HTTP route and
`app/workers/tts_batch.py` — and that is the whole design constraint. If the
worker billed a job differently from the route, a deployment would charge
different amounts depending on whether RabbitMQ happened to be running, and
nobody would notice until a customer compared two invoices. So the route never
does billing of its own: it calls `create_job`, and if the queue is absent it
calls the very same `submit_job` the worker would have called.

## The no-broker path is a supported configuration, not a fallback

`broker.publish` returns False when there is no `RABBITMQ_URL` and when the
publish failed, and `app/core/broker.py` requires every caller to have a path
that still works — the same contract `NullCache` sets for Redis. Here that path
is to submit inline: upstream's own `POST /v1/batch` answers in milliseconds
because it only enqueues on its side too, so the request that created the job
can hand it over itself. A queue that is merely absent must not become an
outage. The test suite has no broker, which is what keeps this honest: the
inline path is the one that is exercised, not merely intended.

What the queue buys, when it is there, is admission control in front of a
single GPU — see `app/core/broker.py` — plus a poll loop that costs nobody a
request. Without it, polling happens when someone reads the job.

## Idempotency, twice over

Our own job id is sent as upstream's `idempotency_key`. A redelivered queue
message — a broker restart, a worker killed between the upstream call and the
ack — therefore gets the *original* upstream job back instead of a second one,
which matters more here than anywhere else in this codebase: synthesising a
500-item batch twice costs us the GPU time twice and gives the user nothing.
Our own row is the second guard: `submit_job` refuses to run for a job that has
left `queued`, and `settle_job` refuses to run for a job that is terminal.

Every one of those guards is a conditional *write* rather than a check, because
the row is read before the upstream call and written after it with a network
round trip in between, and a `DELETE` is free to land inside that gap. `submit_job` claims the job with `UPDATE ... WHERE state = 'queued'`, so
a cancel that lands during the POST makes the claim match nothing and the job
stays cancelled — a blind write there resurrects a job whose hold has already
been released, and upstream then synthesises the whole corpus for free. And a
redelivered submit can come back already `succeeded`, because the idempotency key
returns the original job and that job has had time to finish; a terminal answer
to a *submit* is therefore settled on the spot. Writing it and waiting for a poll
would be the end of the job — a terminal row is never polled again, so the hold
would stand forever and the GPU time upstream really spent would be billed at
zero.

`settle_job` stamps its terminal state the same way, and for the mirror image of
the same race. A `DELETE` on a `queued` job skips the upstream cancel precisely
because there is no `upstream_job_id` to cancel yet, and then spends several
round trips settling the session; if the worker's submit claim wins inside that
window, a blind `state = 'cancelled'` lands on a row that now carries an upstream
job id. Nothing polls a terminal row, so the DELETE upstream is never sent, the
card renders the whole corpus, `GET /tts/batch/{id}/results` hands back every
clip — it reads `upstream_job_id`, which the cancel did not clear — and the job
is billed zero. Conditional on the state read at entry, that stamp matches
nothing instead, and `_overtaken_settlement` stops the card. The same blind write
in the other direction is a stale cancel stamping `billed_characters = 0` over a
poll that really did charge: a refunded-looking row against a real debit, which
is support's worst kind of question.

`refresh_job` stamps its non-terminal poll result the same way, which is the
third face of the one race: a cancel landing while we ask upstream where the job
is has already settled it, and a blind `state = 'running'` afterwards puts a
terminal row back on the poller and tells its owner their cancelled job is still
running. That one costs no money — the settlement's own guards see to that — but
a row that contradicts the receipt is still a row nobody can explain.

What a key is *bound to* is written down at `create_job`:
`session_service.request_digest_for` fingerprints the corpus and the job-level
synthesis options onto the session, so "the same key" becomes a claim about the
request rather than about its price. Bound to a price it is not a claim at all —
the price book rounds up to the thousand characters, so a thousand different
corpora quote identically. Nothing here refuses on a mismatch, because the route
publishes the opposite promise: `POST /tts/batch` says a reused key returns the
job it already created *even when the corpus differs*, since that answer is
already priced, held and possibly half-synthesised. Changing that is a contract
decision rather than a bug fix; the digest is what makes the promise auditable
today, and what a guard would read the day it is revisited.

## Every hold has a deadline, including one upstream never accepted

`TTS_BATCH_MAX_POLL_SECONDS` is the only thing that bounds how long a batch may
hold credit, so it is measured from `created_at` whenever there is no
`submitted_at` — a job whose submit was dead-lettered has no `submitted_at` and
is precisely the job that most needs the deadline. `refresh_job` checks it
*before* it tries to resubmit, so reading a job upstream will never accept
settles it and hands the credit back instead of retrying a failing POST for as
long as the user keeps looking at it.

The job's deadline and its session's are one clock, which they were not.
`AiSession.expires_at` was set from the moment the session opened while
`_has_expired` measures from `submitted_at`, which is always later — by the
broker hop, by the retry backoff, by hours when a refused submit is resubmitted
and `submitted_at` is stamped again. The session therefore came due first for
every batch that reached its deadline, so the reaper in
`billing/reconcile_service.py` closed the session of a perfectly healthy job:
the job kept polling, upstream rendered the whole corpus, and the settlement
found the session already terminal, rebuilt a zero and stamped the job succeeded
with nothing charged. So `submit_job` pushes `expires_at` to
`submitted_at + TTS_BATCH_MAX_POLL_SECONDS` in the same transaction as its
claim. This lifecycle belongs to `expire_job`, and the reaper now stands off any
session a live job points at — that exclusion is what closes the hole, and this
is what keeps the backstop *honest* for the one case an exclusion cannot cover:
a batch whose job row has somehow gone must still reach a deadline rather than
hold credit forever.

Something has to *look* at a job for that deadline to fire, and `refresh_job`
runs only on a worker's poll message or on a read of the job. `sweep_stale_jobs`
is the backstop for the job that gets neither — a submit that was dead-lettered,
or an inline job on the no-broker path whose owner never came back — and it
exists precisely because the reaper stands off: nothing outside this module may
end a live batch's session, so this module has to end it itself.

## Settlement is upstream's count, clamped by ours

We hold against the characters we counted in the payload. We charge the
characters upstream says it synthesised, which is lower whenever items failed —
nobody pays for audio that was never produced. Higher is possible in principle
and is exactly what `settle_oneshot`'s ceiling exists for: it clamps to the
hold and flags the session `disputed`, so an upstream that starts over-counting
becomes a support question instead of a silent overcharge.

`audio_ms` is recorded on the job row and never priced; `app/models/tts_job.py`
explains why at length.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.broker import RK_BATCH_SUBMIT, get_broker
from app.core import metrics
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadGatewayError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.db.base import as_utc, utcnow
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_BATCH_STATES,
    TERMINAL_SESSION_STATUSES,
    BillingService,
    SessionEndReason,
    TtsBatchJobState,
    UsageMetric,
)
from app.models.tts_job import TtsBatchJob
from app.models.user import User
from app.services.ai import tts_client
from app.services.billing import session_service

if TYPE_CHECKING:  # pragma: no cover - the schema is the route's vocabulary
    from app.schemas.tts import BatchCreateRequest

logger = logging.getLogger("synora.tts")

# Upstream's own per-item cap. Restated here so an over-long item is refused
# before a hold is placed rather than after, and so the message names the item.
ITEM_TEXT_MAX_CHARACTERS = 20_000

# Upstream's `state` vocabulary mapped onto ours. `pending` and `running` are
# both "upstream has it and is not done", and we keep them apart only because
# the difference is the first thing anyone asks when a job feels slow.
UPSTREAM_STATES = {
    "pending": TtsBatchJobState.SUBMITTED,
    "running": TtsBatchJobState.RUNNING,
    "succeeded": TtsBatchJobState.SUCCEEDED,
    "failed": TtsBatchJobState.FAILED,
    "cancelled": TtsBatchJobState.CANCELLED,
    "canceled": TtsBatchJobState.CANCELLED,
}

# Why each terminal state ended the metered session. A closed set, because
# `SessionEndReason` is what "why do jobs end?" is answered from.
END_REASONS = {
    TtsBatchJobState.SUCCEEDED: SessionEndReason.COMPLETED,
    TtsBatchJobState.FAILED: SessionEndReason.UPSTREAM_ERROR,
    TtsBatchJobState.CANCELLED: SessionEndReason.USER_CANCELLED,
    TtsBatchJobState.EXPIRED: SessionEndReason.TIMEOUT,
}


# --- reading upstream's answers --------------------------------------------


def _int(value: Any) -> int:
    """A counter out of a JSON body, or zero. Never raises on upstream's shape."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_of(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    usage = (payload or {}).get("usage")
    return usage if isinstance(usage, Mapping) else {}


def _audio_ms_of(usage: Mapping[str, Any]) -> int:
    """`usage.audio_seconds` in milliseconds.

    The one float this module touches, and it is safe because it is the one
    number that is never priced: it lands on `TtsBatchJob.audio_ms` for
    observability and never on a `cum_*` column. Truncated rather than rounded —
    a millisecond either way is below the resolution of what upstream measures.
    """
    try:
        return max(0, int(float(usage.get("audio_seconds") or 0) * 1000))
    except (TypeError, ValueError):
        return 0


def _counters(payload: Mapping[str, Any]) -> dict[str, int]:
    """Upstream's progress as columns, keeping `total_items` ours.

    `total_items` is what we submitted and what the hold was priced from, so an
    upstream that reports a different total is not allowed to rewrite history —
    the completed and failed counts are the ones that move.

    A mapping rather than a write, because `submit_job` needs these two columns
    inside an `update(...).values(...)` and `refresh_job` needs them on the
    instance. Two spellings of the same two counters is how they drift apart.
    """
    return {
        "completed_items": _int(payload.get("completed_items")),
        "failed_items": _int(payload.get("failed_items")),
    }


def _apply_counters(job: TtsBatchJob, payload: Mapping[str, Any]) -> None:
    for column, value in _counters(payload).items():
        setattr(job, column, value)


def _is_transient(error: AppError) -> bool:
    """Whether this failure is worth asking again about, or is the final word.

    The same rule `app/workers/tts_batch.py` retries on — a saturated GPU (429)
    or a box that is down, timing out or answering nonsense (5xx, including the
    503 a rejected key becomes). Everything else below 500 is about the payload
    and will not change however many times the same bytes are sent.

    Stated once and shared, because the two submit paths must agree: if the
    inline path treated a 429 as permanent, the same batch would survive a busy
    GPU on a deployment with a broker and be destroyed by it on one without.
    """
    return error.status_code >= 500 or error.status_code == 429


# --- loading ----------------------------------------------------------------


async def _require_job(
    session: AsyncSession, job_id: uuid.UUID, *, fresh: bool = False
) -> TtsBatchJob:
    """The job, or a 404.

    `fresh=True` reads the row over whatever this session already holds in
    memory, and is not the paranoia it looks like: `SessionLocal` is built with
    `expire_on_commit=False`, so an instance loaded before a commit keeps the
    values it was loaded with, and a plain SELECT for a row already in the
    identity map is answered from that map without touching the database. That
    is wrong in exactly one situation — another transaction has moved the row
    underneath us — which is the only situation anybody passes this flag in.
    """
    stmt = select(TtsBatchJob).where(TtsBatchJob.id == job_id)
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise NotFoundError("No such batch job.", code="tts_batch_not_found")
    return job


async def load_job(
    session: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> TtsBatchJob:
    """One job, optionally scoped to its owner.

    Someone else's job id is a 404 rather than a 403: "no such job" and "not
    yours" are the same sentence to anyone who should not know the difference,
    and the alternative leaks which ids exist.
    """
    job = await _require_job(session, job_id)
    if user_id is not None and job.user_id != user_id:
        raise NotFoundError("No such batch job.", code="tts_batch_not_found")
    return job


async def _job_for_session(
    session: AsyncSession, ai_session_id: uuid.UUID
) -> TtsBatchJob | None:
    return (
        await session.execute(
            select(TtsBatchJob).where(TtsBatchJob.ai_session_id == ai_session_id)
        )
    ).scalar_one_or_none()


# --- creating ---------------------------------------------------------------


def _validate_items(payload: BatchCreateRequest) -> tuple[list[dict[str, Any]], int]:
    """Upstream's `items` array, and the characters we will hold against.

    Every check here happens before `open_oneshot`, because a refusal after the
    hold is credit that has to be given back and a release we never wrote cannot
    leak.
    """
    items = list(payload.items)
    if not items:
        raise BadRequestError("A batch needs at least one item.", code="tts_batch_empty")
    if len(items) > settings.tts_batch_max_items:
        raise BadRequestError(
            f"A batch may hold {settings.tts_batch_max_items} items; this one has "
            f"{len(items)}.",
            code="tts_batch_too_many_items",
        )

    seen: set[str] = set()
    total = 0
    rendered: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.id)
        if item_id in seen:
            # Upstream keys its results by this id, so a duplicate makes the
            # results array unmatchable to the request that produced it.
            raise BadRequestError(
                f"Item id '{item_id}' appears twice.",
                code="tts_batch_duplicate_item_id",
            )
        seen.add(item_id)

        length = len(item.text)
        if not length:
            raise BadRequestError(
                f"Item '{item_id}' has no text.", code="tts_batch_item_empty"
            )
        if length > ITEM_TEXT_MAX_CHARACTERS:
            raise BadRequestError(
                f"Item '{item_id}' is {length} characters; the limit is "
                f"{ITEM_TEXT_MAX_CHARACTERS}.",
                code="tts_batch_item_too_long",
            )
        total += length
        rendered.append(
            {"id": item_id, "text": item.text, "voice_id": item.voice_id or ""}
        )

    if total > settings.tts_batch_max_characters:
        raise BadRequestError(
            f"This batch is {total} characters; the limit is "
            f"{settings.tts_batch_max_characters}.",
            code="tts_batch_too_large",
        )
    return rendered, total


def _request_digest(payload: BatchCreateRequest, items: list[dict[str, Any]]) -> str:
    """Fingerprint the corpus an idempotency key is being spent on.

    `session_service.request_digest_for` is the shared spelling — one field
    order, one hash, computed one way for every surface — and the list below is
    this surface's: everything that decides what gets synthesised, and nothing
    that does not. The *rendered* items rather than `payload.items`, because
    `_validate_items` has already resolved a null item voice to `""`, and two
    requests that produce byte-identical audio must not fingerprint differently.

    The item count leads the items so the boundary between "one item saying ab"
    and "two items saying a and b" cannot be crossed by a text that happens to
    contain the field separator — which is a collision an attacker picks rather
    than one they wait for, since the texts here are theirs to choose.

    Written down and, for now, not compared. `POST /tts/batch` publishes that a
    reused key returns the job it already created *even when the corpus
    differs*, and unlike the streaming route there is no free synthesis in
    that: the caller gets the original job back, priced and held exactly once.
    So the guard that would read this column is a contract change rather than a
    bug fix, and it belongs to whoever owns that promise. What the column buys
    today is evidence — "this key was spent on a different corpus" stops being
    unanswerable — and parity with the speech path, where the same digest is
    the thing standing between one paid character and five thousand free ones.
    Why not the price: `request_digest_for`'s own docstring has it. The price
    book rounds up to the thousand characters, and a bucket is not an identity.
    """
    return session_service.request_digest_for(
        # Normalised the way the job row and the upstream body normalise them,
        # so "no voice" spelled two ways is one request rather than two.
        payload.voice_id or None,
        payload.quality,
        payload.audio_format,
        payload.sample_rate,
        payload.style or None,
        len(items),
        *(
            field
            for item in items
            for field in (item["id"], item["text"], item["voice_id"])
        ),
    )


async def create_job(
    session: AsyncSession,
    user: User,
    payload: BatchCreateRequest,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> TtsBatchJob:
    """Price the whole batch, hold for it, write the row, hand it over.

    Returns as soon as the job is durable and either queued or submitted. The
    row is what makes the work recoverable from that point on, which is why it
    carries the items until upstream acknowledges them.
    """
    tts_client.require_configured()
    items, characters = _validate_items(payload)

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
        # The surface this key belongs to. Without it, a one-character
        # `POST /tts/speech` sent under a running batch's key resolves to the
        # batch's session and settles it for one character — half a million
        # characters of GPU time billed at a quarter of a credit, and the
        # batch's own settlement finding nothing left to charge.
        scope="batch",
        idempotency_key=payload.idempotency_key,
        # What that key was spent on, so a replay is answerable as "the same
        # request?" rather than "the same price?" — see `_request_digest`.
        request_digest=_request_digest(payload, items),
        # A batch legitimately runs for hours, so the session's backstop is the
        # same deadline the poller gives up at. The default ten minutes is
        # sized for a streaming request and would have the reaper expiring
        # perfectly healthy jobs.
        ttl_seconds=settings.tts_batch_max_poll_seconds,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    if ticket.replayed:
        existing = await _job_for_session(session, ticket.ai_session_id)
        if existing is not None:
            return existing
        # The key opened a session that is not a batch — a streaming call
        # reusing it, most likely. Attaching a job to it would settle somebody
        # else's hold.
        raise ConflictError(
            "This idempotency key belongs to a different request.",
            code="tts_batch_idempotency_conflict",
        )

    key = (
        f"{user.id}:{payload.idempotency_key}"
        if payload.idempotency_key
        # Derived from the session rather than minted at random, so the row's
        # key and the session it settles are traceable to each other by eye.
        else f"{user.id}:session:{ticket.ai_session_id}"
    )
    job = TtsBatchJob(
        user_id=user.id,
        wallet_id=ticket.wallet_id,
        ai_session_id=ticket.ai_session_id,
        state=TtsBatchJobState.QUEUED,
        idempotency_key=key,
        voice_id=payload.voice_id or None,
        audio_format=payload.audio_format,
        quality=payload.quality,
        sample_rate=payload.sample_rate,
        style=payload.style or None,
        total_items=len(items),
        submitted_characters=characters,
        # Upstream's array verbatim, so a resubmit sends byte-identical text.
        items_json=json.dumps(items, ensure_ascii=False),
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        # Two requests racing on one key, and this one lost. The hold it placed
        # has to go back — nothing else will release it, because the row that
        # would have settled it is the one that failed to insert.
        await session.rollback()
        await session_service.abandon_oneshot(
            session,
            ai_session_id=ticket.ai_session_id,
            end_reason=SessionEndReason.INTERNAL_ERROR,
            error_code="tts_batch_duplicate",
        )
        winner = (
            await session.execute(
                select(TtsBatchJob).where(TtsBatchJob.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover - a different constraint fired
            raise ConflictError(
                "This batch could not be created. Please retry.",
                code="tts_batch_conflict",
            ) from None
        return winner

    await session.commit()

    # Published after the commit, never before: a worker that picks the message
    # up in the microsecond before this transaction lands would find no row and
    # dead-letter a job that is about to exist.
    if await get_broker().publish(RK_BATCH_SUBMIT, {"job_id": str(job.id)}):
        logger.info(
            "tts_batch_queued job=%s user=%s items=%d chars=%d",
            job.id,
            user.id,
            job.total_items,
            characters,
        )
        return job

    # No broker, or the broker refused. Upstream's own POST /v1/batch answers in
    # milliseconds — it enqueues on its side too — so the request that created
    # the job hands it over itself rather than leaving it sitting in `queued`
    # waiting for a worker that does not exist. This is the configuration the
    # test suite runs in; see the module docstring.
    #
    # Read before the call rather than after it: a `submit_job` that rolls back
    # expires this instance, and the first attribute touched on an expired
    # instance is a lazy SELECT — which under asyncio is a `MissingGreenlet`
    # instead of a value.
    job_id = job.id
    try:
        return await submit_job(session, job_id)
    except AppError as error:
        if _is_transient(error):
            # A saturated GPU or a box that is down. Neither is a reason to
            # destroy the payload: the row stays `queued` with `items_json`
            # intact, which is exactly the state `refresh_job` resubmits from —
            # and `GET /tts/batch/{job_id}` calls it, so the caller's next read
            # is the retry. Settling here would clear up to
            # `TTS_BATCH_MAX_CHARACTERS` of text that nobody else has a copy of
            # and turn "try again in a moment" into "upload it all again". The
            # hold is not stranded by that, because `_has_expired` measures from
            # `created_at` for precisely this row. The error is re-raised rather
            # than swallowed so the caller sees the real status, `Retry-After`
            # and all.
            #
            # The trade, named: a caller who retries under the same idempotency
            # key replays onto this same job and places no second hold, but one
            # retrying without a key leaves a queued job holding credit each
            # time, until the deadline releases them. Credit that comes back
            # late is recoverable; a corpus we threw away is not.
            raise
        # A permanent refusal: upstream rejected this payload, or handed back a
        # job id it had already given away. `submit_job` settles the job itself
        # on both of those branches, and this is the belt to that pair of
        # braces — no 4xx may leave a `queued` row holding credit against a
        # submit that will never be accepted. `settle_job` is a no-op on a job
        # that is already terminal, so the usual case costs one SELECT.
        await settle_job(
            session,
            await _require_job(session, job_id),
            state=TtsBatchJobState.FAILED,
            usage={},
            error=f"{error.code}: {error.detail}",
        )
        raise


# --- submitting -------------------------------------------------------------


async def submit_job(session: AsyncSession, job_id: uuid.UUID) -> TtsBatchJob:
    """Hand a queued job to upstream. Idempotent, and safe to redeliver.

    Called by the worker and, when there is no broker, by the request that
    created the job. A job that has already left `queued` is returned untouched,
    which is what makes a duplicate queue message cheap rather than dangerous.
    """
    job = await _require_job(session, job_id)
    if job.state is not TtsBatchJobState.QUEUED:
        return job

    items = json.loads(job.items_json) if job.items_json else None
    if not items:
        # `items_json` is cleared only when `upstream_job_id` is set, so an
        # empty payload on a `queued` row means the text is gone with nobody
        # holding a copy. Nothing can recover it; the honest move is to charge
        # nothing and say so.
        logger.error("tts_batch_payload_lost job=%s", job.id)
        return await settle_job(
            session,
            job,
            state=TtsBatchJobState.FAILED,
            usage={},
            error="The job payload was lost before upstream accepted it.",
            end_reason=SessionEndReason.INTERNAL_ERROR,
        )

    body = {
        "items": items,
        "voice_id": job.voice_id or "",
        "quality": job.quality,
        "format": job.audio_format,
        "sample_rate": job.sample_rate,
        "style": job.style or "",
        # Our own id, so a redelivered message returns the job upstream already
        # created instead of synthesising — and billing us for — the whole batch
        # a second time.
        "idempotency_key": str(job.id),
    }

    try:
        payload = await tts_client.submit_batch(body)
    except (BadRequestError, NotFoundError) as error:
        # Upstream refused the payload itself. Retrying sends the same bytes to
        # the same validator, so this is terminal: settle at zero, release the
        # hold, and let the caller see the reason.
        await settle_job(
            session,
            job,
            state=TtsBatchJobState.FAILED,
            usage={},
            error=f"{error.code}: {error.detail}",
        )
        raise

    upstream_id = str(payload.get("job_id") or "").strip()
    if not upstream_id:
        # A 2xx with no id is a job we can never poll and never settle, so it is
        # treated as unreachable rather than accepted. The row stays `queued`
        # with its items, which is exactly the state a retry needs.
        raise BadGatewayError(
            "The speech service accepted the batch without naming it.",
            code="tts_unreadable",
        )

    now = utcnow()
    state = UPSTREAM_STATES.get(
        str(payload.get("state") or "").lower(), TtsBatchJobState.SUBMITTED
    )
    terminal = state in TERMINAL_BATCH_STATES
    values: dict[str, Any] = {
        "upstream_job_id": upstream_id,
        # Upstream holds the text from here on, so our copy is only weight in
        # the row — and the row is read on every poll.
        "items_json": None,
        # A terminal answer is claimed as `submitted` and settled immediately
        # below rather than written straight in, because `settle_job` refuses to
        # run on a job that is already terminal: stamping the final state here
        # would skip the charge and leave the hold with nothing left to release
        # it. A non-terminal one is written as upstream reported it, so a job
        # already `running` does not read as merely `submitted`.
        "state": TtsBatchJobState.SUBMITTED if terminal else state,
        "submitted_at": now,
        # When the next poll is *due*. Whether one happens is the worker's
        # business with a broker, and the read path's without one. Set even for
        # a terminal answer: the settlement below nulls it, and if that
        # settlement fails then a job that is still polled is the recoverable
        # outcome.
        "next_poll_at": now + timedelta(seconds=settings.tts_batch_poll_seconds),
        # `Base.updated_at`'s `onupdate` is applied by SQLAlchemy in Python and
        # does NOT fire on a bulk update — see `app/db/base.py`.
        "updated_at": now,
        **_counters(payload),
    }

    try:
        # Conditional on the job still being `queued`, because this row was read
        # before the upstream POST and cancelling a queued job is a documented
        # thing for a user to do. A blind ORM write loses that race silently:
        # `cancel_job` has already settled the session at zero and released the
        # hold, and writing `submitted` over `cancelled` resurrects a
        # non-terminal job that upstream then synthesises in full — a whole
        # corpus delivered and nothing charged for it. With the state in the
        # WHERE clause the loser is this statement, which is the right way
        # round.
        claimed = await session.execute(
            update(TtsBatchJob)
            .where(
                TtsBatchJob.id == job.id,
                TtsBatchJob.state == TtsBatchJobState.QUEUED,
            )
            .values(**values)
            # Nothing to synchronise: the instance is brought level by hand
            # below, and only when the claim actually took.
            .execution_options(synchronize_session=False)
        )
        # Read before the commit, which closes the result.
        rowcount = claimed.rowcount
        if rowcount:
            # The session's deadline, moved onto the job's clock, in the same
            # transaction as the claim that set `submitted_at`. `_has_expired`
            # measures from `submitted_at`, which is always later than the
            # moment the session was opened — by the broker hop, by the retry
            # backoff, by hours when `refresh_job` resubmits a job upstream
            # refused and stamps `submitted_at` again. Left where `create_job`
            # put it, `expires_at` comes due first for every batch that reaches
            # its deadline, and whatever reads it closes the session of a job
            # that is still being polled: upstream renders the whole corpus and
            # the settlement finds the session terminal and charges nothing.
            #
            # This job owns this session's lifecycle. `expire_job` is what ends
            # it, and `reconcile_service.reap_expired_sessions` stands off any
            # session a live job points at — that exclusion is what makes the
            # sentence true. This write is what keeps the backstop meaningful
            # anyway: a batch whose job row has gone must still reach a
            # deadline rather than hold credit forever, and it can only do that
            # if the two clocks agree.
            #
            # Conditional on the session still being live, because `cancel_job`
            # settles the session *before* it stamps the job. A cancel in that
            # window loses the claim above but has already closed the session,
            # and pushing a finished session's deadline out would write over
            # the record of how it ended.
            await session.execute(
                update(AiSession)
                .where(
                    AiSession.id == job.ai_session_id,
                    AiSession.status.not_in(TERMINAL_SESSION_STATUSES),
                )
                .values(
                    expires_at=now
                    + timedelta(seconds=settings.tts_batch_max_poll_seconds),
                    # Bulk update: `Base.updated_at`'s `onupdate` does not fire.
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
        await session.commit()
    except IntegrityError:
        # `uq_tts_batch_jobs_upstream_job_id`. Our own job id is the
        # idempotency key we sent, so upstream can only hand this id to us and
        # to nobody else — unless it reuses ids across keys, which no retry
        # fixes because the retry sends the same key. Two rows polling one
        # upstream job would settle it twice, so this job dies here instead,
        # and it dies settled rather than holding credit nothing will release.
        await session.rollback()
        job = await _require_job(session, job_id)
        logger.error("tts_batch_upstream_id_taken job=%s upstream=%s", job.id, upstream_id)
        await settle_job(
            session,
            job,
            state=TtsBatchJobState.FAILED,
            usage={},
            error=f"The speech service reused job id '{upstream_id}'.",
        )
        raise BadGatewayError(
            "The speech service could not accept this batch. Please try again.",
            code="tts_batch_upstream_id_conflict",
        ) from None

    if rowcount == 0:
        return await _overtaken_submit(session, job_id=job_id, upstream_id=upstream_id)

    # The instance brought level with the row: the statement above ran with
    # `synchronize_session=False`, so nothing updated this copy, and `settle_job`
    # reads `upstream_job_id` off it for the settlement's `upstream_request_id`.
    for column, value in values.items():
        setattr(job, column, value)

    logger.info(
        "tts_batch_submitted job=%s upstream=%s items=%d chars=%d state=%s",
        job.id,
        upstream_id,
        job.total_items,
        job.submitted_characters,
        state.value,
    )

    if terminal:
        # Upstream answered the submit with a job that had already finished,
        # which is what the idempotency key is *for*: a redelivered message — a
        # worker killed between the POST and its ack, `refresh_job` recovering a
        # submit that was lost — gets the original job back, and by then it may
        # well have run to completion. Committing that state and returning would
        # be the end of the job: `refresh_job` returns at `if job.is_terminal`
        # and the worker acks, so nothing would ever poll it, the hold would
        # stand forever and real GPU time would be billed at zero. This same
        # answer carries the counters to settle on.
        logger.info(
            "tts_batch_submit_returned_finished job=%s upstream=%s state=%s",
            job.id,
            upstream_id,
            state.value,
        )
        return await settle_job(
            session,
            job,
            state=state,
            usage=_usage_of(payload),
            error=payload.get("error"),
        )
    return job


async def _overtaken_submit(
    session: AsyncSession, *, job_id: uuid.UUID, upstream_id: str
) -> TtsBatchJob:
    """The job left `queued` while our POST was in flight. Stop the GPU.

    Reached only when the conditional claim in `submit_job` matched no row, so
    somebody else has moved this job on and whatever they decided is now the
    truth. The one thing that must not happen is upstream quietly synthesising a
    batch against a hold that has already been released.
    """
    # Read over the top of the instance `submit_job` was holding: that one was
    # loaded before the upstream POST and still says `queued`, which is the very
    # claim the database has just refused. The commit in `submit_job` wrote
    # nothing but did end the transaction, so this read sees whatever the other
    # transaction committed.
    job = await _require_job(session, job_id, fresh=True)

    if job.upstream_job_id == upstream_id:
        # Two submits of the same job running at once — a redelivered queue
        # message being worked twice. Our own job id is the upstream idempotency
        # key, so both calls were handed the *same* upstream job: the one that
        # won the claim is polling exactly what this one would have. Cancelling
        # here would kill a job that is legitimately ours.
        return job

    # Cancelled by the user mid-POST, or failed and settled. Either way the
    # session is closed and the hold is gone, so every character upstream
    # synthesises from here is work nobody can be charged for.
    logger.warning(
        "tts_batch_submit_overtaken job=%s state=%s upstream=%s: cancelling upstream",
        job.id,
        job.state.value,
        upstream_id,
    )
    try:
        await tts_client.cancel_batch(upstream_id)
    except AppError as error:
        # Not raised: our own row is in a perfectly consistent, settled state and
        # there is nothing here for a caller to retry. The GPU time is ours to
        # eat and this line is the record of it.
        logger.error(
            "tts_batch_orphan_upstream job=%s upstream=%s not cancelled (%s)",
            job.id,
            upstream_id,
            error.code,
        )
    return job


# --- polling ----------------------------------------------------------------


def _is_due(job: TtsBatchJob) -> bool:
    return job.next_poll_at is None or as_utc(job.next_poll_at) <= utcnow()


def _deadline_of(submitted_at: datetime | None, created_at: datetime) -> datetime:
    """When this job's hold runs out, from the two timestamps that decide it.

    Split out of `_has_expired` rather than inlined there so `sweep_stale_jobs`
    can read the same deadline off a scan of columns without loading a row per
    candidate — and, the half that matters, so "over" has one definition rather
    than a Python one here and an arithmetic one restated in a WHERE clause,
    which would agree today and drift the first time this deadline grows a case.
    """
    return as_utc(submitted_at or created_at) + timedelta(
        seconds=settings.tts_batch_max_poll_seconds
    )


def _has_expired(job: TtsBatchJob) -> bool:
    """Whether the job has outlived `TTS_BATCH_MAX_POLL_SECONDS`.

    Measured from `submitted_at` when there is one and from `created_at`
    otherwise. Keyed off `submitted_at` alone this answers False forever for the
    job that needs the deadline most: one whose submit was dead-lettered — three
    upstream 502s and `app/workers/tts_batch.py` gives up — sits in `queued` with
    no `submitted_at`, and nothing else in this codebase releases its hold. The
    clock has to start when the credit was taken, and that is `created_at`.

    `AiSession.expires_at` is kept on this same clock rather than on its own:
    `submit_job` pushes it to `submitted_at + TTS_BATCH_MAX_POLL_SECONDS` as it
    stamps `submitted_at`. Before that, the session's deadline was always the
    earlier of the two and the reaper systematically beat `expire_job` to every
    batch that ran to its limit.
    """
    return utcnow() >= _deadline_of(job.submitted_at, job.created_at)


async def refresh_job(session: AsyncSession, job_id: uuid.UUID) -> TtsBatchJob:
    """Ask upstream where the job is, and settle it if it has finished.

    The worker calls this on every delayed poll message; the route calls it when
    someone reads the job, which is what advances a job on a deployment with no
    broker. Both are safe at any frequency: a job that is not yet due is
    returned as it stands rather than turned into another upstream request.
    """
    job = await _require_job(session, job_id)
    if job.is_terminal:
        return job

    # Above the resubmit branch, not below it. A job upstream never accepted is
    # reachable only through this function, and below the resubmit it would
    # answer every read with the same failing POST — the deadline that exists to
    # hand the hold back would never be reached, however long the job sat there
    # or however often the user looked at it.
    if _has_expired(job):
        return await expire_job(session, job)

    if job.state is TtsBatchJobState.QUEUED or not job.upstream_job_id:
        # A poll for a job upstream has never seen means the submit was lost —
        # a broker restart, a worker killed before its ack. This is the recovery
        # `items_json` exists for, and the upstream idempotency key makes it
        # free even if the original submit did land.
        return await submit_job(session, job.id)

    if not _is_due(job):
        return job

    # Read before the upstream call, for the same reason `submit_job` and
    # `settle_job` read theirs: everything below is written after a network
    # round trip, and a cancel is free to land inside it.
    job_id = job.id
    entry_state = job.state

    try:
        payload = await tts_client.batch_status(job.upstream_job_id)
    except NotFoundError:
        # Upstream has forgotten the job, so there is no count to settle on and
        # no work we can prove happened. Charge nothing.
        logger.warning(
            "tts_batch_vanished job=%s upstream=%s", job.id, job.upstream_job_id
        )
        return await settle_job(
            session,
            job,
            state=TtsBatchJobState.FAILED,
            usage={},
            error="The speech service no longer knows this job.",
        )

    now = utcnow()
    job.poll_count = job.poll_count + 1
    _apply_counters(job, payload)

    raw_state = str(payload.get("state") or "").lower()
    state = UPSTREAM_STATES.get(raw_state)
    if state is None:
        # A state we have no branch for is not a reason to stop polling: keep
        # the job alive and let the expiry deadline be the backstop.
        logger.warning(
            "tts_batch_unknown_state job=%s upstream=%s state=%r",
            job.id,
            job.upstream_job_id,
            raw_state,
        )
        state = TtsBatchJobState.RUNNING

    if state in TERMINAL_BATCH_STATES:
        return await settle_job(
            session,
            job,
            state=state,
            usage=_usage_of(payload),
            error=payload.get("error"),
        )

    # Conditional for the third time in this module, and against the third
    # face of one race. A `DELETE` landing while we were asking upstream has
    # already settled this job; a blind `state = 'running'` here puts a
    # terminal, settled row back on the poller and tells its owner their
    # cancelled job is still going. Upstream's counters and the poll count are
    # written by this same statement rather than left to the instance's flush,
    # so the whole poll result lands or none of it does.
    advanced = await session.execute(
        update(TtsBatchJob)
        .where(TtsBatchJob.id == job_id, TtsBatchJob.state == entry_state)
        .values(
            state=state,
            poll_count=job.poll_count,
            next_poll_at=now + timedelta(seconds=settings.tts_batch_poll_seconds),
            # `Base.updated_at`'s `onupdate` does not fire on a bulk update.
            updated_at=now,
            **_counters(payload),
        )
        .execution_options(synchronize_session=False)
    )
    rowcount = advanced.rowcount
    if rowcount == 0:
        # Rolled back rather than committed, because the counter increments
        # made on the instance further up are still pending: a commit would
        # flush them, blind, onto a row somebody else has already settled.
        logger.warning("tts_batch_poll_overtaken job=%s wanted=%s", job_id, state.value)
        await session.rollback()
    else:
        await session.commit()
    # Fresh either way, because the instance carries whichever of those two
    # endings did not happen.
    return await _require_job(session, job_id, fresh=True)


# --- settling ---------------------------------------------------------------


async def settle_job(
    session: AsyncSession,
    job: TtsBatchJob,
    *,
    state: TtsBatchJobState,
    usage: Mapping[str, Any],
    error: str | None = None,
    end_reason: SessionEndReason | None = None,
) -> TtsBatchJob:
    """Charge for what upstream produced, release the hold, close the job.

    Idempotent on the job's own terminal state, because every caller reaches it
    from somewhere that can run twice: a redelivered poll, a cancel racing the
    poller, a route retried by an impatient client.

    The terminal stamp is a conditional write, the same shape as `submit_job`'s
    claim and against the same race read from the other end. Between the state
    this call reads at entry and the state it writes there is a whole
    settlement — several round trips and a commit — and a `queued` job can be
    claimed by the worker's submit inside it. Blind, the stamp then lands
    `cancelled`, `billed_characters=0`, `items_json=NULL` on a row that has
    since acquired an upstream job id: nothing polls a terminal row, so no
    DELETE is ever sent upstream, the card renders the corpus, the results
    route hands every clip back off that same `upstream_job_id`, and the job is
    billed zero. With the entry state in the WHERE clause the loser is this
    statement, and `_overtaken_settlement` decides what that means.
    """
    if job.is_terminal:
        return job

    # Both read before the settlement, which can roll its own transaction back
    # — and a rollback expires this instance, after which the first attribute
    # touched is a lazy SELECT, which under asyncio is a `MissingGreenlet`
    # rather than a value.
    job_id = job.id
    entry_state = job.state

    characters = _int(usage.get("characters"))
    audio_ms = _audio_ms_of(usage)

    # The money first, the bookkeeping second, in two commits rather than one.
    # `settle_oneshot` rolls its own transaction back when it loses the race for
    # `uq_usage_events_session_sequence`, and a rollback that also discarded the
    # job's terminal state would leave a settled session being polled forever.
    # In the other order the worst case is a settled session under a job that
    # still says `running`, which the next poll stamps correctly.
    settlement = await session_service.settle_oneshot(
        session,
        ai_session_id=job.ai_session_id,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
        end_reason=end_reason or END_REASONS[state],
        upstream_request_id=job.upstream_job_id,
    )

    now = utcnow()
    values: dict[str, Any] = {
        "state": state,
        "billed_characters": characters,
        "audio_ms": audio_ms,
        # Truncated to the column rather than left to the database: Postgres
        # answers an over-long value with a 500 and SQLite silently stores the
        # whole thing.
        "error": str(error)[:512] if error else None,
        "finished_at": now,
        # Null once there is nothing left to poll, which keeps the row out of
        # `ix_tts_batch_jobs_state_next_poll` for good.
        "next_poll_at": None,
        "items_json": None,
        # `Base.updated_at`'s `onupdate` is applied by SQLAlchemy in Python and
        # does NOT fire on a bulk update — see `app/db/base.py`.
        "updated_at": now,
    }

    stamped = await session.execute(
        update(TtsBatchJob)
        .where(TtsBatchJob.id == job_id, TtsBatchJob.state == entry_state)
        .values(**values)
        # Nothing to synchronise: the instance is re-read below, and only when
        # the stamp actually took.
        .execution_options(synchronize_session=False)
    )
    # Read before the commit, which closes the result.
    rowcount = stamped.rowcount
    await session.commit()

    if rowcount == 0:
        return await _overtaken_settlement(session, job_id=job_id, wanted=state)

    # Re-read rather than assigning `values` back onto the instance. The
    # statement above ran with `synchronize_session=False`, so this copy still
    # holds what it was loaded with — and assigning the new values would leave
    # it *dirty*, so the caller's next commit would flush them again, blind,
    # which is precisely the write this function has just stopped making. One
    # primary-key SELECT buys an instance that matches the row and is safe to
    # read from even when the settlement rolled back underneath it.
    job = await _require_job(session, job_id, fresh=True)

    # `state` rather than `job.state`: the re-read above can come back holding
    # whatever a racing caller wrote, and what this pass settled is the state
    # it stamped. The characters are upstream's count, which is what was
    # billed — `submitted_characters` is what we asked for, and the gap between
    # the two is the failed clips nobody pays for.
    metrics.record_batch_job(state=state.value, characters=characters)

    logger.info(
        "tts_batch_settled job=%s state=%s chars=%d/%d audio_ms=%d charged=%s clamped=%s",
        job.id,
        state.value,
        characters,
        job.submitted_characters,
        audio_ms,
        settlement.price_micros,
        settlement.clamped,
    )
    return job


async def _overtaken_settlement(
    session: AsyncSession, *, job_id: uuid.UUID, wanted: TtsBatchJobState
) -> TtsBatchJob:
    """Somebody moved the job while we were settling it. Leave their row alone.

    Reached only when `settle_job`'s conditional stamp matched no row, so the
    state read at entry is no longer the state on the row and whatever the
    other caller decided is now the truth. Writing over it is the lost update
    the condition exists to prevent, so the winner is returned as it stands.

    What is *not* left alone is the GPU. The settlement ran before the stamp, so
    this session is terminal and its hold is released whichever way the race
    went — which makes any upstream job this row now points at work that nobody
    can be charged for. That is exactly what a cancel losing to a submit leaves
    behind: the `DELETE` skipped the upstream cancel because `upstream_job_id`
    was still null, and the submit that won the claim put one there a moment
    later. Sending that cancel here is the only thing between a released hold
    and a whole corpus rendered for free.

    Neither raised nor stamped terminal. Our own row is consistent — the money
    is settled and the winner's state is on it — and a job left `submitted`
    against an upstream job we have just cancelled converges on the next poll,
    which is the recoverable outcome; `expire_job` is the floor under that. The
    mirror of `_overtaken_submit`, and it makes the same trade for the same
    reason.
    """
    # Over the top of whatever this session already holds: the instance
    # `settle_job` was working from was loaded before the settlement and still
    # carries the state the database has just refused.
    job = await _require_job(session, job_id, fresh=True)
    logger.warning(
        "tts_batch_settle_overtaken job=%s wanted=%s found=%s upstream=%s",
        job.id,
        wanted.value,
        job.state.value,
        job.upstream_job_id or "-",
    )

    if job.is_terminal or not job.upstream_job_id:
        # Another settlement won the row, or there is no upstream job to stop.
        return job

    try:
        await tts_client.cancel_batch(job.upstream_job_id)
    except AppError as error:
        # Not raised: our own row is settled and consistent, and there is
        # nothing here for a caller to retry. The GPU time is ours to eat and
        # this line is the record of it.
        logger.error(
            "tts_batch_orphan_upstream job=%s upstream=%s not cancelled (%s)",
            job.id,
            job.upstream_job_id,
            error.code,
        )
    return job


async def cancel_job(session: AsyncSession, job: TtsBatchJob) -> TtsBatchJob:
    """Stop the job upstream, then bill for whatever it managed to produce.

    A transient upstream failure is deliberately allowed to propagate. If we
    cannot reach the box we cannot stop the card, and releasing the hold anyway
    would leave us synthesising audio we can no longer bill for; a 502 the
    caller can retry is the cheaper mistake.
    """
    if job.is_terminal:
        return job

    usage: Mapping[str, Any] = {}
    if job.upstream_job_id:
        try:
            payload = await tts_client.cancel_batch(job.upstream_job_id)
        except NotFoundError:
            # Already gone upstream. Nothing to stop, and nothing further to
            # count than whatever the status call below can still tell us.
            payload = None
        usage = _usage_of(payload)
        if not usage:
            # The DELETE answered without counters. One extra read is worth it:
            # this is the number the charge is made from, and defaulting it to
            # zero would give away every cancelled job's work.
            try:
                usage = _usage_of(await tts_client.batch_status(job.upstream_job_id))
            except AppError as error:
                logger.warning(
                    "tts_batch_cancel_usage_unknown job=%s: %s", job.id, error.code
                )

    return await settle_job(
        session,
        job,
        state=TtsBatchJobState.CANCELLED,
        usage=usage,
        error=None,
        end_reason=SessionEndReason.USER_CANCELLED,
    )


async def expire_job(session: AsyncSession, job: TtsBatchJob) -> TtsBatchJob:
    """Give up on a job that has outlived `TTS_BATCH_MAX_POLL_SECONDS`.

    A hold held forever is worse than a charge that might be slightly wrong: the
    user's credit is frozen and the wallet's reserved total drifts away from
    anything a human can explain. So the job settles at the last usage upstream
    admitted to and the session is flagged `disputed`, which is the marker
    support looks for when deciding what to refund.
    """
    if job.is_terminal:
        return job

    usage: Mapping[str, Any] = {}
    if job.upstream_job_id:
        try:
            usage = _usage_of(await tts_client.batch_status(job.upstream_job_id))
        except AppError as error:
            # Best effort by definition — a job reaching its deadline usually
            # means upstream stopped answering about it.
            logger.warning("tts_batch_expiry_usage_unknown job=%s: %s", job.id, error.code)

    logger.warning(
        "tts_batch_expired job=%s upstream=%s after %d polls",
        job.id,
        job.upstream_job_id or "-",
        job.poll_count,
    )
    job = await settle_job(
        session,
        job,
        state=TtsBatchJobState.EXPIRED,
        usage=usage,
        error=(
            f"The job did not finish within {settings.tts_batch_max_poll_seconds} "
            "seconds and was settled at the last reported usage."
        ),
        end_reason=SessionEndReason.TIMEOUT,
    )

    # Set after the settlement, which writes `disputed` itself when the charge
    # was clamped. This is the other reason to dispute one: we settled on
    # incomplete information.
    ai_session = await session.get(AiSession, job.ai_session_id)
    if ai_session is not None and not ai_session.disputed:
        ai_session.disputed = True
        await session.commit()
    return job


# --- sweeping ---------------------------------------------------------------


async def sweep_stale_jobs(session: AsyncSession, *, limit: int = 200) -> int:
    """Finish jobs past their deadline that nobody is polling. Returns how many.

    ## Why this is a pass of its own and not part of the session reaper

    The batch lifecycle owns its session's deadline. `submit_job` pushes
    `AiSession.expires_at` onto the job's clock in the same transaction as its
    claim, `expire_job` is the only thing allowed to end that session, and
    `reconcile_service.reap_expired_sessions` stands off any session a
    non-terminal job points at. That exclusion is what stops the two of them
    racing: the session's clock starts when the session was opened and the job's
    when upstream accepted it, so a reaper acting on the earlier of the two was
    systematically closing the sessions of perfectly healthy jobs — upstream
    rendered the whole corpus and the settlement found the session already
    terminal and charged nothing for it.

    The price of standing off is that the batch side has to provide its own
    backstop, and this function is that price being paid. Without it `expire_job`
    is reachable only from `refresh_job`, which runs on a worker's poll message
    or on a read of the job — so a job whose submit was dead-lettered (three
    upstream 502s, three publish failures, a purged queue) or one submitted
    inline on the no-broker path whose owner never reads it again holds its
    credit forever. `POST /admin/reconcile` cannot free it either: `heal_reserved`
    only stamps sessions that are already terminal and `verify_wallet` counts a
    live session's hold as legitimate, so the drift reads zero and nothing even
    alarms while the customer's money stays frozen.

    ## How it finishes them

    Through the paths that already exist, never by writing state of its own:
    `refresh_job` first where a poll is genuinely due — a job that has actually
    finished should settle at its real usage and its real state rather than be
    stamped `EXPIRED` at whatever was last known about it — and `expire_job` as
    the floor beneath that. Neither can be talked into a resubmit here, because
    `refresh_job` reads the deadline *above* its `queued` branch. Nothing in this
    function touches a wallet: `wallet_repo` is the only module allowed to move a
    balance, `tests/test_billing_invariants.py` walks the source tree to enforce
    it, and a repair pass hand-rolling a release is exactly the regression that
    guard exists to catch.

    The scan reads columns and `_deadline_of` makes the decision, so the deadline
    is the one `_has_expired` uses rather than a second copy of it in SQL. Rows
    come oldest deadline first, which pays twice over: if `limit` bites, the
    credit that has been frozen longest comes back first, and since rows sort by
    the very key the decision reads, everything after the first live row in the
    page is live too. Each candidate is then re-read fresh before it is touched —
    the scan is one statement and every settlement below is a transaction of its
    own, so by the time a row's turn comes it may have been polled, cancelled, or
    resubmitted with its deadline pushed out from under this pass.

    The trade, named: a sweeper is not a substitute for a caller. This has to run
    on a timer beside `reconcile_all`, and until it does, a backstop nobody runs
    is the same thing as no backstop at all.
    """
    now = utcnow()
    candidates = (
        await session.execute(
            select(TtsBatchJob.id, TtsBatchJob.submitted_at, TtsBatchJob.created_at)
            .where(
                # "Not terminal" rather than a list of live states, so a state
                # added later is swept by default. Missing a stranded hold is
                # the failure this exists to prevent; visiting a job that has
                # since finished costs one primary-key read and a `continue`.
                TtsBatchJob.state.not_in(TERMINAL_BATCH_STATES)
            )
            .order_by(func.coalesce(TtsBatchJob.submitted_at, TtsBatchJob.created_at))
            .limit(limit)
        )
    ).all()

    swept = 0
    for job_id, submitted_at, created_at in candidates:
        if _deadline_of(submitted_at, created_at) > now:
            continue

        try:
            # Fresh over whatever this session already holds: `SessionLocal` is
            # built with `expire_on_commit=False`, and the commits the previous
            # candidate's settlement made are exactly the kind of change an
            # instance from the identity map would not have.
            job = await _require_job(session, job_id, fresh=True)
            if job.is_terminal:
                # Somebody polled, cancelled or settled it between the scan and
                # here. Their answer is the truth and the hold is already gone.
                continue

            if _is_due(job):
                # The poll first, because upstream may have finished this job
                # while nothing was listening, and a real `succeeded` settled at
                # upstream's counters is a better receipt than an `EXPIRED` row
                # settled at the last thing we heard. Its own guards decide what
                # actually happens: past the deadline it goes straight to
                # `expire_job`, and a row whose deadline moved after the scan —
                # a resubmit stamps `submitted_at` again — is polled and left
                # alone, which is why the expiry below is asked again rather
                # than assumed.
                job = await refresh_job(session, job.id)
            if not job.is_terminal and _has_expired(job):
                job = await expire_job(session, job)
        except Exception:  # noqa: BLE001 - one bad row must not strand the rest
            # A wallet under contention answers `wallet_busy`, upstream can be
            # down, and this pass may be holding a hundred and ninety-nine other
            # frozen holds behind this one. Roll back whatever the failed attempt
            # left pending so the next iteration starts clean, and let the next
            # run retry this job — its deadline has not moved.
            logger.exception("tts_batch_sweep_failed job=%s", job_id)
            await session.rollback()
            continue

        if not job.is_terminal:
            # Legitimately still alive: the poll found a deadline that had moved,
            # or the conditional advance was overtaken by somebody else's write.
            # Not this pass's job to finish, and not counted as finished.
            continue

        swept += 1
        # WARNING, not INFO. A job that had to be swept is evidence of something
        # upstream of here — a submit message nobody ever worked, an owner who
        # stopped reading — and it settled on whatever was known rather than on
        # what the job did, so the id is the thread support pulls. Routine
        # housekeeping finds nothing; a quiet sweeper is the only healthy one.
        logger.warning(
            "tts_batch_swept job=%s state=%s chars=%d/%d upstream=%s deadline=%s polls=%d",
            job.id,
            job.state.value,
            job.billed_characters,
            job.submitted_characters,
            job.upstream_job_id or "-",
            _deadline_of(job.submitted_at, job.created_at),
            job.poll_count,
        )

    return swept
