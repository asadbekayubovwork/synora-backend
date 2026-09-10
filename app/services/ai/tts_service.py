"""One metered synthesis: the hold, the relay, and the settlement afterwards.

`tts_client` knows the socket and `session_service` knows the wallet. This is
the twenty lines where they meet, and almost all of its length is about *when*
things happen rather than what they are.

## Why the price is on the response headers

Full-text billing is the product decision this whole module is shaped around:
the caller is charged for `len(text)`, whatever the stream does afterwards. The
entire input is in the request body before any work starts, so the price is
known before the hold is placed, the hold is the price rather than a guess at
it, and the settlement can never exceed what was held. That is what lets our
own `200` carry `X-Synora-Price-Micros` — the bill is final before the first
byte of audio exists.

## Why upstream is opened here and not inside the generator

Starlette writes `http.response.start` — status line and headers — *before* it
pulls the first chunk out of a `StreamingResponse` body. So a failure that
surfaces on the generator's first `__anext__` is already too late to become a
`502`: the client has been told `200` and the only remaining move is to end the
stream. Opening the upstream response inside `synthesize`, before the route has
built any response at all, is what keeps upstream's refusal mappable onto a
real status code. The live response is then carried into the generator through
an `AsyncExitStack`, which is also what closes it on every exit path.

After the first byte the situation is genuinely different and is treated
differently: the status is on the wire, so there is no code left to choose. The
failure is logged and the response is *aborted* — the connection dropped
without its terminating chunk, which is the only thing HTTP has that means
"this body is incomplete". Ending the body cleanly instead would hand the
caller a well-formed `200` around a truncated audio file, billed in full, with
nothing on it they could check. The user is still billed either way, because
"billed for the whole text" is the rule and not an approximation of one: every
character reached the GPU before any audio came back.

## Why the body generator is started before it is handed over

An async generator only gets a finalizer once it has been iterated at least
once — `aclose()` on a never-started generator is a no-op and the loop's
asyncgen hooks have never heard of it. Starlette reaches exactly that state:
`StreamingResponse` starts `stream_response` in a task group and cancels it
from `listen_for_disconnect`, and when the client is already gone the
disconnect message is waiting, so the cancel lands before the body's first
step. The hold would sit on the wallet forever and the upstream socket would
never go back to the pool. So `synthesize` pulls one empty chunk out of the
generator itself before returning it; from then on the `finally` is a promise
rather than a hope.

## Why the settlement opens its own database session

The generator outlives the request. `app/db/session.py::get_session` says why
in its own docstring — FastAPI closes the dependency when the *request* ends,
which for a stream is before the generator has finished — so the request-scoped
session is closed under us at exactly the moment settlement needs it. The
`finally` therefore opens `SessionLocal()` itself, and it does so behind
`asyncio.shield`, because a client disconnect reaches us as a cancellation and
an unshielded `await` in a cancelled task re-raises before it runs. Shielding
alone is not enough — a shielded task nobody joins is an orphan the loop drops
at shutdown — so every one of them is held in `_PENDING_SETTLEMENTS` and
`drain_settlements()` joins them from the lifespan shutdown.

## Why a live body stamps a heartbeat as it goes

`expires_at` is a deadline on *starting*, not on finishing, and for a streamed
response those are different questions. `aiter_raw` pulls from upstream only
when the generator is pulled, so a phone on a throttled connection or a paused
`<audio>` element legitimately holds a session open for far longer than a TTL
sized for a healthy stream, without ever tripping a read timeout. The reaper
that frees stranded holds cannot tell that from a process that died mid-stream
unless the stream says so — and while it could not, it closed live ones: the
hold released, the row stamped `FAILED`, and the synthesis then settling into a
terminal session for nothing at all, with no record it had ever been billable.

So `_body` calls `session_service.touch_session` as bytes move. Not per chunk:
that is one round trip per 32 KiB, thirty writes to say one thing about a
single 900 KB `mp3` and thousands about a long one. Once per
`PROGRESS_TOUCH_INTERVAL_SECONDS` of wall clock, which is derived from the
reaper's grace period rather than picked next to it, because neither number
means anything without the other.

## Why a replayed idempotency key has to match the request it replays

`open_oneshot` hands back somebody else's session when the key has been seen
before, and a borrower does not get to spend or close that account. Streaming
different text under a spent key is unlimited free synthesis — the session is
already terminal, so settling it charges nothing — and abandoning a session
that a concurrent original is still streaming voids that caller's charge
outright. So a replay is checked against the session it names before any GPU
time is spent (`_require_matching_replay`), and a replay that does pass is
explicitly denied the lifecycle: it neither settles nor abandons.

What that check compares is a *digest of the request*, never its price. Price
was the obvious comparison and it was the wrong one: the price book charges per
thousand characters with CEIL rounding, so every text from one character to a
thousand quotes the same number and a key checked against its price unlocks all
of them. One paid character bought five free thousand-character syntheses under
the same key, and for a hundred-thousand-character original the bucket was
every text between 99001 and 100000. A price is a bucket; a bucket is not an
identity. `_request_digest` fingerprints what decides the audio — text, voice,
quality, format, sample rate, style — `open_oneshot` writes it on the session,
and a replay whose digest differs is refused whatever it would have cost.

The refusal comes in two codes because they are two different instructions.
`tts_idempotency_conflict` says the key was spent on a *different* request, so
the fix is a fresh key for the new text. `tts_idempotency_spent` says this same
request already finished and was charged, so the fix is to stop retrying: the
audio was streamed and not stored, and re-running the GPU under a session
somebody has already paid for is synthesis nobody is charged for. One code
meaning both is a code a client cannot act on — and since `_finalise` settles
the moment the body ends, "already finished" is the shape of nearly every real
retry, so that client was being told to open a second session and pay twice for
the one request the header exists to make safe.

## Audio duration is measured and never priced

Delivered bytes give the duration exactly for `pcm` and `wav` and not at all
for the compressed formats, so it is derived for those two, reported in the log
line, and kept away from the priced path entirely. The price book has a
`tts`/`tts_characters` row and no `tts`/`tts_audio_ms` row, and
`pricing.price_cumulative` raises `BadRequestError` for an unpriced metric with
a quantity above zero — out of a `finally`, where the response is already gone
and nobody is left to catch it. See the long comment in
`session_service.settle_oneshot`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import AsyncExitStack
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.cache import get_cache, user_sessions_key
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    TooManyRequestsError,
)
from app.core.money import format_credits
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_SESSION_STATUSES,
    BillingService,
    SessionEndReason,
    UsageMetric,
)
from app.models.user import User
from app.services.ai import tts_client
from app.services.billing import reconcile_service, session_service
from app.services.billing.session_service import Ticket

logger = logging.getLogger("synora.tts")

# Our own headers on our own 200. Named here rather than spelled at the call
# site because `main.py` has to repeat them in `CORSMiddleware(expose_headers=)`
# — a browser cannot read a response header that is not exposed, and it fails
# silently, with `undefined` where the price should be.
HEADER_SESSION_ID = "X-Synora-Session-Id"
HEADER_CHARACTERS = "X-Synora-Characters"
HEADER_PRICE_MICROS = "X-Synora-Price-Micros"
HEADER_PRICE = "X-Synora-Price"
HEADER_SAMPLE_RATE = "X-Synora-Sample-Rate"

EXPOSED_HEADERS: tuple[str, ...] = (
    HEADER_SESSION_ID,
    HEADER_CHARACTERS,
    HEADER_PRICE_MICROS,
    HEADER_PRICE,
    HEADER_SAMPLE_RATE,
    # Upstream's own echo of the rate it actually synthesised at, relayed
    # verbatim. It is the only usage-ish header the streaming endpoint sends.
    tts_client.HEADER_SAMPLE_RATE,
)

# The formats whose bytes are raw samples, so byte count and duration are the
# same fact in two units. `mp3` and `opus` are variable-bitrate containers and
# get no duration at all rather than a plausible-looking wrong one.
LINEAR_PCM_FORMATS = frozenset({"pcm", "wav"})
# Signed 16-bit mono, which is what upstream produces and what
# `x-audio-sample-rate` describes. A stereo or 24-bit stream would halve or
# two-thirds this number, and would need upstream to start telling us.
PCM_BYTES_PER_SAMPLE = 2

MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    # Upstream sends Ogg-framed Opus, not raw Opus packets.
    "opus": "audio/ogg",
}

# The window the per-user concurrency cap counts in. The `Cache` protocol has
# an increment and deliberately no decrement, so an exact "how many are open
# right now?" gauge is not available; what is available is "how many started
# recently", and sizing the window at roughly one synthesis makes the two
# coincide for the traffic this actually guards against — a client stuck in a
# retry loop pinning the GPU. Without Redis `NullCache.increment` returns 0, so
# the cap is off entirely, which is the documented no-Redis behaviour.
CONCURRENCY_WINDOW_SECONDS = 60

# The surface this module's idempotency keys belong to. `open_oneshot` requires
# it and refuses to default it, because the constraint behind the key is global
# and cannot tell a streaming call from a batch: without the scope, a
# one-character `POST /tts/speech` sent under a running batch's key settles the
# batch's session for one character and hands its whole hold back. Streaming is
# `speech`; `tts_batch_service` passes `batch`.
IDEMPOTENCY_SCOPE = "speech"

# Every shielded settlement, alive until it finishes.
#
# `asyncio.shield` protects the *inner* coroutine from a cancellation that
# reaches the awaiter, but it does nothing to keep that coroutine alive: the
# shielded task is referenced only by the awaiting frame, which is exactly the
# frame the cancellation is unwinding. A SIGTERM that cancels the request tasks
# therefore leaves the settlement running as an orphan, and the loop closes
# under it — the charge is lost and the hold is stranded until the reaper
# notices. Holding a strong reference here and joining the set from the
# lifespan shutdown is what turns "probably settled" into "settled before the
# process exits". See `drain_settlements`.
_PENDING_SETTLEMENTS: set[asyncio.Task[None]] = set()

# How long the shutdown will wait for those settlements before giving up on
# them. Longer than a healthy settlement by two orders of magnitude and shorter
# than the container runtime's own kill timeout, so the choice between "wait"
# and "get killed mid-write" is never actually made.
DRAIN_TIMEOUT_SECONDS = 30.0

# How many progress marks a healthy stream gets to leave inside one of the
# reaper's grace periods.
#
# The interval below is derived from `reconcile_service.PROGRESS_GRACE_SECONDS`
# rather than written next to it, because the two numbers are one decision and
# reading either alone tells you nothing. The reaper closes a session that is
# past its deadline *and* has been silent for a whole grace period, so this
# ratio is the margin: thirty heartbeats fit in that window, which means
# twenty-nine of them can be missed — an upstream read taking its full
# 300-second budget, a client pausing playback, a heartbeat write swallowed by
# a database blip — before a live stream looks dead. Raising it buys margin at
# the cost of writes; lowering it below about three would let one slow upstream
# read spend the whole grace period.
_PROGRESS_TOUCHES_PER_GRACE_PERIOD = 30

# The floor on the gap between two progress marks from one stream, in seconds
# of wall clock rather than chunks of audio: chunk sizes are upstream's
# business and the reaper asks a question about time.
#
# Thirty seconds, and the cost side is why it is not smaller. Stamping every
# chunk is a round trip per 32 KiB — around thirty writes for a single 900 KB
# `mp3` that streams in under a second, thousands for a long synthesis, all to
# repeat one fact. At this interval that same `mp3` costs nothing at all and an
# hour-long throttled download costs 120 writes, which is the number a
# heartbeat is worth.
PROGRESS_TOUCH_INTERVAL_SECONDS = (
    reconcile_service.PROGRESS_GRACE_SECONDS / _PROGRESS_TOUCHES_PER_GRACE_PERIOD
)


def media_type_for(audio_format: str) -> str:
    """The `Content-Type` for our own response, from the requested format.

    `pcm` is headerless samples, so it gets `application/octet-stream` rather
    than `audio/L16`: nothing decodes L16 without being told the rate and
    channel count out of band, and claiming an audio type for bytes no player
    can open is worse than admitting they are bytes. The rate is on
    `X-Synora-Sample-Rate` for the caller that knows what to do with them.
    """
    return MEDIA_TYPES.get(audio_format, "application/octet-stream")


def audio_ms_for(delivered_bytes: int, *, audio_format: str, sample_rate: int) -> int:
    """Milliseconds of audio in `delivered_bytes`. Zero for compressed formats.

    The 44-byte RIFF header on a `wav` stream is counted as audio and is worth
    less than half a millisecond at any sample rate we accept, which is below
    the resolution of the number itself. Observability only — never priced.
    """
    if audio_format not in LINEAR_PCM_FORMATS or sample_rate <= 0:
        return 0
    return (delivered_bytes * 1000) // (sample_rate * PCM_BYTES_PER_SAMPLE)


def _upstream_body(
    *,
    text: str,
    voice_id: str | None,
    quality: str,
    audio_format: str,
    sample_rate: int,
    style: str | None,
) -> dict[str, Any]:
    """`SynthesizeBody`, in upstream's spelling.

    `format` rather than `audio_format`, and empty strings rather than nulls for
    the optional fields: upstream's model declares them `str = ""` and answers a
    422 for a null. This is the one place the two vocabularies differ, so it is
    the one place that has to know.
    """
    return {
        "text": text,
        "voice_id": voice_id or "",
        "quality": quality,
        "format": audio_format,
        "sample_rate": sample_rate,
        "style": style or "",
    }


def _request_digest(
    *,
    text: str,
    voice_id: str | None,
    quality: str,
    audio_format: str,
    sample_rate: int,
    style: str | None,
) -> str:
    """Fingerprint everything that decides what the audio is.

    Deliberately the same field list as `_upstream_body`, and the two have to
    move together: anything that changes what upstream renders and is *not* in
    here is a field a replay may change freely under a key somebody else paid
    for. A new knob added to one and forgotten in the other is the next version
    of the bug this function exists to close, which is why they are adjacent.

    Keyword-only, because the fields are passed positionally to
    `request_digest_for` one line down and a digest computed from the same
    values in a different order is a replay that stops matching itself.
    """
    return session_service.request_digest_for(
        text, voice_id, quality, audio_format, sample_rate, style
    )


async def _record_progress(ai_session_id: uuid.UUID) -> None:
    """Tell the reaper this stream is still moving. Never raises.

    Its own short-lived `SessionLocal()`, for the same reason the settlement
    opens one: the request-scoped session belongs to a request that ended
    before the body did. `touch_session` is a single `UPDATE` against the
    primary key and its own commit — the reaper runs in another process on
    another connection, and a heartbeat it cannot see is not a heartbeat.

    Swallows everything short of a cancellation. A heartbeat is evidence, not
    work: a database that cannot take one is not a reason to abort a synthesis
    the caller is already being charged for, and the worst case is the reaper
    acting a grace period from now on evidence one interval stale. `Exception`
    and not `BaseException` on purpose — a `CancelledError` here is the client
    hanging up mid-write, and that has to keep propagating so the `finally`
    that settles the stream runs.
    """
    try:
        async with SessionLocal() as db:
            await session_service.touch_session(db, ai_session_id=ai_session_id)
    except Exception as error:  # noqa: BLE001 - a missed heartbeat is not a failure
        logger.warning(
            "tts_progress_touch_failed session=%s: %s", ai_session_id, error
        )


async def _guard_concurrency(user: User) -> None:
    """Refuse a caller who already has too many syntheses in flight."""
    started = await get_cache().increment(
        user_sessions_key(str(user.id)), CONCURRENCY_WINDOW_SECONDS
    )
    if started > settings.tts_max_concurrent_per_user:
        raise TooManyRequestsError(
            "You have too many syntheses running. Please wait for one to finish.",
            code="tts_too_many_concurrent",
            # The window itself: by the time it has rolled, the count this
            # caller tripped on is gone.
            retry_after=CONCURRENCY_WINDOW_SECONDS,
        )


async def _tracked_shield(work: Coroutine[Any, Any, None]) -> None:
    """Run `work` to completion even if this `await` is cancelled.

    Two separate guarantees, and the billing path needs both. The shield is
    what survives a *client* disconnect: it arrives as a cancellation of the
    task doing the awaiting, and an unshielded `await` in a cancelled task
    re-raises at its first suspension — which here is the database write that
    charges or releases. `_PENDING_SETTLEMENTS` is what survives the
    *process*, because a shielded task nobody joins has no owner left once the
    awaiting frame unwinds.

    `ensure_future` before the first `await` on purpose: it is ordinary
    synchronous code, so the task is created and registered even when the
    caller is already being cancelled and the `await` below will not run a
    single step of it.
    """
    task = asyncio.ensure_future(work)
    _PENDING_SETTLEMENTS.add(task)
    task.add_done_callback(_PENDING_SETTLEMENTS.discard)
    await asyncio.shield(task)


async def drain_settlements(timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
    """Join every settlement still in flight. Returns how many there were.

    Called from `main.py`'s lifespan shutdown, and it has to be called *before*
    the HTTP client and the engine are closed: a settlement mid-write still
    needs a database, and one that finds the pool disposed leaves the hold
    exactly where a settlement that was never run would.

    The timeout is not a nicety. Uvicorn's graceful shutdown has already
    cancelled the request tasks by the time this runs, so anything left here is
    shielded work that will not be interrupted by anything short of the process
    dying; without a bound, one wedged connection would hold the deployment
    open until the orchestrator SIGKILLs it — which is the one ending that
    loses *more* than giving up here does.

    That bound is `asyncio.wait` and emphatically not a `gather` under
    `asyncio.timeout`, and the difference is most of the reason this docstring
    is long. A timing-out `gather` is *cancelled*, and `_GatheringFuture.cancel`
    forwards that to every child — so the bound written to stop us waiting for
    the settlements instead killed them, mid-transaction, inside the very
    shield that exists to make them uninterruptible. `CancelledError` is a
    `BaseException`, so `_finalise`'s `except Exception` never saw it: no
    rollback, and no `tts_settle_failed` line naming the session whose hold was
    now stranded. The diagnostic lied about it too — `gather` returns only once
    its children have finished being cancelled, so every task was `done()` and
    the operator was told `unfinished=0` however many had just been killed.
    `wait` observes rather than owns: two sets back, tasks untouched.

    So nothing is cancelled on the way out, and the count below is the honest
    one. Whatever is still running gets the rest of the shutdown — closing the
    HTTP client, the broker and the engine — to land in, and for the few that
    still do not, `reconcile_service.reap_expired_sessions` is the designed
    backstop. A hold left behind here is late, not lost; a settlement killed
    here was lost *and* silent.
    """
    pending = tuple(_PENDING_SETTLEMENTS)
    if not pending:
        return 0

    logger.info("tts_drain_settlements pending=%d", len(pending))
    # No `return_exceptions` to pass and nothing to catch: `wait` never
    # re-raises what its tasks raised. `_finalise` is documented never to raise
    # anyway, so an exception in there would be a bug in that promise rather
    # than something this function could act on — and letting it reach here
    # would cost the other settlements their chance to finish.
    _settled, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        logger.error(
            "tts_drain_settlements_timeout unfinished=%d after %.0fs: left running "
            "rather than cancelled, and their holds are the reaper's problem now",
            len(still_running),
            timeout,
        )
    return len(pending)


async def _finalise(
    stack: AsyncExitStack,
    *,
    ticket: Ticket,
    characters: int,
    delivered_bytes: int,
    audio_format: str,
    sample_rate: int,
    end_reason: SessionEndReason,
    error_code: str | None,
) -> None:
    """Close upstream, then charge for the call. Runs once, whatever happened.

    Never raises. It is the last thing a dead stream does, and an exception here
    would replace a recorded charge with a stack trace and a stranded hold. It
    is also the recovery path for a failure *before* the first byte, where an
    escape would replace the mapped upstream status code with a database error.

    A replayed ticket is finalised by logging and nothing else: see the block
    below for why the borrower must not settle or abandon.
    """
    try:
        await stack.aclose()
    except Exception as error:  # noqa: BLE001 - closing a dead socket is not news
        logger.warning("tts_stream_close session=%s: %s", ticket.ai_session_id, error)

    audio_ms = audio_ms_for(
        delivered_bytes, audio_format=audio_format, sample_rate=sample_rate
    )

    if ticket.replayed:
        # This request borrowed a session an earlier request opened, and a
        # borrower does not get to close the account. Both branches below would
        # do real damage here. `settle_oneshot` on a session the original
        # already finished is a silent no-op, which is what made a spent key
        # into free synthesis; `abandon_oneshot` on a session the original is
        # *still streaming* releases that caller's hold and stamps the row
        # terminal, so their own settlement later finds nothing to charge and
        # they get the whole synthesis free. `_require_matching_replay` has
        # already established that this replay is the same request, so the
        # first request's charge is the right answer for both of them.
        logger.info(
            "tts_stream_replay_end session=%s user=%s chars=%d bytes=%d",
            ticket.ai_session_id,
            ticket.user_id,
            characters,
            delivered_bytes,
        )
    else:
        # Its own session, never the request's: `get_session` closes when the
        # request ends, which for a stream is before the generator has finished.
        async with SessionLocal() as session:
            try:
                if delivered_bytes:
                    # Full text, not delivered text. A caller who hung up after
                    # one chunk still made us synthesise the whole thing.
                    await session_service.settle_oneshot(
                        session,
                        ai_session_id=ticket.ai_session_id,
                        quantities={UsageMetric.TTS_CHARACTERS: characters},
                        end_reason=end_reason,
                    )
                else:
                    # Not one byte of audio reached us, so there is nothing to
                    # bill for and our supplier's bad afternoon is not the
                    # user's problem. The hold goes back and the session
                    # charges zero.
                    await session_service.abandon_oneshot(
                        session,
                        ai_session_id=ticket.ai_session_id,
                        end_reason=end_reason,
                        error_code=error_code,
                    )
            except Exception:
                logger.exception(
                    "tts_settle_failed session=%s chars=%d bytes=%d",
                    ticket.ai_session_id,
                    characters,
                    delivered_bytes,
                )
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001 - the database is what failed
                    # Guarded because the commonest reason to be here at all is
                    # that the database went away, and rolling back over the
                    # same dead connection raises again — out of a function
                    # documented "never raises", through the shield, into a
                    # generator's `finally` with nobody left to catch it. That
                    # would take the log line above with it: the one record
                    # naming the session whose hold is now stranded, which is
                    # what `reconcile_service.reap_expired_sessions` will be
                    # cleaning up at the TTL.
                    logger.warning(
                        "tts_settle_rollback_failed session=%s",
                        ticket.ai_session_id,
                    )

    # Paired with the `inc()` in `synthesize`, and this function is the only
    # place that decrements: every path that opens an upstream response reaches
    # it exactly once, including the one where opening the response is what
    # failed. A gauge that leaks here reads as syntheses that never end, which
    # is the same shape as the bug it exists to reveal.
    metrics.tts_streams_inflight.dec()
    metrics.record_stream(
        end_reason=end_reason.value,
        characters=characters,
        audio_bytes=delivered_bytes,
        replayed=ticket.replayed,
    )

    logger.info(
        "tts_stream session=%s user=%s chars=%d bytes=%d audio_ms=%d reason=%s error=%s",
        ticket.ai_session_id,
        ticket.user_id,
        characters,
        delivered_bytes,
        audio_ms,
        end_reason.value,
        error_code or "-",
    )


async def _require_matching_replay(
    session: AsyncSession, *, ticket: Ticket, request_digest: str
) -> None:
    """Refuse a replayed key that is not this same request coming back.

    An `Idempotency-Key` means "this request again". `open_oneshot` can only
    check that the *key* has been seen, never what it was first spent on, so
    without this the key becomes a bearer token for somebody else's paid
    session and two distinct exploits open up. Streaming different text under a
    spent key is unlimited free synthesis: the session is terminal, so the
    settlement rebuilds the first call's answer and charges nothing, forever.
    Streaming *any* text under a key whose original is still on the wire risks
    the reverse — one of the two finalises the shared session and the other's
    charge evaporates.

    So the session the key names is reloaded and asked two questions, in this
    order, and the order is the fix rather than a detail.

    **Is this the same request?** Compared on `request_digest`, which
    `open_oneshot` wrote from `_request_digest` when the session was opened,
    and asked first so that it holds whatever state the original is in — an
    original still ACTIVE is no evidence at all that the replay resembles it.
    The comparison used to be the two prices, which cannot work: the price book
    charges per thousand characters with CEIL rounding, so every text from one
    character to a thousand quotes the same number and a paid single character
    bought free thousand-character syntheses under the same key for as long as
    the original stream was left undrained. A price is a bucket, and a bucket
    is not an identity.

    A session carrying no digest at all cannot answer the question, so it is
    refused too. That is a session opened before this column existed, or by a
    caller that stored nothing; "we wrote nothing down" is not evidence that
    this is the same request, and the window is one deploy plus one TTL.

    Dropping the price comparison also drops a false refusal it caused: a price
    book published between the original and its retry made an honest retry look
    like a different request. The digest is computed from the request alone, so
    nothing our side publishes can change the answer.

    **Has it already finished?** Terminal means the original was billed and
    there is nothing left to replay; re-running the GPU would be work nobody
    pays for, since the audio is not stored anywhere and "return the original
    response" is not on the menu. That is a `409` too, but under its own code:
    `tts_idempotency_spent` tells a client to stop retrying this key, where
    `tts_idempotency_conflict` tells it to send a fresh key for the new text.
    They arrive at the same status and mean opposite things to a retry loop.

    `session` is the request's, which `open_oneshot` rolled back on its way to
    the replay; only plain values are read here, never the caller's `user` row.
    """
    row = (
        await session.execute(
            select(AiSession)
            .where(AiSession.id == ticket.ai_session_id)
            # `open_oneshot` loaded this row a moment ago to build the ticket,
            # so it is in the identity map and a plain `select` would hand back
            # that copy without going to the database. The whole question here
            # is whether somebody else has moved the status since, so the copy
            # is the one thing that cannot answer it.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if row is None:  # pragma: no cover - the ticket was built from this row
        raise ConflictError(
            "This synthesis could not be replayed. Please retry with a fresh "
            "idempotency key.",
            code="tts_idempotency_conflict",
        )

    if row.request_digest is None or row.request_digest != request_digest:
        raise ConflictError(
            "This idempotency key belongs to a different request. Use a fresh "
            "key for different text.",
            code="tts_idempotency_conflict",
        )

    if row.status in TERMINAL_SESSION_STATUSES:
        raise ConflictError(
            "This idempotency key has already been used for a synthesis that "
            "finished and was charged. Use a fresh key to synthesise again.",
            code="tts_idempotency_spent",
        )


async def synthesize(
    session: AsyncSession,
    user: User,
    *,
    text: str,
    voice_id: str | None = None,
    quality: str,
    audio_format: str,
    sample_rate: int,
    style: str | None = None,
    idempotency_key: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[Ticket, dict[str, str], AsyncIterator[bytes]]:
    """Hold the credit, open the upstream stream, hand back a billed body.

    Returns before any audio has moved but *after* upstream has answered, so
    everything the route needs for its own `200` — the session id, the price,
    the sample rate upstream chose — is already known, and every way upstream
    can refuse has already been turned into one of `tts_client`'s errors.

    `session` is the request-scoped one and is used only for the hold and the
    replay check. The returned iterator settles on a session of its own; see
    `_finalise`.

    The iterator comes back already started — one empty chunk has been pulled
    from it — because only a started async generator is guaranteed to be
    finalised, and the settlement lives in its `finally`.

    Raises `ConflictError` for an idempotency key that names a session this
    request is not a replay of — `tts_idempotency_conflict` when the key was
    spent on a different request, `tts_idempotency_spent` when this same
    request has already finished and been charged. See
    `_require_matching_replay`.
    """
    tts_client.require_configured()

    characters = len(text)
    if not characters:
        raise BadRequestError(
            "There is nothing to synthesise.", code="tts_text_empty"
        )
    # Ours, restated from upstream's own cap, and checked before the hold: a
    # refusal after a `place_hold` is credit that has to be given back, and a
    # release we never had to write cannot leak.
    if characters > settings.tts_max_characters:
        raise BadRequestError(
            f"This text is {characters} characters; the limit is "
            f"{settings.tts_max_characters}.",
            code="tts_text_too_long",
        )

    await _guard_concurrency(user)

    # Computed before the session is opened and used twice: written onto a new
    # session, compared against a replayed one. Both sides of the idempotency
    # question therefore come from one function over one field list, which is
    # what makes "the same request" a decidable statement rather than a guess
    # from its price.
    digest = _request_digest(
        text=text,
        voice_id=voice_id,
        quality=quality,
        audio_format=audio_format,
        sample_rate=sample_rate,
        style=style,
    )

    ticket = await session_service.open_oneshot(
        session,
        user_id=user.id,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
        scope=IDEMPOTENCY_SCOPE,
        idempotency_key=idempotency_key,
        request_digest=digest,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    if ticket.replayed:
        # Checked here, before a single character reaches the GPU: a replay
        # this request is not entitled to costs nothing to refuse at this
        # point, and there is no hold of ours to give back either — the ticket
        # names a session somebody else opened and paid for.
        #
        # `ticket.user_id` rather than `user.id` from here down, and on this
        # branch it is not a stylistic preference: reaching a replay means
        # `open_oneshot` caught the duplicate key and rolled its session back,
        # and a rollback drops every row loaded since the transaction began out
        # of the identity map. `user` is the row `CurrentUser` loaded on that
        # same session, so it is detached by now and touching any column on it
        # raises `MissingGreenlet` — a 500 on the one path whose entire purpose
        # is to be the safe answer to a retry. Nothing below may read the ORM
        # object; the ticket carries plain values for that reason.
        await _require_matching_replay(session, ticket=ticket, request_digest=digest)
        # The audio is synthesised again rather than served from a cache,
        # because storing megabytes of it against the chance of a retry is the
        # more expensive mistake. What does not happen twice is the money: no
        # second hold, no second charge, and no lifecycle call at all from this
        # request. See the replay branch in `_finalise`.
        logger.info(
            "tts_stream_replay session=%s user=%s", ticket.ai_session_id, ticket.user_id
        )

    # Incremented here rather than at the top of the function, because
    # everything above can still refuse — a spent idempotency key, a
    # concurrency cap, a text over the ceiling — and none of those reaches
    # `_finalise`, which is what brings the gauge back down.
    metrics.tts_streams_inflight.inc()

    stack = AsyncExitStack()
    try:
        response = await stack.enter_async_context(
            tts_client.stream_speech(
                _upstream_body(
                    text=text,
                    voice_id=voice_id,
                    quality=quality,
                    audio_format=audio_format,
                    sample_rate=sample_rate,
                    style=style,
                )
            )
        )
    except BaseException as error:
        # Nothing was delivered, so nothing is charged and the hold goes back.
        # The route still gets to choose the status code, which is why the
        # original exception is re-raised untouched below.
        #
        # `_finalise` rather than a bare `abandon_oneshot`, for three reasons
        # that all showed up as bugs on this path: it closes the stack, it
        # honours the replay guard — a failed retry must not abandon a session
        # the original is still streaming on — and it is documented never to
        # raise, so a database that is also unwell cannot replace the mapped
        # upstream error with something the route has no answer for.
        #
        # Shielded and tracked for the same reason the body's `finally` is, and
        # here the danger is specific: `except BaseException` catches
        # `CancelledError` too — a shutdown or an outer cancel scope firing
        # while we wait on the GPU's headers, which is a 300-second window —
        # and an unshielded `await` in a cancelled task re-raises at its first
        # suspension. That suspension is the database write that gives the
        # credit back, and the hold is already committed and durable by now.
        cancelled = isinstance(error, asyncio.CancelledError)
        await _tracked_shield(
            _finalise(
                stack,
                ticket=ticket,
                characters=characters,
                delivered_bytes=0,
                audio_format=audio_format,
                sample_rate=sample_rate,
                # Told apart in the row because it is the first thing anyone
                # reading the table wants to know: did our supplier fail, or
                # did we stop waiting?
                end_reason=(
                    SessionEndReason.CLIENT_DISCONNECTED
                    if cancelled
                    else SessionEndReason.UPSTREAM_ERROR
                ),
                error_code=(
                    None
                    if cancelled
                    else error.code
                    if isinstance(error, AppError)
                    else "tts_unreachable"
                ),
            )
        )
        raise

    headers = {
        HEADER_SESSION_ID: str(ticket.ai_session_id),
        HEADER_CHARACTERS: str(characters),
        HEADER_PRICE_MICROS: str(ticket.estimated_micros),
        HEADER_PRICE: format_credits(ticket.estimated_micros),
        HEADER_SAMPLE_RATE: str(sample_rate),
    }
    upstream_rate = response.headers.get(tts_client.HEADER_SAMPLE_RATE)
    if upstream_rate:
        headers[tts_client.HEADER_SAMPLE_RATE] = upstream_rate

    async def _body() -> AsyncIterator[bytes]:
        delivered = 0
        end_reason = SessionEndReason.COMPLETED
        error_code: str | None = None
        # The relay's own clock, measured here rather than in the middleware:
        # `synora_http_request_seconds` stops at the status line, which on this
        # route is before the first byte of audio.
        relay_started = time.monotonic()
        # The clock the interval is measured from, started here rather than
        # left null so that the *first* mark also waits an interval. An
        # ordinary synthesis ends well inside one and therefore writes no
        # heartbeat at all, which is the point: a stream that finishes in a
        # second cannot outlive a ten-minute deadline, so it has nothing to
        # prove and should not pay a round trip to prove it.
        #
        # The case that is left open by that choice, and is the reason this is
        # a comment: a stream that stalls before its first mark — a client that
        # buffers hard in the first thirty seconds and then pauses playback —
        # reaches `expires_at` with `last_heartbeat_at` still null, and a null
        # is reapable on the deadline alone. Marking the first chunk instead
        # would move that from the deadline to the deadline plus a grace
        # period, at the price of one write on every synthesis, and it would
        # not fix the case either: a paused stream stamps nothing while it is
        # paused, so anything paused for longer than the grace period is reaped
        # under either rule. What actually bounds this is the one-shot TTL, and
        # `DEFAULT_ONESHOT_TTL_SECONDS` is where that argument belongs.
        touched_at = time.monotonic()
        # A replay borrows a session the original opened, and a borrower does
        # not get to hold that account open any more than it gets to close it
        # (see `_finalise`). The progress that decides the original's fate is
        # the original's own reading of its own body; stamping it from here
        # would let a stalled original be kept alive indefinitely by somebody
        # else's retries, which is the reaper switched off by a header.
        report_progress = not ticket.replayed
        try:
            # This empty chunk is what makes the `finally` below reachable, and
            # it is the first statement inside the `try` so that no failure can
            # get in front of it.
            #
            # An async generator registers a finalizer with the event loop on
            # its *first* iteration; one that was created and never started has
            # none, so `aclose()` on it is a no-op and `loop.shutdown_asyncgens`
            # has never heard of it. Starlette reaches exactly that state when
            # the client has already gone: `StreamingResponse` starts
            # `stream_response` in a task group and awaits `listen_for_
            # disconnect`, whose `receive()` returns the queued disconnect
            # without suspending, so the cancel lands before the body's first
            # step. The `finally` would never run: the hold would stay on the
            # wallet forever and the `httpx.Response` would hold its pooled
            # connection open until the pool ran dry. `synthesize` therefore
            # pulls this chunk itself before handing the generator over.
            #
            # It costs nothing on the wire. h11 drops a zero-length data chunk
            # rather than writing it — under chunked encoding those bytes are
            # the terminator — and a `Content-Length` writer treats it as no
            # bytes written, which is what it is.
            yield b""

            # `aiter_raw`, never `aiter_bytes`: content decoding buffers, and
            # buffering costs the 100 ms first-audio latency this endpoint
            # exists for. See `tts_client.stream_speech`.
            async for chunk in response.aiter_raw():
                if not chunk:
                    continue
                delivered += len(chunk)
                # Progress is stamped on the clock, not on the chunk count:
                # chunk sizes are upstream's business and the reaper asks a
                # question about time. `time.monotonic` rather than wall clock
                # because an NTP step during a long download must not turn one
                # interval into an hour — or into never.
                now = time.monotonic()
                if report_progress and (
                    now - touched_at >= PROGRESS_TOUCH_INTERVAL_SECONDS
                ):
                    touched_at = now
                    await _record_progress(ticket.ai_session_id)
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            # The client hung up, or the server is shutting down. Billed for
            # the whole text either way — the synthesis happened.
            end_reason = SessionEndReason.CLIENT_DISCONNECTED
            raise
        except httpx.HTTPError as error:
            # The status line went out before the first chunk was pulled, so
            # there is no status code left to choose. What is left is the
            # connection, and dropping it is the message.
            end_reason = SessionEndReason.UPSTREAM_ERROR
            error_code = "tts_unreachable"
            logger.warning(
                "tts_stream_broken session=%s after %d bytes: %s",
                ticket.ai_session_id,
                delivered,
                error,
            )
            # Re-raised, not swallowed. Returning here ends the body *cleanly*:
            # Starlette sends a final empty chunk, uvicorn writes the
            # terminating zero-length chunk, and the caller receives a
            # well-formed `200` wrapped around a truncated audio file. A
            # streamed response carries no `Content-Length`, and no mapping
            # from characters to bytes exists for a compressed format, so
            # neither a browser nor `curl` nor an SDK has anything to check it
            # against — they save half an mp3 and are billed for all of it.
            # Aborting the response without its terminator is the only signal
            # HTTP has for "this body is incomplete", and every client already
            # understands it. The `finally` below still settles for the full
            # text, which is the agreed policy: the GPU did the whole job.
            raise
        finally:
            # Observed before the shielded await, not after: a cancelled task
            # re-raises at its first suspension once the shield's coroutine is
            # done, so a line below it is a line that does not run on the
            # disconnect path — which is exactly the case worth measuring.
            metrics.tts_stream_seconds.observe(time.monotonic() - relay_started)
            # Shielded and tracked: a disconnect arrives as a cancellation, and
            # an unshielded await in a cancelled task re-raises before it runs.
            # The bill for a disconnected stream is the whole text, so this is
            # the one await that must not be skippable — nor droppable at
            # shutdown, which is what `_PENDING_SETTLEMENTS` adds on top.
            await _tracked_shield(
                _finalise(
                    stack,
                    ticket=ticket,
                    characters=characters,
                    delivered_bytes=delivered,
                    audio_format=audio_format,
                    sample_rate=sample_rate,
                    end_reason=end_reason,
                    error_code=error_code,
                )
            )

    # Started here rather than handed over cold. One `__anext__` advances it to
    # the empty `yield` above and no further — no audio has moved, which is
    # what this function's contract promises — and that single step is what
    # registers the generator with the loop's asyncgen finalizer. Everything
    # after this point is guaranteed to reach the `finally`.
    body = _body()
    await body.__anext__()
    return ticket, headers, body
