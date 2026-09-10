"""The arithmetic, with no database anywhere near it.

Most of the money risk lives in these functions, and none of it needs a
connection — so these are the tests to reach for first when something bills
wrongly.
"""

from __future__ import annotations

import pytest

from app.core.money import (
    ceil_div,
    credits_for_tiyin,
    format_credits,
    format_uzs,
    half_up_div,
    tiyin_for_credits,
)
from app.models.billing_enums import RoundingMode as R
from app.services.billing.pricing import billable_units, line_micros
from app.services.billing.wallet_repo import split_bonus_first

MINUTE_MS = 60_000


# --- the anti-drift property ----------------------------------------------


def _settle_incrementally(cumulative_values: list[int], **price) -> tuple[int, list[int]]:
    """Walk a session the way the heartbeat path does, charging the difference."""
    settled = 0
    charges = []
    for cumulative in cumulative_values:
        total = line_micros(quantity=cumulative, **price)
        charges.append(total - settled)
        settled = total
    return settled, charges


def test_a_five_minute_call_costs_five_minutes_however_often_it_reports():
    """The whole reason prices are applied to cumulative quantities.

    Twenty 15-second heartbeats over a five-minute call. Pricing each *delta*
    with CEIL would round 15 seconds up to a minute twenty times and charge 4x.
    """
    price = dict(unit_size=MINUTE_MS, rate_micros_per_unit=1_000, rounding=R.CEIL)
    reports = [n * 15_000 for n in range(1, 21)]

    settled, charges = _settle_incrementally(reports, **price)

    assert settled == 5_000
    assert sum(charges) == settled
    # Only every fourth report crosses a minute boundary.
    assert [i for i, c in enumerate(charges) if c] == [0, 4, 8, 12, 16]
    naive = sum(line_micros(quantity=15_000, **price) for _ in reports)
    assert naive == 20_000, "the bug this test exists to catch"


@pytest.mark.parametrize("heartbeat_ms", [1_000, 5_000, 15_000, 60_000])
def test_the_total_does_not_depend_on_the_reporting_interval(heartbeat_ms):
    price = dict(unit_size=MINUTE_MS, rate_micros_per_unit=1_000, rounding=R.CEIL)
    reports = list(range(heartbeat_ms, 300_000 + 1, heartbeat_ms))

    settled, charges = _settle_incrementally(reports, **price)

    assert settled == 5_000
    assert sum(charges) == settled


def test_a_minimum_charge_lands_once_not_once_per_report():
    price = dict(
        unit_size=MINUTE_MS,
        rate_micros_per_unit=1_000,
        rounding=R.CEIL,
        min_charge_micros=2_500,
    )
    settled, charges = _settle_incrementally([n * 15_000 for n in range(1, 21)], **price)

    assert charges[0] == 2_500, "the floor arrives with the first report"
    assert settled == 5_000, "and is then absorbed, not re-applied"
    assert sum(charges) == settled


def test_the_minimum_applies_as_soon_as_anything_is_billable():
    assert line_micros(
        quantity=1, unit_size=MINUTE_MS, rate_micros_per_unit=1_000,
        rounding=R.CEIL, min_charge_micros=2_500,
    ) == 2_500


def test_nothing_billable_costs_nothing_not_even_the_minimum():
    """A session that recorded nothing must not be billed for existing."""
    assert line_micros(
        quantity=0, unit_size=1, rate_micros_per_unit=10,
        rounding=R.CEIL, min_charge_micros=99,
    ) == 0
    assert line_micros(
        quantity=1_000, unit_size=1, rate_micros_per_unit=10,
        rounding=R.CEIL, included_quantity=1_000, min_charge_micros=99,
    ) == 0


def test_an_included_allowance_is_subtracted_before_pricing():
    charged = line_micros(
        quantity=1_500, unit_size=1_000, rate_micros_per_unit=3_000,
        rounding=R.CEIL, included_quantity=1_000,
    )
    assert charged == 3_000, "500 over the allowance, rounded up to one unit"


# --- rounding modes --------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "mode", "expected"),
    [
        (1_500, R.CEIL, 6_000),
        (1_500, R.FLOOR, 3_000),
        (1_500, R.HALF_UP, 6_000),
        (1_400, R.HALF_UP, 3_000),
        (1_500, R.EXACT, 4_500),
        (1_001, R.EXACT, 3_003),
    ],
)
def test_rounding_modes(quantity, mode, expected):
    assert line_micros(
        quantity=quantity, unit_size=1_000, rate_micros_per_unit=3_000, rounding=mode
    ) == expected


def test_half_up_rounds_a_true_half_upwards():
    """Not to even. A customer's invoice should not depend on bankers' rounding."""
    assert billable_units(30, 60, R.HALF_UP) == 1
    assert billable_units(29, 60, R.HALF_UP) == 0
    assert billable_units(90, 60, R.HALF_UP) == 2
    assert half_up_div(1, 2) == 1


def test_ceil_charges_a_started_unit():
    assert billable_units(1, 60, R.CEIL) == 1
    assert billable_units(60, 60, R.CEIL) == 1
    assert billable_units(61, 60, R.CEIL) == 2
    assert ceil_div(0, 60) == 0


def test_a_negative_quantity_is_a_programming_error_not_a_credit():
    with pytest.raises(ValueError):
        line_micros(quantity=-1, unit_size=1, rate_micros_per_unit=1, rounding=R.CEIL)


def test_a_zero_unit_size_is_refused_rather_than_dividing_by_zero():
    with pytest.raises(ValueError):
        line_micros(quantity=10, unit_size=0, rate_micros_per_unit=1, rounding=R.CEIL)


# --- the bonus-first split -------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "bonus", "expected"),
    [
        (100, 0, (0, 100)),
        (100, 40, (40, 60)),
        (100, 100, (100, 0)),
        (100, 250, (100, 0)),
        (0, 250, (0, 0)),
    ],
)
def test_bonus_is_spent_before_paid_credit(amount, bonus, expected):
    split = split_bonus_first(amount, bonus)
    assert (split.from_bonus, split.from_paid) == expected
    assert split.total == amount


def test_a_negative_bonus_never_becomes_a_charge():
    """Defensive: a bonus column can only be >= 0, but the maths must not
    depend on the constraint holding."""
    split = split_bonus_first(100, -50)
    assert (split.from_bonus, split.from_paid) == (0, 100)


# --- fiat conversion -------------------------------------------------------


def test_credits_are_floored_at_an_awkward_rate():
    """1234 UZS per credit, so nothing divides evenly."""
    rate = 123_400  # tiyin per credit
    assert credits_for_tiyin(5_000_000, rate) == 40_518_638
    # Never grant more than was paid for.
    assert credits_for_tiyin(5_000_000, rate) * rate <= 5_000_000 * 1_000_000


def test_quoting_a_shortfall_rounds_up_so_it_actually_covers_it():
    rate = 123_400
    shortfall = 40_518_638
    quoted = tiyin_for_credits(shortfall, rate)
    assert credits_for_tiyin(quoted, rate) >= shortfall


def test_a_round_rate_is_exact():
    assert credits_for_tiyin(15_000, 15_000) == 1_000_000  # 150 UZS buys one credit


def test_display_strings_never_go_through_a_float():
    assert format_credits(1_205_000) == "1.205000"
    assert format_credits(-1_205_000) == "-1.205000"
    assert format_credits(1) == "0.000001"
    assert format_credits(0) == "0.000000"
    assert format_uzs(5_000_000) == "50000.00"
    assert format_uzs(1) == "0.01"
