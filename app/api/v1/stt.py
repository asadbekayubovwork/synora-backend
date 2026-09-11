"""Speech to text: one metered upload.

The second gateway, and the same trade as `tts.py`. Our credential with the
transcription service never leaves this process, the caller authenticates with
their own JWT, and every second of audio is metered here — so the count that
produced the bill and the count `GET /usage` reports are one count, read from
our own tables.

One route, on purpose. `POST /stt/transcribe` takes the file and answers with
the transcript; there is no estimate endpoint, because pricing this work means
knowing how long the audio is, and knowing that means uploading it. The place
to find out what a transcription will cost is the price book and the duration
of your own file.
"""

from __future__ import annotations

import logging

import asyncio
import contextlib
import uuid

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep, get_user_from_access_token
from app.core.config import settings
from app.core.exceptions import AppError, BadRequestError, NotFoundError
from app.db.session import SessionLocal
from app.schemas.auth import ErrorResponse
from app.schemas.common import Cursor, MessageResponse, PageInfo, clamp_limit, decode_cursor
from app.schemas.stt import (
    TranscriptionPageResponse,
    TranscriptionRecordResponse,
    TranscriptionResponse,
    transcription_record_response,
    transcription_response,
)
from app.services.ai import (
    recording_store,
    stt_client,
    stt_service,
    stt_stream_service,
    stt_transcription_service,
)
from app.services.billing.session_service import MAX_CLIENT_IDEMPOTENCY_KEY

logger = logging.getLogger("synora.stt")

router = APIRouter(prefix="/stt", tags=["STT"])

# How long a socket may stay open having said nothing. A connection that never
# sends `start` costs a file descriptor and holds no credit, so this is tidiness
# rather than protection — but an untidy server accumulates them.
_START_TIMEOUT_SECONDS = 15

