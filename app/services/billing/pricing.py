"""Turning quantities into micro-credits.

Two halves. The pure arithmetic at the top has no database and no I/O, which is
where nearly all of the money risk lives and where nearly all of the tests
point. The resolution functions below it read the pinned price book.

The rule that makes this correct: **prices are applied to cumulative
quantities, never to per-report deltas.** A session's cost is recomputed in
full every time a report arrives, and only the difference against what was
already settled is charged. Rounding therefore happens once per metric per
session instead of once per heartbeat — the difference between charging 5 000
micro-credits for a five-minute call and charging 20 000 for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, ServiceUnavailableError
from app.core.money import ceil_div, half_up_div
from app.db.base import utcnow
from app.models.billing_enums import BillingService, PriceBookStatus, RoundingMode, UsageMetric
from app.models.price_book import MODEL_KEY_ANY, Price, PriceBookVersion

# --- the arithmetic --------------------------------------------------------


def billable_units(quantity: int, unit_size: int, rounding: RoundingMode) -> int:
    """How many priced units `quantity` base units come to."""
    if unit_size <= 0:
        raise ValueError("unit_size must be positive")
    if quantity <= 0:
        return 0
    if rounding is RoundingMode.FLOOR:
        return quantity // unit_size
    if rounding is RoundingMode.HALF_UP:
        return half_up_div(quantity, unit_size)
    # CEIL is the default: a started unit is a charged unit.
    return ceil_div(quantity, unit_size)


def line_micros(
    *,
    quantity: int,
    unit_size: int,
    rate_micros_per_unit: int,
    rounding: RoundingMode,
    included_quantity: int = 0,
    min_charge_micros: int = 0,
) -> int:
    """Charge for a **cumulative** quantity. Pure, total, integers only.

    `EXACT` skips unit rounding altogether and charges the true fraction, which
    is what you want for a per-token rate where a partial unit is meaningful.
    Everything else rounds the unit count.

    `min_charge_micros` sits inside this function on purpose. Being a floor on
    the *cumulative* line, it lands on the first report and later reports only
    charge the increment above it — rather than being re-applied on every one
    of the three hundred heartbeats in a long call.

    A quantity entirely inside `included_quantity` costs nothing at all, not
    even the minimum: a session that recorded nothing should not be billed for
    existing.
    """
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    if rate_micros_per_unit < 0 or min_charge_micros < 0 or included_quantity < 0:
        raise ValueError("rates and floors must not be negative")

    billable = quantity - included_quantity
    if billable <= 0:
        return 0

    if rounding is RoundingMode.EXACT:
        raw = (billable * rate_micros_per_unit) // unit_size
    else:
        raw = billable_units(billable, unit_size, rounding) * rate_micros_per_unit
    return max(raw, min_charge_micros)


@dataclass(frozen=True)
class PricedLine:
    """One metric, priced at its cumulative quantity.

    Carries a snapshot of the three numbers that produced the charge, because
    `usage_event_items` stores them alongside `price_id` so an invoice renders
    without joining and a hand-edited price shows up as a mismatch.
    """

    metric: UsageMetric
    price_id: uuid.UUID
    quantity: int                 # cumulative
    unit_size: int
    price_micros_per_unit: int
    rounding: RoundingMode
    price_micros: int             # cumulative charge for this line
    cost_micros: int              # cumulative upstream cost for this line


@dataclass(frozen=True)
class PricedUsage:
    """A session's whole cost, as of some set of cumulative quantities."""

    price_book_version_id: uuid.UUID
    lines: tuple[PricedLine, ...]
    price_micros: int
    cost_micros: int

    def line_for(self, metric: UsageMetric) -> PricedLine | None:
        return next((line for line in self.lines if line.metric is metric), None)


def price_cumulative(
    quantities: dict[UsageMetric, int],
    prices: dict[UsageMetric, Price],
    *,
    price_book_version_id: uuid.UUID,
) -> PricedUsage:
    """Price a full set of cumulative quantities.

    An unpriced metric is an error rather than a zero. A quantity we accepted
    but could not price is revenue we would never charge, and it would be
    invisible — so the caller gets a `400` and the microservice team gets told
    their metric is not in the price book.
    """
    lines: list[PricedLine] = []
    for metric, quantity in sorted(quantities.items(), key=lambda kv: kv[0].value):
        if quantity <= 0:
            continue
        price = prices.get(metric)
        if price is None:
            raise BadRequestError(
                f"'{metric.value}' is not priced for this service and model.",
                code="usage_metric_unknown",
            )
        lines.append(
            PricedLine(
                metric=metric,
                price_id=price.id,
                quantity=quantity,
                unit_size=price.unit_size,
                price_micros_per_unit=price.price_micros_per_unit,
                rounding=price.rounding,
                price_micros=line_micros(
                    quantity=quantity,
                    unit_size=price.unit_size,
                    rate_micros_per_unit=price.price_micros_per_unit,
                    rounding=price.rounding,
                    included_quantity=price.included_quantity,
                    min_charge_micros=price.min_charge_micros,
                ),
                # Cost has no minimum — a floor is a commercial decision about
                # what we charge, not a fact about what we paid.
                cost_micros=line_micros(
                    quantity=quantity,
                    unit_size=price.unit_size,
                    rate_micros_per_unit=price.cost_micros_per_unit,
                    rounding=price.rounding,
                    included_quantity=price.included_quantity,
                ),
            )
        )

    return PricedUsage(
        price_book_version_id=price_book_version_id,
        lines=tuple(lines),
        price_micros=sum(line.price_micros for line in lines),
        cost_micros=sum(line.cost_micros for line in lines),
    )


# --- resolution ------------------------------------------------------------


async def active_price_book(session: AsyncSession, at: datetime | None = None) -> PriceBookVersion:
    """The price book in force, or a `503`.

    Never guess and never fall back to zero: an unpriced deployment must refuse
    to sell rather than give the service away. `503` rather than `500` because
    the deployment is misconfigured, not broken — the same distinction
    `oauth/registry.py` draws for an unconfigured provider.
    """
    moment = at or utcnow()
    row = (
        await session.execute(
            select(PriceBookVersion)
            .where(
                PriceBookVersion.status == PriceBookStatus.ACTIVE,
                PriceBookVersion.effective_from <= moment,
            )
            .order_by(PriceBookVersion.effective_from.desc(), PriceBookVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        raise ServiceUnavailableError(
            "No price book is published on this server, so nothing can be billed.",
            code="price_book_missing",
        )
    return row


async def prices_for(
    session: AsyncSession,
    *,
    price_book_version_id: uuid.UUID,
    service: BillingService,
    model_key: str,
) -> dict[UsageMetric, Price]:
    """Every priced metric for one (service, model), exact rows beating `"*"`.

    One query for both, then resolved in Python — a `model_key IN (:exact, '*')`
    fetch is a single index scan, whereas asking the database to prefer one
    over the other needs a window function or two round trips.
    """
    rows = (
        await session.execute(
            select(Price).where(
                Price.price_book_version_id == price_book_version_id,
                Price.service == service,
                Price.model_key.in_({model_key, MODEL_KEY_ANY}),
            )
        )
    ).scalars()

    resolved: dict[UsageMetric, Price] = {}
    for price in rows:
        current = resolved.get(price.metric)
        if current is None or price.model_key != MODEL_KEY_ANY:
            resolved[price.metric] = price
    return resolved
