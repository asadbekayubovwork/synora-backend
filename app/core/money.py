"""Micro-credits: the one money unit in this system.

Every balance, price and charge is an integer number of *micro-credits*. There
is no `Decimal` and no `float` anywhere on the money path, because both bring
rounding behaviour that depends on the value and on the dialect: SQLite hands
`NUMERIC` back through a C double, and a float balance is a balance that can
drift. Integers cannot drift, compare exactly, and let a decrement be a single
atomic `UPDATE`.

The scale is large on purpose. One LLM token can cost a small fraction of a
credit, so charges are routinely in the hundreds of micro-credits; a coarser
unit would round individual events to zero.

    1 credit = 1_000_000 micro-credits ("micros")

Fiat is separate and equally integral: UZS is carried in **tiyin**
(1 UZS = 100 tiyin), and the two are bridged only by `credits_for_tiyin`,
whose rate is recorded on the row that used it so an old receipt stays
reproducible after the rate changes.
"""

from __future__ import annotations

MICROS_PER_CREDIT = 1_000_000
TIYIN_PER_UZS = 100


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division, for non-negative numerators.

    `-(-a // b)` would also work, but only by relying on Python's floor
    division of negatives; this spelling says what it means and stays correct
    if a caller ever passes a negative by mistake.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    whole, remainder = divmod(numerator, denominator)
    return whole + 1 if remainder else whole


def half_up_div(numerator: int, denominator: int) -> int:
    """Round-half-up division, in integers only.

    `(2n + d) // 2d` is exact for non-negative integers and rounds a true .5
    upwards, which is what "half up" is expected to mean. Doing this in floats
    would round .5 to even and disagree with the invoice a customer sees.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (2 * numerator + denominator) // (2 * denominator)


def credits_for_tiyin(amount_tiyin: int, uzs_per_credit_tiyin: int) -> int:
    """Micro-credits bought by `amount_tiyin` at the given rate.

    `uzs_per_credit_tiyin` is the price of ONE credit, in tiyin — so 150 UZS
    per credit is 15_000. Floored, so a top-up never grants a fraction of a
    micro-credit that the ledger cannot represent; the remainder is a few
    millionths of a credit and is simply not granted.
    """
    if amount_tiyin < 0:
        raise ValueError("amount_tiyin must not be negative")
    if uzs_per_credit_tiyin <= 0:
        raise ValueError("uzs_per_credit_tiyin must be positive")
    return (amount_tiyin * MICROS_PER_CREDIT) // uzs_per_credit_tiyin


def tiyin_for_credits(micros: int, uzs_per_credit_tiyin: int) -> int:
    """The inverse, for quoting a shortfall as "top up at least this much".

    Rounded up, so the amount quoted actually covers the shortfall rather than
    landing a micro-credit short of it.
    """
    if micros < 0:
        raise ValueError("micros must not be negative")
    if uzs_per_credit_tiyin <= 0:
        raise ValueError("uzs_per_credit_tiyin must be positive")
    return ceil_div(micros * uzs_per_credit_tiyin, MICROS_PER_CREDIT)


def format_credits(micros: int) -> str:
    """Credits as a fixed-point string, for API responses and receipts.

    A string rather than a float: the wire format has to survive a JavaScript
    client, and `0.1 + 0.2` is the reason. Clients that do arithmetic use the
    `*_micros` integer alongside it.
    """
    sign = "-" if micros < 0 else ""
    whole, fraction = divmod(abs(micros), MICROS_PER_CREDIT)
    return f"{sign}{whole}.{fraction:06d}"


def format_uzs(amount_tiyin: int) -> str:
    """Som as a fixed-point string. Same reasoning as `format_credits`."""
    sign = "-" if amount_tiyin < 0 else ""
    whole, fraction = divmod(abs(amount_tiyin), TIYIN_PER_UZS)
    return f"{sign}{whole}.{fraction:02d}"
