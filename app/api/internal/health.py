from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import HealthReaderDep, SessionDep
from app.core import signing
from app.core.cache import get_cache
from app.core.config import settings
from app.db.base import utcnow
from app.models.billing_enums import PriceBookStatus
from app.schemas.auth import ErrorResponse
from app.schemas.internal import EchoSignatureResponse, InternalHealthResponse

router = APIRouter(tags=["Internal"])

ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Signature or key rejected"},
    403: {"model": ErrorResponse, "description": "Key not scoped for this"},
    404: {"model": ErrorResponse, "description": "Internal API disabled"},
}


@router.get(
    "/health",
    response_model=InternalHealthResponse,
    responses=ERRORS,
    summary="Is the control plane ready to meter?",
    description=(
        "Poll this every 30 seconds. Two things to act on:\n\n"
        "* `state` — `degraded` means Redis is unreachable, so the gating "
        "counters are being served from Postgres. Usage reporting still works; "
        "expect a little more latency.\n"
        "* `server_time` — alarm if it differs from your own clock by more than "
        "two seconds. Signed requests are rejected outside a "
        f"{settings.internal_signature_window_seconds}-second window, and clock drift "
        "is the single commonest reason an integration that worked yesterday "
        "stops working today.\n\n"
        "`price_book_version` changes when prices are republished. A session "
        "keeps the version it was authorized under, so a change mid-call never "
        "re-rates that call — but a new session will quote differently."
    ),
)
async def health(caller: HealthReaderDep, session: SessionDep) -> InternalHealthResponse:  # noqa: ARG001
    from sqlalchemy import select

    from app.models.price_book import PriceBookVersion

    version = (
        await session.execute(
            select(PriceBookVersion.version)
            .where(PriceBookVersion.status == PriceBookStatus.ACTIVE)
            .order_by(PriceBookVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    cache = get_cache()
    return InternalHealthResponse(
        state="ready" if (version is not None and cache.is_available) else "degraded",
        price_book_version=version,
        redis="up" if cache.is_available else "down",
        server_time=utcnow(),
    )


@router.post(
    "/debug/echo-signature",
    response_model=EchoSignatureResponse,
    responses=ERRORS,
    summary="Show the exact string we signed (development and staging only)",
    description=(
        "Getting the canonical string byte-identical across two languages is "
        "where an HMAC integration actually loses its time: one side sorts the "
        "query, the other does not; one hashes the raw body, the other hashes "
        "re-serialised JSON. Rather than let that be discovered by trial and "
        "error against a 401, this returns what we computed.\n\n"
        "Send whatever body and headers you like. If `signature_matched` is "
        "false, diff your canonical string against `canonical` — the difference "
        "is the bug.\n\n"
        "Returns **404 outside development and staging**, so it cannot become a "
        "production oracle. It is safe as far as it goes — you have to hold a "
        "valid key to reach it at all — but a signing helper that lives in "
        "production is a signing helper somebody will point at production."
    ),
)
async def echo_signature(
    request: Request, caller: HealthReaderDep
) -> EchoSignatureResponse:
    from app.core.exceptions import NotFoundError

    if not (settings.is_development or settings.environment.lower() == "staging"):
        raise NotFoundError("Not found.", code="not_found")

    body = await request.body()
    timestamp = request.headers.get(signing.HEADER_TIMESTAMP, "")
    nonce = request.headers.get(signing.HEADER_NONCE, "")
    canonical = signing.canonical_string(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        timestamp=timestamp,
        nonce=nonce,
        key_id=caller.key_id,
        body=body,
    )
    return EchoSignatureResponse(
        canonical=canonical,
        body_sha256=signing.body_digest(body),
        key_id=caller.key_id,
        scopes=sorted(caller.scopes),
        # True by definition here — the dependency already verified it — but
        # stated so a caller comparing responses has something unambiguous.
        signature_matched=True,
        server_time=utcnow(),
    )
