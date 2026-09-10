"""Enums shared across the billing tables.

They live in one module because `wallets`, `ai_sessions`, `usage_events`,
`prices` and `topups` all name the same handful of things, and a service that
is spelled `voice_agent` in one table and `voice-agent` in another is a class
of bug worth designing out.

All of them are mapped with `Enum(X, native_enum=False, length=32)`, following
the convention already set by `app/models/otp.py`. That stores a VARCHAR rather
than creating a Postgres type, which means adding a member needs no DDL at all
— important for `SessionEndReason`, which will grow.
"""

from __future__ import annotations

import enum


class BillingService(str, enum.Enum):
    """The metered services. `voice_agent` is the composite, realtime one."""

    TTS = "tts"
    STT = "stt"
    CHAT = "chat"
    VOICE_AGENT = "voice_agent"


class UsageMetric(str, enum.Enum):
    """What a microservice counts.

    Every name carries its unit, because "duration" in a billing payload is
    how you end up charging milliseconds at the per-second rate. Durations are
    milliseconds throughout; nothing here is ever fractional.

    There is exactly one `AiSession.cum_*` column per member, so adding a
    metric means a migration. That is deliberate: an unpriced metric silently
    accepted is revenue never charged, and a mistyped one silently stored is a
    dashboard that lies.
    """

    SESSION_MS = "session_ms"
    STT_AUDIO_MS = "stt_audio_ms"
    TTS_CHARACTERS = "tts_characters"
    TTS_AUDIO_MS = "tts_audio_ms"
    LLM_INPUT_TOKENS = "llm_input_tokens"
    LLM_CACHED_INPUT_TOKENS = "llm_cached_input_tokens"
    LLM_OUTPUT_TOKENS = "llm_output_tokens"

    @property
    def cumulative_column(self) -> str:
        """The `AiSession` column holding this metric's running total."""
        return f"cum_{self.value}"


class RoundingMode(str, enum.Enum):
    """How a priced quantity becomes a number of billable units.

    Applied to the session's *cumulative* quantity, once, never per report —
    see `app/services/billing/pricing.py`.
    """

    CEIL = "ceil"        # a started unit is a charged unit; the default
    FLOOR = "floor"
    HALF_UP = "half_up"
    EXACT = "exact"      # no unit rounding at all: charge the exact fraction


class LedgerBucket(str, enum.Enum):
    """Which wallet counter a ledger entry moves."""

    PAID = "paid"
    BONUS = "bonus"
    RESERVED = "reserved"


class LedgerEntryKind(str, enum.Enum):
    """Why a ledger entry exists.

    The sign rule, stated once and never varied: `amount_micros` is the delta
    applied to `bucket`. So `DEBIT` is negative on `paid`/`bonus`, `TOPUP` is
    positive on `paid`, `HOLD` is positive on `reserved`, `RELEASE` is negative
    on `reserved`, `EXPIRY` is negative on `bonus`.
    """

    TOPUP = "topup"
    BONUS_GRANT = "bonus_grant"
    DEBIT = "debit"
    REFUND = "refund"
    HOLD = "hold"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"
    EXPIRY = "expiry"
    REVERSAL = "reversal"


class LedgerRefType(str, enum.Enum):
    """What the entry points at, so a statement line can be explained."""

    TOPUP = "topup"
    PAYMENT = "payment"
    AI_SESSION = "ai_session"
    USAGE_EVENT = "usage_event"
    ADMIN_GRANT = "admin_grant"
    SIGNUP_BONUS = "signup_bonus"
    BONUS_EXPIRY = "bonus_expiry"
    RECONCILE = "reconcile"


class PriceBookStatus(str, enum.Enum):
    """A price book is editable only while it is a draft."""

    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class CreditRateStatus(str, enum.Enum):
    """Same lifecycle as a price book, for the UZS-to-credit rate."""

    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class AiSessionKind(str, enum.Enum):
    """`oneshot` lives inside a single request; `realtime` outlives it."""

    ONESHOT = "oneshot"
    REALTIME = "realtime"


class AiSessionStatus(str, enum.Enum):
    """See `app/services/billing/session_service.py` for the transitions.

    `CLOSED`, `EXPIRED`, `KILLED` and `FAILED` are terminal, and every one of
    them must leave zero held credit for the session.
    """

    PENDING = "pending"    # hold placed, the microservice has not claimed it
    ACTIVE = "active"      # claimed and serving
    GRACE = "grace"        # out of credit: warned, grace running
    CLOSING = "closing"    # stop requested, final report awaited
    CLOSED = "closed"      # terminal, settled normally
    EXPIRED = "expired"    # terminal, never claimed
    KILLED = "killed"      # terminal, cut short by us
    FAILED = "failed"      # terminal, lost contact or upstream error