ERRORS: dict[int | str, dict] = {
    400: {
        "model": ErrorResponse,
        "description": (
            "The audio or the language was refused. `stt_audio_too_large` and "
            "`stt_audio_too_long` are ours and arrive before any hold; "
            "`stt_rejected_input` is the service's own words about the file"
        ),
    },
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    402: {"model": ErrorResponse, "description": "Not enough credit"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    409: {
        "model": ErrorResponse,
        "description": "`stt_idempotency_spent` — this key has already been charged",
    },
    422: {"model": ErrorResponse, "description": "Validation error"},
    429: {"model": ErrorResponse, "description": "The transcription service is busy"},
    502: {"model": ErrorResponse, "description": "The transcription service was unreachable"},
    503: {"model": ErrorResponse, "description": "Transcription is not available here"},
}


@router.post(
    "/transcribe",
    response_model=TranscriptionResponse,
    responses=ERRORS,
    summary="Transcribe one audio file",
    description=(
        "Upload audio as `multipart/form-data` and get the text back, charged "
        "by the duration the transcription service measured.\n\n"
        "```bash\n"
        "curl -X POST \"$API/stt/transcribe\" \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -F 'file=@clip.wav' -F 'language=uz'\n"
        "```\n\n"
        "**The hold is an estimate; the charge is not.** Credit is reserved "
        "before the upload goes anywhere, and something has to price that "
        "before anything has decoded the file. For `wav` the duration is read "
        "out of the RIFF header and is exact. For compressed formats it is the "
        "byte count over a deliberately low assumed bitrate, so the hold lands "
        "*above* the real cost — you may briefly see a larger `reserved` than "
        "the call finally costs, and the difference comes back at settlement. "
        "The alternative, holding too little, would bill less than the audio "
        "and nobody would notice.\n\n"
        "`audio_ms` in the response is the service's own count, and it is what "
        "the debit was computed from. `price` is the same amount as the "
        "`X-Synora-Price` header, and `X-Synora-Session-Id` threads back to "
        "the hold, the release and the debit in `GET /wallet/transactions`.\n\n"
        "**Nothing is charged for a transcription that did not happen.** Audio "
        "the service cannot decode, a language it does not serve, a model still "
        "warming up — all of them release the hold in full and write no usage "
        "event. The one thing that is billed is a response that arrived.\n\n"
        "Languages are `uz`, `ru` and `en`; anything else is refused here "
        "rather than after a hold has been placed. The ceilings are "
        f"{settings.stt_max_audio_bytes // (1024 * 1024)} MB and "
        f"{settings.stt_max_audio_seconds // 60} minutes — and **send "
        "compressed audio**, because for uncompressed WAV the size ceiling "
        "bites long before the duration one: ten minutes is 18 MB at 16 kHz "
        "mono and 55 MB at 48 kHz. Nothing is lost by compressing, since the "
        "service resamples to 16 kHz before transcribing. A WAV refused on "
        "size is told how long it is and what it would weigh as mp3.\n\n"
        "An `Idempotency-Key` here means *do not charge twice*, and a key that "
        "has already been spent is a `409 stt_idempotency_spent` rather than a "
        "second transcription. It cannot return the first answer: transcripts "
        "are not stored, so there is nothing to hand back."
    ),
)
async def transcribe(
    request: Request,
    response: Response,
    user: CurrentUser,
    session: SessionDep,
    file: UploadFile = File(description="The audio. Any format the service's decoder accepts."),
    language: str = Form(
        default="uz",
        description="One of `uz`, `ru`, `en`.",
        examples=["uz"],
    ),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=MAX_CLIENT_IDEMPOTENCY_KEY,
        description=(
            "Makes a retry safe by refusing it: a spent key answers `409 "
            "stt_idempotency_spent` rather than charging a second time."
        ),
    ),
) -> TranscriptionResponse:
    # Checked before the file is read, so an unconfigured deployment does not
    # pull 25 MB off the socket to refuse it.
    stt_client.require_configured()

    if language not in stt_client.LANGUAGES:
        # Refused here rather than relayed: upstream would answer 400 for it
        # too, but only after a hold had been placed and released again.
        raise BadRequestError(
            f"`{language}` is not one of {', '.join(stt_client.LANGUAGES)}.",
            code="stt_language_unsupported",
        )

    # Starlette spools an upload over `SpooledTemporaryFile`, so this is a read
    # from memory for small clips and from a temp file for large ones — the
    # ceiling that matters is `STT_MAX_AUDIO_BYTES`, enforced in the service
    # before the wallet is touched.
    audio = await file.read()

    client_ip = request.client.host[:64] if request.client else None
    user_agent = request.headers.get("user-agent")

    transcription = await stt_service.transcribe(
        session,
        user,
        audio=audio,
        filename=file.filename or "audio",
        content_type=file.content_type,
        language=language,
        idempotency_key=idempotency_key,
        client_ip=client_ip,
        user_agent=user_agent[:255] if user_agent else None,
    )

    # The same numbers as the body, on headers a browser can read without
    # parsing the JSON — and the reason `EXPOSED_HEADERS` names them in the
    # CORS configuration. See `app/main.py`. The body is the contract; these
    # are for the client that wants the price without deserialising.
    response.headers.update(transcription.headers)
    return transcription_response(transcription)


# --- kept transcriptions ----------------------------------------------------

TranscriptionIdPath = Path(
    description="A transcription id, as `GET /stt/transcriptions` returned it."
)
LimitQuery = Query(
    default=None, ge=1, le=100, description="Rows per page. Defaults to 25, capped at 100."
)
CursorQuery = Query(
    default=None, description="The `page.next_cursor` from the previous response."
)

RECORD_ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "No such transcription"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


