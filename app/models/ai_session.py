"""Every metered operation, one-shot or realtime.

Unifying the two is the point: one hold/settle algorithm, one place that
enforces the balance, one join for reporting. A one-shot STT call is a session
that runs `pending -> active -> closed` inside a single request with a zero
hold; a voice-agent call is the same row living for twenty minutes.

The cumulative quantities are denormalised onto this row, one column per
`UsageMetric`, because the heartbeat path is the hot path: pricing a report
means reading and compare-and-swapping a single row with no join. That is also
what makes rounding drift-free — the price is recomputed from the cumulative
total every time and only the difference is charged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.billing_enums import (
    AiSessionKind,
    AiSessionStatus,
    BillingService,
    SessionEndReason,
    UsageMetric,
)


def _zero() -> Any:
    """A counter starting at zero. Spelled once because it appears twelve times.

    Returns a fresh `mapped_column()` each call — sharing one instance between
    columns would have them fight over which attribute they map.
    """
    return mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)


class AiSession(Base):
    __tablename__ = "ai_sessions"
    __table_args__ = (
        # The session token is single-use: `authorize` consumes this, and the
        # unique index is the fallback for when Redis is unavailable.
        UniqueConstraint("authorize_jti", name="uq_ai_sessions_authorize_jti"),
        UniqueConstraint("idempotency_key", name="uq_ai_sessions_idempotency_key"),
        CheckConstraint("reserved_micros >= 0", name="reserved_nonneg"),
        CheckConstraint("settled_micros >= 0", name="settled_nonneg"),
        CheckConstraint("estimated_micros >= 0", name="estimated_nonneg"),
        CheckConstraint("writeoff_micros >= 0", name="writeoff_nonneg"),
        CheckConstraint("cost_micros >= 0", name="cost_nonneg"),
        CheckConstraint("version >= 0", name="version_nonneg"),
        CheckConstraint("last_sequence >= 0", name="last_sequence_nonneg"),
        Index("ix_ai_sessions_user_created", "user_id", "created_at"),
        # The reaper's query: live sessions ordered by how long they have been
        # quiet.
        Index("ix_ai_sessions_status_heartbeat", "status", "last_heartbeat_at"),
        Index("ix_ai_sessions_wallet_status", "wallet_id", "status"),
        # The hold-reconciliation query: which sessions still hold credit on
        # this wallet. Partial, because a released hold is the overwhelming
        # majority of rows and none of them are ever the answer. The predicate
        # names a column rather than a status value, so renaming a status
        # cannot silently orphan the index.
        Index(
            "ix_ai_sessions_unreleased_holds",
            "wallet_id",
            postgresql_where=text("hold_released_at IS NULL"),
            sqlite_where=text("hold_released_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    service: Mapped[BillingService] = mapped_column(
        Enum(BillingService, native_enum=False, length=32), nullable=False
    )
    kind: Mapped[AiSessionKind] = mapped_column(
        Enum(AiSessionKind, native_enum=False, length=32), nullable=False
    )
    status: Mapped[AiSessionStatus] = mapped_column(
        Enum(AiSessionStatus, native_enum=False, length=32),
        default=AiSessionStatus.PENDING,
        server_default=AiSessionStatus.PENDING.value,
        nullable=False,
    )
    # Pinned at open time so "authorized for cheap, ran expensive" is not a
    # thing the microservice can do.
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Pinned once, never re-resolved: a price book published mid-call cannot
    # change the rate of a call already in progress.
    price_book_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_book_versions.id", ondelete="RESTRICT"), nullable=False
    )

    # --- money -------------------------------------------------------------
    # Currently held on the wallet for this session. Goes to zero on release.
    reserved_micros: Mapped[int] = _zero()
    # The largest hold this session ever had, for diagnosing hold sizing.
    hold_peak_micros: Mapped[int] = _zero()
    # Already charged. `settled` chases `estimated`.
    settled_micros: Mapped[int] = _zero()
    # What the cumulative quantities price to right now.
    estimated_micros: Mapped[int] = _zero()
    # Charged for but never collected, because the wallet ran dry during grace.
    # Writes no ledger entry: no money moved.
    writeoff_micros: Mapped[int] = _zero()
    # What this session cost us upstream, same unit as the price.
    cost_micros: Mapped[int] = _zero()

    # --- cumulative quantities, one per UsageMetric ------------------------
    cum_session_ms: Mapped[int] = _zero()
    cum_stt_audio_ms: Mapped[int] = _zero()
    cum_tts_characters: Mapped[int] = _zero()
    cum_tts_audio_ms: Mapped[int] = _zero()
    cum_llm_input_tokens: Mapped[int] = _zero()
    cum_llm_cached_input_tokens: Mapped[int] = _zero()
    cum_llm_output_tokens: Mapped[int] = _zero()

    # --- lifecycle ---------------------------------------------------------
    # When the microservice claimed the ticket. Null while `pending`.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Hard stop, enforced by the reaper even if the service ignores its limit.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Non-null exactly once. This is the idempotency guard for releasing the
    # hold — releasing twice would corrupt `wallets.reserved_micros`
    # permanently, and nothing would ever notice.
    hold_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # Highest report sequence accepted. A report at or below this is a replay.
    last_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    resume_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    grace_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    grace_micros_granted: Mapped[int] = _zero()
    end_reason: Mapped[SessionEndReason | None] = mapped_column(
        Enum(SessionEndReason, native_enum=False, length=32), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set when we settled on incomplete information — a lost heartbeat, a
    # clamped over-report — so support can find the calls worth refunding.
    disputed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    # --- auth handle -------------------------------------------------------
    # `jti` of the short-lived session JWT we minted. Consumed by `authorize`.
    authorize_jti: Mapped[str] = mapped_column(String(64), nullable=False)
    service_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_api_keys.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    # 200, and the number lives here rather than in the service that writes it.
    # What gets stored is `{user_id}:{scope}:{client key}` — thirty-seven
    # characters of uuid and colons in front of a client key whose published
    # ceiling is 128 — so the widest legal value is 165 plus the scope. At 128
    # this column could not hold the keys our own OpenAPI advertises as legal:
    # Postgres answers an over-long value with 22001 and a 500 on
    # `POST /tts/speech`, and SQLite silently stores the whole string, so the
    # test suite was green for as long as it ran on SQLite. Widening is the
    # direction that keeps the published contract; narrowing the contract
    # instead would break the clients that read the docs and believed them.
    #
    # `session_service.IDEMPOTENCY_KEY_MAX_LENGTH` is read off this column for
    # that reason. A constant that merely agrees with the schema is a constant
    # that will one day stop agreeing with it, and no SQLite test can ever be
    # the thing that notices.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # What the key was spent on: sha256 over the fields that decide what gets
    # synthesised, sixty-four hex characters, written at open time.
    #
    # An idempotency key means "this request again", and the only way to check
    # that claim is to have written down what the request *was*. The replay
    # guard used to compare the *price* instead, which cannot work: the price
    # book charges per thousand characters with CEIL rounding, so every text
    # from one to a thousand characters quotes the same number and one paid
    # character bought unlimited free synthesis inside that bucket. A price is
    # a bucket, and a bucket is not an identity.
    #
    # Nullable, because rows that predate this column have no digest and a
    # realtime session has no single request to fingerprint. A null means "we
    # cannot vouch that this is the same request", which a replay guard must
    # read as a refusal rather than as a pass — the safe direction is a `409`
    # and a fresh key, never a second synthesis nobody is charged for.
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Compare-and-swap token for the heartbeat path and for the state
    # transitions the reaper races against.
    version: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )

    def cumulative(self, metric: UsageMetric) -> int:
        return int(getattr(self, metric.cumulative_column))

    def cumulative_quantities(self) -> dict[UsageMetric, int]:
        """Everything counted so far, skipping the metrics never reported."""
        return {m: self.cumulative(m) for m in UsageMetric if self.cumulative(m) > 0}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AiSession {self.service.value} {self.kind.value} {self.status.value}>"
