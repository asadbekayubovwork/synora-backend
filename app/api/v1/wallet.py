from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select, tuple_

from app.api.deps import CurrentUser, SessionDep
from app.core.money import MICROS_PER_CREDIT
from app.models.ledger import LedgerEntry
from app.schemas.auth import ErrorResponse
from app.schemas.common import Cursor, PageInfo, clamp_limit, decode_cursor
from app.schemas.wallet import (
    LedgerPageResponse,
    WalletResponse,
    ledger_entry_response,
    wallet_response,
)
from app.services.billing import wallet_service

router = APIRouter(prefix="/wallet", tags=["Wallet"])

ERRORS: dict[int | str, dict] = {
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}

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


@router.get(
    "",
    response_model=WalletResponse,
    responses=ERRORS,
    summary="The signed-in user's credit balance",
    description=(
        "Amounts are in **micro-credits**: "
        f"`1 credit = {MICROS_PER_CREDIT:,} micros`. Each one is also given as a "
        "fixed-point string for display, because a JavaScript client should not "
        "be doing money arithmetic on a parsed float.\n\n"
        "`available_micros` is the number that matters — paid credit plus any "
        "*unexpired* bonus, minus credit already committed to sessions that have "
        "not settled. Bonus credit is always spent first, since it is the part "
        "that can lapse.\n\n"
        "The wallet is created on first read, so this never 404s for a signed-in "
        "user. Poll it after a top-up, or subscribe to the balance stream."
    ),
)
async def balance(user: CurrentUser, session: SessionDep) -> WalletResponse:
    result = await wallet_service.get_balance(session, user.id)
    await session.commit()
    return wallet_response(result)


@router.get(
    "/transactions",
    response_model=LedgerPageResponse,
    responses=ERRORS,
    summary="Statement of every credit movement, newest first",
    description=(
        "Cursor-paginated. Pass `page.next_cursor` back as `?cursor=` to walk "
        "backwards through time; `page.has_more` tells you when to stop.\n\n"
        "Cursors rather than an offset because this list is append-only and read "
        "newest-first, which is exactly where `OFFSET` goes wrong: rows arriving "
        "between two requests shift the window, so a reader sees one row twice "
        "and misses another entirely.\n\n"
        "One operation can produce more than one row — a charge that took the "
        "last of a bonus and the rest from paid credit writes one of each. Rows "
        "sharing a `group_id` were written together and describe the same "
        "resulting balance, so group on it to show one line per operation."
    ),
)
async def transactions(
    user: CurrentUser,
    session: SessionDep,
    limit: int | None = LimitQuery,
    cursor: str | None = CursorQuery,
) -> LedgerPageResponse:
    page_size = clamp_limit(limit)
    position = decode_cursor(cursor)

    # Keyset on (created_at, id). `created_at` alone is not unique — a split
    # debit writes two rows in one statement — so the id breaks the tie and
    # keeps the ordering total, which is what stops a cursor from skipping or
    # repeating a row.
    #
    # `tuple_()` and not a plain Python tuple: `(col_a, col_b) < (x, y)` would
    # be evaluated by Python, comparing a SQL expression object for truthiness
    # instead of emitting a row-value comparison.
    query = select(LedgerEntry).where(LedgerEntry.user_id == user.id)
    if position is not None:
        query = query.where(
            tuple_(LedgerEntry.created_at, LedgerEntry.id)
            < tuple_(position.created_at, position.row_id)
        )
    query = query.order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(
        page_size + 1
    )

    rows = list((await session.execute(query)).scalars())
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = (
        Cursor(created_at=rows[-1].created_at, row_id=rows[-1].id).encode()
        if has_more and rows
        else None
    )

    return LedgerPageResponse(
        entries=[ledger_entry_response(row) for row in rows],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page_size),
    )
