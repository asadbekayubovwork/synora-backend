"""How a voice call ends, and what it costs when it does.

`voice_agent_service` opens calls and answers their requests; this module
decides when a call is over and settles it, whoever noticed first. The split is
along the one line that matters: everything here runs as well from the sweeper
as from a request, and none of it needs a request to exist.

## What a call is billed for

From `connected_at` — the first heartbeat, sent the moment the media is up —
to the latest proof it was still up, capped at the ceiling that runs from the
agent's answer, on our clock throughout:

- **A client that hangs up** is billed to the moment it said so.
- **A client that went quiet** — no heartbeat for the timeout — is billed to its
  last proof of life plus one interval, and flagged `disputed`, because that end
  is an inference rather than a report.
- **A call that never connected** — no heartbeat, ever — costs nothing: ICE that
  fails for want of a TURN server is the commonest failure the agent's guide
  lists, and it is not the user's to pay for.

## Proof of life, and who may give it

The client, by heartbeating. And, when the client stops, the agent: a call we
would otherwise end is first probed (`voice_agent_client.probe`), and one the
agent still holds is *kept* — `agent_seen_at` stamped, billing carried on —
rather than ended at a bill the client chose by going silent. That is what turns
"stop heartbeating and talk for free" from a trick into a call that is billed
until the agent drops it.

A call that never heartbeated at all is the careful case, because "the agent
holds it" is not "it connected". A browser that closed before relaying a single
ICE candidate leaves the agent a peer with nothing to check: aioice then never
finishes ICE, and the live agent drops such a peer after about a minute on a
timeout of its own (measured against it, not assumed). So the first time a
never-heartbeated call is found held, it is *nudged* — one unroutable
candidate, `voice_agent_client.NUDGE_CANDIDATE`, which fails in about a minute
on any aiortc agent, timeout or not — and it is taken as connected from its
answer only if the agent still holds it `NUDGE_GRACE_SECONDS` after the nudge
and `NEVER_CONNECTED_GRACE_SECONDS` after the answer. A peer that failed to
connect is gone by then on every agent this has been run against; one that
connected and is simply not heartbeating is still there, and is billed.

The probe is believed only when `voice_agent_client.liveness_is_trustworthy`
says so. Otherwise every rule above falls back to the heartbeat alone, which is
the behaviour this module had before the agent could be asked.

## What still cannot be closed from here

A client that hangs up by `DELETE` and keeps its peer connection open is billed
to the `DELETE`; so is one that runs past the ceiling. The agent has no route
that ends a call, so nothing here can stop the audio. What *can* be done is keep
it to one call: a call whose agent connection outlives its settlement still
occupies its user's line (`lingering_lines`), so one account can never hold more
agent connections than its cap — the shared agent's capacity stays everyone's.
The fix for the rest is the agent's: its own session ceiling, or the signed
usage reports in `docs/INTERNAL_API.md`.

## Settling is safe to race

A hang-up, a heartbeat past the ceiling, the sweep on the user's next `POST`,
the in-process loop and the admin reconcile can all reach one call together.
The price is a function of the timestamps on the row, not of who computes it,
and `session_service` makes a second settlement of a session a replay of the
first. What a racer must not do is read an ORM attribute after losing: the
loser's `session_service` call rolls its transaction back, which expires every
loaded instance, and a lazy load on an async session raises. So `_settle` reads
everything it needs *before* it settles, and nothing after.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import settings
from app.db.base import as_utc, utcnow
from app.db.session import SessionLocal
from app.models.ai_session import AiSession
from app.models.billing_enums import (
    TERMINAL_SESSION_STATUSES,
    AiSessionKind,
    BillingService,
    SessionEndReason,
    UsageMetric,
)
from app.models.voice_call import OWN_SESSION_KEY_PATTERN, VoiceCall
from app.services.ai import voice_agent_client
from app.services.billing import session_service

logger = logging.getLogger("synora.voice")

# The ways a call can end, exported at zero — see `metrics.prime_voice_series`.
# A connected call ends by a hang-up, a silence, the ceiling or the agent
# dropping it; an answered one that never connected by the same, short of the
# ceiling; and one the agent never answered by being refused, cancelled, lost
# to our own failure, or left unanswered past twice the offer deadline.
metrics.prime_voice_series(
    connected_reasons=[
        reason.value
        for reason in (
            SessionEndReason.CLIENT_HANGUP,
            SessionEndReason.HEARTBEAT_TIMEOUT,
            SessionEndReason.MAX_DURATION,
            SessionEndReason.UPSTREAM_ERROR,
        )
    ],
    unconnected_reasons=[
        reason.value
        for reason in (
            SessionEndReason.CLIENT_HANGUP,
            SessionEndReason.HEARTBEAT_TIMEOUT,
            SessionEndReason.UPSTREAM_ERROR,
        )
    ],
    unanswered_reasons=[
        reason.value
        for reason in (
            SessionEndReason.UPSTREAM_ERROR,
            SessionEndReason.USER_CANCELLED,
            SessionEndReason.CLIENT_DISCONNECTED,
            SessionEndReason.INTERNAL_ERROR,
            SessionEndReason.TIMEOUT,
            SessionEndReason.CLIENT_HANGUP,
        )
    ],
)

# One sweep pass, at most. The in-process loop comes back in thirty seconds.
SWEEP_BATCH = 200
# When a never-heartbeated call the agent still holds is taken to have
# connected: this long after its answer *and* `NUDGE_GRACE_SECONDS` after the
# nudge. The live agent drops a peer that never connected about sixty seconds
# after the offer, and aioice fails the nudge's pair about sixty-four seconds
# after it is relayed (seven STUN transmissions from half a second, doubling),
# so both numbers sit well clear of the moment an honest failure disappears. An
# earlier version used sixty after the answer alone — the very second the live
# agent drops such a peer — and would have billed some ICE failures in full.
NEVER_CONNECTED_GRACE_SECONDS = 120
NUDGE_GRACE_SECONDS = 90
# A hang-up the agent has not noticed yet is ordinary for a few seconds: it
# drops a call on a closed DTLS session at once, and on a silent data channel
# after three. Within this, a line still held is asked again before it is
# refused.
HANGUP_SETTLE_SECONDS = 5
# How far back `lingering_lines` looks for a call whose agent connection might
# still be up. Past a day it is a leak on the agent's side, not a line in use.
LINGER_WINDOW = timedelta(hours=24)
# Ended calls that have not proved they connected and are not counted against
# the line, most recent first. Two, because that is how many of an honest
# user's failed attempts the agent can still be holding when they try again: an
# attempt takes the offer (~6 s) plus the client's connect wait before it gives
# up, and the agent drops a peer that never connected about sixty seconds after
# its offer. One was measured refusing the third try on a network where ICE
# fails; more would let an account stack free agent connections that never
# heartbeat — with two, it holds at most its cap plus two.
UNPROVEN_EXEMPT = 2
# What a user refused for attempts the agent is still tearing down is told to
# wait: its drop of an unconnected peer at about sixty seconds after the offer,
# less the time an attempt has already spent getting refused.
ATTEMPT_TEARDOWN_SECONDS = 30
# Asked about per open, at most, concurrently. Enough that no account can push
# a call it is still holding out of view: at most one unproven call is ever
# exempt (see `lingering_lines`), so any other the agent holds counts and the
# opens that would bury it are refused.
LINGER_CANDIDATES = 10


def ceiling() -> timedelta:
    return timedelta(seconds=settings.voice_agent_max_session_seconds)


def heartbeat_timeout() -> timedelta:
    return timedelta(seconds=settings.voice_agent_heartbeat_timeout_seconds)


def unanswered_grace() -> timedelta:
    """How long a call may sit unanswered before the sweeper may release it.

    Twice the longest an offer can take, because the other side of that
    comparison is a request still in flight: release it early and the agent's
    answer lands on a call that has already given its hold back.
    """
    return timedelta(seconds=2 * voice_agent_client.offer_deadline_seconds())


def ms(delta: timedelta) -> int:
    return max(0, delta // timedelta(milliseconds=1))


def proof_of_life(call: VoiceCall) -> datetime:
    """The latest moment anybody vouched for the call: its client, or the agent."""
    seen = as_utc(call.last_seen_at)
    if call.agent_seen_at is None:
        return seen
    return max(seen, as_utc(call.agent_seen_at))


def billed_duration(
    call: VoiceCall, *, now: datetime, end_reason: SessionEndReason
) -> tuple[int, bool, SessionEndReason]:
    """How long a connected call is billed for, whether that is an inference, and why it ended.

    Connection to hang-up on our own clock. "Connection" is `connected_at` —
    the first heartbeat, which a client sends the moment its media is up — and
    not the answer: ICE through two NATs has been measured taking over thirty
    seconds after the answer on a real network, and those are seconds of
    silence nobody should pay for. A call that never heartbeated and was taken
    as connected from the agent's word has `connected_at` set to its answer,
    so the one kind of call that could shave its bill by delaying that first
    heartbeat is billed from the earliest moment instead. The ceiling still
    runs from the answer — it is what the hold was priced from — so the bill
    can never exceed it. Two corrections on top. A call nobody has
    vouched for in the timeout is billed to its last proof of life plus one
    interval, rather than to whenever somebody got round to settling it — so a
    crashed tab costs the same whether the sweeper found it in thirty seconds
    or the user's next call found it in a week. And nothing is billed past the
    ceiling, whatever the client did after being told to stop.

    Inferred — and so `disputed` — when the end is that last-proof estimate, and
    also when the agent had to vouch for the call at all: a client that stopped
    heartbeating mid-call is the first thing anyone looking at its bill will ask
    about.

    Never zero, for the reason `stt_stream_service._elapsed_ms` gives: a zero
    quantity skips the price line entirely, and the connection fee's minimum
    would then never apply to the one kind of call a minimum exists for.
    """
    answered = as_utc(call.answered_at)
    start = as_utc(call.connected_at) if call.connected_at is not None else answered
    proof = proof_of_life(call)
    cap_end = answered + ceiling()
    quiet = now - proof > heartbeat_timeout()
    if quiet:
        end = proof + timedelta(seconds=settings.voice_agent_heartbeat_seconds)
        end_reason = SessionEndReason.HEARTBEAT_TIMEOUT
    else:
        end = now
    if end >= cap_end:
        end = cap_end
        if not quiet:
            end_reason = SessionEndReason.MAX_DURATION
    inferred = quiet or call.agent_seen_at is not None
    return max(1, ms(end - start)), inferred, end_reason


# --- asking the agent --------------------------------------------------------


async def agent_holds(call: VoiceCall) -> bool | None:
    """Whether the agent still holds this call's peer connection, if it can say."""
    if call.upstream_pc_id is None or not settings.has_voice_agent:
        return None
    if not await voice_agent_client.liveness_is_trustworthy():
        return None
    return await voice_agent_client.probe(call.upstream_pc_id)