TERMINAL_SESSION_STATUSES = frozenset(
    {
        AiSessionStatus.CLOSED,
        AiSessionStatus.EXPIRED,
        AiSessionStatus.KILLED,
        AiSessionStatus.FAILED,
    }
)
LIVE_SESSION_STATUSES = frozenset(
    {
        AiSessionStatus.PENDING,
        AiSessionStatus.ACTIVE,
        AiSessionStatus.GRACE,
        AiSessionStatus.CLOSING,
    }
)


class SessionEndReason(str, enum.Enum):
    """A closed set — anything else is a `422` on `finalize`.

    A free-text reason would be unaggregatable, and "why do calls end?" is the
    first question anyone asks of this data.
    """

    COMPLETED = "completed"
    CLIENT_HANGUP = "client_hangup"
    CLIENT_DISCONNECTED = "client_disconnected"
    STOP_REQUESTED = "stop_requested"
    USER_CANCELLED = "user_cancelled"
    ADMIN_KILLED = "admin_killed"
    INSUFFICIENT_CREDIT = "insufficient_credit"
    GRACE_EXHAUSTED = "grace_exhausted"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    MAX_DURATION = "max_duration"
    NEVER_CLAIMED = "never_claimed"
    BACKEND_UNREACHABLE = "backend_unreachable"
    UPSTREAM_ERROR = "upstream_error"
    INTERNAL_ERROR = "internal_error"
    TIMEOUT = "timeout"


class TtsBatchJobState(str, enum.Enum):
    """Where a batch job is, from our side of the queue.

    Deliberately not a copy of upstream's `state`. Upstream knows `pending`,
    `running`, `succeeded`, `failed` and `cancelled` about work it has already
    accepted. `queued` is the state only we can be in — priced, held for, and
    not yet handed over — and `expired` is the verdict only we can reach, when
    a job has outlived `tts_batch_max_poll_seconds` and we settle at the last
    usage upstream admitted to rather than hold the credit forever.

    Closed for the same reason `AiSessionStatus` is: the poller selects on this
    column and settlement branches on it, so a member nobody wrote a branch for
    is a job that is never polled again or never releases its hold. `SUCCEEDED`,
    `FAILED`, `CANCELLED` and `EXPIRED` are terminal, and every one of them must
    leave the backing `AiSession` settled and holding nothing.
    """

    QUEUED = "queued"        # our row exists; upstream has never seen it
    SUBMITTED = "submitted"  # upstream accepted it, `upstream_job_id` is set
    RUNNING = "running"      # upstream is synthesising
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"      # we stopped polling and settled at last known usage


TERMINAL_BATCH_STATES = frozenset(
    {
        TtsBatchJobState.SUCCEEDED,
        TtsBatchJobState.FAILED,
        TtsBatchJobState.CANCELLED,
        TtsBatchJobState.EXPIRED,
    }
)


class UsageEventKind(str, enum.Enum):
    ONESHOT = "oneshot"
    HEARTBEAT = "heartbeat"
    FINAL = "final"
    CORRECTION = "correction"


class UsageEventStatus(str, enum.Enum):
    RECORDED = "recorded"    # priced and debited
    PENDING = "pending"      # one-shot: hold placed, upstream call in flight
    ABANDONED = "abandoned"  # one-shot: we crashed mid-call, nothing charged
    REJECTED = "rejected"    # priced but not charged (session already terminal)
    VOIDED = "voided"        # reversed by a refund


class SessionAction(str, enum.Enum):
    """What we tell a microservice to do next, on every report."""

    CONTINUE = "continue"
    WARN = "warn"
    STOP = "stop"


class TopupProvider(str, enum.Enum):
    PAYME = "payme"
    CLICK = "click"
    MANUAL = "manual"   # an admin moved it by hand
    PROMO = "promo"     # a campaign grant, no money in


class TopupStatus(str, enum.Enum):
    CREATED = "created"      # our intent exists, nothing has happened
    PREPARED = "prepared"    # the provider acknowledged, awaiting capture
    PAID = "paid"            # captured; crediting in progress
    CREDITED = "credited"    # terminal happy path
    CANCELLED = "cancelled"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class PaymentState(str, enum.Enum):
    """One provider transaction's own lifecycle."""

    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    FAILED = "failed"
