"""One voice-agent call: hold, relay the signalling, keep the heartbeat.

The third gateway, and the first that the work does not pass through. A
synthesis streams through `tts_service` and a live transcription through
`stt_stream_service`, so each of them *watches* its work end and settles on
what it saw. A voice call is WebRTC: the browser and the agent exchange audio
directly over UDP, and all that reaches us is the negotiation — one SDP offer,
its answer, and a burst of ICE candidates — within the first second or so of
a call that may last ten minutes. The key the agent is opened with never leaves
this process, which is the reason the proxy exists at all; the agent's own
guide says as much, and it is the same trade the other two gateways make.

This module is the request half: open a call, relay its candidates, take its
heartbeats, hang it up. When and how a call *ends* — and what it costs when it
does — is `voice_call_lifecycle`, because the sweeper needs all of that without
a request to hang it on; reading calls back, and what a client needs before it
starts one, is `voice_call_reads`.

## What it costs not to see the media

The agent has no route that ends a call, lists calls or reports usage, so the
evidence of how long a call lasted is the client's heartbeat, backed by asking
the agent whether it still holds a call whose client went quiet. The rules that
follow from that — every timestamp ours, a call that never connected free,
silence billed to the last proof of life, the ceiling a hard stop on the bill —
are set out in `voice_call_lifecycle`, next to the code that applies them.

## It is a bounded one-shot, like the realtime transcription

`session_service` has no hold extension, so the hold covers the ceiling and the
difference comes back at settlement, exactly as `stt_stream_service` does it.
The hold is also why a user with less credit than the ceiling costs cannot open
a call at all; `GET /voice/config` says how much that is, so a client can tell
the user before they press the button rather than after.

## One line per user, counted twice and asked once

The line cap is counted before the hold and again once the call is a row,
which is what makes it exact rather than advisory against two opens racing. And
a call that was hung up but whose agent connection is still up counts too —
see `voice_call_lifecycle.lingering_lines` — so one account can never hold more
of the shared agent than its cap, whatever its client does after `DELETE`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics, throttle
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from app.db.base import as_utc, utcnow
from app.db.session import SessionLocal
from app.models.billing_enums import (
    AiSessionKind,
    BillingService,
    SessionAction,
    SessionEndReason,
)
from app.models.user import User
from app.models.voice_call import IDEMPOTENCY_SCOPE, VoiceCall
from app.services.ai import voice_agent_client, voice_call_lifecycle
from app.services.ai.voice_call_lifecycle import (
    billed_duration,
    ceiling,
    heartbeat_timeout,
    live_calls,
    start_sweeper,
    stop_sweeper,
    sweep,
    unanswered_grace,
)
from app.services.ai.voice_call_reads import (
    CallRecord,
    VoiceConfig,
    ceiling_quantities,
    config,
    get_call,
    page,
)
from app.services.billing import session_service

logger = logging.getLogger("synora.voice")

# Re-exported: the lifespan, the admin reconcile and the tests reach them here.
__all__ = [
    "IDEMPOTENCY_SCOPE",
    "CallRecord",
    "VoiceConfig",
    "billed_duration",
    "ceiling_quantities",
    "config",
    "get_call",
    "page",
    "start_sweeper",
    "stop_sweeper",
    "sweep",
]

# A browser's offer with audio, video and a data channel is six to ten
# kilobytes, most of it the video codec list. Sixty-four is room for every
# codec a browser ships and still refuses a body nobody's browser wrote.
MAX_SDP_CHARACTERS = 64 * 1024
# A candidate line is a hundred-odd characters; a browser gathers a handful per
# interface, plus one per STUN and TURN server. The per-call cap is what stops a
# client looping on `onicecandidate` from spending the agent's rate limit on
# the one key every one of our users shares.
MAX_CANDIDATE_CHARACTERS = 1024
MAX_CANDIDATES_PER_REQUEST = 32
MAX_CANDIDATES_PER_CALL = 64
# When a heartbeat starts answering `warn`: a minute before the ceiling, so a
# client can tell the user the call is about to end rather than dropping it.
WARN_BEFORE_END_SECONDS = 60
# The window `VOICE_AGENT_MAX_OPENS_PER_MINUTE` is counted over.
OPEN_WINDOW = timedelta(seconds=60)

_MEDIA_LINE = re.compile(r"^m=(audio|video|application)\b", re.MULTILINE)


# --- what the callers get back ---------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenedCall:
    """An answered call: the SDP to hand the browser, and how to keep it alive."""

    ai_session_id: uuid.UUID
    sdp: str
    type: str
    answered_at: datetime
    reserved_micros: int


@dataclass(frozen=True, slots=True)
class Pulse:
    """What one heartbeat was told."""

    action: SessionAction
    elapsed_ms: int
    remaining_ms: int


# --- rules that need no database -------------------------------------------


def validate_offer(sdp: str, sdp_type: str) -> None:
    """Refuse an offer that cannot work, before anything is held for it.

    The agent's guide lists three mistakes that produce a call which looks
    connected and is not. Two of them are visible in the offer itself, and this
    turns the worse one — no video transceiver, so no audio ever flows and
    nothing anywhere reports an error — into a `400` that names the fix. The
    missing data channel is deliberately *not* refused: the guide calls a call
    without one legitimate, just blind to transcript and state events.
    """
    if sdp_type != "offer":
        raise BadRequestError(
            "A call is opened with a fresh `offer`; renegotiating an existing "
            "one is not supported — end it and open another.",
            code="voice_offer_invalid",
        )
    if len(sdp) > MAX_SDP_CHARACTERS:
        raise BadRequestError(
            f"The offer is larger than {MAX_SDP_CHARACTERS // 1024} KB.",
            code="voice_offer_too_large",
        )
    if not sdp.startswith("v=0"):
        raise BadRequestError(
            "That is not an SDP offer. Send `pc.localDescription.sdp` unchanged.",
            code="voice_offer_invalid",
        )
    media = set(_MEDIA_LINE.findall(sdp))
    if "audio" not in media:
        raise BadRequestError(
            "The offer carries no audio. Add the microphone track with "
            '`pc.addTransceiver(track, {direction: "sendrecv"})` before '
            "`createOffer()`.",
            code="voice_offer_no_audio",
        )
    if "video" not in media:
        raise BadRequestError(
            "The offer has no video transceiver. The agent needs one negotiated "
            'even for a voice call — add `pc.addTransceiver("video", '
            '{direction: "sendrecv"})` before `createOffer()`. The camera is '
            "never opened; without it the call connects and no audio flows.",
            code="voice_offer_no_video",
        )


def usable_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The candidates worth relaying, in the agent's field names.

    An empty `candidate` is the browser's end-of-candidates marker. The agent
    has no use for it and the guide's own client never sends it, so it is
    dropped here rather than costing a round trip.
    """
    return [
        {
            "candidate": entry["candidate"],
            "sdp_mid": entry.get("sdp_mid"),
            "sdp_mline_index": entry.get("sdp_mline_index"),
        }
        for entry in candidates
        if entry.get("candidate")
    ]


