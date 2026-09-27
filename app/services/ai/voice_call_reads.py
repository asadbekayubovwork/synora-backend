"""Reading voice calls: one call, a page of them, and what a client needs first.

Split from `voice_agent_service`, which opens calls and answers their requests,
because none of this touches the agent or the wallet: it reads rows, and —
for `config` — mints the one per-user credential a call needs before it starts.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, NotFoundError, ServiceUnavailableError
from app.models.ai_session import AiSession
from app.models.billing_enums import BillingService, SessionEndReason, UsageMetric
from app.models.voice_call import VoiceCall
from app.schemas.common import Cursor
from app.services.billing import pricing

logger = logging.getLogger("synora.voice")


def ceiling_quantities() -> dict[UsageMetric, int]:
    """What the hold is priced from: a call that runs to the ceiling."""
    return {UsageMetric.SESSION_MS: settings.voice_agent_max_session_seconds * 1000}


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One call as a client sees it: the row, and the money off its session."""

    ai_session_id: uuid.UUID
    live: bool
    created_at: datetime
    answered_at: datetime | None
    connected_at: datetime | None
    ended_at: datetime | None
    end_reason: SessionEndReason | None
    billed_ms: int
    price_micros: int
    reserved_micros: int
    heartbeats: int
    disputed: bool


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Everything a client needs before it builds its peer connection."""

    available: bool
    ice_servers: list[dict[str, Any]]
    per_minute_micros: int | None
    hold_micros: int | None


def _turn_server(user_id: uuid.UUID) -> dict[str, Any] | None:
    """A TURN entry with a credential minted for this user, or None.

    The TURN REST scheme coturn serves as `use-auth-secret`: the username is an
    expiry and the user's id, the password `base64(hmac_sha1(secret, username))`.
    The TURN server checks the HMAC and the expiry and needs no database, and a
    credential copied out of a browser stops working when the call could no
    longer be running anyway.
    """
    if not settings.has_voice_turn:
        return None
    ttl = settings.voice_agent_turn_ttl_seconds or settings.voice_agent_max_session_seconds + 600
    username = f"{int(time.time()) + ttl}:{user_id}"
    digest = hmac.new(
        settings.voice_agent_turn_secret.strip().encode(), username.encode(), hashlib.sha1
    ).digest()
    return {
        "urls": settings.voice_agent_turn_url_list,
        "username": username,
        "credential": base64.b64encode(digest).decode(),
    }


def _record(call: VoiceCall, row: AiSession) -> CallRecord:
    return CallRecord(
        ai_session_id=call.id,
        live=call.ended_at is None,
        created_at=call.created_at,
        answered_at=call.answered_at,
        connected_at=call.connected_at,
        ended_at=call.ended_at,
        end_reason=row.end_reason,
        # The settled quantity, off the session, rather than a copy on the call
        # row: two settlers racing can compute different durations, and the one
        # that was charged is the one `session_service` kept.
        billed_ms=row.cum_session_ms,
        price_micros=row.settled_micros,
        reserved_micros=row.reserved_micros,
        heartbeats=call.heartbeats,
        disputed=row.disputed,
    )


async def get_call(session: AsyncSession, user_id: uuid.UUID, call_id: uuid.UUID) -> CallRecord:
    found = (
        await session.execute(
            select(VoiceCall, AiSession)
            .join(AiSession, AiSession.id == VoiceCall.id)
            .where(VoiceCall.id == call_id, VoiceCall.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if found is None:
        raise NotFoundError("No such call.", code="voice_call_not_found")
    return _record(*found)


async def page(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int, position: Cursor | None
) -> tuple[list[CallRecord], bool]:
    """One keyset page of calls, newest first, tied on `(created_at, id)`."""
    query = (
        select(VoiceCall, AiSession)
        .join(AiSession, AiSession.id == VoiceCall.id)
        .where(VoiceCall.user_id == user_id)
    )
    if position is not None:
        query = query.where(
            tuple_(VoiceCall.created_at, VoiceCall.id)
            < tuple_(position.created_at, position.row_id)
        )
    rows = (
        await session.execute(
            query.order_by(VoiceCall.created_at.desc(), VoiceCall.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    return [_record(call, row) for call, row in rows[:limit]], has_more


async def config(session: AsyncSession, user_id: uuid.UUID) -> VoiceConfig:
    """What a client needs before `new RTCPeerConnection()`, and what a call costs.

    Answers on a deployment with no agent too — `available: false` — so a
    client can decide whether to draw the call button at all without treating
    a 503 as the normal case, and hands out no TURN credential there. The two
    prices come from one read of the price book, and are absent rather than an
    error when there is none: an unpriced deployment cannot hold for a call,
    which `POST` will say.
    """
    try:
        ice_servers = list(settings.voice_agent_ice_server_list)
    except ValueError as error:
        # Refused at boot outside development; here it is a laptop's `.env`,
        # and a clean 503 naming the variable beats a 500 naming a JSON parser.
        logger.error("voice_agent_ice_servers_invalid: %s", error)
        raise ServiceUnavailableError(
            "Voice calls are misconfigured on this server (VOICE_AGENT_ICE_SERVERS).",
            code="voice_agent_misconfigured",
        ) from None
    per_minute = hold = None
    if settings.has_voice_agent:
        if turn := _turn_server(user_id):
            ice_servers.append(turn)
        with contextlib.suppress(AppError):
            book = await pricing.active_price_book(session)
            prices = await pricing.prices_for(
                session,
                price_book_version_id=book.id,
                service=BillingService.VOICE_AGENT,
                model_key=settings.voice_agent_model_key,
            )
            per_minute = pricing.price_cumulative(
                {UsageMetric.SESSION_MS: 60_000}, prices, price_book_version_id=book.id
            ).price_micros
            hold = pricing.price_cumulative(
                ceiling_quantities(), prices, price_book_version_id=book.id
            ).price_micros
    return VoiceConfig(
        available=settings.has_voice_agent,
        ice_servers=ice_servers,
        per_minute_micros=per_minute,
        hold_micros=hold,
    )
