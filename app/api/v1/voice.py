"""Voice calls with the agent: signalling through us, audio straight to it.

The browser negotiates a WebRTC call with the voice agent, and these routes are
the negotiation. The audio itself never touches this server — it flows between
the browser and the agent over UDP — so what is here is small: hand the
browser's offer on with our key attached, relay its network candidates, and
hear from it every fifteen seconds that the call is still up, because that is
the only way this side ever learns how long a call lasted.

    GET    /voice/config                      ICE servers, heartbeat cadence, prices
    POST   /voice/sessions                    offer in, answer out — the call starts
    POST   /voice/sessions/{id}/candidates    trickle ICE, batched
    POST   /voice/sessions/{id}/heartbeat     every 15 s: continue, warn or stop
    DELETE /voice/sessions/{id}               hang up — billed, hold released
    GET    /voice/sessions[/{id}]             call history

The agent's own guide lists three client mistakes that break a call silently.
Two are the client's alone — creating the data channel, pinging it every
second — and `docs/VOICE_AGENT.md` has a client that does both. The third, the
video transceiver an audio call still has to negotiate, is refused at `POST`
with a `400 voice_offer_no_video` that says how to fix it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Path, Query, Request, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.schemas.auth import ErrorResponse
from app.schemas.common import Cursor, PageInfo, clamp_limit, decode_cursor
from app.schemas.voice import (
    VoiceCallPageResponse,
    VoiceCallResponse,
    VoiceCandidatesRequest,
    VoiceCandidatesResponse,
    VoiceConfigResponse,
    VoiceHeartbeatResponse,
    VoiceOfferRequest,
    VoiceSessionResponse,
    voice_call_response,
    voice_config_response,
    voice_heartbeat_response,
    voice_session_response,
)
from app.services.ai import voice_agent_service

router = APIRouter(prefix="/voice", tags=["Voice agent"])

SessionIdPath = Path(description="The `ai_session_id` `POST /voice/sessions` answered with.")

AUTH_ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
}
CALL_ERRORS: dict[int | str, dict] = {
    **AUTH_ERRORS,
    404: {"model": ErrorResponse, "description": "`voice_call_not_found` — not a call of yours"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


@router.get(
    "/config",
    response_model=VoiceConfigResponse,
    responses=AUTH_ERRORS,
    summary="What a client needs before it builds a peer connection",
    description=(
        "ICE servers for `new RTCPeerConnection({iceServers})`, the heartbeat "
        "cadence, the ceiling on one call, and what a call costs.\n\n"
        "Answers `available: false` rather than a 503 on a deployment with no "
        "agent, so a client can decide whether to draw the call button at all.\n\n"
        "`hold` is the least an account needs to start a call: the price of one "
        "that runs to the ceiling, reserved when it opens, the unspent part "
        "coming back when it ends. Check it against `GET /wallet` before the "
        "user presses the button, rather than showing them a 402 after."
    ),
)
async def config(user: CurrentUser, session: SessionDep) -> VoiceConfigResponse:
    return voice_config_response(await voice_agent_service.config(session, user.id))


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=VoiceSessionResponse,
    responses={
        **AUTH_ERRORS,
        400: {
            "model": ErrorResponse,
            "description": (
                "The offer cannot work. `voice_offer_no_video` — no video "
                "transceiver, the mistake that otherwise connects a call with no "
                "audio and no error; `voice_offer_no_audio`; `voice_offer_invalid` "
                "— not SDP at all; or `voice_offer_rejected`, the agent's own "
                "words about it."
            ),
        },
        402: {"model": ErrorResponse, "description": "Less credit than the hold — see `GET /voice/config`"},
        422: {
            "model": ErrorResponse,
            "description": "`validation_error` — an `sdp` over 64 KB, or a `type` other than `offer`",
        },
        429: {
            "model": ErrorResponse,
            "description": (
                "`voice_call_limit` — a call is already up on this account; "
                "`voice_call_still_connected` — an ended call's connection to "
                "the agent is still open, so the tab or app holding it has to "
                "close it; "
                "`voice_call_rate_limited` — too many started this minute; "
                "`voice_agent_busy` — every line on the agent is taken."
            ),
        },
        502: {"model": ErrorResponse, "description": "The agent was unreachable or answered nonsense"},
        503: {
            "model": ErrorResponse,
            "description": (
                "`voice_agent_not_configured`, `voice_agent_key_rejected`, or "
                "`voice_agent_unavailable` — warming up, with `Retry-After`."
            ),
        },
    },
    summary="Start a call: the browser's offer in, the agent's answer out",
    description=(
        "Send `pc.localDescription` after `setLocalDescription(await "
        "pc.createOffer())`, and pass the answer to `setRemoteDescription`.\n\n"
        "```json\n"
        '{"sdp": "v=0\\r\\no=- 4611… ", "type": "offer"}\n'
        "```\n\n"
        "**The offer must negotiate video**, even though the call is voice only: "
        '`pc.addTransceiver("video", {direction: "sendrecv"})` before '
        "`createOffer()`. The camera is never opened. Without it the agent "
        "connects the call and no audio ever flows, with no error anywhere, so "
        "it is refused here instead.\n\n"
        "**Create the data channel yourself** — `pc.createDataChannel(\"events\")` "
        "— and send `ping` on it every second once it opens. The agent never "
        "creates one, sends transcript and state events only on yours, and drops "
        "a call whose channel goes quiet for three seconds.\n\n"
        "**Credit is held for the ceiling** when the call opens and the "
        "difference comes back when it ends. A call the agent refuses charges "
        "nothing and holds nothing; so does one that never connects.\n\n"
        "The first call after the agent restarts can take up to thirty seconds "
        "to answer while it warms up. Show a connecting state, not a spinner that "
        "looks frozen."
    ),
)
async def open_session(
    request: Request, body: VoiceOfferRequest, user: CurrentUser, session: SessionDep
) -> VoiceSessionResponse:
    user_agent = request.headers.get("user-agent")
    opened = await voice_agent_service.open_call(
        session,
        user,
        sdp=body.sdp,
        sdp_type=body.type,
        client_ip=request.client.host[:64] if request.client else None,
        user_agent=user_agent[:255] if user_agent else None,
    )
    return voice_session_response(opened)


@router.post(
    "/sessions/{session_id}/candidates",
    response_model=VoiceCandidatesResponse,
    responses={
        **CALL_ERRORS,
        400: {
            "model": ErrorResponse,
            "description": "`voice_candidates_exhausted`, or `voice_candidate_rejected` from the agent",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`voice_call_ended`, or `voice_call_gone` — the agent has already "
                "torn the call down, and it has been ended here too."
            ),
        },
        502: {"model": ErrorResponse, "description": "The agent was unreachable"},
        503: {"model": ErrorResponse, "description": "Voice calls are not available here"},
    },
    summary="Relay the browser's ICE candidates to the agent",
    description=(
        "Trickle ICE. Candidates the browser gathers before `POST "
        "/voice/sessions` has answered have nowhere to go yet — queue them, and "
        "post the queue in one request once you have the session id. Batch "
        "later ones the same way: every candidate since the last post, as one "
        "list.\n\n"
        "`event.candidate.toJSON()` can be posted as it stands: `sdpMid` and "
        "`sdpMLineIndex` are accepted beside `sdp_mid` and `sdp_mline_index`, "
        "and the end-of-candidates marker (an empty `candidate`) is skipped.\n\n"
        "A call relays at most 64 candidates. A batch that would cross that is "
        "cut to what fits rather than refused, and `relayed` says how many went."
    ),
)
async def add_candidates(
    body: VoiceCandidatesRequest,
    user: CurrentUser,
    session: SessionDep,
    session_id: uuid.UUID = SessionIdPath,
) -> VoiceCandidatesResponse:
    relayed = await voice_agent_service.relay_candidates(
        session, user, session_id, [candidate.as_payload() for candidate in body.candidates]
    )
    return VoiceCandidatesResponse(relayed=relayed)


@router.post(
    "/sessions/{session_id}/heartbeat",
    response_model=VoiceHeartbeatResponse,
    responses={
        **CALL_ERRORS,
        409: {"model": ErrorResponse, "description": "`voice_call_not_answered`"},
    },
    summary="Say the call is still up. Every 15 seconds, from connected to hang-up",
    description=(
        "Start when `pc.connectionState` reaches `connected` and keep going "
        f"every `{settings.voice_agent_heartbeat_seconds}` seconds. The first "
        "one is what makes a call billable: a call that never sends one never "
        "connected, and is ended at no charge — unless the agent is still "
        "holding it two minutes after its answer and ninety seconds after a "
        "nudge that fails any peer still stuck in ICE, which only a connected "
        "call survives.\n\n"
        "**This is how this server learns how long a call lasted**, because the "
        "audio never passes through it. Go quiet for "
        f"`{settings.voice_agent_heartbeat_timeout_seconds}` seconds and the "
        "agent is asked whether it still holds the call: if it does, billing "
        "carries on; if not, the call is over — billed to the last heartbeat plus "
        "one interval and "
        "flagged `disputed`, because that end is inferred rather than "
        "reported.\n\n"
        "Act on `action`: `continue`; `warn` inside the last minute before the "
        "ceiling, time to tell the user; `stop` — the call has been ended and "
        "billed, so close the peer connection. A heartbeat on a call that has "
        "already ended answers `stop` as well, not an error."
    ),
)
async def heartbeat(
    user: CurrentUser, session: SessionDep, session_id: uuid.UUID = SessionIdPath
) -> VoiceHeartbeatResponse:
    return voice_heartbeat_response(
        await voice_agent_service.heartbeat(session, user, session_id)
    )


@router.delete(
    "/sessions/{session_id}",
    response_model=VoiceCallResponse,
    responses=CALL_ERRORS,
    summary="Hang up: the call is billed and its hold released",
    description=(
        "Call it from the Stop button *and* from `pagehide` — the second with "
        "`fetch(url, {method: \"DELETE\", keepalive: true, headers})`, which "
        "survives the page closing. Ending an ended call is not an error: it "
        "answers with the first ending's bill.\n\n"
        "Closing the peer connection is still the client's job. This route ends "
        "the *billing*; the agent tears its side down when the browser's "
        "connection closes, and nothing here can do that for it.\n\n"
        "`billed_ms` runs from the call's first heartbeat to now, capped at the "
        "ceiling. A call that never connected is ended with `price` zero."
    ),
)
async def end_session(
    user: CurrentUser, session: SessionDep, session_id: uuid.UUID = SessionIdPath
) -> VoiceCallResponse:
    return voice_call_response(await voice_agent_service.end_call(session, user, session_id))


@router.get(
    "/sessions",
    response_model=VoiceCallPageResponse,
    responses={**AUTH_ERRORS, 422: {"model": ErrorResponse, "description": "Validation error"}},
    summary="Every call this account has placed, newest first",
    description="Cursor-paginated exactly as `GET /stt/transcriptions` is.",
)
async def list_sessions(
    user: CurrentUser,
    session: SessionDep,
    limit: int | None = Query(default=None, ge=1, le=100, description="Rows per page. Defaults to 25."),
    cursor: str | None = Query(default=None, description="`page.next_cursor` from the previous page."),
) -> VoiceCallPageResponse:
    page_size = clamp_limit(limit)
    records, has_more = await voice_agent_service.page(
        session, user_id=user.id, limit=page_size, position=decode_cursor(cursor)
    )
    next_cursor = (
        Cursor(created_at=records[-1].created_at, row_id=records[-1].ai_session_id).encode()
        if has_more and records
        else None
    )
    return VoiceCallPageResponse(
        calls=[voice_call_response(record) for record in records],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page_size),
    )


@router.get(
    "/sessions/{session_id}",
    response_model=VoiceCallResponse,
    responses=CALL_ERRORS,
    summary="One call",
    description="Somebody else's id is a `404`, never a `403`: a 403 confirms the id exists.",
)
async def get_session(
    user: CurrentUser, session: SessionDep, session_id: uuid.UUID = SessionIdPath
) -> VoiceCallResponse:
    return voice_call_response(await voice_agent_service.get_call(session, user.id, session_id))