# --- opening ---------------------------------------------------------------


def _line_busy(*, lingering: bool = False, attempts_only: bool = False) -> TooManyRequestsError:
    limit = settings.voice_agent_max_concurrent_per_user
    if lingering and attempts_only:
        # Held peers that never connected: failed attempts the agent has not
        # dropped yet. Nothing the user can close — the agent lets them go on
        # its own within about a minute of their offer.
        return TooManyRequestsError(
            "Your last connection attempts are still being closed by the voice "
            "agent. Please try again in a moment.",
            code="voice_call_still_connected",
            retry_after=voice_call_lifecycle.ATTEMPT_TEARDOWN_SECONDS,
        )
    if lingering:
        # Its own code, because the instruction is different: nothing here can
        # end that call — the tab or app still holding it has to close it.
        return TooManyRequestsError(
            "Your previous call is still connected to the voice agent. Close it "
            "— the tab or the app it is running in — and try again.",
            code="voice_call_still_connected",
            retry_after=voice_call_lifecycle.HANGUP_SETTLE_SECONDS,
        )
    return TooManyRequestsError(
        f"You already have {limit} call{'s' if limit != 1 else ''} in progress. "
        "End it before starting another.",
        code="voice_call_limit",
        retry_after=settings.voice_agent_heartbeat_timeout_seconds,
    )


