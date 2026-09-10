"""Credentials for a microservice calling *us*.

The key is presented as `syn_<env>_<key_id>_<secret>`. Only `key_id` is stored;
the secret half is **derived** from `INTERNAL_KEY_SECRET`:

    secret(key_id) = HMAC-SHA256(INTERNAL_KEY_SECRET, "synora-internal-key|v1|" + key_id)

That is a deliberate resolution of a genuine conflict. Requests are signed with
HMAC, which needs the plaintext secret on the server to verify — so "store only
a hash of the API key" and "verify an HMAC signature" cannot both be true.
Deriving gets the property that mattered: a database dump yields no usable
credential, because the row holds nothing secret at all. Revocation stays a
single `UPDATE` — instant, no deploy.

The cost is stated plainly: compromising `INTERNAL_KEY_SECRET` compromises
every service key. That is the same blast radius as `JWT_SECRET`, which this
codebase already accepts and guards in `assert_production_ready()`. The
strictly better answer is Ed25519 with only a public key stored, and it is the
upgrade path — worth taking the day `cryptography` becomes a dependency for
some other reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, as_utc, utcnow
from app.models.billing_enums import BillingService


class ServiceApiKey(Base):
    __tablename__ = "service_api_keys"
    __table_args__ = (
        UniqueConstraint("key_id", name="uq_service_api_keys_key_id"),
        CheckConstraint("use_count >= 0", name="use_count_nonneg"),
        CheckConstraint("key_version > 0", name="key_version_positive"),
        Index("ix_service_api_keys_is_active", "is_active"),
    )

    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # Null means the key may act for any service. Set it in practice, so a
    # compromised TTS key cannot open voice-agent sessions.
    service: Mapped[BillingService | None] = mapped_column(
        Enum(BillingService, native_enum=False, length=32), nullable=True
    )
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which derivation generation this key belongs to, so the master secret
    # can be rotated with an overlap window rather than a flag day.
    key_version: Mapped[int] = mapped_column(
        BigInteger, default=1, server_default=text("1"), nullable=False
    )

    # Comma separated and read through `scope_list`, matching how `Settings`
    # carries `cors_origins`.
    # No `server_default`: an empty-string default reflects back differently
    # on SQLite than on Postgres, and the ORM supplies it on every insert.
    scopes: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    # Every key gets one, so rotation is forced rather than remembered.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Written from a Redis counter by the sweeper, not on every call: the
    # accuracy of a usage counter is not worth a row lock per request.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    use_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    @property
    def scope_list(self) -> list[str]:
        return [scope.strip() for scope in self.scopes.split(",") if scope.strip()]

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and as_utc(self.expires_at) <= utcnow()

    @property
    def is_usable(self) -> bool:
        return self.is_active and self.revoked_at is None and not self.is_expired

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ServiceApiKey {self.label} {self.key_id} usable={self.is_usable}>"
