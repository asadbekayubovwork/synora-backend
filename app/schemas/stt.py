"""Response shapes for transcription.

There is no request model here, and that is not an oversight: the request is
`multipart/form-data` with a file part, which FastAPI builds from `UploadFile`
and `Form` parameters on the route rather than from a Pydantic model. The
validation that would have lived in a model — the language set, the size
ceiling — lives in `app/api/v1/stt.py` and `stt_service` instead, and the
language set is restated from `stt_client.LANGUAGES` so one list governs both
what we advertise and what we send.

The money convention is `app/schemas/wallet.py`'s, as everywhere: an integer of
micro-credits for arithmetic and a fixed-point string for display, because the
client is JavaScript. And as on the speech page, `cost_micros` — what the GPU
time costs us — is deliberately absent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.schemas.common import PageInfo, _Schema


class TranscriptionResponse(_Schema):
    """One charged transcription.

    `audio_ms` is what the bill was computed from, and it is the transcription
    service's own count rather than ours: it decoded the file and we did not.
    The hold that was placed before the work may have been larger — see
    `stt_service` — and the difference went back at settlement.
    """

    ok: bool = True
    ai_session_id: uuid.UUID = Field(
        description=(
            "The metered session behind this call. The hold, the release and "
            "the debit all carry it in `GET /wallet/transactions`."
        ),
    )
    text: str = Field(
        description="The transcript. Empty when the audio contained no speech.",
        examples=["Assalomu alaykum, bugun havo juda yaxshi."],
    )
    language: str = Field(
        description="The language the service transcribed in.", examples=["uz"]
    )
    audio_ms: int = Field(
        description="Duration billed for, in milliseconds, as the service counted it.",
        examples=[1280],
    )
    price_micros: int = Field(description="What it cost, in micro-credits.", examples=[1200000])
    price: str = Field(description="The same amount as a fixed-point string.", examples=["1.200000"])
    infer_seconds: float | None = Field(
        default=None,
        description=(
            "How long the model spent on it. Reported for observability and "
            "never billed — a slow card is our problem, not the caller's."
        ),
        examples=[0.33],
    )


def transcription_response(transcription) -> TranscriptionResponse:
    """One `stt_service.Transcription`, field by field."""
    from app.core.money import format_credits

    return TranscriptionResponse(
        ai_session_id=transcription.ai_session_id,
        text=transcription.text,
        language=transcription.language,
        audio_ms=transcription.audio_ms,
        price_micros=transcription.price_micros,
        price=format_credits(transcription.price_micros),
        infer_seconds=transcription.infer_seconds,
    )


class TranscriptionRecordResponse(_Schema):
    """One kept transcription: the upload, and the text it produced.

    `text` is the transcript and `filename` is what the client called the file
    it sent. The audio itself is a second request — `/audio` — for the reason
    the speech side's recordings are: a page of these inlined as base64 is tens
    of megabytes almost every caller throws away.
    """

    id: uuid.UUID = Field(description="Use it on the audio and delete routes.")
    ai_session_id: uuid.UUID = Field(
        description="The metered session that paid for it, as it appears on the ledger.",
    )
    text: str = Field(
        description="The transcript. Empty when the audio contained no speech.",
        examples=["Assalomu alaykum, bugun havo juda yaxshi."],
    )
    language: str = Field(description="The language it was transcribed in.", examples=["uz"])
    audio_ms: int = Field(description="Duration billed for, in milliseconds.", examples=[1280])
    infer_ms: int = Field(
        description="How long the model took. Recorded, never billed.", examples=[413]
    )
    filename: str | None = Field(
        default=None, description="The name the upload arrived under.", examples=["clip.wav"]
    )
    content_type: str | None = Field(default=None, examples=["audio/wav"])
    audio_bytes: int = Field(description="Size of the uploaded audio.", examples=[169004])
    sha256: str = Field(
        description="Digest of the audio. The file is stored under it, so it can be verified.",
    )
    created_at: datetime


class TranscriptionPageResponse(_Schema):
    ok: bool = True
    transcriptions: list[TranscriptionRecordResponse]
    page: PageInfo


def transcription_record_response(row) -> TranscriptionRecordResponse:
    """One row, key by key. `body` on the row, `text` on the wire."""
    return TranscriptionRecordResponse(
        id=row.id,
        ai_session_id=row.ai_session_id,
        text=row.body,
        language=row.language,
        audio_ms=row.audio_ms,
        infer_ms=row.infer_ms,
        filename=row.filename,
        content_type=row.content_type,
        audio_bytes=row.audio_bytes,
        sha256=row.sha256,
        created_at=row.created_at,
    )