@router.get(
    "/transcriptions",
    response_model=TranscriptionPageResponse,
    responses=RECORD_ERRORS,
    summary="Every transcription this account has kept",
    description=(
        "What was transcribed and what came back — newest first, "
        "cursor-paginated exactly as `GET /tts/recordings` is.\n\n"
        "**The audio is a separate request.** "
        "`GET /stt/transcriptions/{id}/audio` returns the file that was "
        "uploaded, byte for byte, and it costs nothing: the transcription was "
        "paid for when it happened.\n\n"
        "A transcription is kept when it was charged for. A call the service "
        "refused — audio it could not decode, a language it does not serve — "
        "charged nothing and is not here; nor is anything transcribed while "
        "`RECORDINGS_ENABLED` was false.\n\n"
        "**These rows do not expire**, and they hold audio the *user* "
        "uploaded. `DELETE /stt/transcriptions/{id}` is how one is erased."
    ),
)
async def transcriptions(
    user: CurrentUser,
    session: SessionDep,
    limit: int | None = LimitQuery,
    cursor: str | None = CursorQuery,
) -> TranscriptionPageResponse:
    page_size = clamp_limit(limit)
    rows, has_more = await stt_transcription_service.page(
        session, user_id=user.id, limit=page_size, position=decode_cursor(cursor)
    )
    next_cursor = (
        Cursor(created_at=rows[-1].created_at, row_id=rows[-1].id).encode()
        if has_more and rows
        else None
    )
    return TranscriptionPageResponse(
        transcriptions=[transcription_record_response(row) for row in rows],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page_size),
    )


@router.get(
    "/transcriptions/{transcription_id}",
    response_model=TranscriptionRecordResponse,
    responses=RECORD_ERRORS,
    summary="One kept transcription",
    description=(
        "Somebody else's id is a `404` rather than a `403`, for the reason it "
        "is everywhere else here: a 403 confirms the id exists."
    ),
)
async def transcription(
    user: CurrentUser,
    session: SessionDep,
    transcription_id: uuid.UUID = TranscriptionIdPath,
) -> TranscriptionRecordResponse:
    return transcription_record_response(
        await stt_transcription_service.require_own(
            session, user_id=user.id, transcription_id=transcription_id
        )
    )


@router.get(
    "/transcriptions/{transcription_id}/audio",
    responses={
        **RECORD_ERRORS,
        200: {
            "content": {"audio/wav": {}, "audio/mpeg": {}, "application/octet-stream": {}},
            "description": "The audio exactly as it was uploaded.",
        },
    },
    summary="The audio of a kept transcription",
    description=(
        "The bytes that were uploaded, verifiable against the `sha256` on the "
        "record — the file is stored under that digest.\n\n"
        "A `404` here with the transcription still listed means the row "
        "survived and the file did not: a restore that missed "
        "`RECORDINGS_DIR`, or a disk that was cleared. The row stays, because "
        "it is still the record that the work happened."
    ),
)
async def transcription_audio(
    user: CurrentUser,
    session: SessionDep,
    transcription_id: uuid.UUID = TranscriptionIdPath,
) -> FileResponse:
    row = await stt_transcription_service.require_own(
        session, user_id=user.id, transcription_id=transcription_id
    )
    path = recording_store.path_for(row.storage_key)
    if path is None or not path.exists():
        logger.warning(
            "transcription_file_missing id=%s key=%s", row.id, row.storage_key
        )
        raise NotFoundError(
            "The audio for this transcription is no longer on disk.",
            code="transcription_audio_missing",
        )
    return FileResponse(
        path,
        # What the client said it was sending. Unverified — nothing on our side
        # decoded the file — which is why it falls back to a type that promises
        # nothing rather than to a guess.
        media_type=row.content_type or "application/octet-stream",
        filename=row.filename or f"synora-{row.id}",
    )


@router.delete(
    "/transcriptions/{transcription_id}",
    response_model=MessageResponse,
    responses=RECORD_ERRORS,
    summary="Erase one kept transcription",
    description=(
        "Removes the row and, once nothing else names the same audio, the "
        "file.\n\n"
        "**The file can be shared with a synthesis.** Audio is stored under "
        "the sha256 of its own bytes, so transcribing something this account "
        "synthesised is one file with a row in each table. Deleting here never "
        "empties the recording's playback, and vice versa.\n\n"
        "Nothing about the charge changes: the ledger and `GET /usage` are the "
        "record of what was billed, and this route does not touch them."
    ),
)
async def delete_transcription(
    user: CurrentUser,
    session: SessionDep,
    transcription_id: uuid.UUID = TranscriptionIdPath,
) -> MessageResponse:
    await stt_transcription_service.delete(
        session, user_id=user.id, transcription_id=transcription_id
    )
    return MessageResponse(message="Transcription deleted.")


