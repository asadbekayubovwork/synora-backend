"""Request and response shapes for voice-agent calls.

The SDP strings are opaque here as everywhere: a client passes
`pc.localDescription.sdp` in and hands the answer's `sdp` to
`setRemoteDescription` unchanged. The only structure this layer checks is size;
what the offer must *contain* is `voice_agent_service.validate_offer`'s, because
its refusals are the ones worth a sentence of explanation each.

ICE candidates accept both spellings — the agent's `sdp_mid` and the browser's
`sdpMid` — so a client can post `event.candidate.toJSON()` as it stands rather
than renaming three fields per candidate. Extra fields the browser adds, such as
`usernameFragment`, are ignored.

The money convention is `app/schemas/wallet.py`'s: an integer of micro-credits
for arithmetic and a fixed-point string for display.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field, field_validator

from app.core.config import settings
from app.core.money import format_credits
from app.models.billing_enums import SessionAction, SessionEndReason
from app.schemas.common import PageInfo, _Schema, ensure_utc
from app.services.ai.voice_agent_service import (
    MAX_CANDIDATE_CHARACTERS,
    MAX_CANDIDATES_PER_REQUEST,
    MAX_SDP_CHARACTERS,
    CallRecord,
    OpenedCall,
    Pulse,
    VoiceConfig,
)


def _credits(micros: int | None) -> str | None:
    return None if micros is None else format_credits(micros)


# --- requests ----------------------------------------------------------------


class VoiceOfferRequest(_Schema):
    """`pc.localDescription`, as the browser produced it."""

    sdp: str = Field(
        min_length=1,
        max_length=MAX_SDP_CHARACTERS,
        description="The SDP offer, unchanged. Must negotiate audio *and* video — see the route.",
    )
    type: Literal["offer"] = Field(default="offer", description="Always `offer`.")


class IceCandidateIn(_Schema):
    """One trickle-ICE candidate. `RTCIceCandidate.toJSON()` fits as it stands."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    candidate: str = Field(
        max_length=MAX_CANDIDATE_CHARACTERS,
        description="The `candidate:` line. Empty is the end-of-candidates marker and is skipped.",
        examples=["candidate:842163049 1 udp 1677729535 203.0.113.7 51234 typ srflx"],
    )
    sdp_mid: str | None = Field(
        default=None,
        max_length=64,
        validation_alias=AliasChoices("sdp_mid", "sdpMid"),
        examples=["0"],
    )
    sdp_mline_index: int | None = Field(
        default=None,
        ge=0,
        le=64,
        validation_alias=AliasChoices("sdp_mline_index", "sdpMLineIndex"),
        examples=[0],
    )

    @field_validator("candidate")
    @classmethod
    def _one_line(cls, value: str) -> str:
        # A candidate is one SDP attribute line. A line break inside it is the
        # one way to smuggle a second attribute into the agent's SDP parser.
        if "\r" in value or "\n" in value:
            raise ValueError("a candidate is a single line")
        return value

    def as_payload(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "sdp_mid": self.sdp_mid,
            "sdp_mline_index": self.sdp_mline_index,
        }


class VoiceCandidatesRequest(_Schema):
    candidates: list[IceCandidateIn] = Field(
        min_length=1,
        max_length=MAX_CANDIDATES_PER_REQUEST,
        description="Batch them: every candidate gathered since the last post, in one request.",
    )


# --- responses ---------------------------------------------------------------


class VoiceConfigResponse(_Schema):
    """What a client reads before it builds a peer connection."""

    ok: bool = True
    available: bool = Field(
        description="Whether this deployment can place calls at all. `false` means hide the button.",
    )
    ice_servers: list[dict[str, Any]] = Field(
        description="Pass as `new RTCPeerConnection({iceServers})`, unchanged.",
        examples=[[{"urls": ["stun:stun.l.google.com:19302"]}]],
    )
    heartbeat_interval_seconds: int = Field(
        description="How often to `POST …/heartbeat` once the call is connected.",
        examples=[15],
    )
    heartbeat_timeout_seconds: int = Field(
        description=(
            "Silence past this puts the call to the agent: still held, it is billed "
            "on; gone, it ends, billed to the last heartbeat plus one interval."
        ),
        examples=[45],
    )
    max_session_seconds: int = Field(description="The ceiling on one call.", examples=[600])
    max_concurrent_calls: int = Field(description="Calls one account may have up at once.", examples=[1])
    per_minute_micros: int | None = Field(
        default=None,
        description="What a minute of conversation costs. Null when nothing is priced.",
        examples=[500000],
    )
    per_minute: str | None = Field(default=None, examples=["0.500000"])
    hold_micros: int | None = Field(
        default=None,
        description=(
            "Credit reserved when a call opens — the price of a call that runs to "
            "the ceiling — and the least an account needs to start one. The "
            "unspent part comes back when the call ends."
        ),
        examples=[5000000],
    )
    hold: str | None = Field(default=None, examples=["5.000000"])


