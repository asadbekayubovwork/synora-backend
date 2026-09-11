"""Request and response shapes for the speech endpoints.

The money convention is `app/schemas/wallet.py`'s and is not restated here:
every amount is an integer of micro-credits for arithmetic and a fixed-point
string for display, because the client is JavaScript and `0.1 + 0.2` is the
reason. What is worth stating is which amount is deliberately *absent*.
`pricing.PricedUsage` and `session_service.Quote` both carry `cost_micros` —
what the GPU time costs us — and nothing on this page does. An estimate that
also published our margin would be a pricing decision made by accident, so the
builders below drop that number on purpose rather than by oversight.

## Why upstream's limits are restated here

Upstream enforces its own maximums and answers a violation with a 422. Every
one of them is repeated as a `Field` constraint anyway, because of where the
two rejections land. Ours lands in FastAPI's validation pass: before the wallet
is touched, naming the offending field, costing a round trip and nothing else.
Upstream's lands after `open_oneshot` has priced the request, placed a hold and
opened a session — so the same bad text now needs a release, a terminal session
and an explanation, and the caller watched credit vanish for work that was
never going to happen. Paying for the constraint twice is the cheaper half of
that trade, and it is why `text` carries `max_length` even though
`tts_service.synthesize` checks the length again on its way past.

The numbers are ours to be stricter with, and in one place we are: upstream
takes 5 000 items in a batch and `tts_batch_max_items` defaults to 500. The
queue in front of a single GPU is admission control, not throughput, and a
500-item job already runs long enough that the person who submitted it starts
asking whether it is stuck.

The drift this buys is named rather than hidden. `BATCH_ITEM_MAX_CHARACTERS`
and the three format vocabularies below are upstream's constants copied into
our source, and the day upstream adds an output format or raises a limit, this
file is where it has to be said again. Copying them is still better than
learning them one support ticket at a time.

One limit is pointedly *not* copied. `idempotency_key`'s ceiling is imported
from `session_service`, because unlike upstream's numbers that one is enforced
a few modules away in our own code, where a copy can drift without anybody
noticing until a client generates a key at the length this schema advertises
and is refused for a reason the schema never mentioned.

## Upstream's JSON never becomes one of these models directly

`voice_response` and `batch_result_item` read dicts key by key with defaults
instead of validating whatever came back. A response model is a promise we make
to our own client; upstream is not a party to it, and a field renamed on their
side one afternoon must not turn `GET /tts/voices` into a 500 for everybody who
only wanted the list.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.core.config import settings
from app.core.money import format_credits
from app.models.billing_enums import BillingService, TtsBatchJobState, UsageMetric
from app.schemas.common import PageInfo, _Schema, ensure_utc
from app.services.billing.session_service import MAX_CLIENT_IDEMPOTENCY_KEY

# --- upstream's vocabulary, copied ------------------------------------------

# Literal aliases rather than `enum.Enum` classes, following the closed sets in
# `app/schemas/internal.py`. These never reach a database column — `TtsBatchJob`
# stores them as plain `String` — so an enum member would only add a `.value`
# that every call site has to remember.
SynthesisQuality = Literal["low_latency", "balanced", "high_fidelity"]
AudioFormat = Literal["pcm", "wav", "mp3", "opus"]
VoiceClipFormat = Literal["wav", "mp3", "flac", "ogg"]

# Not a range. Upstream resamples to a fixed set of rates, and a rate it has to
# refuse is a 422 we could have answered ourselves; a rate it silently rounds is
# worse, because the `X-Synora-Sample-Rate` header would then describe audio the
# caller is not receiving.
SampleRate = Literal[8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000]

# Upstream's per-item ceiling for a batch. Four times the streaming limit,
# because nothing is waiting on the first byte here.
BATCH_ITEM_MAX_CHARACTERS = 20_000

# A voice clip is 3-30 seconds. Thirty seconds of 48 kHz 16-bit stereo WAV is
# 5.76 MB, which is 7.7 MB once base64 has added its third; 12 MB clears that
# with room for a container header and a client that wraps its lines. The cap
# exists so an over-large body is refused by our parser rather than pushed
# through the tunnel to be refused by theirs.
VOICE_CLIP_MAX_BASE64 = 12 * 1024 * 1024

# Browsers hand `FileReader.readAsDataURL` output straight to fetch(), so the
# `data:audio/wav;base64,` preamble arrives more often than not. Stripping it is
# a kindness that costs one regex; refusing it teaches the caller nothing.
_DATA_URI_PREFIX = re.compile(r"^data:[^;,]*;base64,", re.IGNORECASE)


def _require_text(value: str) -> str:
    """Refuse text that is only whitespace, without touching what is there.

    Deliberately not `.strip()`. The charge is `len(text)`, so trimming would
    bill a different number from the one the caller counted, and the first
    invoice nobody can reproduce is the last one they trust. Blank is still
    refused: a hold placed on a single space is a charge with no explanation.
    """
    if not value.strip():
        raise ValueError("must contain at least one non-whitespace character")
    return value


def _clean_base64(value: str) -> str:
    """Normalise a clip to bare base64, and prove it decodes before we send it.

    `b64decode(validate=True)` rejects the newlines that MIME-wrapped base64 is
    full of, so the whitespace comes out first. Checking here rather than
    letting upstream do it turns "12 MB pushed through the tunnel and refused"
    into a validation error that names the field.
    """
    compact = "".join(_DATA_URI_PREFIX.sub("", value.strip()).split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("must be base64, optionally with a data: URI prefix") from None
    if not decoded:
        raise ValueError("decoded to no audio at all")
    return compact


# --- synthesis --------------------------------------------------------------


class SynthesizeRequest(_Schema):
    """Body of `POST /tts/speech`, the metered streaming route.

    Every field here except `text` is a knob passed through to upstream
    unchanged. `text` is the one that costs money, and the price is settled
    before the first byte leaves the GPU.
    """

    text: str = Field(
        min_length=1,
        max_length=settings.tts_max_characters,
        description=(
            "What to say. Billed at its character count — all of it, including "
            "when the connection drops halfway through, because the whole text "
            "has already been sent to the GPU by then. At most "
            f"{settings.tts_max_characters:,} characters; longer copy belongs in a batch."
        ),
        examples=["Salom! Bugungi ob-havo haqida qisqacha aytib beraman."],
    )
    voice_id: str | None = Field(
        default=None,
        max_length=64,
        description="A voice from `GET /tts/voices`. Null uses the service default.",
        examples=["vc_7f3a1c9e2b"],
    )
    quality: SynthesisQuality = Field(
        default="balanced",
        description=(
            "`low_latency` reaches the first audio soonest and sounds it; "
            "`high_fidelity` holds the connection open longer than a live "
            "player usually wants. `balanced` is the default for that reason."
        ),
        examples=["balanced"],
    )
    audio_format: AudioFormat = Field(
        # Upstream, the OpenAI-compatible route and every example anyone will
        # have read all call this field `format`. We call it `audio_format`
        # because `format` is a Python builtin and shadows it in any client
        # generated from this schema — but a caller who sends the name the
        # supplier documents must not be silently ignored. Pydantic drops an
        # unknown key without a word, so before this alias existed
        # `{"format": "wav"}` quietly synthesised mp3 and the caller only
        # found out by reading the bytes. Accepting both spellings costs one
        # line; the alternative costs a support ticket that starts "your API
        # ignores my format".
        validation_alias=AliasChoices("audio_format", "format"),
        default="mp3",
        description=(
            "`mp3` plays straight out of an `<audio>` element, which is why it "
            "is the default. `pcm` is the choice for a player that assembles "
            "frames itself: no container, lowest latency, 16-bit mono at "
            "`sample_rate`."
        ),
        examples=["mp3"],
    )
    sample_rate: SampleRate = Field(
        default=48_000,
        description="Hz. Echoed back on `X-Synora-Sample-Rate`.",
        examples=[48_000],
    )
    style: str | None = Field(
        default=None,
        max_length=255,
        description="A delivery hint for the voice, if it supports one.",
        examples=["calm narration"],
    )

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        return _require_text(value)


class EstimateRequest(_Schema):
    """Body of `POST /tts/estimate`. Prices text without touching the wallet."""

    text: str = Field(
        min_length=1,
        # The batch ceiling, not the streaming one. Pricing a job you have not
        # committed to is most of what an estimate is for, and refusing to
        # quote the only request large enough to be worth quoting would be an
        # odd place to draw the line.
        max_length=settings.tts_batch_max_characters,
        description=(
            "The text you are about to synthesise. Nothing is held and nothing "
            "is charged; the same characters submitted to `/tts/speech` or "
            "`/tts/batch` cost the quoted price, provided the price book has "
            "not been rolled over in between."
        ),
        examples=["Salom! Bugungi ob-havo haqida qisqacha aytib beraman."],
    )

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        return _require_text(value)


class EstimateResponse(_Schema):
    """What the text would cost, and whether the wallet covers it."""

    ok: bool = True

    characters: int = Field(
        description="Characters counted. This is the billed quantity.",
        examples=[54],
    )
    price_micros: int = Field(
        description="What synthesising this text would charge.",
        examples=[250_000],
    )
    price: str = Field(examples=["0.250000"])

    available_micros: int = Field(
        description="Spendable credit right now, as `GET /wallet` reports it.",
        examples=[36_633_183],
    )
    available: str = Field(examples=["36.633183"])
    sufficient_credit: bool = Field(
        description="False means `/tts/speech` would answer 402 rather than synthesise.",
        examples=[True],
    )
    shortfall_micros: int = Field(
        description="How much to top up by. Zero when the balance already covers it.",
        examples=[0],
    )
    shortfall: str = Field(examples=["0.000000"])

    price_book_version_id: uuid.UUID = Field(
        description=(
            "The price book this quote came from. A charge priced against a "
            "different one is a quote that expired between the two calls."
        ),
    )


def estimate_response(quote, *, characters: int, available_micros: int) -> EstimateResponse:
    """Built from a `session_service.Quote` and the caller's own balance.

    `quote.cost_micros` is dropped here, and that is the point of having a
    builder rather than a `model_validate`: the one number on the quote that
    must never reach a customer cannot be forgotten about by a route that
    serialises the dataclass wholesale.
    """
    shortfall_micros = max(0, quote.price_micros - available_micros)
    return EstimateResponse(
        characters=characters,
        price_micros=quote.price_micros,
        price=format_credits(quote.price_micros),
        available_micros=available_micros,
        available=format_credits(available_micros),
        sufficient_credit=shortfall_micros == 0,
        shortfall_micros=shortfall_micros,
        shortfall=format_credits(shortfall_micros),
        price_book_version_id=quote.price_book_version_id,
    )


# --- voices -----------------------------------------------------------------


class RegisterVoiceRequest(_Schema):
    """Body of `POST /tts/voices` — clone a voice from a short clip.

    Free: no session, no hold, no charge. What it costs is bandwidth and a slot
    in the tenant's voice list, which is why the size ceiling is here.
    """

    display_name: str = Field(
        min_length=1,
        max_length=64,
        description="How the voice is labelled in `GET /tts/voices`.",
        examples=["Aziza - narration"],
    )
    audio_base64: str = Field(
        min_length=1,
        max_length=VOICE_CLIP_MAX_BASE64,
        description=(
            "A 3-30 second clip of one speaker, base64-encoded. A "
            "`data:audio/wav;base64,` prefix and MIME line breaks are both "
            "accepted and stripped. Clean speech clones better than a long "
            "noisy sample does."
        ),
        examples=["UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA="],
    )
    audio_format: VoiceClipFormat = Field(
        default="wav",
        description="The container the clip is in, before base64.",
        examples=["wav"],
    )
    transcript: str | None = Field(
        default=None,
        max_length=2_000,
        description=(
            "What the clip says, if you have it. Supplying it improves the "
            "clone; leaving it out makes upstream transcribe the clip itself."
        ),
        examples=["Bugun havo juda yaxshi, ko'chaga chiqsak bo'ladi."],
    )
    denoise: bool = Field(
        default=False,
        description=(
            "Run noise reduction over the clip first. Off by default: it is "
            "destructive, and a clean studio take comes out worse for it."
        ),
        examples=[False],
    )

    @field_validator("display_name")
    @classmethod
    def _reject_blank_name(cls, value: str) -> str:
        return _require_text(value).strip()

    @field_validator("audio_base64")
    @classmethod
    def _normalize_clip(cls, value: str) -> str:
        return _clean_base64(value)


class VoiceResponse(_Schema):
    """One voice, as upstream describes it.

    Every field but `voice_id` is optional on the wire because every field but
    `voice_id` is upstream's to rename. See `voice_response`.
    """

    voice_id: str = Field(
        description="Pass as `voice_id` when synthesising.",
        examples=["vc_7f3a1c9e2b"],
    )
    display_name: str | None = Field(
        default=None,
        description="The label given at registration.",
        examples=["Aziza - narration"],
    )
    supports_ultimate: bool = Field(
        default=False,
        description="Whether this voice can be driven at `high_fidelity`.",
        examples=[True],
    )
    reference_seconds: float | None = Field(
        default=None,
        description="Length of the clip the voice was cloned from. Null for built-ins.",
        examples=[12.4],
    )
    has_speaker_embedding: bool = Field(
        default=False,
        description="False means the clone has not finished processing yet.",
        examples=[True],
    )
    created_at: datetime | None = Field(default=None, examples=["2026-09-07T12:34:56Z"])

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)


class VoiceListResponse(_Schema):
    ok: bool = True
    voices: list[VoiceResponse] = Field(
        description="Built-in voices and this tenant's clones, in upstream's order.",
    )


def voice_response(raw: dict[str, Any]) -> VoiceResponse:
    """Read one voice out of upstream's JSON, key by key.

    Not `VoiceResponse.model_validate(raw)`. That would make every field
    upstream ever renames a 500 on a route whose caller only wanted a list, and
    a voice list is exactly the sort of read that should degrade to "less
    detail" rather than to "no answer".
    """
    return VoiceResponse(
        voice_id=str(raw.get("voice_id") or ""),
        display_name=raw.get("display_name"),
        supports_ultimate=bool(raw.get("supports_ultimate")),
        reference_seconds=raw.get("reference_seconds"),
        has_speaker_embedding=bool(raw.get("has_speaker_embedding")),
        created_at=raw.get("created_at"),
    )


def voice_list_response(rows: list[dict[str, Any]]) -> VoiceListResponse:
    # An entry with no `voice_id` is dropped rather than returned empty: it is
    # a voice nothing can be synthesised with, so listing it only produces a
    # picker option that 400s when someone chooses it.
    voices = [voice_response(row) for row in rows]
    return VoiceListResponse(voices=[voice for voice in voices if voice.voice_id])


# --- batch requests ---------------------------------------------------------


class BatchItem(_Schema):
    """One clip in a batch."""

    id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Your own handle for this clip. Results come back keyed on it, so "
            "it has to be unique within the job."
        ),
        examples=["chapter-01"],
    )
    text: str = Field(
        min_length=1,
        max_length=BATCH_ITEM_MAX_CHARACTERS,
        description=f"What this clip says. At most {BATCH_ITEM_MAX_CHARACTERS:,} characters.",
        examples=["Birinchi bob. Kechqurun shahar tinch edi."],
    )
    voice_id: str | None = Field(
        default=None,
        max_length=64,
        description="Overrides the job's voice for this clip alone. Null inherits it.",
        examples=[None],
    )

    @field_validator("id")
    @classmethod
    def _reject_blank_id(cls, value: str) -> str:
        return _require_text(value).strip()

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        return _require_text(value)


class BatchCreateRequest(_Schema):
    """Body of `POST /tts/batch`.

    The whole job is priced and held for at creation, from the character counts
    in this payload, so the two ceilings below are the ones that decide what a
    single request can commit the wallet to.
    """

    items: list[BatchItem] = Field(
        min_length=1,
        max_length=settings.tts_batch_max_items,
        description=(
            f"The clips to render, at most {settings.tts_batch_max_items:,} of "
            f"them and {settings.tts_batch_max_characters:,} characters in "
            "total. Split a larger corpus into several jobs; they queue behind "
            "each other either way."
        ),
    )
    voice_id: str | None = Field(
        default=None,
        max_length=64,
        description="The job's voice. Individual items may override it.",
        examples=["vc_7f3a1c9e2b"],
    )
    quality: SynthesisQuality = Field(
        default="high_fidelity",
        description=(
            "`high_fidelity` by default, the opposite of the streaming route: "
            "nothing is waiting on the first byte, so there is no latency to "
            "trade away."
        ),
        examples=["high_fidelity"],
    )
    audio_format: AudioFormat = Field(
        # Upstream, the OpenAI-compatible route and every example anyone will
        # have read all call this field `format`. We call it `audio_format`
        # because `format` is a Python builtin and shadows it in any client
        # generated from this schema — but a caller who sends the name the
        # supplier documents must not be silently ignored. Pydantic drops an
        # unknown key without a word, so before this alias existed
        # `{"format": "wav"}` quietly synthesised mp3 and the caller only
        # found out by reading the bytes. Accepting both spellings costs one
        # line; the alternative costs a support ticket that starts "your API
        # ignores my format".
        validation_alias=AliasChoices("audio_format", "format"),
        default="wav",
        description="`wav` by default — these are files to keep, not frames to play.",
        examples=["wav"],
    )
    sample_rate: SampleRate = Field(default=48_000, description="Hz.", examples=[48_000])
    style: str | None = Field(
        default=None,
        max_length=255,
        description="A delivery hint applied to every item.",
        examples=["calm narration"],
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        # Imported, never restated. This field is the *published* limit and
        # `session_service.open_oneshot` is the *enforced* one, and the two
        # spent a release disagreeing: the number here said 128 while the check
        # there counted the stored key, `{user_id}:{scope}:` prefix and all, so
        # anything over 91 characters came back 400 `idempotency_key_too_long`
        # — a constraint named in the error and nowhere in the contract, hit by
        # exactly the clients who read our own Swagger page and generated a key
        # at the length it advertised. How much headroom the prefix needs is
        # `session_service`'s business; this field only repeats its answer.
        max_length=MAX_CLIENT_IDEMPOTENCY_KEY,
        description=(
            "Send the same key again to get the job it already created, rather "
            "than pricing and holding for a second copy of the same work. "
            "Unlike `/tts/speech`'s `Idempotency-Key` header this one does not "
            "go stale — it keeps returning the same job after it has settled, "
            "because a job row is a durable answer where a finished stream is "
            "not. Use a fresh key for a new corpus. "
            f"At most {MAX_CLIENT_IDEMPOTENCY_KEY} characters, and scoped to "
            "your account and to this route — the same key on `POST /tts/speech` "
            "is a different request and gets its own session. Generated for you "
            "when omitted, which means a retried POST with no key is a second job."
        ),
        examples=["book-42-chapters"],
    )

    @property
    def total_characters(self) -> int:
        """What the hold is sized against, counted the same way it is billed."""
        return sum(len(item.text) for item in self.items)

    @model_validator(mode="after")
    def _check_job(self) -> BatchCreateRequest:
        # Duplicate ids are refused rather than de-duplicated. Upstream returns
        # results keyed on this id, so two items sharing one is a job whose
        # results cannot be attributed — and the caller would be charged for
        # both while only ever finding one.
        seen: set[str] = set()
        for item in self.items:
            if item.id in seen:
                raise ValueError(f"item id '{item.id}' appears more than once")
            seen.add(item.id)

        total = self.total_characters
        if total > settings.tts_batch_max_characters:
            raise ValueError(
                f"{total:,} characters is over the "
                f"{settings.tts_batch_max_characters:,} allowed in one job"
            )
        return self


# --- batch responses --------------------------------------------------------


class BatchJobResponse(_Schema):
    """One batch job, ours rather than upstream's.

    `state` is our own vocabulary and not a relay of theirs: `queued` is the
    state only we can be in — priced, held for, not yet handed over — and
    `expired` is the verdict only we can reach. Poll on `is_terminal`, not on
    any particular member of the set.
    """

    id: uuid.UUID = Field(description="The job. Use it on every other `/tts/batch` route.")
    # The thread back to the statement: `GET /wallet/transactions` reports this
    # same id on the hold, the release and the debit this job produced.
    ai_session_id: uuid.UUID = Field(
        description="The metered session paying for this job, as it appears on the ledger.",
    )
    upstream_job_id: str | None = Field(
        default=None,
        description="The speech service's own handle. Null until it accepts the job.",
        examples=["btch_91c0d3"],
    )
    state: TtsBatchJobState = Field(
        description=(
            "`queued` before the job reaches the speech service, then "
            "`submitted`, `running`, and one of `succeeded`, `failed`, "
            "`cancelled` or `expired`."
        ),
        examples=["running"],
    )
    is_terminal: bool = Field(
        description="True once the hold is gone and nothing further will change. Stop polling.",
        examples=[False],
    )
    # Not reported: `poll_count` and `next_poll_at`. They are our scheduler's
    # business, they change on a timer nobody asked about, and a client that
    # started polling on our polling interval would be pacing itself off an
    # implementation detail.

    voice_id: str | None = Field(default=None, examples=["vc_7f3a1c9e2b"])
    audio_format: str = Field(description="As submitted; pinned for every retry.", examples=["wav"])
    quality: str = Field(examples=["high_fidelity"])
    sample_rate: int = Field(examples=[48_000])
    style: str | None = Field(default=None, examples=["calm narration"])

    total_items: int = Field(description="Clips submitted.", examples=[42])
    completed_items: int = Field(description="Clips rendered so far.", examples=[17])
    failed_items: int = Field(
        description="Clips upstream gave up on. Not billed — see `billed_characters`.",
        examples=[0],
    )

    submitted_characters: int = Field(
        description="What we counted out of your payload and placed the hold against.",
        examples=[128_400],
    )
    billed_characters: int = Field(
        description=(
            "What the speech service reported synthesising, and what the "
            "settlement charged. Lands below `submitted_characters` when items "
            "failed: nobody is billed for audio that was never produced. Zero "
            "until the job settles."
        ),
        examples=[128_400],
    )
    audio_ms: int = Field(
        description=(
            "Audio produced, in milliseconds. Reported because it is worth "
            "knowing and never priced: text-to-speech is sold by input "
            "characters, which is the one quantity you can count before "
            "spending anything."
        ),
        examples=[3_612_000],
    )

    estimated_micros: int = Field(
        description="What the job was quoted at, and the size of the hold placed for it.",
        examples=[32_100_000],
    )
    estimated: str = Field(examples=["32.100000"])
    reserved_micros: int = Field(
        description="Still held against this job. Zero once it has settled.",
        examples=[32_100_000],
    )
    reserved: str = Field(examples=["32.100000"])
    settled_micros: int = Field(
        description="Actually charged. Zero until the job reaches a terminal state.",
        examples=[0],
    )
    settled: str = Field(examples=["0.000000"])

    error: str | None = Field(
        default=None,
        description="Why it ended badly, when it did. Null on the happy path.",
        examples=[None],
    )
    created_at: datetime = Field(examples=["2026-09-07T12:34:56Z"])
    submitted_at: datetime | None = Field(
        default=None,
        description="When the speech service accepted the job.",
        examples=["2026-09-07T12:34:58Z"],
    )
    finished_at: datetime | None = Field(default=None, examples=[None])

    @field_validator("created_at", "submitted_at", "finished_at")
    @classmethod
    def _ensure_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)


class BatchJobPageResponse(_Schema):
    ok: bool = True
    jobs: list[BatchJobResponse]
    page: PageInfo


def batch_job_response(job, ai_session) -> BatchJobResponse:
    """Built from the job row and the metered session that pays for it.

    Two rows rather than one, because none of the money is on the job. The
    hold, the pinned price book and the debit all live on `ai_sessions`, and
    copying any of them onto `tts_batch_jobs` would create a second place for
    the same number to be wrong. There is exactly one session per job —
    `uq_tts_batch_jobs_ai_session_id` says so — which is what makes the list
    route a join rather than a query per row.
    """
    return BatchJobResponse(
        id=job.id,
        ai_session_id=job.ai_session_id,
        upstream_job_id=job.upstream_job_id,
        state=job.state,
        is_terminal=job.is_terminal,
        voice_id=job.voice_id,
        audio_format=job.audio_format,
        quality=job.quality,
        sample_rate=job.sample_rate,
        style=job.style,
        total_items=job.total_items,
        completed_items=job.completed_items,
        failed_items=job.failed_items,
        submitted_characters=job.submitted_characters,
        billed_characters=job.billed_characters,
        audio_ms=job.audio_ms,
        estimated_micros=ai_session.estimated_micros,
        estimated=format_credits(ai_session.estimated_micros),
        reserved_micros=ai_session.reserved_micros,
        reserved=format_credits(ai_session.reserved_micros),
        settled_micros=ai_session.settled_micros,
        settled=format_credits(ai_session.settled_micros),
        error=job.error,
        created_at=job.created_at,
        submitted_at=job.submitted_at,
        finished_at=job.finished_at,
    )


class BatchResultItem(_Schema):
    """One rendered clip, as upstream reports it."""

    id: str = Field(
        description="The id you gave this item.",
        examples=["chapter-01"],
    )
    ok: bool = Field(
        description=(
            "Whether this clip rendered. The envelope's own `ok` is about the "
            "request; this one is about the item."
        ),
        examples=[True],
    )
    path: str | None = Field(
        default=None,
        description=(
            "The speech service's own storage handle for the clip. Not a URL "
            "this API serves — quote it in a support request."
        ),
        examples=["/var/lib/tts/btch_91c0d3/chapter-01.wav"],
    )
    characters: int = Field(
        default=0,
        description="Characters synthesised for this clip, as counted upstream.",
        examples=[3_058],
    )
    audio_seconds: float | None = Field(
        default=None,
        description="Length of the clip. Reported, never priced.",
        examples=[86.4],
    )
    similarity: float | None = Field(
        default=None,
        description="How close the clone came to its reference, 0 to 1. Null for built-in voices.",
        examples=[0.91],
    )
    retries: int = Field(
        default=0,
        description="How many attempts upstream needed. Above zero is worth a listen.",
        examples=[0],
    )
    error: str | None = Field(default=None, examples=[None])


class BatchResultsResponse(_Schema):
    ok: bool = True
    job_id: uuid.UUID = Field(description="Our job id, not the speech service's.")
    state: TtsBatchJobState = Field(
        description="The job's state as we hold it, so results and state cannot disagree.",
        examples=["succeeded"],
    )
    results: list[BatchResultItem] = Field(
        description="One entry per submitted item, including the ones that failed.",
    )


def batch_result_item(raw: dict[str, Any]) -> BatchResultItem:
    """Read one result out of upstream's JSON, key by key. See `voice_response`."""
    return BatchResultItem(
        id=str(raw.get("id") or ""),
        ok=bool(raw.get("ok")),
        path=raw.get("path"),
        characters=int(raw.get("characters") or 0),
        audio_seconds=raw.get("audio_seconds"),
        similarity=raw.get("similarity"),
        retries=int(raw.get("retries") or 0),
        error=raw.get("error"),
    )


