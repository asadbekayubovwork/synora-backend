#!/usr/bin/env python3
"""Copy a SQLite database into a freshly migrated Postgres one.

    # 1. stop the API, so nothing writes to either side
    sudo systemctl stop synora-api
    # 2. create the schema on the target, at the same revision
    DATABASE_URL=postgresql+asyncpg://synora:...@localhost/synora .venv/bin/alembic upgrade head
    # 3. copy
    .venv/bin/python devtools/migrate_to_postgres.py \\
        --source sqlite+aiosqlite:///data/synora.db \\
        --target postgresql+asyncpg://synora:...@localhost/synora

Reads every table in foreign-key order, writes it to the target, then proves
the result rather than announcing it: row counts per table, and a full ledger
audit — every wallet's balance recomputed from its own entries. A money
migration that only says "done" is a migration nobody can sign off.

## It refuses a target that is not empty

Copying twice is the failure this guards against, and it is not a hypothetical
one: the obvious response to a copy that died halfway is to run it again.
`ledger_entries` has an append-only trigger, so the second run's duplicates
cannot be deleted afterwards — the recovery from a half-copied target is to
drop the database and recreate it, which is cheap while it is empty and
impossible to do by hand once the API has started serving from it.

## Naive datetimes become UTC

SQLite has no time zone type, so everything read back from it is naive even
though the columns are `DateTime(timezone=True)`. This codebase writes UTC
everywhere (`app/db/base.utcnow`), so that is what a naive value is stamped as
on the way in. Left alone, asyncpg would refuse them outright, and a driver
that guessed the server's local zone instead would shift every hold, charge and
expiry by the offset — five hours, in Tashkent, silently.

## What it deliberately does not copy

`alembic_version`. The target's revision comes from `alembic upgrade head` in
step 2, which is what makes this a copy into a schema that was *built*, rather
than a schema that was inherited along with whatever state the old database
happened to be in.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

# Before the `app` imports, as every script in this directory does: these are
# run as files rather than as modules, so the project root is not on the path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Registers every mapper on `Base.metadata`; without it the metadata is empty
# and this script copies nothing at all, cheerfully.
import app.models  # noqa: E402, F401
from app.db.base import Base  # noqa: E402

BATCH = 500


def utc(value: object) -> object:
    """A naive datetime as UTC. Everything else untouched."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def copy_table(source, target, table) -> int:
    """Every row of one table, in batches. Returns how many were written."""
    rows = (await source.execute(select(table))).mappings().all()
    if not rows:
        return 0

    payload = [{key: utc(value) for key, value in row.items()} for row in rows]
    for start in range(0, len(payload), BATCH):
        await target.execute(insert(table), payload[start : start + BATCH])
    return len(payload)


async def count(connection, table) -> int:
    return (
        await connection.execute(select(func.count()).select_from(table))
    ).scalar_one()


async def audit(target_url: str) -> tuple[int, int]:
    """Recompute every wallet from its own ledger. Returns (checked, diverged).

    The same `verify_all` reconciliation runs, pointed at the target — so the
    question this answers is not "did the rows arrive" but "does the money
    still add up on the other side", which is the only one worth asking of a
    billing database.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.billing import reconcile_service

    engine = create_async_engine(target_url, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            audits = await reconcile_service.verify_all(session, limit=100_000)
    finally:
        await engine.dispose()

    diverged = [
        a
        for a in audits
        if not a.balances_match or a.first_divergent_group_id is not None
    ]
    for bad in diverged:
        print(
            f"  ! wallet {bad.wallet_id}: paid {bad.paid_micros} vs "
            f"{bad.paid_ledger_micros}, bonus {bad.bonus_micros} vs "
            f"{bad.bonus_ledger_micros}, reserved {bad.reserved_micros} vs "
            f"{bad.reserved_ledger_micros}"
        )
    return len(audits), len(diverged)


async def migrate(source_url: str, target_url: str, *, force: bool) -> int:
    source_engine = create_async_engine(source_url)
    target_engine = create_async_engine(target_url)

    tables = list(Base.metadata.sorted_tables)
    written: dict[str, int] = {}
    mismatched: list[str] = []

    try:
        async with source_engine.connect() as source, target_engine.begin() as target:
            # Counting every table is also the check that every table exists:
            # a missing one raises here, before a single row has been written,
            # which is what happens when `alembic upgrade head` was skipped.
            occupied = [t.name for t in tables if await count(target, t)]
            if occupied and not force:
                print(
                    "Refusing to copy into a target that already has rows in: "
                    + ", ".join(occupied),
                    file=sys.stderr,
                )
                print(
                    "`ledger_entries` is append-only, so duplicates cannot be "
                    "deleted afterwards. Drop the database and recreate it, then "
                    "`alembic upgrade head` and run this again.",
                    file=sys.stderr,
                )
                return 2

            for table in tables:
                written[table.name] = await copy_table(source, target, table)

        # Counted on fresh connections, after the commit, so the numbers are
        # what the next process will read rather than what this one wrote.
        async with source_engine.connect() as source, target_engine.connect() as target:
            print(f"{'table':28} {'source':>8} {'target':>8}")
            for table in tables:
                there = await count(source, table)
                here = await count(target, table)
                flag = "" if there == here else "  <-- MISMATCH"
                if there != here:
                    mismatched.append(table.name)
                print(f"{table.name:28} {there:>8} {here:>8}{flag}")
    finally:
        await source_engine.dispose()
        await target_engine.dispose()

    if mismatched:
        print("\nRow counts disagree: " + ", ".join(mismatched), file=sys.stderr)
        return 1

    print("\nAuditing the ledger on the target...")
    checked, diverged = await audit(target_url)
    if diverged:
        print(
            f"\n{diverged} of {checked} wallets do not match their ledger. "
            "Do not point the API at this database.",
            file=sys.stderr,
        )
        return 1

    print(f"{checked} wallet(s) checked, none diverged.")
    print(
        "\nCopied. Point DATABASE_URL at the target, start the API, and keep the "
        "SQLite file until the first day's charges have been read back."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="sqlite+aiosqlite:///…")
    parser.add_argument("--target", required=True, help="postgresql+asyncpg://…")
    parser.add_argument(
        "--force",
        action="store_true",
        help="copy even if the target has rows. Almost always the wrong answer.",
    )
    args = parser.parse_args()

    if not args.target.startswith("postgresql"):
        print("--target must be a postgresql+asyncpg:// URL", file=sys.stderr)
        return 2

    return asyncio.run(migrate(args.source, args.target, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
