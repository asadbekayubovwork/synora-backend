"""The speech surface: one metered stream, a batch queue, and the voice list.

Every route here is deliberately thin. `tts_client` owns the socket,
`tts_service` owns the stream and the settlement hanging off it, and
`tts_batch_service` owns a job and its billing. What is left for a route is
choosing a status code and a response model — and the one thing a route may
never do is bill for anything itself. `app/workers/tts_batch.py` calls the same
two service functions this module calls, so the moment a route grows a
settlement of its own, a deployment starts charging different amounts depending
on whether RabbitMQ happened to be running, and nobody finds out until a
customer compares two invoices.

## `POST /tts/speech` returns a response object, not a model

It is the only route in this API whose body is bytes rather than JSON, and the
ordering it depends on is subtle enough to be worth stating here as well as in
`tts_service`: Starlette sends the status line and headers *before* it pulls
the first chunk out of a `StreamingResponse`. So everything that can still
become a real status code — the wallet check, upstream's refusal, a rejected
voice id — has already happened by the time this function returns, and
`tts_service.synthesize` is shaped to make that true. After the return there is
no status code left to choose.

The price rides on the response headers because full-text billing makes it
final before the first byte exists. `main.py` names those headers in
`CORSMiddleware(expose_headers=...)`; without that a browser reads `undefined`
where the price should be and reports no error at all.

## Reading a batch job is what moves it, when nothing else will

With a broker, a worker polls upstream and settles. Without one, the only code
that ever runs after `POST /tts/batch` returns is a route — so
`GET /tts/batch/{job_id}` and the results route both refresh the job through
`tts_batch_service`, which is internally rate-limited by
`TTS_BATCH_POLL_SECONDS`. The list route pointedly does not: a page of
twenty-five jobs would become twenty-five upstream calls made on behalf of
somebody who only wanted to see a list.

Advancing a job and reading one are two things, though, and only one of them
needs the speech service. `_advance_job` is where that separation lives: an
upstream refusal downgrades a read to "the row as it stands" instead of
failing it, because refusing to show a customer the state of their own job —
all of which is in our database already — is a strictly worse outage than the
one upstream is having. `DELETE` is the exception and stays failing, since a
cancel that never reached upstream has cancelled nothing.

## Someone else's job id is a 404

`tts_batch_service.load_job` scopes every read to the caller. "No such job" and
"not yours" are the same sentence to anyone who should not be able to tell the
difference, and a 403 here would confirm which ids exist.

## An idempotency key de-duplicates a race; it is not a receipt

Every published description of `Idempotency-Key` on this page is written from
what `tts_service._require_matching_replay` actually does, and that is narrower
than the word usually implies. A key joins the session the first request under
it opened — so a retry that arrives *while* the original is still synthesising
adds no second hold and no second charge — and that is the whole of it. The
moment the original settles, the key is spent and comes back `409`.

The alternative would be to store the audio and serve it again, and the trade
is not close: a five-thousand-character `mp3` is megabytes, the retry rate is a
fraction of a percent, and every stored body is a copy of a customer's content
we then have to keep, secure and delete. So the honest contract is the narrow
one, and the reason it is spelled out at this length in the route description
is that the previous version of that text promised the broad one. An SDK author
who reads "a retry is charged once" and builds a retry loop on it gets a `409`
in production for the one request the header exists to make safe — which is a
worse outage than the header not existing, because it fails on the recovery
path rather than on the happy one.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Header, Path, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.exceptions import (
    BadGatewayError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models.ai_session import AiSession
from app.models.billing_enums import BillingService, UsageMetric
from app.models.tts_job import TtsBatchJob
from app.schemas.auth import ErrorResponse
from app.schemas.common import Cursor, MessageResponse, PageInfo, clamp_limit, decode_cursor
from app.schemas.tts import (
    BatchCreateRequest,
    BatchJobPageResponse,
    BatchJobResponse,
    BatchResultsResponse,
    EstimateRequest,
    EstimateResponse,
    RecordingPageResponse,
    RecordingResponse,
    RegisterVoiceRequest,
    SynthesizeRequest,
    VoiceListResponse,
    VoiceResponse,
    batch_job_response,
    batch_results_response,
    estimate_response,
    recording_response,
    voice_list_response,
    voice_response,
)
from app.services.ai import (
    recording_store,
    tts_batch_service,
    tts_client,
    tts_recording_service,
    tts_service,
)
from app.services.billing import session_service, wallet_service

logger = logging.getLogger("synora.tts")

router = APIRouter(prefix="/tts", tags=["TTS"])

ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "The speech service rejected the input"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "No such voice or job"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    429: {"model": ErrorResponse, "description": "Too many requests"},
    502: {"model": ErrorResponse, "description": "The speech service was unreachable"},
    503: {"model": ErrorResponse, "description": "Speech synthesis is not available here"},
}

# The two extra refusals a route that touches the wallet can produce: 402 when
# the balance will not cover the work, 409 when an idempotency key is spent or
# belongs to a different request. The 409 description names both because they
# are two different instructions to the client — one says "do not retry this,
# you have already paid for it" and the other says "you sent the wrong key" —
# and a single line saying "taken" told an SDK author neither.
BILLED_ERRORS: dict[int | str, dict] = {
    **ERRORS,
    402: {"model": ErrorResponse, "description": "Not enough credit"},
    409: {
        "model": ErrorResponse,
        "description": (
            "That idempotency key is already spent (`tts_idempotency_spent`) "
            "or belongs to a different request (`tts_idempotency_conflict`)"
        ),
    },
}

# `GET /tts/batch/{job_id}` has neither of the upstream statuses left to
# return: `_advance_job` answers an unreachable speech service — 502, and every
# refusal `tts_client` folds into 503, including "this deployment has no TTS at
# all" — from the stored row instead of failing the read. Publishing them
# anyway would be the defect this round fixed, pointing the other way: a
# contract describing responses the route cannot produce.
READ_ERRORS: dict[int | str, dict] = {
    code: spec for code, spec in ERRORS.items() if code not in (502, 503)
}

# The results route has a 409 of its own, and it is not an idempotency
# answer: it is the refusal to serve the clips of a job that ended having
# been billed for nothing. Named here for the reason `BILLED_ERRORS` names
# its two — "Conflict" tells a client neither which refusal it got nor what
# to do about it, and this one is not retryable at all.
RESULTS_ERRORS: dict[int | str, dict] = {
    **ERRORS,
    409: {
        "model": ErrorResponse,
        "description": (
            "This job ended having been billed for no synthesis, so its "
            "results are not served (`tts_batch_results_unbilled`)"
        ),
    },
}

# Declared so the schema shows a binary body rather than an empty 200. All four
# are reachable from one route; which one arrives follows `audio_format`.
AUDIO_RESPONSE: dict[int | str, dict] = {
    200: {
        "description": "The audio, streamed as it is synthesised.",
        "content": {
            "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
            "audio/wav": {"schema": {"type": "string", "format": "binary"}},
            "audio/ogg": {"schema": {"type": "string", "format": "binary"}},
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}

IdempotencyKeyHeader = Header(
    default=None,
    alias="Idempotency-Key",
    # Imported from `session_service` rather than written here, and enforced
    # rather than merely mentioned. The number this header publishes and the
    # number `open_oneshot` checks are the same number by construction now,
    # because when they were two numbers they disagreed: a key at the length
    # our own docs advertised came back 400 `idempotency_key_too_long`, naming
    # a limit that appeared nowhere in the contract. Enforcing it here also
    # moves the refusal to the validation pass, where it names the header
    # instead of surfacing from inside the billing layer.
    max_length=session_service.MAX_CLIENT_IDEMPOTENCY_KEY,
    description=(
        "Optional, and narrower than the word usually implies: it collapses a "
        "retry that **races** the original, not one that follows it.\n\n"
        "While the first request under a key is still synthesising, a second "
        "one carrying the same key joins that session instead of opening a "
        "second one — one hold, one charge, and both callers are streamed "
        "audio. Once the original has ended, the key is *spent*: sending it "
        "again is `409 tts_idempotency_spent`. Nothing was stored to hand back "
        "— the audio was streamed, not saved — and re-running the GPU under a "
        "session somebody has already paid for would be synthesis nobody is "
        "charged for. A key is spent by the attempt rather than by the charge, "
        "so a call the speech service refused releases its hold, charges "
        "nothing, and spends the key all the same. Retry an ended call with a "
        "fresh key, and expect it to be charged if audio flows.\n\n"
        "Sending a key back with a different request — other text, voice, "
        "quality, format, sample rate or style — is "
        "`409 tts_idempotency_conflict`, whatever state the original is in.\n\n"
        f"At most {session_service.MAX_CLIENT_IDEMPOTENCY_KEY} characters, and "
        "scoped to your account and to this route: the same key on "
        "`POST /tts/batch` is a different request and gets its own session."
    ),
)
VoiceIdPath = Path(description="A `voice_id` from `GET /tts/voices`.", examples=["vc_7f3a1c9e2b"])
JobIdPath = Path(description="Our job id, as `POST /tts/batch` returned it.")
LimitQuery = Query(
    default=None,
    ge=1,
    le=100,
    description="Rows per page. Defaults to 25, capped at 100.",
)
CursorQuery = Query(
    default=None,
    description="The `page.next_cursor` from the previous response.",
)


def _client_facts(request: Request) -> tuple[str | None, str | None]:
    """Who asked, in the two columns `ai_sessions` keeps for it.

    Truncated here rather than left to the database, for the reason
    `tts_batch_service.settle_job` truncates `error`: Postgres answers an
    over-long value with a 500 and SQLite stores the whole thing, so an
    oversized `User-Agent` would either kill the request or make two
    deployments disagree about the same session row.
    """
    client_ip = request.client.host[:64] if request.client else None
    user_agent = request.headers.get("user-agent")
    return client_ip, user_agent[:255] if user_agent else None


async def _job_view(session: AsyncSession, job: TtsBatchJob) -> BatchJobResponse:
    """A job plus the metered session that pays for it.

    None of the money lives on `tts_batch_jobs` — the hold, the pinned price
    book and the debit are all on `ai_sessions` — so the wire shape needs both
    rows. See `app/schemas/tts.py::batch_job_response`.
    """
    ai_session = await session.get(AiSession, job.ai_session_id)
    if ai_session is None:  # pragma: no cover - the FK is non-null and RESTRICT
        raise NotFoundError("No such batch job.", code="tts_batch_not_found")
    return batch_job_response(job, ai_session)


async def _advance_job(
    session: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID
) -> TtsBatchJob:
    """Move the job on if upstream will say anything, and read it either way.

    A read is what advances a job where there is no worker, so `refresh_job`
    makes an upstream call — and an upstream call fails. It used to fail the
    *read* with it: while the speech service was unreachable, `502` was the
    only answer `GET /tts/batch/{job_id}` had, even though every field the
    caller came for — state, counters, characters, the price — was already
    sitting in our own database, and even though the route's description
    promises the row as it stands whenever the poll is not due. An outage that
    stops a job progressing must not also stop its owner from looking at it.

    Only the two upstream-shaped refusals are swallowed. A `400` from
    `refresh_job` means the speech service rejected the job's own payload and
    the job has already been settled and failed on the way past, so that status
    is about this job rather than about the weather and the caller should see
    it. `DELETE` deliberately does not come through here at all: a cancel that
    could not reach upstream has cancelled nothing and must never answer as
    though it had.

    The rollback is the load-bearing half of the fallback. `refresh_job` can
    raise with the session dirty — counters applied, a poll stamped — and
    re-reading through that would answer from half of an abandoned transaction;
    what the caller is owed is the last state that was actually committed. It
    also expires the identity map, which is why the row is re-loaded by id
    rather than reused, and why the ownership scope is passed again with it.
    """
    try:
        return await tts_batch_service.refresh_job(session, job_id)
    except (BadGatewayError, ServiceUnavailableError) as error:
        logger.warning("tts_batch_refresh_unavailable job=%s: %s", job_id, error.code)
        await session.rollback()
        return await tts_batch_service.load_job(session, job_id=job_id, user_id=user_id)


# --- synthesis --------------------------------------------------------------


@router.post(
    "/speech",
    response_class=StreamingResponse,
    responses={**AUDIO_RESPONSE, **BILLED_ERRORS},
    summary="Synthesise speech and stream the audio back",
    description=(
        "Streams audio as the GPU produces it, and charges for the whole text.\n\n"
        "**The bill is final before the first byte.** `text` is priced at its "
        "character count, the credit is held before the speech service is "
        "called at all, and the result is on the response headers: "
        "`X-Synora-Price-Micros` and `X-Synora-Price` for the charge, "
        "`X-Synora-Characters` for the quantity behind it, and "
        "`X-Synora-Session-Id` for the id the hold and the debit appear under "
        "in `GET /wallet/transactions`. Browsers can read them because they are "
        "named in `Access-Control-Expose-Headers`; a response header that is "
        "not exposed there reads as `undefined` with nothing in the console to "
        "say why.\n\n"
        "**Hanging up mid-stream is still billed for the whole text.** By the "
        "time any audio is moving, every character has already been sent to the "
        "GPU, so a disconnect saves no work and is charged as if the audio had "
        "been played to the end. `POST /tts/estimate` is how to find out the "
        "price without committing to it.\n\n"
        "**A stream that stalls is charged on its deadline, and flagged.** A "
        "call that takes its headers and then stops reading has still had "
        "every character of `text` handed to the GPU, so a session that goes "
        "silent past its ten-minute deadline is ended by reconciliation and "
        "charged for the text that was sent, because the text was sent — the "
        "same price `X-Synora-Price` quoted before the first byte. The "
        "session is flagged `disputed` when that happens: a charge made on a "
        "deadline rather than on a delivery is a weaker thing than an "
        "ordinary settlement, and support should be able to find it. Read "
        "the body to the end, or hang up.\n\n"
        "**Failures land on one side of the first byte or the other, and the "
        "two behave differently.** Before it, the speech service's refusal is "
        "still a real status code — `502` when the box is unreachable, `503` "
        "when our own key or tenant quota is the problem, `400` when it "
        "rejected your text — and nothing is charged: the hold goes straight "
        "back. After it, the `200` is already on the wire, so a failure ends "
        "the stream short, is logged rather than reported, and the call is "
        "still billed. Compare the bytes you received against "
        "`X-Synora-Characters` if that distinction matters to you.\n\n"
        "`402` carries `shortfallMicros` and leaves no hold behind. `429` with "
        "`tts_too_many_concurrent` means this account has *started* "
        "`TTS_MAX_CONCURRENT_PER_USER` syntheses inside the last 60 seconds — "
        "it counts starts in a rolling window rather than streams still open, "
        "and that window is the `Retry-After`. The counter lives in Redis, so "
        "a deployment with no `REDIS_URL` does not enforce it at all.\n\n"
        "**An `Idempotency-Key` covers a retry that races the original, not "
        "one that follows it.** While the first request under a key is still "
        "synthesising, a second one carrying that key joins its metered "
        "session rather than opening a second one: the audio is produced "
        "again — storing megabytes of it against the chance of a retry is the "
        "more expensive mistake — but there is no second hold and no second "
        "charge. Once the original has *ended* — settled, or abandoned after "
        "an upstream refusal that charged nothing — the key is spent, and "
        "sending it again is `409 tts_idempotency_spent`: nothing was kept to "
        "hand back, and re-running the GPU under a session somebody has "
        "already paid for would be synthesis nobody is charged for. **A retry "
        "of a call that already ended needs a fresh key, and is charged if "
        "audio flows.** The other refusal is `409 tts_idempotency_conflict`, for a "
        "key sent back with a different request — other text, voice, quality, "
        "format, sample rate or style. Keys are scoped to your account and to "
        "this route, so the same string on `POST /tts/batch` is a different "
        "request with its own session and its own hold.\n\n"
        "`Content-Type` follows `audio_format`: `audio/mpeg`, `audio/wav`, "
        "`audio/ogg` for Opus, and `application/octet-stream` for raw `pcm` — "
        "nothing decodes headerless samples without being told the rate, and it "
        "is on `X-Synora-Sample-Rate`."
    ),
)
async def speech(
    payload: SynthesizeRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: str | None = IdempotencyKeyHeader,
) -> StreamingResponse:
    client_ip, user_agent = _client_facts(request)
    # The ticket's numbers are already on `headers`, and the body settles
    # itself; there is nothing left here for the route to do with it.
    _ticket, headers, body = await tts_service.synthesize(
        session,
        user,
        text=payload.text,
        voice_id=payload.voice_id,
        quality=payload.quality,
        audio_format=payload.audio_format,
        sample_rate=payload.sample_rate,
        style=payload.style,
        idempotency_key=idempotency_key,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    return StreamingResponse(
        body,
        media_type=tts_service.media_type_for(payload.audio_format),
        headers=headers,
    )


@router.post(
    "/estimate",
    response_model=EstimateResponse,
    responses=ERRORS,
    summary="What this text would cost, without charging for it",
    description=(
        "Prices text without opening a session, placing a hold or moving a "
        "single micro-credit.\n\n"
        "The quote comes from the same pricing call the charge does, against "
        "the same active price book, so the two cannot disagree by "
        "construction. The one way to be quoted one number and charged another "
        "is for a new price book to be published between the two requests, "
        "which is why `price_book_version_id` is in the response: compare it "
        "against the one on the session you were charged for.\n\n"
        "`sufficient_credit` is false when `/tts/speech` would answer `402` for "
        "this text right now, and `shortfall_micros` is what to top up by. Both "
        "are a snapshot — a concurrent call that places a hold can turn a "
        "sufficient quote insufficient a moment later, and the balance here is "
        "the same `available_micros` `GET /wallet` reports.\n\n"
        "The character ceiling on this route is the batch one rather than the "
        "streaming one. Pricing a corpus you have not committed to is most of "
        "what an estimate is for, and refusing to quote the only request large "
        "enough to be worth quoting would be an odd place to draw the line."
    ),
)
async def estimate(
    payload: EstimateRequest,
    user: CurrentUser,
    session: SessionDep,
) -> EstimateResponse:
    # Checked even though nothing here calls upstream: quoting work this
    # deployment has no way of doing would be a price for a service that
    # answers 503 the moment anyone accepts it.
    tts_client.require_configured()

    characters = len(payload.text)
    quoted = await session_service.quote(
        session,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
    )
    balance = await wallet_service.get_balance(session, user.id)
    # `get_balance` creates the wallet on first read, exactly as `GET /wallet`
    # does. The trivial commit is what makes that row durable.
    await session.commit()
    return estimate_response(
        quoted, characters=characters, available_micros=balance.available_micros
    )


# --- voices -----------------------------------------------------------------


@router.get(
    "/voices",
    response_model=VoiceListResponse,
    responses=ERRORS,
    summary="Every voice this server can synthesise with",
    description=(
        "The speech service's built-in voices plus every clone registered "
        "through `POST /tts/voices`, in its own order.\n\n"
        "**The list is shared, not per-account.** This server holds one "
        "credential with the speech service and every user synthesises through "
        "it, so a voice cloned by one account is visible to — and usable by — "
        "all of them, and `DELETE` removes it for everyone. Treat "
        "`display_name` as public.\n\n"
        "`has_speaker_embedding: false` means the clone has not finished "
        "processing. Synthesising against it before then is not worth doing: at "
        "best the result comes out in the base voice.\n\n"
        "Fields other than `voice_id` are read defensively and may be absent — "
        "they belong to the speech service's vocabulary, not to this API's "
        "promise, and a field renamed upstream must not turn a voice picker "
        "into a 500."
    ),
)
async def voices(user: CurrentUser) -> VoiceListResponse:  # noqa: ARG001 - auth gate only
    return voice_list_response(await tts_client.list_voices())


@router.post(
    "/voices",
    response_model=VoiceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
    summary="Clone a voice from a short clip",
    description=(
        "Registers a new voice from a 3-30 second recording of a single "
        "speaker.\n\n"
        "Free: no session, no hold, no charge. What it does spend is a slot in "
        "a list every account on this deployment can see and use — "
        "`GET /tts/voices` explains why that list is shared.\n\n"
        "Send the clip base64-encoded. A `data:audio/wav;base64,` prefix and "
        "MIME line breaks are both accepted and stripped, and the payload is "
        "decoded here before anything is sent, so an unparseable clip is a "
        "`422` naming the field rather than several megabytes pushed upstream "
        "to be refused there.\n\n"
        "Clean speech clones better than a long noisy sample; `denoise` is off "
        "by default because it is destructive and makes a studio take worse. "
        "Supplying `transcript` improves the clone, and leaving it out makes "
        "the speech service transcribe the clip itself.\n\n"
        "The response's `has_speaker_embedding` says whether the voice is ready "
        "to synthesise with yet."
    ),
)
async def register_voice(
    payload: RegisterVoiceRequest,
    user: CurrentUser,  # noqa: ARG001 - auth gate only
) -> VoiceResponse:
    # Upstream's spelling, and its convention of empty strings rather than
    # nulls for optional fields — the same difference `tts_service`'s
    # `_upstream_body` exists to absorb for the streaming route.
    return voice_response(
        await tts_client.register_voice(
            {
                "display_name": payload.display_name,
                "audio_base64": payload.audio_base64,
                "audio_format": payload.audio_format,
                "transcript": payload.transcript or "",
                "denoise": payload.denoise,
            }
        )
    )


@router.delete(
    "/voices/{voice_id}",
    response_model=MessageResponse,
    responses=ERRORS,
    summary="Remove a cloned voice",
    description=(
        "Deletes the voice from the speech service.\n\n"
        "**For everyone on this deployment**, not only for the account that "
        "registered it: the voice list is shared, and `GET /tts/voices` says "
        "why. A batch job already accepted upstream keeps synthesising with "
        "it, but a `/tts/speech` call naming it afterwards is refused.\n\n"
        "`404` with `tts_not_found` for an id the speech service does not know, "
        "which is also the answer for a voice somebody else deleted a moment "
        "earlier."
    ),
)
async def delete_voice(
    user: CurrentUser,  # noqa: ARG001 - auth gate only
    voice_id: str = VoiceIdPath,
) -> MessageResponse:
    await tts_client.delete_voice(voice_id)
    return MessageResponse(message="Voice removed.")


# --- batch ------------------------------------------------------------------


@router.post(
    "/batch",
    response_model=BatchJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=BILLED_ERRORS,
    summary="Queue a corpus for synthesis",
    description=(
        "Takes a list of clips, prices the whole job, holds the credit for it "
        "and returns a job to poll.\n\n"
        "**The hold is placed at creation, from the character counts in the "
        "payload.** A batch the wallet cannot cover is a `402` before any GPU "
        "time is spent rather than an hour into the work. What is finally "
        "charged is what the speech service reports having synthesised, which "
        "is lower whenever items failed — nobody pays for audio that was never "
        "produced — and can never be higher than the hold.\n\n"
        "Where the job goes next depends on the deployment, and the answer "
        "does not change the billing. With RabbitMQ configured it is published "
        "and a worker hands it over, which is what keeps a fixed number of "
        "items in flight against a single GPU. Without a broker it is "
        "submitted inline, on this request, before the response is written: a "
        "queue that is merely absent must not become an outage. Either way the "
        "job comes back `queued` or `submitted` and everything after that is "
        "identical.\n\n"
        "**A speech service that is merely busy does not cost you the "
        "corpus.** If the inline hand-over is refused for a reason a retry "
        "could fix — `429`, `502`, `503` — that status is what this request "
        "answers, but the job itself already exists, still `queued`, still "
        "holding both your text and the credit for it. It is in "
        "`GET /tts/batch`, and reading it with `GET /tts/batch/{job_id}` hands "
        "it over on your behalf. A refusal of the payload itself — a `400` — is "
        "final instead, because the same bytes get the same answer from the "
        "same validator: the job is failed on the spot and the hold goes back "
        "in full.\n\n"
        "`idempotency_key` in the body returns the job it already created "
        "instead of pricing and holding for a second copy of the same corpus — "
        "even when the corpus differs, since the answer is already priced, held "
        "and possibly half-synthesised. It is scoped to your account and to "
        "this route, so the same string on `POST /tts/speech` is a different "
        "request rather than a collision. Omitting it means a retried POST is a "
        "second job with a second hold.\n\n"
        "**A batch key does not expire the way the streaming one does.** "
        "`POST /tts/speech` refuses a key whose synthesis already finished, "
        "because a stream leaves nothing behind to hand back; a job row does, "
        "so this key keeps returning that same job — including after it has "
        "settled. Use a fresh key for a new corpus. The one refusal here is "
        "`409 tts_batch_idempotency_conflict`: the key names a metered session "
        "with no job on it, which in practice means two requests raced on one "
        "key before the first job row landed. Retry it.\n\n"
        "Poll `GET /tts/batch/{job_id}` until `is_terminal`. `DELETE` cancels "
        "and settles at whatever was reported up to that moment.\n\n"
        "**A job that outlives `TTS_BATCH_MAX_POLL_SECONDS` is given up on**: "
        "settled at the last usage the speech service admitted to, marked "
        "`expired` and flagged for review. The clock starts when the speech "
        "service accepted the job, or — for one it never accepted — when this "
        "request created it and took the credit, so a job that was refused "
        "upstream and left `queued` has a deadline of its own to reach, and "
        "gets its hold back charged nothing when it does. Holding a "
        "customer's credit indefinitely is the worse failure.\n\n"
        "**Reaching that deadline takes something running.** The clock is "
        "read by whatever next looks at the job — a worker's poll, a read of "
        "`GET /tts/batch/{job_id}`, or the batch sweep inside "
        "`POST /admin/reconcile`, which is the only one of the three that "
        "happens on nobody's behalf. So the promise above holds whenever "
        "reconciliation runs; on a deployment with no worker and no schedule "
        "behind that route, a job nobody reads again keeps its hold until "
        "somebody does. Poll your jobs to the end, or read them once more "
        "after giving up on them."
    ),
)
async def create_batch(
    payload: BatchCreateRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> BatchJobResponse:
    client_ip, user_agent = _client_facts(request)
    job = await tts_batch_service.create_job(
        session, user, payload, client_ip=client_ip, user_agent=user_agent
    )
    return await _job_view(session, job)


@router.get(
    "/batch",
    response_model=BatchJobPageResponse,
    responses=ERRORS,
    summary="Your batch jobs, newest first",
    description=(
        "Cursor-paginated exactly as `GET /wallet/transactions` is: pass "
        "`page.next_cursor` back as `?cursor=` to walk backwards through time, "
        "and stop when `page.has_more` is false. Offsets are wrong for this "
        "list for the same reason they are wrong for the statement — it is "
        "append-only and read newest-first, so a job created between two "
        "requests shifts the window and the reader sees one row twice while "
        "missing another.\n\n"
        "**This route never asks the speech service anything.** A page of "
        "twenty-five jobs would otherwise become twenty-five upstream calls on "
        "behalf of somebody who only wanted a list. Reading a single job with "
        "`GET /tts/batch/{job_id}` is what advances it, so on a deployment with "
        "no worker this list can show a job as `submitted` that has in fact "
        "already finished.\n\n"
        "Only your own jobs appear, and the money on each one — `estimated`, "
        "`reserved`, `settled` — comes from the metered session behind it, "
        "which is the same session id the statement reports."
    ),
)
async def batch_jobs(
    user: CurrentUser,
    session: SessionDep,
    limit: int | None = LimitQuery,
    cursor: str | None = CursorQuery,
) -> BatchJobPageResponse:
    page_size = clamp_limit(limit)
    position = decode_cursor(cursor)

    # Keyset on (created_at, id), and the id is not decoration: `created_at`
    # comes from `func.now()`, which SQLite resolves to whole seconds, so two
    # jobs submitted in the same second share a sort key. The id breaks the tie
    # and keeps the ordering total, which is what stops a cursor from skipping
    # or repeating a row.
    #
    # `tuple_()` and not a plain Python tuple: `(col_a, col_b) < (x, y)` would
    # be evaluated by Python, comparing a SQL expression object for truthiness
    # instead of emitting a row-value comparison.
    #
    # Joined rather than one lookup per row: `uq_tts_batch_jobs_ai_session_id`
    # makes it exactly one session per job, so the whole page is one query.
    query = (
        select(TtsBatchJob, AiSession)
        .join(AiSession, AiSession.id == TtsBatchJob.ai_session_id)
        .where(TtsBatchJob.user_id == user.id)
    )
    if position is not None:
        query = query.where(
            tuple_(TtsBatchJob.created_at, TtsBatchJob.id)
            < tuple_(position.created_at, position.row_id)
        )
    query = query.order_by(
        TtsBatchJob.created_at.desc(), TtsBatchJob.id.desc()
    ).limit(page_size + 1)

    rows = list((await session.execute(query)).all())
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = (
        Cursor(created_at=rows[-1][0].created_at, row_id=rows[-1][0].id).encode()
        if has_more and rows
        else None
    )

    return BatchJobPageResponse(
        jobs=[batch_job_response(job, ai_session) for job, ai_session in rows],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page_size),
    )


@router.get(
    "/batch/{job_id}",
    response_model=BatchJobResponse,
    responses=READ_ERRORS,
    summary="One batch job, refreshed against the speech service",
    description=(
        "**Reading is what advances a job when nothing else will.** This route "
        "asks the speech service for the job's counters, records them, and "
        "settles the wallet if it has finished — which on a deployment without "
        "RabbitMQ is the only thing that ever does. A job the speech service "
        "never received is resubmitted here, which is how a queue message lost "
        "to a broker restart recovers — unless it is already past "
        "`TTS_BATCH_MAX_POLL_SECONDS`, in which case the deadline wins and the "
        "read settles it `expired` instead. That order is deliberate: a job "
        "upstream keeps refusing would otherwise answer every read with the "
        "same failing submit and never reach the deadline that hands its hold "
        "back.\n\n"
        "The upstream poll is rate-limited internally by "
        "`TTS_BATCH_POLL_SECONDS`, so polling this in a tight loop costs us "
        "nothing and tells you nothing: until the next poll is due it returns "
        "the row as it stands.\n\n"
        "**A speech service that cannot be reached is not an error on this "
        "route.** If the poll or the resubmission fails, the failure is logged "
        "on our side and you get the job exactly as we last recorded it — the "
        "same body a poll that was not yet due returns, and deliberately "
        "indistinguishable from it. Everything you came for is ours, not "
        "theirs: `state`, the counters, `submitted_characters` and every "
        "amount are read from our database. Keep polling; the job resumes "
        "advancing the moment the speech service does. `DELETE` is the one "
        "route that still fails on an outage, because a cancel that never "
        "arrived has cancelled nothing.\n\n"
        "A `400` is the exception and is still returned: it means the speech "
        "service refused this job's own payload rather than that it is having "
        "a bad afternoon, and the job has already been failed and its hold "
        "released on the way past. Do not retry that one.\n\n"
        "Stop when `is_terminal` is true. At that point the hold is gone, "
        "`settled_micros` is what was charged and nothing further will change. "
        "`billed_characters` below `submitted_characters` means items failed "
        "and were not billed for; `error` says what went wrong when something "
        "did.\n\n"
        "Someone else's job id is a `404` — the same answer an id that never "
        "existed gets."
    ),
)
async def batch_job(
    user: CurrentUser,
    session: SessionDep,
    job_id: uuid.UUID = JobIdPath,
) -> BatchJobResponse:
    # Scoped first, refreshed second. `refresh_job` takes an id and does not
    # know whose it is, so the ownership check has to happen before it can make
    # an upstream call on a stranger's job.
    await tts_batch_service.load_job(session, job_id=job_id, user_id=user.id)
    job = await _advance_job(session, job_id=job_id, user_id=user.id)
    return await _job_view(session, job)


@router.delete(
    "/batch/{job_id}",
    response_model=BatchJobResponse,
    responses=ERRORS,
    summary="Cancel a batch job and settle it",
    description=(
        "Stops the job at the speech service and charges for whatever it "
        "reports having produced by then.\n\n"
        "Not a refund: work already done is billed for. A job cancelled before "
        "it was ever submitted charges nothing, because there is nothing to "
        "charge for, and its hold goes back in full.\n\n"
        "**A cancel that cannot reach the speech service fails with `502` "
        "rather than succeeding locally.** If we cannot stop the card we cannot "
        "stop the bill either, and releasing the hold anyway would leave us "
        "synthesising audio nobody can be charged for. Retry it.\n\n"
        "Cancelling a job that has already reached a terminal state returns it "
        "untouched rather than erroring, so a cancel racing the poller is safe."
    ),
)
async def cancel_batch_job(
    user: CurrentUser,
    session: SessionDep,
    job_id: uuid.UUID = JobIdPath,
) -> BatchJobResponse:
    job = await tts_batch_service.load_job(session, job_id=job_id, user_id=user.id)
    return await _job_view(session, await tts_batch_service.cancel_job(session, job))


@router.get(
    "/batch/{job_id}/results",
    response_model=BatchResultsResponse,
    responses=RESULTS_ERRORS,
    summary="Per-item results for a batch job",
    description=(
        "Which clips rendered, how long each came out, and what the speech "
        "service stored them as. One entry per submitted item, keyed on the id "
        "you gave it; failed items are present with `ok: false` and an "
        "`error`.\n\n"
        "`state` is read from our own job row rather than from the results "
        "payload, so results and state cannot disagree about a job that settled "
        "between two reads. Like `GET /tts/batch/{job_id}`, this refreshes the "
        "job first — which is what advances it where there is no worker — and "
        "like it, a refresh the speech service refuses is logged and skipped "
        "rather than failed. The results themselves are the one thing here we "
        "do not hold a copy of, so an outage that still lets you read the "
        "job's state can leave this route answering `502` for its body. Read "
        "`GET /tts/batch/{job_id}` in the meantime.\n\n"
        "`path` is the speech service's own storage handle, not a URL this API "
        "serves. Quote it in a support request; there is no download route "
        "here.\n\n"
        "**A job that ended having been billed nothing has no results "
        "here** — `409 tts_batch_results_unbilled`. A job cancelled before "
        "it was billed settles at zero characters and its hold goes back in "
        "full, so whatever the speech service happened to render in the "
        "meantime was never paid for, and this route does not hand it over. "
        "A job billed even partially keeps its results, including one that "
        "failed after being charged for the items that did run.\n\n"
        "A live job the speech service has not accepted yet has no results "
        "and comes back with an empty array rather than a `404`."
    ),
)
async def job_results(
    user: CurrentUser,
    session: SessionDep,
    job_id: uuid.UUID = JobIdPath,
) -> BatchResultsResponse:
    await tts_batch_service.load_job(session, job_id=job_id, user_id=user.id)
    job = await _advance_job(session, job_id=job_id, user_id=user.id)
    # The results are the product. A route that hands them over therefore has
    # to ask the question `/tts/speech` asks before it opens a socket — has
    # this been paid for? — and this one did not: it keyed off
    # `upstream_job_id` alone and ignored state entirely. So a job cancelled in
    # the window before its submit landed, settled at zero characters with its
    # hold returned in full exactly as the cancel contract promises, still
    # handed back every clip the GPU had finished before the DELETE arrived.
    # Free synthesis, through the one route nobody thinks of as a delivery
    # mechanism because it looks like metadata.
    #
    # Both halves of the condition are load-bearing. Terminal, because a live
    # job has not been charged yet for the honest reason that it has not
    # finished, and refusing those would break the ordinary poll. Billed
    # nothing, because anything above zero means this corpus was paid for as
    # far as it got — a partial failure, or a job that failed after being
    # charged for the items that did run — and the clips behind that charge are
    # owed to the caller.
    if job.is_terminal and job.billed_characters <= 0:
        raise ConflictError(
            "This job ended without being billed for any synthesis, so its "
            "results are not available.",
            code="tts_batch_results_unbilled",
        )
    if not job.upstream_job_id:
        # Still ours alone: nothing has been synthesised, so there is nothing
        # upstream to ask about. An empty array with our own state is the
        # truthful answer, and a 404 here would read as "no such job".
        return batch_results_response(job, {})
    return batch_results_response(
        job, await tts_client.batch_results(job.upstream_job_id)
    )


# --- recordings -------------------------------------------------------------

RecordingIdPath = Path(description="A recording id, as `GET /tts/recordings` returned it.")

RECORDING_ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "No such recording"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}


@router.get(
    "/recordings",
    response_model=RecordingPageResponse,
    responses=RECORDING_ERRORS,
    summary="Every synthesis this account has kept",
    description=(
        "What was said, in whose voice, and when — newest first, "
        "cursor-paginated exactly as `GET /tts/batch` is.\n\n"
        "**The audio is a separate request.** `GET /tts/recordings/{id}/audio` "
        "returns the file; a page of twenty-five clips inlined here would be "
        "tens of megabytes that almost every caller discards.\n\n"
        "A synthesis is recorded when it delivered audio and was charged for "
        "it. Three cases are therefore absent by design: a call upstream "
        "refused before the first byte, which charged nothing; a retry under a "
        "spent `Idempotency-Key`, which re-synthesised the same text and is "
        "already here under the original; and everything synthesised while "
        "`RECORDINGS_ENABLED` was false.\n\n"
        "**These rows do not expire.** They hold the exact text that was "
        "submitted, for as long as the account exists or until "
        "`DELETE /tts/recordings/{id}`."
    ),
)
async def recordings(
    user: CurrentUser,
    session: SessionDep,
    limit: int | None = LimitQuery,
    cursor: str | None = CursorQuery,
) -> RecordingPageResponse:
    page_size = clamp_limit(limit)
    rows, has_more = await tts_recording_service.page(
        session, user_id=user.id, limit=page_size, position=decode_cursor(cursor)
    )
    next_cursor = (
        Cursor(created_at=rows[-1].created_at, row_id=rows[-1].id).encode()
        if has_more and rows
        else None
    )
    return RecordingPageResponse(
        recordings=[recording_response(row) for row in rows],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page_size),
    )


@router.get(
    "/recordings/{recording_id}",
    response_model=RecordingResponse,
    responses=RECORDING_ERRORS,
    summary="One kept synthesis",
    description=(
        "Somebody else's recording is a `404` rather than a `403`, for the "
        "reason a batch job is: a 403 confirms the id exists, which is the one "
        "thing a stranger walking the id space is trying to learn."
    ),
)
async def recording(
    user: CurrentUser,
    session: SessionDep,
    recording_id: uuid.UUID = RecordingIdPath,
) -> RecordingResponse:
    return recording_response(
        await tts_recording_service.require_own(
            session, user_id=user.id, recording_id=recording_id
        )
    )


@router.get(
    "/recordings/{recording_id}/audio",
    responses={
        **RECORDING_ERRORS,
        200: {
            "content": {"audio/mpeg": {}, "audio/wav": {}, "audio/opus": {}},
            "description": "The audio exactly as it was delivered.",
        },
    },
    summary="The audio of a kept synthesis",
    description=(
        "The same bytes the original `POST /tts/speech` streamed, byte for "
        "byte — the file is stored under the sha256 in the recording, so it "
        "can be verified rather than trusted.\n\n"
        "Costs nothing and charges nothing: the synthesis was paid for when it "
        "happened, and playing it back does not reach the speech service at "
        "all.\n\n"
        "A `404` here with the recording still listed means the row survived "
        "and the file did not — a restore that missed `RECORDINGS_DIR`, or a "
        "disk that was cleared. The row is left in place rather than swept, "
        "because it is still the record that the synthesis happened."
    ),
)
async def recording_audio(
    user: CurrentUser,
    session: SessionDep,
    recording_id: uuid.UUID = RecordingIdPath,
) -> FileResponse:
    row = await tts_recording_service.require_own(
        session, user_id=user.id, recording_id=recording_id
    )
    path = recording_store.path_for(row.storage_key)
    if path is None or not path.exists():
        logger.warning(
            "recording_file_missing id=%s key=%s", row.id, row.storage_key
        )
        raise NotFoundError(
            "The audio for this recording is no longer on disk.",
            code="recording_audio_missing",
        )
    return FileResponse(
        path,
        media_type=tts_service.media_type_for(row.audio_format),
        # Named for the recording rather than for its content address, because
        # a browser puts this in the user's downloads folder and `a1b2c3….wav`
        # is not a filename anybody can find again.
        filename=f"synora-{row.id}.{recording_store.extension_for(row.audio_format)}",
    )


@router.delete(
    "/recordings/{recording_id}",
    response_model=MessageResponse,
    responses=RECORDING_ERRORS,
    summary="Erase one kept synthesis",
    description=(
        "Removes the row and, once no other recording names the same audio, "
        "the file.\n\n"
        "**The file is shared when the audio is identical.** It is stored under "
        "the sha256 of its own bytes, so the same text in the same voice at the "
        "same settings is one file however many times it was synthesised. "
        "Deleting yours never empties somebody else's playback.\n\n"
        "Nothing about the charge changes: `usage_events`, the ledger and "
        "`GET /usage` are the record of what was billed, and this route does "
        "not touch them. Deleting a recording erases what was said, not that it "
        "was paid for."
    ),
)
async def delete_recording(
    user: CurrentUser,
    session: SessionDep,
    recording_id: uuid.UUID = RecordingIdPath,
) -> MessageResponse:
    await tts_recording_service.delete(
        session, user_id=user.id, recording_id=recording_id
    )
    return MessageResponse(message="Recording deleted.")