async def _enforce_open_rate(session: AsyncSession, user_id: uuid.UUID) -> None:
    """At most `VOICE_AGENT_MAX_OPENS_PER_MINUTE` opens a minute, counted in the database.

    Not `app.core.throttle`, which counts in Redis and is off without it —
    acceptable for a login brake, and not for the one bound on how fast an
    account can make the agent build pipelines. The rows are already here and
    `ix_voice_calls_user_created` answers this in one range scan.
    """
    now = utcnow()
    count, oldest = (
        await session.execute(
            select(func.count(), func.min(VoiceCall.created_at)).where(
                VoiceCall.user_id == user_id, VoiceCall.created_at > now - OPEN_WINDOW
            )
        )
    ).one()
    if count >= settings.voice_agent_max_opens_per_minute:
        # `oldest` is None only when the limit is zero or below — refused at
        # boot outside development, and a whole window's wait here.
        retry_after = (
            max(1, int((as_utc(oldest) + OPEN_WINDOW - now).total_seconds()) + 1)
            if oldest is not None
            else int(OPEN_WINDOW.total_seconds())
        )
        raise TooManyRequestsError(
            "Too many calls started in the last minute. Please wait a moment.",
            code="voice_call_rate_limited",
            retry_after=retry_after,
        )


async def open_call(
    session: AsyncSession,
    user: User,
    *,
    sdp: str,
    sdp_type: str,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> OpenedCall:
    """Hold for the ceiling, hand the offer to the agent, return its answer.

    Raises before the hold for everything that can be known without the agent
    — configuration, the offer's shape, the throttle, the line cap — and after
    it only for the agent's own refusals, every one of which gives the hold
    back whole: a call the agent never answered is not a call.

    `user.id` is read once, here. The sweep below may roll this session back on
    a row it could not settle, and a rollback expires `user` along with
    everything else the session loaded.
    """
    user_id = user.id
    voice_agent_client.require_configured()
    validate_offer(sdp, sdp_type)
    # Attempts, not only calls: a refused open writes no row for the count
    # below to see, and every attempt may probe the agent on the key all our
    # users share. A brake rather than a wall — Redis-counted, off without it,
    # like every `throttle` — so its limit is generous.
    await throttle.enforce(
        subject=f"user:{user_id}",
        action="voice_attempt",
        limit=max(30, 3 * settings.voice_agent_max_opens_per_minute),
        window_seconds=int(OPEN_WINDOW.total_seconds()),
        message="Too many call attempts in the last minute. Please wait a moment.",
        code="voice_call_rate_limited",
    )
    await _enforce_open_rate(session, user_id)

    # This user's own dead calls first. A tab that crashed a minute ago must not
    # be what refuses the call they are now trying to make, and must not keep a
    # whole ceiling of their credit reserved while they make it.
    await sweep(session, user_id=user_id)
    limit = settings.voice_agent_max_concurrent_per_user
    live = await live_calls(session, user_id)
    if live >= limit:
        raise _line_busy()
    held, only_attempts = await voice_call_lifecycle.lingering_lines(session, user_id)
    if live + held >= limit:
        raise _line_busy(lingering=True, attempts_only=only_attempts)

    ticket = await session_service.open_oneshot(
        session,
        user_id=user_id,
        service=BillingService.VOICE_AGENT,
        model_key=settings.voice_agent_model_key,
        quantities=ceiling_quantities(),
        scope=IDEMPOTENCY_SCOPE,
        kind=AiSessionKind.REALTIME,
        # Informational: the generic reaper stands off voice sessions, and the
        # deadlines that bind are the lifecycle module's. Sized to outlive every
        # one of them, so the column never claims a live call is late.
        ttl_seconds=int((ceiling() + heartbeat_timeout() + unanswered_grace()).total_seconds()),
        client_ip=client_ip,
        user_agent=user_agent,
    )
    call_id = ticket.ai_session_id

    try:
        session.add(VoiceCall(id=call_id, user_id=user_id, last_seen_at=utcnow()))
        await session.commit()
        # Counted again now that this call is a row, which is what makes the
        # cap exact rather than advisory: two opens racing both pass the count
        # above, and both see each other here. In a tight race both give up,
        # and the user presses the button again; neither ever gets a second line.
        others = await live_calls(session, user_id, excluding=call_id)
        # Ends the read above. Without it the count's transaction stays open
        # through an offer that can take most of a minute, pinning a pooled
        # connection — and, on SQLite, a read lock every writer waits behind.
        await session.commit()
        if others >= limit:
            raise _line_busy()
    except AppError as error:
        await _give_up(call_id, SessionEndReason.USER_CANCELLED, error.code)
        raise
    except BaseException:
        await asyncio.shield(
            _give_up(call_id, SessionEndReason.INTERNAL_ERROR, "voice_call_not_recorded")
        )
        raise

    try:
        answer = await voice_agent_client.offer(sdp=sdp, sdp_type=sdp_type)
    except AppError as error:
        await _give_up(call_id, SessionEndReason.UPSTREAM_ERROR, error.code)
        raise
    except asyncio.CancelledError:
        # The request was cancelled mid-offer — a shutdown, most often. Shielded,
        # because the release is exactly the work a cancellation would drop.
        await asyncio.shield(
            _give_up(call_id, SessionEndReason.CLIENT_DISCONNECTED, "voice_offer_cancelled")
        )
        raise
    except Exception:
        logger.exception("voice_offer_failed call=%s", call_id)
        await _give_up(call_id, SessionEndReason.INTERNAL_ERROR, "voice_offer_failed")
        raise

    answered_at = utcnow()
    try:
        recorded = await session.execute(
            update(VoiceCall)
            .where(VoiceCall.id == call_id, VoiceCall.ended_at.is_(None))
            .values(
                upstream_pc_id=answer.pc_id,
                answered_at=answered_at,
                last_seen_at=answered_at,
                updated_at=answered_at,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    except BaseException:
        await asyncio.shield(
            _give_up(call_id, SessionEndReason.INTERNAL_ERROR, "voice_answer_not_recorded")
        )
        raise
    if recorded.rowcount != 1:
        # Released while the offer was in flight. The sweeper waits twice the
        # offer's own deadline before it may, so this is a clock that jumped or
        # a deadline edited under a running process — or the user's other tab
        # hanging up a call it found in the list. The call is already given
        # back, so the only thing left to do is say so.
        raise ServiceUnavailableError(
            "The voice agent took too long to answer. Please try again.",
            code="voice_offer_expired",
        )

    logger.info(
        "voice_call_answered call=%s user=%s hold=%s", call_id, user_id, ticket.reserved_micros
    )
    return OpenedCall(
        ai_session_id=call_id,
        sdp=answer.sdp,
        type=answer.type,
        answered_at=answered_at,
        reserved_micros=ticket.reserved_micros,
    )


async def _give_up(call_id: uuid.UUID, end_reason: SessionEndReason, error_code: str) -> None:
    """Release a call that never got an answer. Never raises.

    A session of its own rather than the caller's, because the caller's may be
    the thing that failed, and this runs inside `except` blocks where a second
    exception would replace the first and strand the hold. Counted only when it
    is what ended the call: a call a second tab hung up mid-offer has been
    counted already, by `voice_call_lifecycle.settle`.
    """
    try:
        async with SessionLocal() as own:
            await session_service.abandon_oneshot(
                own, ai_session_id=call_id, end_reason=end_reason, error_code=error_code
            )
            now = utcnow()
            stamped = await own.execute(
                update(VoiceCall)
                .where(VoiceCall.id == call_id, VoiceCall.ended_at.is_(None))
                .values(ended_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await own.commit()
        if stamped.rowcount == 1:
            metrics.record_voice_call(
                end_reason=end_reason.value, connected=False, answered=False, billed_seconds=0
            )
    except Exception:  # noqa: BLE001 - the sweeper is the backstop
        logger.exception("voice_give_up_failed call=%s", call_id)


# --- during the call -------------------------------------------------------


async def _own(session: AsyncSession, user_id: uuid.UUID, call_id: uuid.UUID) -> VoiceCall:
    """The caller's call, freshly read. Somebody else's is a 404, never a 403."""
    call = (
        await session.execute(
            select(VoiceCall)
            .where(VoiceCall.id == call_id, VoiceCall.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError("No such call.", code="voice_call_not_found")
    return call


async def _take_candidate_budget(
    session: AsyncSession, user_id: uuid.UUID, call_id: uuid.UUID, wanted: int
) -> tuple[str, int]:
    """Reserve room for up to `wanted` candidates. Returns the agent's handle and how many fit.

    Takes the part of a batch that fits rather than refusing it whole: the
    candidates a browser gathers last are its TURN relays, and on a network
    where only a relay works they are the ones that matter. A compare-and-swap
    on the counter, retried, because two relays for one call can overlap.
    """
    for _ in range(3):
        call = await _own(session, user_id, call_id)
        if call.ended_at is not None:
            raise ConflictError("This call has ended.", code="voice_call_ended")
        if call.upstream_pc_id is None:
            raise ConflictError("This call has not been answered yet.", code="voice_call_not_answered")
        take = min(wanted, MAX_CANDIDATES_PER_CALL - call.candidates)
        if take <= 0:
            raise BadRequestError(
                f"A call may relay at most {MAX_CANDIDATES_PER_CALL} network candidates.",
                code="voice_candidates_exhausted",
            )
        taken = await session.execute(
            update(VoiceCall)
            .where(
                VoiceCall.id == call_id,
                VoiceCall.ended_at.is_(None),
                VoiceCall.candidates == call.candidates,
            )
            .values(candidates=VoiceCall.candidates + take, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if taken.rowcount == 1:
            return call.upstream_pc_id, take
    raise ConflictError("This call is busy. Please retry.", code="voice_call_busy")


async def relay_candidates(
    session: AsyncSession,
    user: User,
    call_id: uuid.UUID,
    candidates: list[dict[str, Any]],
) -> int:
    """Relay a batch of the browser's ICE candidates. Returns how many went.

    The database session is committed before the agent is contacted, so no
    pooled connection waits on somebody else's network.
    """
    user_id = user.id
    voice_agent_client.require_configured()
    usable = usable_candidates(candidates)
    if not usable:
        call = await _own(session, user_id, call_id)
        if call.ended_at is not None:
            raise ConflictError("This call has ended.", code="voice_call_ended")
        return 0

    pc_id, take = await _take_candidate_budget(session, user_id, call_id, len(usable))
    try:
        await voice_agent_client.add_candidates(pc_id=pc_id, candidates=usable[:take])
    except ConflictError as error:
        if error.code == "voice_call_gone":
            # The agent has already torn this call down — the one authoritative
            # end this module ever hears about. Settled now, and marked gone so
            # the line is free at once rather than after a probe.
            call = await _own(session, user_id, call_id)
            if call.ended_at is None:
                await voice_call_lifecycle.settle(
                    session, call, end_reason=SessionEndReason.UPSTREAM_ERROR, gone=True
                )
        raise
    return take


async def heartbeat(session: AsyncSession, user: User, call_id: uuid.UUID) -> Pulse:
    """Record that a call is still up, and say whether it may stay up.

    The hot path of a call — once every `VOICE_AGENT_HEARTBEAT_SECONDS` for its
    whole length — so the ordinary case is a single conditional `UPDATE` that
    both checks and stamps: the caller's, live, answered, inside the ceiling
    and vouched for within the timeout. The first one also stamps
    `connected_at`, which is what turns a call from "released free" into
    "billed".

    Anything the `UPDATE` did not match takes the slow path. A call past its
    ceiling is settled and answered `stop`. A call that went quiet for longer
    than the timeout is put to the agent: still held, and the heartbeat revives
    it — the client was cut off from us, not from the call; gone, or nobody to
    ask, and it is settled at its last proof of life and answered `stop`.
    """
    user_id = user.id
    now = utcnow()
    quiet_before = now - heartbeat_timeout()
    answered_at = (
        await session.execute(
            update(VoiceCall)
            .where(
                VoiceCall.id == call_id,
                VoiceCall.user_id == user_id,
                VoiceCall.ended_at.is_(None),
                VoiceCall.answered_at.is_not(None),
                VoiceCall.answered_at > now - ceiling(),
                or_(
                    VoiceCall.last_seen_at >= quiet_before,
                    VoiceCall.agent_seen_at >= quiet_before,
                ),
            )
            .values(
                last_seen_at=now,
                heartbeats=VoiceCall.heartbeats + 1,
                # Bound with the column's own type, so SQLite stores it in the
                # same format as every other timestamp in the row.
                connected_at=func.coalesce(
                    VoiceCall.connected_at, literal(now, VoiceCall.connected_at.type)
                ),
                updated_at=now,
            )
            .returning(VoiceCall.answered_at)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    await session.commit()

    if answered_at is not None:
        return _pulse(now, as_utc(answered_at))

    call = await _own(session, user_id, call_id)
    if call.ended_at is None:
        if call.answered_at is None:
            raise ConflictError("This call has not been answered yet.", code="voice_call_not_answered")
        if now - as_utc(call.answered_at) >= ceiling():
            # Past the ceiling: the sweep's own decision, probe included, so a
            # quiet call the agent still holds is billed *to* the ceiling here
            # too rather than to a last proof of life up to a minute short.
            await voice_call_lifecycle.resolve(session, call)
        else:
            await session.commit()
            alive = await voice_call_lifecycle.agent_holds(call)
            gone = alive is False
            if alive and await _revive(session, call, now):
                return _pulse(now, as_utc(call.answered_at))
            # Read again: the probe took seconds, and a sweep in another
            # worker may have kept this call meanwhile — settling off the read
            # from before the probe would bill a call the agent vouched for.
            call = await _own(session, user_id, call_id)
            fresh = utcnow()
            if call.ended_at is None and not gone and (
                fresh - voice_call_lifecycle.proof_of_life(call) <= heartbeat_timeout()
            ) and await _revive(session, call, fresh):
                return _pulse(fresh, as_utc(call.answered_at))
            if call.ended_at is None:
                await voice_call_lifecycle.settle(
                    session, call, end_reason=SessionEndReason.HEARTBEAT_TIMEOUT, gone=gone
                )
    record = await get_call(session, user_id, call_id)
    return Pulse(action=SessionAction.STOP, elapsed_ms=record.billed_ms, remaining_ms=0)


def _pulse(now: datetime, answered_at: datetime) -> Pulse:
    elapsed = now - answered_at
    remaining = ceiling() - elapsed
    action = (
        SessionAction.WARN
        if remaining <= timedelta(seconds=WARN_BEFORE_END_SECONDS)
        else SessionAction.CONTINUE
    )
    return Pulse(
        action=action,
        elapsed_ms=voice_call_lifecycle.ms(elapsed),
        remaining_ms=voice_call_lifecycle.ms(remaining),
    )


async def _revive(session: AsyncSession, call: VoiceCall, now: datetime) -> bool:
    """A quiet call the agent vouches for heartbeats again: stamp it live."""
    revived = await session.execute(
        update(VoiceCall)
        .where(VoiceCall.id == call.id, VoiceCall.ended_at.is_(None))
        .values(
            last_seen_at=now,
            agent_seen_at=now,
            heartbeats=VoiceCall.heartbeats + 1,
            connected_at=func.coalesce(
                VoiceCall.connected_at, literal(now, VoiceCall.connected_at.type)
            ),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if revived.rowcount == 1:
        logger.warning(
            "voice_call_revived call=%s quiet_for=%ds",
            call.id,
            (now - as_utc(call.last_seen_at)).total_seconds(),
        )
    return revived.rowcount == 1


# --- ending ----------------------------------------------------------------


async def end_call(session: AsyncSession, user: User, call_id: uuid.UUID) -> CallRecord:
    """The client hung up. Bill the call and hand back what it cost.

    Idempotent: ending an ended call answers with the first ending's bill,
    because the client's `pagehide` handler and its Stop button routinely both
    fire for the same call.
    """
    user_id = user.id
    call = await _own(session, user_id, call_id)
    if call.ended_at is None:
        await voice_call_lifecycle.settle(session, call, end_reason=SessionEndReason.CLIENT_HANGUP)
    return await get_call(session, user_id, call_id)