def batch_results_response(job, payload: dict[str, Any]) -> BatchResultsResponse:
    """Our job row plus upstream's results array.

    The state comes from our row rather than from `payload["state"]`, which is
    the same value one poll earlier at best. Ours is the one the settlement
    branched on, so it is the one that agrees with the wallet.
    """
    raw_results = payload.get("results")
    items = raw_results if isinstance(raw_results, list) else []
    return BatchResultsResponse(
        job_id=job.id,
        state=job.state,
        results=[batch_result_item(row) for row in items if isinstance(row, dict)],
    )


# --- recordings -------------------------------------------------------------


class RecordingResponse(_Schema):
    """One kept synthesis: what was asked for, and what came back.

    The audio itself is a second request — `GET /tts/recordings/{id}/audio` —
    because a list of twenty-five of these would otherwise be tens of megabytes
    of base64 that almost every caller throws away.
    """

    id: uuid.UUID = Field(description="The recording. Use it on the audio and delete routes.")
    ai_session_id: uuid.UUID = Field(
        description="The metered session that paid for it, as it appears on the ledger.",
    )
    text: str = Field(
        description="The text that was synthesised, exactly as it was submitted.",
        examples=["Assalomu alaykum, bugun havo juda yaxshi."],
    )
    voice_id: str | None = Field(
        default=None, description="The voice, or null for the server default."
    )
    quality: str = Field(description="`low_latency`, `balanced` or `high_fidelity`.")
    audio_format: str = Field(description="`mp3`, `wav`, `pcm` or `opus`.")
    sample_rate: int = Field(description="Samples per second, as requested.", examples=[48000])
    style: str | None = Field(default=None, description="The style prompt, if one was sent.")
    characters: int = Field(description="Characters charged for.", examples=[41])
    audio_bytes: int = Field(description="Bytes of audio delivered.", examples=[307244])
    audio_ms: int = Field(
        description=(
            "Duration, for the formats where bytes and duration are the same "
            "fact in two units. Zero for `mp3` and `opus`, which are "
            "variable-bitrate containers -- a plausible-looking wrong number "
            "would be worse than none."
        ),
        examples=[3200],
    )
    sha256: str = Field(
        description="Digest of the audio. The file is stored under it, so it can be verified.",
    )
    created_at: datetime


