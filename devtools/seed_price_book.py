#!/usr/bin/env python3
"""Publish a starter price book and som-per-credit rate.

Prices live in the database and are published through the admin API, so a
change needs no deploy and every old invoice stays reproducible. That leaves a
fresh install with no prices at all, which is correct — the backend refuses to
sell rather than guess — but tedious for local work. This is the bootstrap.

**The numbers here are placeholders.** They are round so that arithmetic in a
test reads as arithmetic. Replace them before anyone is charged, either by
editing this file for a fresh install or by publishing a new version through
`POST /api/v1/admin/price-books` on a running one.

    python3 devtools/seed_price_book.py
    python3 devtools/seed_price_book.py --uzs-per-credit 200
    python3 devtools/seed_price_book.py --show
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

MINUTE_MS = 60_000
CREDIT = 1_000_000

# (service, metric, unit_size, price_per_unit, cost_per_unit, min_charge)
#
# `unit_size` is how many base units make one priced unit: 1000 for
# per-1k-tokens, 60000 for per-minute when the metric is milliseconds.
# `cost_per_unit` is what the unit costs us upstream, in the same micro-credit
# unit as the price, so margin is a subtraction rather than a currency join.
PLACEHOLDER_PRICES = [
    # Text to speech, per 1000 characters.
    ("tts", "tts_characters", 1_000, 250_000, 100_000, 0),
    # Speech to text, per minute of audio.
    ("stt", "stt_audio_ms", MINUTE_MS, 1_200_000, 500_000, 0),
    # Chat, per 1000 tokens. Cached input is cheap because it is cheap for us.
    ("chat", "llm_input_tokens", 1_000, 3_000_000, 1_000_000, 0),
    ("chat", "llm_cached_input_tokens", 1_000, 300_000, 100_000, 0),
    ("chat", "llm_output_tokens", 1_000, 12_000_000, 4_000_000, 0),
    # The voice agent bills its components, plus a connection fee per minute
    # with a floor — so a ten-second call is not free, and a long silent one is
    # not either.
    ("voice_agent", "session_ms", MINUTE_MS, 500_000, 0, 250_000),
    ("voice_agent", "stt_audio_ms", MINUTE_MS, 1_200_000, 500_000, 0),
    ("voice_agent", "tts_characters", 1_000, 250_000, 100_000, 0),
    ("voice_agent", "llm_input_tokens", 1_000, 3_000_000, 1_000_000, 0),
    ("voice_agent", "llm_cached_input_tokens", 1_000, 300_000, 100_000, 0),
    ("voice_agent", "llm_output_tokens", 1_000, 12_000_000, 4_000_000, 0),
]


def env_value(name: str) -> str | None:
    if value := os.environ.get(name):
        return value
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


async def show(database_url: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.money import format_credits, format_uzs
    from app.models.credit_rate import CreditRate
    from app.models.price_book import Price, PriceBookVersion

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            books = list((await db.execute(select(PriceBookVersion).order_by(PriceBookVersion.version))).scalars())
            if not books:
                print("No price book. Nothing can be billed; run this without --show.")
                return 0
            for book in books:
                print(f"price book v{book.version} [{book.status.value}] {book.label}")
                prices = list(
                    (
                        await db.execute(
                            select(Price)
                            .where(Price.price_book_version_id == book.id)
                            .order_by(Price.service, Price.metric)
                        )
                    ).scalars()
                )
                for price in prices:
                    per = f"per {price.unit_size}" if price.unit_size != 1 else "each"
                    margin = price.price_micros_per_unit - price.cost_micros_per_unit
                    print(
                        f"  {price.service.value:12} {price.metric.value:26} "
                        f"{format_credits(price.price_micros_per_unit):>14} {per:<14} "
                        f"margin {format_credits(margin)}"
                    )
            rates = list((await db.execute(select(CreditRate).order_by(CreditRate.version))).scalars())
            for rate in rates:
                print(
                    f"credit rate v{rate.version} [{rate.status.value}]: "
                    f"1 credit = {format_uzs(rate.uzs_per_credit_tiyin)} UZS"
                )
            return 0
    finally:
        await engine.dispose()


async def seed(database_url: str, uzs_per_credit: int, label: str) -> int:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.billing_enums import (
        BillingService,
        CreditRateStatus,
        PriceBookStatus,
        RoundingMode,
        UsageMetric,
    )
    from app.models.credit_rate import CreditRate
    from app.models.price_book import MODEL_KEY_ANY, Price, PriceBookVersion

    engine = create_async_engine(database_url)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            existing = (
                await db.execute(
                    select(PriceBookVersion).where(
                        PriceBookVersion.status == PriceBookStatus.ACTIVE
                    )
                )
            ).scalars().first()
            if existing is not None:
                print(
                    f"Price book v{existing.version} is already active. "
                    "Publish a new version through the admin API instead of reseeding.",
                    file=sys.stderr,
                )
                return 1

            now = datetime.now(UTC)
            highest = (await db.execute(select(PriceBookVersion.version))).scalars().all()
            book = PriceBookVersion(
                version=(max(highest) + 1) if highest else 1,
                label=label,
                status=PriceBookStatus.ACTIVE,
                effective_from=now,
                published_at=now,
                notes="Seeded by devtools/seed_price_book.py. Placeholder numbers.",
            )
            db.add(book)
            await db.flush()

            for service, metric, unit, rate, cost, minimum in PLACEHOLDER_PRICES:
                db.add(
                    Price(
                        price_book_version_id=book.id,
                        service=BillingService(service),
                        model_key=MODEL_KEY_ANY,
                        metric=UsageMetric(metric),
                        unit_size=unit,
                        price_micros_per_unit=rate,
                        cost_micros_per_unit=cost,
                        min_charge_micros=minimum,
                        rounding=RoundingMode.CEIL,
                        display_unit=(
                            "per minute" if unit == MINUTE_MS
                            else f"per {unit:,}" if unit != 1 else "each"
                        ),
                    )
                )

            rate_versions = (await db.execute(select(CreditRate.version))).scalars().all()
            db.add(
                CreditRate(
                    version=(max(rate_versions) + 1) if rate_versions else 1,
                    uzs_per_credit_tiyin=uzs_per_credit * 100,
                    status=CreditRateStatus.ACTIVE,
                    effective_from=now,
                    published_at=now,
                    note="Seeded placeholder. Confirm with whoever owns pricing.",
                )
            )
            version = book.version
            await db.commit()

            print(f"Published price book v{version} with {len(PLACEHOLDER_PRICES)} prices.")
            print(f"1 credit = {uzs_per_credit} UZS.")
            print("These are placeholders — replace them before anyone is charged.")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--uzs-per-credit", type=int, default=150, help="som per credit (default: 150)"
    )
    parser.add_argument("--label", default="seed", help="a name for this price book version")
    parser.add_argument("--show", action="store_true", help="print what is published and exit")
    args = parser.parse_args()

    if args.uzs_per_credit <= 0:
        parser.error("--uzs-per-credit must be positive")

    database_url = env_value("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set, and .env does not define it.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT))
    if args.show:
        return asyncio.run(show(database_url))
    return asyncio.run(seed(database_url, args.uzs_per_credit, args.label))


if __name__ == "__main__":
    raise SystemExit(main())
