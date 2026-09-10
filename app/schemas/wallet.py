"""Wallet, ledger and admin request/response shapes.

Every amount appears twice: once as `*_micros` for arithmetic, once as a
fixed-point string for display. The string exists because the client is
JavaScript, where `0.1 + 0.2` is the reason nobody should do money arithmetic
on a parsed float; the integer exists because the client still needs to
compare and subtract.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.core.money import MICROS_PER_CREDIT, format_credits
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType
from app.schemas.common import PageInfo, _Schema, ensure_utc

# --- responses -------------------------------------------------------------


class WalletResponse(_Schema):
    """The balance, as `GET /v1/wallet` reports it."""

    ok: bool = True

    available_micros: int = Field(
        description="What can be spent right now: paid + unexpired bonus - reserved.",
        examples=[36_633_183],
    )
    available: str = Field(description="`available_micros` as credits.", examples=["36.633183"])

    paid_micros: int = Field(description="Credit bought with money. Never expires.", examples=[30_000_000])
    paid: str = Field(examples=["30.000000"])
    # The *effective* bonus. Reporting a bonus that `available` refuses to
    # spend reads as a bug to the user, and they would be right.
    bonus_micros: int = Field(description="Granted credit, spent first. Zero once expired.", examples=[6_633_183])
    bonus: str = Field(examples=["6.633183"])
    bonus_expires_at: datetime | None = Field(default=None, examples=["2026-10-07T12:00:00Z"])

    reserved_micros: int = Field(
        description="Committed to sessions that have not settled yet. Not spent.",
        examples=[0],
    )
    reserved: str = Field(examples=["0.000000"])

    is_low: bool = Field(
        description="Below `low_balance_threshold_micros`. Show a top-up prompt.",
        examples=[False],
    )
    low_balance_threshold_micros: int = Field(examples=[10_000_000])
    is_frozen: bool = Field(
        description="On hold after a payment reversal or an admin action. Nothing can be spent.",
        examples=[False],
    )
    micros_per_credit: int = Field(
        default=MICROS_PER_CREDIT,
        description="Fixed at 1 000 000. Sent so a client never has to hard-code it.",
        examples=[MICROS_PER_CREDIT],
    )

    @field_validator("bonus_expires_at")
    @classmethod
    def _ensure_utc(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)


def wallet_response(balance) -> WalletResponse:
    """Built from a `Balance`, not from the ORM row.

    The service layer hands back a frozen dataclass; this is the one place
    that turns it into the wire shape, so the display strings cannot drift
    from the integers they were computed from.
    """
    return WalletResponse(
        available_micros=balance.available_micros,
        available=format_credits(balance.available_micros),
        paid_micros=balance.paid_micros,
        paid=format_credits(balance.paid_micros),
        bonus_micros=balance.bonus_micros,
        bonus=format_credits(balance.bonus_micros),
        bonus_expires_at=balance.bonus_expires_at,
        reserved_micros=balance.reserved_micros,
        reserved=format_credits(balance.reserved_micros),
        is_low=balance.is_low,
        low_balance_threshold_micros=balance.low_balance_threshold_micros,
        is_frozen=balance.is_frozen,
    )


class LedgerEntryResponse(_Schema):
    """One line of the statement."""

    id: uuid.UUID
    kind: LedgerEntryKind = Field(
        description="What moved the credit: topup, debit, hold, release, bonus_grant, "
        "refund, reversal, adjustment or expiry.",
        examples=["debit"],
    )
    bucket: LedgerBucket = Field(description="Which counter moved.", examples=["paid"])
    amount_micros: int = Field(
        description="Signed delta applied to `bucket`. Negative took credit out.",
        examples=[-1_205_000],
    )
    amount: str = Field(examples=["-1.205000"])
    balance_after_micros: int = Field(
        description="That bucket immediately after this operation.",
        examples=[28_795_000],
    )
    ref_type: LedgerRefType = Field(examples=["usage_event"])
    ai_session_id: uuid.UUID | None = None
    usage_event_id: uuid.UUID | None = None
    topup_id: uuid.UUID | None = None
    # Rows sharing a `group_id` were written by one operation — the two halves
    # of a debit that spanned both buckets, for instance. A client that wants
    # to show one line per operation groups on this.
    group_id: uuid.UUID
    note: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class LedgerPageResponse(_Schema):
    ok: bool = True
    entries: list[LedgerEntryResponse]
    page: PageInfo


def ledger_entry_response(entry) -> LedgerEntryResponse:
    balance_after = {
        LedgerBucket.PAID: entry.balance_after_paid_micros,
        LedgerBucket.BONUS: entry.balance_after_bonus_micros,
        LedgerBucket.RESERVED: entry.balance_after_reserved_micros,
    }[entry.bucket]
    return LedgerEntryResponse(
        id=entry.id,
        kind=entry.kind,
        bucket=entry.bucket,
        amount_micros=entry.amount_micros,
        amount=format_credits(entry.amount_micros),
        balance_after_micros=balance_after,
        ref_type=entry.ref_type,
        ai_session_id=entry.ai_session_id,
        usage_event_id=entry.usage_event_id,
        topup_id=entry.topup_id,
        group_id=entry.group_id,
        note=entry.note,
        created_at=entry.created_at,
    )


# --- admin requests --------------------------------------------------------


class AdminCreditRequest(_Schema):
    """Move credit by hand. One route, with a `bucket`, rather than two.

    `note` is required: a manual credit with no reason is unauditable, and the
    note is what ends up on the ledger entry alongside the admin's user id.
    """

    amount_micros: int = Field(
        gt=0,
        le=1_000_000_000_000,
        description="Micro-credits to grant. 1 000 000 = one credit.",
        examples=[50_000_000],
    )
    bucket: LedgerBucket = Field(
        default=LedgerBucket.PAID,
        description="`paid` never expires; `bonus` can, and is spent first.",
        examples=["paid"],
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Only meaningful for `bonus`. Null means it never expires.",
        examples=["2026-12-31T23:59:59Z"],
    )
    note: str = Field(
        min_length=3,
        max_length=255,
        description="Why. Recorded on the ledger entry.",
        examples=["Goodwill credit for ticket #482"],
    )

    @field_validator("bucket")
    @classmethod
    def _reject_reserved(cls, value: LedgerBucket) -> LedgerBucket:
        if value is LedgerBucket.RESERVED:
            raise ValueError("reserved is not a bucket credit can be granted into")
        return value


class AdminFreezeRequest(_Schema):
    note: str = Field(min_length=3, max_length=255, examples=["Chargeback under investigation"])


class WalletAuditResponse(_Schema):
    """`POST /v1/admin/reconcile` — does the ledger still add up?"""

    ok: bool = True
    checked: int = Field(examples=[128])
    swept: int = Field(
        description="Batch jobs past `TTS_BATCH_MAX_POLL_SECONDS` that were "
        "settled here at the last usage the speech service reported, releasing "
        "what was left of their hold. A job whose queue message was lost and "
        "whose owner stopped polling is reached by nothing else; each one is "
        "flagged `disputed`, since it was charged on a deadline rather than on "
        "a delivery.",
        examples=[0],
    )
    reaped: int = Field(
        description="Sessions that outlived their deadline and were closed, "
        "handing back the credit they still held. Above zero means something "
        "upstream failed to settle a call it started; the log names each one.",
        examples=[0],
    )
    healed_micros: int = Field(
        description="Reserved credit released because no live session held it.",
        examples=[0],
    )
    diverged: int = Field(
        description="Wallets whose balance disagrees with their ledger. Any "
        "number above zero needs a human.",
        examples=[0],
    )