async def _mark_gone(session: AsyncSession, call_id: uuid.UUID) -> None:
    now = utcnow()
    await session.execute(
        update(VoiceCall)
        .where(VoiceCall.id == call_id, VoiceCall.gone_at.is_(None))
        .values(gone_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )


def proven_connected(call: VoiceCall, now: datetime) -> bool:
    """Whether a never-heartbeated call the agent still holds has shown it connected.

    Held long enough past both its answer and its nudge that a peer which failed
    ICE would already be gone: dropped by the agent's own timeout, or failed on
    the nudge's pair.
    """
    if call.connected_at is not None:
        return True
    if call.answered_at is None:
        return False
    # An ended call whose settle-time nudge was lost is timed from its end
    # instead, so a nudge that did not land cannot exempt it for ever. A live
    # one keeps no such fallback: `keep` nudges it again on the next pass.
    nudged = call.nudged_at if call.nudged_at is not None else call.ended_at
    if nudged is None:
        return False
    return now - as_utc(nudged) >= timedelta(
        seconds=NUDGE_GRACE_SECONDS
    ) and now - as_utc(call.answered_at) >= timedelta(seconds=NEVER_CONNECTED_GRACE_SECONDS)


async def _still_held(call: VoiceCall, now: datetime) -> tuple[bool | None, bool]:
    """Whether the agent still holds an ended call, and whether it was really asked.

    One probe — repeated for a few seconds when the call was only just hung up.
    A "still held" from the last few seconds, recorded *after* the call ended,
    is reused rather than asked again: an account retrying a refused open must
    not turn its retry rate into probe traffic on the key every user shares. A
    stamp from while the call was live proves nothing about after its hang-up,
    so it never short-circuits the re-probe a just-ended call is owed.
    """
    seen = as_utc(call.agent_seen_at) if call.agent_seen_at is not None else None
    if (
        seen is not None
        and call.ended_at is not None
        and seen >= as_utc(call.ended_at)
        and now - seen < timedelta(seconds=HANGUP_SETTLE_SECONDS)
    ):
        return True, False
    alive = await agent_holds(call)
    recent = call.ended_at is not None and now - as_utc(call.ended_at) < timedelta(
        seconds=HANGUP_SETTLE_SECONDS
    )
    attempts = HANGUP_SETTLE_SECONDS
    while alive and recent and attempts:
        await asyncio.sleep(1)
        alive = await voice_agent_client.probe(call.upstream_pc_id)
        attempts -= 1
    return alive, True


async def lingering_lines(session: AsyncSession, user_id: uuid.UUID) -> tuple[int, bool]:
    """Ended calls of this user whose agent connection is still up. Commits.

    A call is settled the moment its client hangs up, and the agent drops it
    when the browser closes the connection — which a well-behaved client does
    first, and a modified one need not do at all. Without this the line would
    be free again at the `DELETE`, and one account could stack up as many live
    agent connections as the open throttle lets it start. With it, a line is in
    use for as long as the agent says so.

    The most recent `UNPROVEN_EXEMPT` calls are exempt while they have not yet
    proved they connected (`proven_connected`). Until then "held" can mean ICE
    is still failing — the live agent keeps such a peer for a minute — and
    refusing the retry right after a failed connect, the one moment a user most
    needs the button to work, would be the worst place to be strict. Only a
    couple, and only the most recent, because an exemption per unproven call
    was a way round the whole cap: open, `DELETE` at once without ever
    heartbeating, keep the peer, repeat — each call unproven for its first two
    minutes, and so each invisible to the next open. With a fixed exemption an
    account holds at most its cap plus that many agent connections, however it
    plays.

    Returns how many are held, and whether every one of them never connected —
    a user refused for those should hear "your last attempt is still being torn
    down", not "close the other tab".

    Asked about the most recent calls not yet confirmed gone, concurrently. An
    agent that cannot be asked counts nothing: refusing honest users because
    the probe is down would be the worse failure.
    """
    now = utcnow()
    calls = list(
        (
            await session.execute(
                select(VoiceCall)
                .where(
                    VoiceCall.user_id == user_id,
                    VoiceCall.ended_at.is_not(None),
                    VoiceCall.gone_at.is_(None),
                    VoiceCall.upstream_pc_id.is_not(None),
                    VoiceCall.answered_at > now - LINGER_WINDOW,
                )
                .order_by(VoiceCall.ended_at.desc())
                .limit(LINGER_CANDIDATES)
            )
        ).scalars()
    )
    exempt = 0
    while calls and exempt < UNPROVEN_EXEMPT and not proven_connected(calls[0], now):
        calls = calls[1:]
        exempt += 1
    # Ends the read before the network: no pooled connection waits on a probe.
    await session.commit()
    if not calls or not await voice_agent_client.liveness_is_trustworthy():
        return 0, False

    verdicts = await asyncio.gather(*(_still_held(call, now) for call in calls))
    held = 0
    connected_held = False
    for call, (alive, asked) in zip(calls, verdicts, strict=True):
        if alive is False:
            await _mark_gone(session, call.id)
        elif alive:
            held += 1
            connected_held = connected_held or call.connected_at is not None
            if asked:
                # Stamped on the ended row, which billing never reads again, so
                # a retry within `HANGUP_SETTLE_SECONDS` of this *real* answer
                # reuses it — and only then: a reused answer is not re-stamped,
                # so the reuse ends five seconds after the agent last said so.
                await session.execute(
                    update(VoiceCall)
                    .where(VoiceCall.id == call.id)
                    .values(agent_seen_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
            logger.warning(
                "voice_call_outlived_settlement call=%s ended_at=%s", call.id, call.ended_at
            )
    await session.commit()
    return held, held > 0 and not connected_held


# --- ending a call -----------------------------------------------------------


async def settle(
    session: AsyncSession,
    call: VoiceCall,
    *,
    end_reason: SessionEndReason,
    gone: bool = False,
) -> None:
    """Bill one live call and mark it over. Safe to race; commits.

    Everything the settlement needs is read off `call` first, because the
    `session_service` call below may roll this session back — a racer that
    lost — and after that `call` is an expired instance that cannot be read.

    A call that never connected is nudged on its way out, unless it already
    was: the agent may be holding a peer stuck in ICE checking for it, and one
    unroutable candidate is what gets that peer failed and dropped rather than
    left holding a slot on the agent.
    """
    call_id = call.id
    heartbeats = call.heartbeats
    pc_id = call.upstream_pc_id
    answered = call.answered_at is not None
    connected = answered and call.connected_at is not None
    needs_nudge = not connected and not gone and pc_id is not None and call.nudged_at is None
    now = utcnow()
    billed_ms = 0
    if not connected:
        # Never answered, or answered and never heartbeated and never shown to
        # have connected: nothing reached the user, so nothing is owed, and
        # `FAILED` is what `session_service` calls a session that charged
        # nothing.
        await session_service.abandon_oneshot(
            session,
            ai_session_id=call_id,
            end_reason=end_reason,
            error_code="voice_never_connected",
        )
    else:
        billed_ms, inferred, end_reason = billed_duration(call, now=now, end_reason=end_reason)
        await session_service.settle_oneshot(
            session,
            ai_session_id=call_id,
            quantities={UsageMetric.SESSION_MS: billed_ms},
            end_reason=end_reason,
            disputed=inferred,
        )

    values: dict = {"ended_at": now, "updated_at": now}
    if gone:
        values["gone_at"] = now
    # After `session_service` has committed, so the network wait holds no
    # connection; recorded only when the agent took it, so a lost nudge is
    # never mistaken for one whose grace has run.
    if needs_nudge and await voice_agent_client.nudge(pc_id):
        values["nudged_at"] = utcnow()
    stamped = await session.execute(
        update(VoiceCall)
        .where(VoiceCall.id == call_id, VoiceCall.ended_at.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if gone and stamped.rowcount == 0:
        await _mark_gone(session, call_id)
    await session.commit()
    if stamped.rowcount == 1:
        metrics.record_voice_call(
            end_reason=end_reason.value,
            connected=connected,
            answered=answered,
            billed_seconds=billed_ms / 1000,
        )
        logger.info(
            "voice_call_ended call=%s connected=%s billed_ms=%d reason=%s heartbeats=%d",
            call_id,
            "yes" if connected else "no",
            billed_ms,
            end_reason.value,
            heartbeats,
        )


async def keep(session: AsyncSession, call: VoiceCall) -> None:
    """The client went quiet and the agent says the call is up: carry on billing.

    `agent_seen_at` becomes the call's proof of life. A call that never
    heartbeated is nudged the first time it is kept, and taken to have connected
    at its answer once `proven_connected` says so — not before, because until
    then the peer the agent holds may be one still failing ICE.
    """
    now = utcnow()
    values: dict = {"agent_seen_at": now, "updated_at": now}
    answered = as_utc(call.answered_at)
    # The first keep since the client last spoke, as against the sweep coming
    # back to the same silence a minute later: counted once per spell.
    first_of_spell = call.agent_seen_at is None or as_utc(call.agent_seen_at) <= as_utc(
        call.last_seen_at
    )
    if call.connected_at is None:
        if call.nudged_at is None:
            # Sent before the stamp and stamped only if it landed: a nudge the
            # agent never took must not start the grace that trusts it.
            if call.upstream_pc_id and await voice_agent_client.nudge(call.upstream_pc_id):
                values["nudged_at"] = utcnow()
        elif proven_connected(call, now):
            values["connected_at"] = answered
    await session.execute(
        update(VoiceCall)
        .where(VoiceCall.id == call.id, VoiceCall.ended_at.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    # A connected call's first quiet spell, or a never-heartbeated call the
    # moment it proves it connected. Not the re-checks of the same silence, and
    # not a peer still failing ICE — the first read as abuse a minute at a time,
    # the second as abuse where it is a network.
    if (call.connected_at is not None and first_of_spell) or "connected_at" in values:
        metrics.record_voice_call_kept()
    # WARNING: a client that stops heartbeating while its call carries on is
    # either a network that reaches the agent and not us, a bug, or somebody
    # seeing what they can get away with, and the call id is how to tell which.
    logger.warning(
        "voice_call_kept call=%s client_quiet_for=%ds connected=%s",
        call.id,
        (now - as_utc(call.last_seen_at)).total_seconds(),
        "yes"
        if (call.connected_at is not None or "connected_at" in values)
        else "nudged" if "nudged_at" in values else "not yet",
    )


def verdict(call: VoiceCall, now: datetime) -> str | None:
    """What the sweeper should do with one freshly read call: `settle`, `ask`, or nothing."""
    if call.ended_at is not None:
        return None
    if call.answered_at is None:
        return "settle" if now - as_utc(call.last_seen_at) > unanswered_grace() else None
    if now - as_utc(call.answered_at) >= ceiling():
        return "settle"
    if now - proof_of_life(call) > heartbeat_timeout():
        return "ask"
    return None


async def _fresh(session: AsyncSession, call_id: uuid.UUID) -> VoiceCall | None:
    return (
        await session.execute(
            select(VoiceCall)
            .where(VoiceCall.id == call_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def resolve(session: AsyncSession, call: VoiceCall) -> str:
    """Settle or keep one call the sweeper picked. Returns `settled`, `kept` or `skipped`.

    A quiet call is put to the agent first — including one past its ceiling,
    which the agent still holding is what bills *to* the ceiling rather than to
    a last proof of life up to a minute old. And whatever the agent answers, the
    row is read again before it is settled: the probe took seconds, and a
    heartbeat in another worker may have revived the call meanwhile.
    """
    now = utcnow()
    decision = verdict(call, now)
    if decision is None:
        return "skipped"
    gone = False
    quiet = call.answered_at is not None and now - proof_of_life(call) > heartbeat_timeout()
    if quiet:
        # Ends the read first, so the probe holds no pooled connection.
        await session.commit()
        alive = await agent_holds(call)
        if alive:
            await keep(session, call)
            if decision == "ask":
                return "kept"
        gone = alive is False
        fresh = await _fresh(session, call.id)
        if fresh is None or verdict(fresh, utcnow()) is None:
            return "skipped"
        call = fresh
    await settle(
        session,
        call,
        end_reason=(
            SessionEndReason.HEARTBEAT_TIMEOUT
            if call.answered_at is not None
            else SessionEndReason.TIMEOUT
        ),
        gone=gone,
    )
    return "settled"


# --- the sweep ---------------------------------------------------------------


async def sweep(
    session: AsyncSession, *, user_id: uuid.UUID | None = None, limit: int = SWEEP_BATCH
) -> int:
    """Settle every call that is over and nobody has said so. Returns how many.

    Picks live calls nobody has vouched for in the timeout, calls past their
    ceiling, and offers that outlived twice their deadline — our own process
    died mid-offer. Each is re-read before it is decided, as `reconcile_service`
    re-reads before reaping, and each quiet one is put to the agent before it
    is ended. Scoped to one user when `open_call` asks, which is what frees
    their line. Calls the agent kept are not counted: they are not over.
    """
    now = utcnow()
    quiet_before = now - heartbeat_timeout()
    query = select(VoiceCall.id).where(
        VoiceCall.ended_at.is_(None),
        or_(
            and_(
                VoiceCall.answered_at.is_not(None),
                or_(
                    and_(
                        VoiceCall.last_seen_at < quiet_before,
                        or_(
                            VoiceCall.agent_seen_at.is_(None),
                            VoiceCall.agent_seen_at < quiet_before,
                        ),
                    ),
                    VoiceCall.answered_at <= now - ceiling(),
                ),
            ),
            and_(
                VoiceCall.answered_at.is_(None),
                VoiceCall.last_seen_at < now - unanswered_grace(),
            ),
        ),
    )
    if user_id is not None:
        query = query.where(VoiceCall.user_id == user_id)
    call_ids = list(
        (await session.execute(query.order_by(VoiceCall.last_seen_at).limit(limit))).scalars()
    )

    settled = 0
    for call_id in call_ids:
        try:
            call = (
                await session.execute(
                    select(VoiceCall)
                    .where(VoiceCall.id == call_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if call is not None and await resolve(session, call) == "settled":
                settled += 1
        except Exception:  # noqa: BLE001 - one bad row must not strand the rest
            logger.exception("voice_sweep_failed call=%s", call_id)
            await session.rollback()

    return settled + await _sweep_orphans(session, user_id=user_id, limit=limit)


async def _sweep_orphans(
    session: AsyncSession, *, user_id: uuid.UUID | None, limit: int
) -> int:
    """Release voice sessions whose call row was never written.

    `open_call` commits the hold and then the row, in two transactions because
    `session_service.open_oneshot` owns its commit. A process killed between the
    two leaves a live session holding a ceiling of credit with nothing pointing
    at it, and both the generic reaper and this module's call sweep stand off
    it — so without this nothing would ever give it back. Nothing was relayed
    for it, so nothing is owed.
    """
    cutoff = utcnow() - unanswered_grace()
    has_call = select(VoiceCall.id).where(VoiceCall.id == AiSession.id).exists()
    query = select(AiSession.id).where(
        AiSession.service == BillingService.VOICE_AGENT,
        AiSession.kind == AiSessionKind.REALTIME,
        # Ours, and only ours. `voice_agent` is also the service the
        # agent-reported sessions of `docs/INTERNAL_API.md` bill under, and they
        # have no call row by design — so "voice, and no row" alone would free
        # every one of those at zero. What only this module writes is the key:
        # `open_call` passes none, so `open_oneshot` mints this shape, and no
        # caller can supply a key for a voice session to imitate it.
        AiSession.idempotency_key.like(OWN_SESSION_KEY_PATTERN),
        # Equivalent to "not terminal" — `_finish` stamps both together — and
        # the half of the pair an index serves: `ix_ai_sessions_unreleased_holds`
        # is partial on exactly this, so the pass reads the handful of live holds
        # rather than every session ever opened.
        AiSession.hold_released_at.is_(None),
        AiSession.status.not_in(TERMINAL_SESSION_STATUSES),
        AiSession.created_at < cutoff,
        ~has_call,
    )
    if user_id is not None:
        query = query.where(AiSession.user_id == user_id)
    orphan_ids = list((await session.execute(query.limit(limit))).scalars())

    released = 0
    for ai_session_id in orphan_ids:
        try:
            await session_service.abandon_oneshot(
                session,
                ai_session_id=ai_session_id,
                end_reason=SessionEndReason.INTERNAL_ERROR,
                error_code="voice_call_orphaned",
            )
            released += 1
            # WARNING: a hold with no call is a process that died mid-open, and
            # the session id is the thread to pull.
            logger.warning("voice_orphan_released session=%s", ai_session_id)
        except Exception:  # noqa: BLE001 - one bad row must not strand the rest
            logger.exception("voice_orphan_release_failed session=%s", ai_session_id)
            await session.rollback()
    return released


# --- the in-process loop -----------------------------------------------------

_sweeper_task: asyncio.Task[None] | None = None
_sweeper_stop: asyncio.Event | None = None


def start_sweeper() -> None:
    """Start the in-process sweep loop. The lifespan calls it; idempotent.

    Whether or not an agent is configured: sweeping reads and writes the
    database and nothing else, and a deployment that has just switched the
    agent off still has calls whose holds need settling. Without an agent the
    probes answer "unknown" and every call is decided on its heartbeats.

    One per process, and every worker may run its own: two workers settling the
    same call is the race `settle` is already safe against, and the query is a
    range scan over a partial index of live calls.
    """
    global _sweeper_task, _sweeper_stop
    if _sweeper_task is not None or settings.voice_agent_sweep_seconds <= 0:
        return
    _sweeper_stop = asyncio.Event()
    _sweeper_task = asyncio.create_task(_sweep_forever(_sweeper_stop), name="voice-sweeper")


async def stop_sweeper(timeout_seconds: float = 10.0) -> None:
    """Ask the loop to finish its pass, and wait for it — without cancelling.

    A cancelled settlement is a lost charge, and `CancelledError` slips past
    every `except Exception` on the way out, taking the log line that names the
    call with it. So the loop is asked to stop between passes, and a pass still
    running past the bound is left to land; `app/main.py` closes the database
    after this, which is the same "late, not lost" the TTS drain settles for.
    """
    global _sweeper_task, _sweeper_stop
    task, stop = _sweeper_task, _sweeper_stop
    _sweeper_task = _sweeper_stop = None
    if task is None or stop is None:
        return
    stop.set()
    done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    if not done:
        logger.warning("voice_sweeper_still_running after %.0fs", timeout_seconds)


async def _sweep_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                settled = await sweep(session)
            if settled:
                logger.info("voice_swept calls=%d", settled)
        except Exception:  # noqa: BLE001 - the loop must outlive a bad pass
            logger.exception("voice_sweep_pass_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.voice_agent_sweep_seconds)


def _count_live(user_id: uuid.UUID, *, excluding: uuid.UUID | None = None):
    """The statement for "this user's calls not yet settled"."""
    query = select(func.count()).select_from(VoiceCall).where(
        VoiceCall.user_id == user_id, VoiceCall.ended_at.is_(None)
    )
    if excluding is not None:
        query = query.where(VoiceCall.id != excluding)
    return query


async def live_calls(
    session: AsyncSession, user_id: uuid.UUID, *, excluding: uuid.UUID | None = None
) -> int:
    return int((await session.execute(_count_live(user_id, excluding=excluding))).scalar_one())
