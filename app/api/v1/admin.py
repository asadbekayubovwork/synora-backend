"""Admin routes. Every one of these moves money or changes what money buys.

Gated on `AdminUser`, which requires `users.is_superuser`. There is no
separate audit table: attribution rides on the ledger entry itself, which
records the acting user id alongside a required `note`. The audit belongs in
the append-only place.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Path

from app.api.deps import AdminUser, SessionDep
from app.core.exceptions import BadRequestError
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType
from app.schemas.auth import ErrorResponse
from app.schemas.wallet import (
    AdminCreditRequest,
    AdminFreezeRequest,
    WalletAuditResponse,
    WalletResponse,
    wallet_response,
)
from app.services.ai import tts_batch_service
from app.services.billing import reconcile_service, wallet_repo, wallet_service

router = APIRouter(prefix="/admin", tags=["Admin"])

ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}

UserIdPath = Path(description="The user whose wallet to act on.")

# The key is stored prefixed, so the client's share of the column is what is
# left after it. Scoped rather than stored bare for the reason `open_oneshot`
# scopes its own: `uq_ledger_entries_idempotency_key` is global, and an admin's
# `retry-1` must not collide with a session hold's.
IDEMPOTENCY_KEY_PREFIX = "admin:"

# Published *and* enforced, and derived from the column rather than written
# down, because this header used to be neither. It advertised "any unique
# string" and had no `max_length` at all, while what it becomes —
# `admin:` plus the key — goes into `ledger_entries.idempotency_key`, a
# VARCHAR(128). Anything over 122 characters therefore reached Postgres as an
# over-long INSERT: 22001, an unhandled `DataError`, and a 500 on the one route
# whose entire job is to make a retried grant of real money safe. SQLite stores
# the whole string and shrugs, which is why the suite was green.
#
# `max_length` here rather than a check in the handler so the refusal happens in
# the validation pass, where it names the header the client actually sent, and
# so the number appears in OpenAPI instead of only in an error nobody can see
# in advance. See `app/api/v1/tts.py`, which learned this the same way.
MAX_ADMIN_IDEMPOTENCY_KEY = (
    wallet_repo.LEDGER_IDEMPOTENCY_KEY_MAX_LENGTH - len(IDEMPOTENCY_KEY_PREFIX)
)

IdempotencyKeyHeader = Header(
    default=None,
    alias="Idempotency-Key",
    max_length=MAX_ADMIN_IDEMPOTENCY_KEY,
    description=(
        "Required. Any unique string; the same one replays instead of granting "
        f"twice. At most {MAX_ADMIN_IDEMPOTENCY_KEY} characters."
    ),
)


@router.post(
    "/wallets/{user_id}/credits",
    response_model=WalletResponse,
    responses=ERRORS,
    summary="Grant credit by hand",
    description=(
        "Puts credit into a wallet without a payment behind it — goodwill, a "
        "campaign, or settling a support ticket.\n\n"
        "**`Idempotency-Key` is required.** A double-submitted manual credit is "
        "real money, and a browser retrying a slow request is not a rare event. "
        "Replaying the same key returns the balance without granting again.\n\n"
        "`note` is required too. It lands on the ledger entry next to the "
        "granting admin's user id, which is the whole audit trail for this "
        "action — there is no separate log to consult."
    ),
)
async def grant_credits(
    payload: AdminCreditRequest,
    admin: AdminUser,
    session: SessionDep,
    user_id: uuid.UUID = UserIdPath,
    idempotency_key: str | None = IdempotencyKeyHeader,
) -> WalletResponse:
    if not idempotency_key or not idempotency_key.strip():
        raise BadRequestError(
            "An Idempotency-Key header is required so a retry cannot grant twice.",
            code="idempotency_key_required",
        )

    snapshot = await wallet_service.ensure_wallet(session, user_id)
    is_bonus = payload.bucket is LedgerBucket.BONUS
    await wallet_repo.credit(
        session,
        wallet_id=snapshot.wallet_id,
        paid_micros=0 if is_bonus else payload.amount_micros,
        bonus_micros=payload.amount_micros if is_bonus else 0,
        bonus_expires_at=payload.expires_at if is_bonus else None,
        kind=LedgerEntryKind.BONUS_GRANT if is_bonus else LedgerEntryKind.ADJUSTMENT,
        ref_type=LedgerRefType.ADMIN_GRANT,
        # Stripping only ever shortens, so a header that passed `max_length`
        # above cannot outgrow the column here.
        idempotency_key=f"{IDEMPOTENCY_KEY_PREFIX}{idempotency_key.strip()}",
        actor_user_id=admin.id,
        note=payload.note,
    )
    balance = await wallet_service.get_balance(session, user_id)
    await session.commit()
    return wallet_response(balance)


@router.post(
    "/wallets/{user_id}/freeze",
    response_model=WalletResponse,
    responses=ERRORS,
    summary="Put a wallet on hold",
    description=(
        "A frozen wallet can neither start a session nor top up. Set "
        "automatically when a payment reversal drives a balance negative — "
        "credit that was already spent cannot be clawed back from thin air — and "
        "settable by hand for a chargeback under investigation.\n\n"
        "Freezing moves no credit, so it writes no ledger entry."
    ),
)
async def freeze(
    payload: AdminFreezeRequest,
    admin: AdminUser,  # noqa: ARG001 - gate only; the note carries the reason
    session: SessionDep,
    user_id: uuid.UUID = UserIdPath,
) -> WalletResponse:
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    await wallet_repo.set_frozen(
        session, wallet_id=snapshot.wallet_id, frozen=True, reason=payload.note
    )
    balance = await wallet_service.get_balance(session, user_id)
    await session.commit()
    return wallet_response(balance)


@router.post(
    "/wallets/{user_id}/unfreeze",
    response_model=WalletResponse,
    responses=ERRORS,
    summary="Take a wallet off hold",
    description="Always a deliberate human decision, never automatic.",
)
async def unfreeze(
    admin: AdminUser,  # noqa: ARG001 - gate only
    session: SessionDep,
    user_id: uuid.UUID = UserIdPath,
) -> WalletResponse:
    snapshot = await wallet_service.ensure_wallet(session, user_id)
    await wallet_repo.set_frozen(session, wallet_id=snapshot.wallet_id, frozen=False)
    balance = await wallet_service.get_balance(session, user_id)
    await session.commit()
    return wallet_response(balance)


@router.get(
    "/wallets/{user_id}",
    response_model=WalletResponse,
    responses=ERRORS,
    summary="Read any user's balance",
)
async def read_wallet(
    admin: AdminUser,  # noqa: ARG001 - gate only
    session: SessionDep,
    user_id: uuid.UUID = UserIdPath,
) -> WalletResponse:
    balance = await wallet_service.get_balance(session, user_id)
    await session.commit()
    return wallet_response(balance)


@router.post(
    "/reconcile",
    response_model=WalletAuditResponse,
    responses=ERRORS,
    summary="Check that every wallet still matches its ledger",
    description=(
        "Runs the same pass the scheduler runs, and reports what it found.\n\n"
        "Four findings, with deliberately different handling, listed in the "
        "order the pass performs them.\n\n"
        "**`swept`** counts batch jobs that outlived "
        "`TTS_BATCH_MAX_POLL_SECONDS` and were settled here at the last usage "
        "the speech service admitted to, handing back whatever was left of "
        "their hold. Every other route to that deadline runs on somebody's "
        "behalf — a worker's poll, or a read of `GET /tts/batch/{job_id}` — so "
        "a job whose queue message was dead-lettered, or whose owner stopped "
        "polling, previously had nothing scheduled to release its credit at "
        "all. The session reaper below cannot be that backstop: it skips any "
        "session a live batch job points at, on purpose, because the two run "
        "on different clocks. A swept job is settled rather than refunded and "
        "its session is flagged `disputed`, because a charge made on a "
        "deadline is a weaker thing than a charge made on a delivery and "
        "support should be able to find it.\n\n"
        "**`reaped`** counts metered sessions that outlived `expires_at` and "
        "were closed here, giving back the credit they were still holding. A "
        "call places its hold before any work starts, so a process that dies "
        "mid-call leaves the hold behind; nothing else in the system can tell "
        "that apart from a call still running, because a live session is "
        "*supposed* to hold credit. This is the only pass that reads a "
        "*session's* deadline; `swept` above owns the batch one. Anything "
        "above zero is a bug upstream of the reconciler rather than routine "
        "housekeeping, and every reaped session id is logged at WARNING.\n\n"
        "**`healed_micros`** is reserved credit that no session claims at all. "
        "It is released automatically: the correct figure is recomputable from "
        "the sessions themselves, and leaving it alone freezes a paying "
        "customer's money for no reason.\n\n"
        "**`diverged`** counts wallets whose balance disagrees with their own "
        "ledger. Nothing is touched there — credit appeared or vanished, and "
        "silently rewriting the number would destroy the only evidence of how. "
        "Any value above zero needs a human, and the log line names the "
        "operation where the replay first went wrong."
    ),
)
async def reconcile(
    admin: AdminUser,  # noqa: ARG001 - gate only
    session: SessionDep,
) -> WalletAuditResponse:
    # Composed here rather than inside `reconcile_service`, and deliberately.
    # A billing module importing from `app/services/ai/` would invert the
    # layering this codebase holds everywhere else — AI code bills through
    # billing, never the reverse — and the route is already the place passes
    # are composed, so the sweep joins them here instead of buying a cycle.
    #
    # Swept first, for the reason `reconcile_all` orders its own three: this
    # is the pass that *creates* work for the others. It stamps a stranded
    # batch job terminal and settles the session under it, and until that has
    # happened the reaper skips that session (a live job points at it) and
    # `heal_reserved` reads its hold as legitimate. Reconcile first and
    # anything the sweep frees waits a whole cycle to be noticed.
    swept = await tts_batch_service.sweep_stale_jobs(session)
    report = await reconcile_service.reconcile_all(session)
    return WalletAuditResponse(
        checked=report["checked"],
        swept=swept,
        reaped=report["reaped"],
        healed_micros=report["healed_micros"],
        diverged=report["diverged"],
    )