class RecordingPageResponse(_Schema):
    ok: bool = True
    recordings: list[RecordingResponse]
    page: PageInfo


def recording_response(recording) -> RecordingResponse:
    """One row, key by key. `body` on the row, `text` on the wire.

    The column is `body` because `text` is `sqlalchemy.text` in every module
    that touches this table; the field is `text` because that is what the
    request called it, and a client should not have to learn our column names.
    """
    return RecordingResponse(
        id=recording.id,
        ai_session_id=recording.ai_session_id,
        text=recording.body,
        voice_id=recording.voice_id,
        quality=recording.quality,
        audio_format=recording.audio_format,
        sample_rate=recording.sample_rate,
        style=recording.style,
        characters=recording.characters,
        audio_bytes=recording.audio_bytes,
        audio_ms=recording.audio_ms,
        sha256=recording.sha256,
        created_at=recording.created_at,
    )


# --- usage ------------------------------------------------------------------


class UsageLineResponse(_Schema):
    """One (service, metric) pair, summed over the window."""

    service: BillingService = Field(
        description="Which metered service the consumption belongs to.",
        examples=["tts"],
    )
    metric: UsageMetric = Field(
        description="What was counted. The name carries its unit.",
        examples=["tts_characters"],
    )
    quantity: int = Field(
        description="How much of it, in the metric's own unit.",
        examples=[128_400],
    )
    events: int = Field(
        description="Usage events behind this line. One per one-shot call.",
        examples=[37],
    )
    price_micros: int = Field(description="Charged for this line.", examples=[32_100_000])
    price: str = Field(examples=["32.100000"])


