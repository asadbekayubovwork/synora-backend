from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache, signing
from app.core.cache import get_cache
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import decode_token
from app.db.base import utcnow
from app.db.session import get_session
from app.models.billing_enums import BillingService
from app.models.user import User
from app.services.billing import service_key_service

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False so a missing header raises our own 401 body rather than
# FastAPI's, keeping every error response the same shape.
bearer_scheme = HTTPBearer(auto_error=False, description="Paste the `access_token` from login.")
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def _user_from_token(session: AsyncSession, token: str, token_type: str) -> User:
    try:
        payload = decode_token(token, token_type)  # type: ignore[arg-type]
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Your session has expired. Please sign in again.", code="token_expired") from None
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid authentication token.", code="token_invalid") from None

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (ValueError, KeyError):
        raise UnauthorizedError("Invalid authentication token.", code="token_invalid") from None

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise UnauthorizedError("This account no longer exists.", code="user_not_found")
    if not user.is_active:
        raise ForbiddenError("This account has been disabled.", code="account_disabled")
    if not user.is_verified:
        raise ForbiddenError("Verify your email before continuing.", code="email_not_verified")

    return user


async def get_current_user(session: SessionDep, credentials: CredentialsDep) -> User:
    if credentials is None:
        raise UnauthorizedError("Authentication is required.", code="not_authenticated")
    return await _user_from_token(session, credentials.credentials, "access")


async def get_user_from_refresh_token(session: AsyncSession, token: str) -> User:
    return await _user_from_token(session, token, "refresh")


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin(user: CurrentUser) -> User:
    """The caller, if they may touch the admin routes.

    Composed on top of `CurrentUser`, so the active-and-verified gates come
    for free and there is one place that decides who is signed in.

    `is_superuser` is a column rather than an `ADMIN_EMAILS` allowlist for
    three reasons: a Telegram-only account has no email and so could never be
    an admin; an email allowlist would turn any future "change my email"
    endpoint into privilege escalation; and every admin action here moves money
    and needs attributing to a real user id, which the ledger records.
    """
    if not user.is_superuser:
        # Deliberately the same message an unknown route would give. There is
        # no reason to confirm to a caller that an admin surface exists here.
        raise ForbiddenError("This endpoint is not available.", code="admin_required")
    return user


AdminUser = Annotated[User, Depends(get_current_admin)]


# --- Service-to-service auth -----------------------------------------------


@dataclass(frozen=True)
class InternalCaller:
    """A microservice, once its request has been verified."""

    key_id: str
    label: str
    service: BillingService | None
    scopes: frozenset[str]

    def may_act_for(self, service: BillingService) -> bool:
        """A key bound to one service cannot act for another.

        So a compromised TTS credential cannot open voice-agent sessions, which
        is the difference between one service's blast radius and all of them.
        """
        return self.service is None or self.service is service


async def verify_internal_caller(request: Request, session: SessionDep) -> InternalCaller:
    """Check the HMAC signature on an internal request.

    The order of the checks is deliberate: cheapest and least informative
    first, so a scanner learns nothing and costs us nothing. Every failure has
    its own `code`, because the other team will be reading these off a log and
    "unauthorized" does not tell them whether to rotate a key or fix a typo.

    **`await request.body()` is load-bearing.** The signature covers a digest
    of the raw bytes, and Starlette caches the result on the request, which is
    the same cache FastAPI's own body parsing reads from — so the route's
    Pydantic model still binds normally afterwards. Switching this to
    `.stream()` would consume the body and silently break every route behind
    this dependency.
    """
    if not settings.internal_api_enabled:
        raise NotFoundError("Not found.", code="not_found")

    key_id = request.headers.get(signing.HEADER_KEY_ID, "")
    timestamp = request.headers.get(signing.HEADER_TIMESTAMP, "")
    nonce = request.headers.get(signing.HEADER_NONCE, "")
    presented = request.headers.get(signing.HEADER_SIGNATURE, "")

    if not (key_id and timestamp and nonce and presented):
        raise UnauthorizedError(
            "This endpoint requires a signed request.", code="signature_missing"
        )
    if not signing.is_valid_key_id(key_id):
        raise UnauthorizedError("Unknown service key.", code="service_key_unknown")

    key = await service_key_service.load_usable(session, key_id)

    now = int(utcnow().timestamp())
    try:
        skew = abs(now - int(timestamp))
    except ValueError:
        raise UnauthorizedError(
            "The timestamp is not an integer number of seconds.",
            code="signature_timestamp_skew",
            extra={"serverTime": now},
        ) from None
    if skew > settings.internal_signature_window_seconds:
        raise UnauthorizedError(
            f"The request timestamp is {skew}s from ours; the window is "
            f"{settings.internal_signature_window_seconds}s. Check the clock on the caller.",
            code="signature_timestamp_skew",
            extra={"serverTime": now},
        )
    if not signing.is_valid_nonce(nonce):
        raise UnauthorizedError(
            "The nonce must be 16-128 URL-safe characters.", code="signature_nonce_invalid"
        )

    body = await request.body()
    canonical = signing.canonical_string(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        timestamp=timestamp,
        nonce=nonce,
        key_id=key_id,
        body=body,
    )
    if not signing.verify(signing.derive_secret(key_id, version=key.key_version), canonical, presented):
        raise UnauthorizedError("The signature does not match.", code="signature_invalid")

    # Last, because it has a side effect and there is no point spending it on a
    # request that was going to fail anyway. Fails open without Redis — the
    # timestamp window above plus the per-report idempotency keys are the real
    # correctness mechanisms, and refusing money-bearing reports because there
    # is nowhere to remember a nonce would be the worse trade.
    fresh = await get_cache().claim_once(
        cache.nonce_key(key_id, nonce),
        settings.internal_signature_window_seconds * 2,
    )
    if not fresh:
        raise UnauthorizedError("This request was already seen.", code="signature_replayed")

    await service_key_service.touch(
        session, key_id=key_id, ip=request.client.host if request.client else None
    )
    return InternalCaller(
        key_id=key.key_id,
        label=key.label,
        service=key.service,
        scopes=frozenset(key.scope_list),
    )


InternalCallerDep = Annotated[InternalCaller, Depends(verify_internal_caller)]


def internal_scope(*required: str) -> Callable[..., Awaitable[InternalCaller]]:
    """A dependency that also insists on particular scopes.

    Written as a factory so each internal route names what it needs at the
    route, where it is visible in the OpenAPI schema and in review, rather than
    checking inside the handler where it is easy to forget.
    """

    async def dependency(caller: InternalCallerDep) -> InternalCaller:
        if not set(required) <= caller.scopes:
            missing = sorted(set(required) - caller.scopes)
            raise ForbiddenError(
                f"This key is not scoped for {', '.join(missing)}.",
                code="service_key_forbidden",
            )
        return caller

    return dependency


SessionAuthorizerDep = Annotated[
    InternalCaller, Depends(internal_scope(service_key_service.SCOPE_SESSIONS_AUTHORIZE))
]
SessionReporterDep = Annotated[
    InternalCaller, Depends(internal_scope(service_key_service.SCOPE_SESSIONS_REPORT))
]
UsageWriterDep = Annotated[
    InternalCaller, Depends(internal_scope(service_key_service.SCOPE_USAGE_WRITE))
]
HealthReaderDep = Annotated[
    InternalCaller, Depends(internal_scope(service_key_service.SCOPE_HEALTH_READ))
]
