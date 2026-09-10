"""What an account has consumed, read from the rows we charged it from.

This endpoint is the answer to "usage has to be controlled on our side". The
gateway model exists so that every metered call passes through this backend and
lands in `usage_events` and `usage_event_items` before any credit moves; this
route is the place that admits it. Nothing here is relayed from a supplier. The
speech service publishes a `GET /v1/usage` of its own and it answers a different
question — it is tenant-wide and reports what *we* have spent against *it*,
which is an operations number and not a customer's.

## Why it aggregates the items and not the events

`usage_events` carries the money and `usage_event_items` carries what the money
was for, one row per priced metric with the unit size and rate it was charged
at. Grouping the items and joining back for the service is what makes "42 000
characters of TTS across 37 calls" expressible at all; the event alone can only
say "32.1 credits", which is the same number the statement already gives and
answers nothing the statement does not.

The join is also why the window filters on `usage_events.occurred_at` rather
than on anything of the item's own. `occurred_at` is when the work happened;
`created_at` is when we stored it, and the gap between them is a backlog. A
usage report that places a late-arriving event in the month we wrote it down
rather than the month it belongs to is a report nobody can reconcile against an
invoice.

## The total is summed from the lines, never queried separately

`app/schemas/tts.py::usage_summary_response` computes it, deliberately. A total
with its own `WHERE` clause is a total that disagrees with the column printed
underneath it the first time the two clauses drift, and a page whose figures do
not add up is unusable however right the total happens to be.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, SessionDep
from app.core.exceptions import BadRequestError
from app.db.base import utcnow
from app.models.billing_enums import UsageEventStatus
from app.models.usage import UsageEvent, UsageEventItem
from app.schemas.auth import ErrorResponse
from app.schemas.common import ensure_utc
from app.schemas.tts import UsageSummaryResponse, usage_summary_response

router = APIRouter(prefix="/usage", tags=["Usage"])

ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "The window makes no sense"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    422: {"model": ErrorResponse, "description": "Validation error"},
}

# A month, near enough. Long enough to cover the billing period anyone is
# actually asking about, short enough that the default query is a small read.
DEFAULT_WINDOW_DAYS = 30

StartQuery = Query(
    default=None,
    description=(
        "Inclusive start of the window, matched against `occurred_at`. "
        f"Defaults to {DEFAULT_WINDOW_DAYS} days before `end`. A bare date is "
        "read as midnight, and a timestamp with no offset is read as UTC."
    ),
    examples=["2026-09-01T00:00:00Z"],
)
EndQuery = Query(
    default=None,
    description="Exclusive end of the window. Defaults to now.",
    examples=["2026-10-01T00:00:00Z"],
)


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Resolve the two bounds, in UTC, or refuse the pair.

    A naive timestamp is read as UTC rather than rejected: the stored column is
    UTC, the default window is UTC, and a client that sent a bare date meant
    the day rather than a validation error. `ensure_utc` is the same function
    every outgoing timestamp goes through, so the reading is the same in both
    directions.
    """
    period_end = ensure_utc(end) or utcnow()
    period_start = ensure_utc(start) or period_end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if period_start >= period_end:
        # An empty window is answerable — zero of everything — but it is far
        # more often two swapped parameters, and returning zeros for that reads
        # as "you have used nothing", which is a different and alarming claim.
        raise BadRequestError(
            "The start of the window must come before its end.",
            code="usage_window_invalid",
        )
    return period_start, period_end


@router.get(
    "",
    response_model=UsageSummaryResponse,
    responses=ERRORS,
    summary="Your own consumption, by service and metric",
    description=(
        "What this account has used over a window, grouped by which service "
        "did the work and what was counted.\n\n"
        "**Computed from our own rows, not relayed from a supplier.** Every "
        "line is summed from the usage items the charges were priced from — the "
        "same rows `GET /wallet/transactions` reports the money side of. That "
        "is the whole point of running the AI services behind this gateway: the "
        "count that produced the bill and the count you are shown are the same "
        "count.\n\n"
        "Events are placed in the window by **when the work happened**, not by "
        "when we wrote it down, so a report that reached us late still lands in "
        "the period it belongs to. `start` is inclusive, `end` is exclusive, "
        f"and together they default to the last {DEFAULT_WINDOW_DAYS} days.\n\n"
        "`quantity` is in the metric's own unit — `tts_characters` is "
        "characters, a `*_ms` metric is milliseconds — so the lines are not "
        "comparable with each other except through `price_micros`. `events` is "
        "one per metered call that recorded the metric, which for a one-shot "
        "call with a single priced metric is the call count; a future service "
        "reporting three metrics per call contributes three.\n\n"
        "`price_micros` is what each line priced to. In the ordinary case that "
        "is exactly what was charged. Where they differ is a session clamped at "
        "its hold — a supplier that over-reported, which flags the session for "
        "review rather than quietly billing the excess — and there the wallet "
        "took less than this page shows. The statement is the authority on "
        "money; this is the authority on consumption.\n\n"
        "Only recorded usage counts. A call abandoned before any work was "
        "delivered wrote no event at all and charged nothing, and an event "
        "voided by a refund stops being consumption at the moment it is."
    ),
)
async def usage(
    user: CurrentUser,
    session: SessionDep,
    start: datetime | None = StartQuery,
    end: datetime | None = EndQuery,
) -> UsageSummaryResponse:
    period_start, period_end = _window(start, end)

    # Every aggregate is labelled, because `usage_line_response` reads the row
    # by name. Positional reads here would let a reordered `select` report
    # characters as money without changing a line of the builder.
    #
    # `count()` over the items rather than over distinct event ids:
    # `uq_usage_event_items_event_metric` allows one item per metric per event,
    # so within a group the two are the same number and one of them is free.
    query = (
        select(
            UsageEvent.service.label("service"),
            UsageEventItem.metric.label("metric"),
            func.sum(UsageEventItem.quantity).label("quantity"),
            func.count(UsageEventItem.id).label("events"),
            func.sum(UsageEventItem.price_micros).label("price_micros"),
        )
        .join(UsageEvent, UsageEvent.id == UsageEventItem.usage_event_id)
        .where(
            UsageEvent.user_id == user.id,
            UsageEvent.status == UsageEventStatus.RECORDED,
            UsageEvent.occurred_at >= period_start,
            UsageEvent.occurred_at < period_end,
        )
        .group_by(UsageEvent.service, UsageEventItem.metric)
        # Ordered so a client rendering the table gets the same rows in the
        # same places on every poll. `ix_usage_events_user_occurred` covers the
        # filter; the sort is over a handful of grouped rows.
        .order_by(UsageEvent.service, UsageEventItem.metric)
    )

    rows = (await session.execute(query)).all()
    return usage_summary_response(
        rows, period_start=period_start, period_end=period_end
    )
