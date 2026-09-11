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

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.schemas.auth import ErrorResponse
from app.schemas.stt import TranscriptionResponse, transcription_response
from app.services.ai import stt_client, stt_service
from app.services.billing.session_service import MAX_CLIENT_IDEMPOTENCY_KEY

logger = logging.getLogger("synora.stt")

router = APIRouter(prefix="/stt", tags=["STT"])

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
        f"{settings.stt_max_audio_seconds // 60} minutes.\n\n"
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
