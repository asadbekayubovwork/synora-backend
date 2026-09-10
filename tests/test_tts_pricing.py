"""What a piece of text costs, before anything has been synthesised.

Speech is sold by input characters, and that choice is what makes every other
test in this suite possible to write: the whole input is in the request body,
so the price is a fact before the GPU is touched, the hold is the price rather
than a guess at it, and a settlement can never exceed what was held. If the
arithmetic here drifts, the hold stops bounding the charge and
`session_service.settle_oneshot`'s grace write-off quietly starts absorbing
ordinary revenue.

So this file points at `session_service.quote` rather than at the pure
functions in `pricing` — those have their own tests in
`test_billing_pricing.py`. What is under test here is the whole path a caller
takes: the active price book, the `"*"` model-key fallback, CEIL rounding on a
1000-character unit, and the refusal that guards the one mistake that would be
raised out of a `finally` where nobody can catch it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.models.billing_enums import BillingService, UsageMetric
from app.models.ledger import LedgerEntry
from app.models.wallet import Wallet
from app.services.billing import session_service
from tests.conftest import make_user

# One unit is 1000 characters at a quarter of a credit, from the `price_book`
# fixture. Spelled out because every expectation below is arithmetic on it.
UNIT_CHARACTERS = 1_000
UNIT_MICROS = 250_000
UNIT_COST_MICROS = 100_000


async def _quote(session, characters: int):
    return await session_service.quote(
        session,
        service=BillingService.TTS,
        model_key=settings.tts_model_key,
        quantities={UsageMetric.TTS_CHARACTERS: characters},
    )


# --- the unit ---------------------------------------------------------------


async def test_a_thousand_characters_is_one_unit(session, price_book):
    quoted = await _quote(session, UNIT_CHARACTERS)

    assert quoted.price_micros == UNIT_MICROS


async def test_one_character_more_is_a_whole_second_unit(session, price_book):
    """CEIL, and it is the rounding mode on purpose: a started unit is a
    charged unit, so the 1001st character costs as much as the previous
    thousand did."""
    quoted = await _quote(session, UNIT_CHARACTERS + 1)

    assert quoted.price_micros == 2 * UNIT_MICROS


@pytest.mark.parametrize(
    ("characters", "micros"),
    [
        (0, 0),
        (1, UNIT_MICROS),
        (999, UNIT_MICROS),
        (1_000, UNIT_MICROS),
        (1_001, 2 * UNIT_MICROS),
        (2_000, 2 * UNIT_MICROS),
        (2_001, 3 * UNIT_MICROS),
        # The streaming ceiling, which is the largest single call this API will
        # accept and therefore the largest hold one request can place.
        (5_000, 5 * UNIT_MICROS),
    ],
)
async def test_the_price_is_a_step_function_of_the_character_count(
    session, price_book, characters, micros
):
    assert (await _quote(session, characters)).price_micros == micros


async def test_nothing_to_say_is_priced_at_nothing_rather_than_a_minimum(session, price_book):
    """The TTS row carries no `min_charge_micros`, unlike the voice agent's
    connection fee. An empty quote therefore has no lines at all, which is what
    keeps a zero-length settlement from writing a ledger entry."""
    quoted = await _quote(session, 0)

    assert quoted.price_micros == 0
    assert quoted.lines == ()


# --- what the quote carries -------------------------------------------------


async def test_the_quote_names_the_book_it_was_priced_against(session, price_book):
    """The one way to be quoted one number and charged another is for a new
    price book to be published between the two calls, so the version travels
    with the quote and is pinned onto the session at `open_oneshot`."""
    quoted = await _quote(session, UNIT_CHARACTERS)

    assert quoted.price_book_version_id == price_book.id


async def test_one_line_per_metric_with_the_numbers_that_produced_it(session, price_book):
    quoted = await _quote(session, 2 * UNIT_CHARACTERS)
    (line,) = quoted.lines

    assert line.metric is UsageMetric.TTS_CHARACTERS
    assert line.quantity == 2 * UNIT_CHARACTERS
    assert line.unit_size == UNIT_CHARACTERS
    assert line.price_micros_per_unit == UNIT_MICROS
    assert line.price_micros == 2 * UNIT_MICROS


async def test_what_the_gpu_time_costs_us_is_quoted_too_and_never_shipped(session, price_book):
    """`cost_micros` is on the quote so margin is reportable from
    `usage_events`. `schemas/tts.py::estimate_response` drops it on the way to
    the wire, which is the reason that builder exists at all."""
    quoted = await _quote(session, 2 * UNIT_CHARACTERS)

    assert quoted.cost_micros == 2 * UNIT_COST_MICROS


async def test_a_model_key_with_no_row_of_its_own_falls_back_to_the_wildcard(
    session, price_book
):
    """The price book prices `tts`/`*`, and `TTS_MODEL_KEY` is whatever this
    deployment calls its voice box. A missing exact row must resolve to the
    wildcard rather than to "unpriced", or every synthesis 400s the day someone
    renames the model."""
    named = await _quote(session, UNIT_CHARACTERS)

    wildcard = await session_service.quote(
        session,
        service=BillingService.TTS,
        model_key="some-model-nobody-priced",
        quantities={UsageMetric.TTS_CHARACTERS: UNIT_CHARACTERS},
    )

    assert named.price_micros == wildcard.price_micros == UNIT_MICROS


# --- the refusal that guards the settlement ---------------------------------


async def test_an_unpriced_metric_is_refused_rather_than_charged_at_zero(session, price_book):
    """The trap the whole TTS path is shaped to avoid.

    There is a `tts`/`tts_characters` row and no `tts`/`tts_audio_ms` one, so
    reporting audio duration "for the dashboard" raises — and on the streaming
    path it would raise inside the `finally` that settles a stream whose
    response has already gone out, where nobody is left to catch it. Duration
    belongs on a response header and a log line. See the long comment in
    `session_service.settle_oneshot`.
    """
    with pytest.raises(BadRequestError) as raised:
        await session_service.quote(
            session,
            service=BillingService.TTS,
            model_key=settings.tts_model_key,
            quantities={
                UsageMetric.TTS_CHARACTERS: UNIT_CHARACTERS,
                UsageMetric.TTS_AUDIO_MS: 4_200,
            },
        )

    assert raised.value.code == "usage_metric_unknown"


async def test_an_unpriced_metric_at_zero_is_not_an_error(session, price_book):
    """Only a quantity above zero has to be priced. A session that recorded no
    audio at all reports `cum_tts_audio_ms = 0`, and that must stay billable."""
    quoted = await _quote(session, UNIT_CHARACTERS)

    assert quoted.price_micros == UNIT_MICROS


# --- what quoting does not do -----------------------------------------------


async def test_quoting_touches_no_wallet_and_moves_no_credit(session, price_book):
    """`POST /tts/estimate` is free in every sense: no session row, no hold, no
    ledger entry, not even a wallet created as a side effect."""
    user = await make_user(session)
    await session.commit()

    quoted = await _quote(session, 4 * UNIT_CHARACTERS)

    assert quoted.price_micros == 4 * UNIT_MICROS
    wallets = (
        await session.execute(
            select(func.count()).select_from(Wallet).where(Wallet.user_id == user.id)
        )
    ).scalar_one()
    entries = (
        await session.execute(select(func.count()).select_from(LedgerEntry))
    ).scalar_one()
    assert (wallets, entries) == (0, 0)