class UsageSummaryResponse(_Schema):
    """`GET /usage` — the signed-in user's own consumption over a window.

    This is the answer to "usage is controlled by us": it is computed from
    `usage_event_items` in our database, the same rows the debits were made
    from, not relayed from any upstream counter. The speech service's own
    `/v1/usage` is tenant-wide and reports what *we* have spent against it.
    """

    ok: bool = True

    period_start: datetime = Field(
        description="Inclusive. Events are placed by `occurred_at`, not by when we stored them.",
        examples=["2026-09-01T00:00:00Z"],
    )
    period_end: datetime = Field(description="Exclusive.", examples=["2026-10-01T00:00:00Z"])

    lines: list[UsageLineResponse] = Field(
        description="One row per service and metric. Empty when nothing was used.",
    )
    events: int = Field(description="Usage events in the window.", examples=[37])
    total_price_micros: int = Field(
        description="Everything charged in the window.",
        examples=[32_100_000],
    )
    total: str = Field(examples=["32.100000"])

    @field_validator("period_start", "period_end")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


def usage_line_response(row) -> UsageLineResponse:
    """Built from one aggregate row.

    The query must label its aggregates `quantity`, `events` and `price_micros`
    — `func.sum(...).label("quantity")` and friends — so that this builder
    names columns rather than positions and a reordered `select` cannot quietly
    report characters as money.
    """
    return UsageLineResponse(
        service=row.service,
        metric=row.metric,
        quantity=int(row.quantity or 0),
        events=int(row.events or 0),
        price_micros=int(row.price_micros or 0),
        price=format_credits(int(row.price_micros or 0)),
    )


def usage_summary_response(
    rows, *, period_start: datetime, period_end: datetime
) -> UsageSummaryResponse:
    """Sum the lines rather than asking the database for a second total.

    A total computed by its own query is a total that disagrees with the lines
    printed underneath it the first time the two `WHERE` clauses drift apart,
    and a page where the column does not add up is unusable however right the
    total happens to be.

    `events` is summed across lines and therefore counts a session once per
    metric it recorded. For a one-shot with a single priced metric — every TTS
    call — that is the call count. It is stated here because the day chat
    arrives with three metrics on one event, this number triples.
    """
    lines = [usage_line_response(row) for row in rows]
    total_micros = sum(line.price_micros for line in lines)
    return UsageSummaryResponse(
        period_start=period_start,
        period_end=period_end,
        lines=lines,
        events=sum(line.events for line in lines),
        total_price_micros=total_micros,
        total=format_credits(total_micros),
    )