def voice_config_response(value: VoiceConfig) -> VoiceConfigResponse:
    return VoiceConfigResponse(
        available=value.available,
        ice_servers=value.ice_servers,
        heartbeat_interval_seconds=settings.voice_agent_heartbeat_seconds,
        heartbeat_timeout_seconds=settings.voice_agent_heartbeat_timeout_seconds,
        max_session_seconds=settings.voice_agent_max_session_seconds,
        max_concurrent_calls=settings.voice_agent_max_concurrent_per_user,
        per_minute_micros=value.per_minute_micros,
        per_minute=_credits(value.per_minute_micros),
        hold_micros=value.hold_micros,
        hold=_credits(value.hold_micros),
    )


class VoiceSessionResponse(_Schema):
    """The agent's answer, and what the client owes the call from here on."""

    ok: bool = True
    ai_session_id: uuid.UUID = Field(
        description=(
            "The call. Every later route takes it. In `GET /wallet/transactions` "
            "the hold and the release carry it as `ai_session_id`; the debit "
            "points at the usage event the call settled into instead."
        ),
    )
    sdp: str = Field(description="Hand to `setRemoteDescription` unchanged.")
    type: Literal["answer"] = "answer"
    answered_at: datetime = Field(
        description="When the agent answered. The ceiling runs from here; the bill from the first heartbeat.",
    )
    expires_at: datetime = Field(description="The ceiling: the call is ended and billed no later than this.")
    heartbeat_interval_seconds: int = Field(examples=[15])
    heartbeat_timeout_seconds: int = Field(examples=[45])
    reserved_micros: int = Field(description="Held for this call until it ends.", examples=[5000000])
    reserved: str = Field(examples=["5.000000"])


def voice_session_response(opened: OpenedCall) -> VoiceSessionResponse:
    return VoiceSessionResponse(
        ai_session_id=opened.ai_session_id,
        sdp=opened.sdp,
        answered_at=ensure_utc(opened.answered_at),
        expires_at=ensure_utc(opened.answered_at)
        + timedelta(seconds=settings.voice_agent_max_session_seconds),
        heartbeat_interval_seconds=settings.voice_agent_heartbeat_seconds,
        heartbeat_timeout_seconds=settings.voice_agent_heartbeat_timeout_seconds,
        reserved_micros=opened.reserved_micros,
        reserved=format_credits(opened.reserved_micros),
    )


class VoiceCandidatesResponse(_Schema):
    ok: bool = True
    relayed: int = Field(description="Candidates passed to the agent. Empty ones are skipped.", examples=[3])


class VoiceHeartbeatResponse(_Schema):
    """`continue`, `warn` a minute before the ceiling, or `stop` — and then hang up."""

    ok: bool = True
    action: SessionAction = Field(
        description=(
            "`continue`; `warn` inside the last minute before the ceiling; `stop` "
            "once the call has been ended and billed — close the peer connection."
        ),
    )
    elapsed_ms: int = Field(description="Since the answer. On `stop`, what was billed.", examples=[73000])
    remaining_ms: int = Field(description="Until the ceiling.", examples=[527000])
    next_heartbeat_seconds: int = Field(examples=[15])


def voice_heartbeat_response(pulse: Pulse) -> VoiceHeartbeatResponse:
    return VoiceHeartbeatResponse(
        action=pulse.action,
        elapsed_ms=pulse.elapsed_ms,
        remaining_ms=pulse.remaining_ms,
        next_heartbeat_seconds=settings.voice_agent_heartbeat_seconds,
    )


class VoiceCallResponse(_Schema):
    """One call: when it ran, how it ended, what it cost."""

    ok: bool = True
    ai_session_id: uuid.UUID
    status: Literal["live", "ended"]
    created_at: datetime
    answered_at: datetime | None = None
    connected_at: datetime | None = Field(
        default=None,
        description=(
            "The first heartbeat — or the answer, for a call that never heartbeated "
            "but that the agent held long past a nudge, which only a connected call "
            "survives. Null on a call that never connected, which costs nothing."
        ),
    )
    ended_at: datetime | None = None
    end_reason: SessionEndReason | None = None
    billed_ms: int = Field(description="Duration charged for. Zero while live.", examples=[73000])
    price_micros: int = Field(examples=[1000000])
    price: str = Field(examples=["1.000000"])
    reserved_micros: int = Field(description="Still held. Zero once the call has ended.", examples=[0])
    heartbeats: int = Field(examples=[5])
    disputed: bool = Field(
        description=(
            "Billed on an inference rather than a hang-up — the heartbeats "
            "stopped, or the agent had to vouch for the call — and so the first "
            "place to look when a charge is questioned."
        ),
    )


def voice_call_response(record: CallRecord) -> VoiceCallResponse:
    return VoiceCallResponse(
        ai_session_id=record.ai_session_id,
        status="live" if record.live else "ended",
        created_at=ensure_utc(record.created_at),
        answered_at=ensure_utc(record.answered_at),
        connected_at=ensure_utc(record.connected_at),
        ended_at=ensure_utc(record.ended_at),
        end_reason=record.end_reason,
        billed_ms=record.billed_ms,
        price_micros=record.price_micros,
        price=format_credits(record.price_micros),
        reserved_micros=record.reserved_micros,
        heartbeats=record.heartbeats,
        disputed=record.disputed,
    )


class VoiceCallPageResponse(_Schema):
    ok: bool = True
    calls: list[VoiceCallResponse]
    page: PageInfo
