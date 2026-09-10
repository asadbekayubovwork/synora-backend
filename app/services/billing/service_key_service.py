"""Provisioning and checking the credentials microservices call us with.

The secret is never stored — see `app/core/signing.py` for why — so this
module deals only in the public half plus its metadata. Minting a key returns
the secret once; there is no endpoint that can hand it back, and losing it
means minting a new key and revoking the old one, which is the correct
recovery path anyway.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import signing
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.db.base import utcnow
from app.models.billing_enums import BillingService
from app.models.service_api_key import ServiceApiKey

logger = logging.getLogger("synora.billing")

# Every key expires, so rotation is forced rather than remembered. Ninety days
# is long enough not to be a nuisance and short enough that a leaked key from a
# laptop stolen last quarter is already dead.
DEFAULT_KEY_LIFETIME_DAYS = 90

# What a key is allowed to do. Narrow on purpose: a compromised usage-reporting
# key should not be able to open sessions, and vice versa.
SCOPE_SESSIONS_AUTHORIZE = "sessions:authorize"
SCOPE_SESSIONS_REPORT = "sessions:report"
SCOPE_USAGE_WRITE = "usage:write"
SCOPE_HEALTH_READ = "health:read"

ALL_SCOPES = (
    SCOPE_SESSIONS_AUTHORIZE,
    SCOPE_SESSIONS_REPORT,
    SCOPE_USAGE_WRITE,
    SCOPE_HEALTH_READ,
)


@dataclass(frozen=True)
class MintedKey:
    """The one and only time the secret is visible."""

    key_id: str
    secret: str
    label: str
    service: BillingService | None
    scopes: tuple[str, ...]
    expires_at: object | None

    @property
    def presented(self) -> str:
        """The single string a microservice puts in its own configuration."""
        return f"{self.key_id}.{self.secret}"


async def mint(
    session: AsyncSession,
    *,
    label: str,
    service: BillingService | None,
    scopes: tuple[str, ...] = ALL_SCOPES,
    lifetime_days: int | None = DEFAULT_KEY_LIFETIME_DAYS,
    created_by_user_id: uuid.UUID | None = None,
) -> MintedKey:
    unknown = set(scopes) - set(ALL_SCOPES)
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")

    key_id = signing.new_key_id(service.value if service else None)
    expires_at = utcnow() + timedelta(days=lifetime_days) if lifetime_days else None

    session.add(
        ServiceApiKey(
            label=label,
            service=service,
            key_id=key_id,
            key_version=1,
            scopes=",".join(scopes),
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
        )
    )
    await session.flush()
    logger.info("Minted service key %s for %s", key_id, service.value if service else "any service")

    return MintedKey(
        key_id=key_id,
        secret=signing.derive_secret(key_id),
        label=label,
        service=service,
        scopes=scopes,
        expires_at=expires_at,
    )


async def revoke(session: AsyncSession, *, key_id: str) -> None:
    now = utcnow()
    result = await session.execute(
        update(ServiceApiKey)
        .where(ServiceApiKey.key_id == key_id, ServiceApiKey.revoked_at.is_(None))
        .values(revoked_at=now, is_active=False, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise NotFoundError("No such active service key.", code="service_key_unknown")
    logger.warning("Revoked service key %s", key_id)


async def load_usable(session: AsyncSession, key_id: str) -> ServiceApiKey:
    """The key, or the 401 that says exactly why not.

    The reasons are kept apart — unknown, revoked, expired — because the other
    team is going to be reading these codes off a log at three in the morning,
    and "unauthorized" tells them nothing about whether to rotate a key or fix
    a typo. None of the three reveals anything a caller holding the key id does
    not already know.
    """
    key = (
        await session.execute(select(ServiceApiKey).where(ServiceApiKey.key_id == key_id))
    ).scalar_one_or_none()

    if key is None:
        raise UnauthorizedError("Unknown service key.", code="service_key_unknown")
    if key.revoked_at is not None or not key.is_active:
        raise UnauthorizedError("This service key was revoked.", code="service_key_revoked")
    if key.is_expired:
        raise UnauthorizedError("This service key has expired.", code="service_key_expired")
    return key


async def touch(session: AsyncSession, *, key_id: str, ip: str | None) -> None:
    """Record that a key was used, and commit it.

    Committing inside what is otherwise an authentication step needs
    justifying, and the justification is that this fact is independent of
    whatever the request goes on to do. "This key was used at 03:14 from this
    address" is true whether the request then succeeds, gets refused for
    insufficient credit, or dies on a bad payload — and it is *most* worth
    having in the cases that fail. Leaving it to the route would mean it is
    recorded only on the happy path, and a `last_used_at` that only counts
    successes cannot answer "is anything still using this key?", which is the
    one question it exists for.

    This is the same reasoning as `otp_service.consume_otp`, which commits an
    attempt counter before raising so a wrong guess is still counted.

    Safe to commit here because the dependency runs before the route body, so
    nothing else is staged on the session yet.

    It is a bulk UPDATE rather than an ORM round trip because this is the
    hottest path in the system. The plan is to buffer the counter in Redis and
    flush it from the sweeper; until that exists, one cheap statement beats a
    column that is always null.
    """
    now = utcnow()
    await session.execute(
        update(ServiceApiKey)
        .where(ServiceApiKey.key_id == key_id)
        .values(
            last_used_at=now,
            last_used_ip=ip,
            use_count=ServiceApiKey.use_count + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