# --- realtime ----------------------------------------------------------------


class _Socket:
    """`stt_stream_service.ClientSocket`, over a FastAPI websocket.

    An adapter rather than passing the `WebSocket` straight through, so the
    service can be driven by a fake: what is under test there is a wallet, and
    it should not need a socket to check it.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> dict:
        return await self._websocket.receive()

    async def send_json(self, message: dict) -> None:
        await self._websocket.send_json(message)


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """Realtime transcription over a websocket. Not in the OpenAPI document.

    OpenAPI cannot describe a websocket, which is why the protocol lives in
    `docs/STT.md` instead of in a `responses=` block.

        → {"type":"start","token":"<access token>","language":"uz","sample_rate":16000}
        → binary frames: little-endian PCM16, mono, at the declared rate
        → {"type":"stop"}

        ← {"type":"ready","ai_session_id":"…","max_seconds":600}
        ← {"type":"speech_started"}                      the barge-in trigger
        ← {"type":"final","seq":0,"text":"…","audio_ms":2100}
        ← {"type":"done","segments":3,"audio_ms":5040,"price":"1.400000", …}
        ← {"type":"error","code":"…","message":"…"}

    **The token goes in the `start` message, not in the query string.** A
    browser cannot set headers on a `WebSocket`, so something has to carry it,
    and a URL is the one place a credential must not go: query strings land in
    nginx access logs, in `Referer` headers, and in any error report the page
    files. A non-browser client may use `Authorization: Bearer` instead, which
    is checked first.

    **Wait for `done`, not for the first `final`.** One spoken turn routinely
    closes several VAD segments, so a client that closes on the first one
    truncates its own transcript — and `done` is also where the bill is.
    """
    await websocket.accept()
    client_ip = websocket.client.host[:64] if websocket.client else None
    user_agent = websocket.headers.get("user-agent")

    try:
        start = await asyncio.wait_for(
            websocket.receive_json(), timeout=_START_TIMEOUT_SECONDS
        )
    except (TimeoutError, ValueError, KeyError):
        # Nothing was opened and nothing held, so there is no session to close
        # — just a socket that never said what it wanted.
        await _refuse(websocket, "stt_stream_start_missing", "Send a `start` message first.")
        return

    if not isinstance(start, dict) or start.get("type") != "start":
        await _refuse(websocket, "stt_stream_start_missing", "The first message must be `start`.")
        return

    header = websocket.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    token = credential if scheme.lower() == "bearer" and credential else start.get("token")
    if not token:
        await _refuse(websocket, "not_authenticated", "Authentication is required.")
        return

    try:
        async with SessionLocal() as session:
            user = await get_user_from_access_token(session, str(token))
        language, sample_rate = stt_stream_service.validate_start(start)
    except AppError as error:
        await _refuse(websocket, error.code, str(error.detail))
        return

    outcome = None
    try:
        outcome = await stt_stream_service.run(
            _Socket(websocket),
            user,
            language=language,
            sample_rate=sample_rate,
            client_ip=client_ip,
            user_agent=user_agent[:255] if user_agent else None,
        )
    except AppError as error:
        # Upstream refused before a word was transcribed. The hold is already
        # back — `stt_stream_service.run` sees to that — so this is only the
        # telling.
        await _refuse(websocket, error.code, str(error.detail))
        return
    except WebSocketDisconnect:
        # The client went away mid-session. It is already settled; there is
        # nobody left to send `done` to.
        return
    finally:
        if outcome is not None:
            with contextlib.suppress(Exception):
                await websocket.send_json(stt_stream_service.done_message(outcome))
            with contextlib.suppress(Exception):
                await websocket.close()


async def _refuse(websocket: WebSocket, code: str, message: str) -> None:
    """Say why, then close. The event first, because a close code is four digits.

    `1008` is "policy violation", which is the nearest thing the websocket
    vocabulary has to "your request was refused" — and it says nothing about
    *which* refusal, so the JSON goes first and carries the same `code` every
    HTTP route on this API would have returned.
    """
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "error", "code": code, "message": message})
    with contextlib.suppress(Exception):
        await websocket.close(code=1008, reason=code[:120])
