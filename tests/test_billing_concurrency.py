"""True concurrency, against a real database.

Skipped on SQLite, and honestly so: its writers serialise at the file and
`with_for_update()` compiles to nothing there, so a "concurrency" test would
pass while proving nothing at all. Run these with

    TEST_DATABASE_URL=postgresql+asyncpg://synora_api:synora_api@localhost/synora_api_test \
        .venv/bin/python -m pytest tests/test_billing_concurrency.py
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.core.exceptions import PaymentRequiredError
from app.db.session import SessionLocal
from app.models.billing_enums import LedgerBucket, LedgerEntryKind, LedgerRefType
from app.models.ledger import LedgerEntry
from app.services.billing import wallet_repo, wallet_service
from tests.conftest import make_user, requires_postgres

CREDIT = 1_000_000

pytestmark = [requires_postgres]


async def _committed_wallet(paid_micros: int) -> uuid.UUID:
    """A funded wallet, committed, so other connections can see it."""
    async with SessionLocal() as db:
        user = await make_user(db)
        snapshot = await wallet_service.ensure_wallet(db, user.id)
        await wallet_repo.credit(
            db, wallet_id=snapshot.wallet_id, paid_micros=paid_micros,
            kind=LedgerEntryKind.TOPUP, ref_type=LedgerRefType.TOPUP,
            idempotency_key=f"seed:{uuid.uuid4()}",
        )
        await db.commit()
        return snapshot.wallet_id


async def _bucket_total(wallet_id: uuid.UUID, bucket: LedgerBucket) -> int:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_micros), 0)).where(
                    LedgerEntry.wallet_id == wallet_id, LedgerEntry.bucket == bucket
                )
            )
        ).scalar_one()


async def test_parallel_debits_cannot_overdraw():
    """Twenty callers, eight credits of balance. Exactly eight may win.

    Each runs on its own connection in its own transaction — going through the
    HTTP client would serialise on the test's single session and prove nothing.
    """
    attempts, affordable = 20, 8
    wallet_id = await _committed_wallet(affordable * CREDIT)

    async def one() -> tuple[str, int]:
        async with SessionLocal() as db:
            try:
                movement = await wallet_repo.debit(
                    db, wallet_id=wallet_id, amount_micros=CREDIT,
                    idempotency_key=f"race:{uuid.uuid4()}",
                    ref_type=LedgerRefType.USAGE_EVENT,
                )
                await db.commit()
                return "ok", movement.cas_attempts
            except PaymentRequiredError:
                return "declined", 0

    outcomes = await asyncio.gather(*(one() for _ in range(attempts)))
    results = [outcome for outcome, _ in outcomes]

    assert results.count("ok") == affordable
    assert results.count("declined") == attempts - affordable

    final = await _bucket_total(wallet_id, LedgerBucket.PAID)
    assert final == 0, "the balance landed exactly on zero, never below"
    async with SessionLocal() as db:
        snapshot = await wallet_repo.snapshot_by_id(db, wallet_id)
        assert snapshot.paid_micros == final


async def test_the_compare_and_swap_path_is_actually_exercised():
    """Without this the overdraw test would also pass on a database that
    serialises everything, and we would learn nothing from it."""
    attempts = 16
    wallet_id = await _committed_wallet(attempts * CREDIT)

    async def one() -> int:
        async with SessionLocal() as db:
            movement = await wallet_repo.debit(
                db, wallet_id=wallet_id, amount_micros=CREDIT,
                idempotency_key=f"cas:{uuid.uuid4()}",
                ref_type=LedgerRefType.USAGE_EVENT,
            )
            await db.commit()
            return movement.cas_attempts

    tries = await asyncio.gather(*(one() for _ in range(attempts)))

    assert sum(tries) > attempts, "no CAS retries happened; nothing contended"
    assert await _bucket_total(wallet_id, LedgerBucket.PAID) == 0


async def test_parallel_holds_cannot_together_exceed_the_balance():
    """The hold path is a single conditional UPDATE rather than a CAS loop,
    so it needs its own race."""
    attempts, affordable = 20, 5
    wallet_id = await _committed_wallet(affordable * CREDIT)

    async def one() -> str:
        async with SessionLocal() as db:
            try:
                await wallet_repo.place_hold(
                    db, wallet_id=wallet_id, amount_micros=CREDIT,
                    idempotency_key=f"hold:{uuid.uuid4()}",
                )
                await db.commit()
                return "ok"
            except PaymentRequiredError:
                return "declined"

    results = await asyncio.gather(*(one() for _ in range(attempts)))

    assert results.count("ok") == affordable
    async with SessionLocal() as db:
        snapshot = await wallet_repo.snapshot_by_id(db, wallet_id)
        assert snapshot.reserved_micros == affordable * CREDIT
        assert snapshot.available_micros == 0


async def test_ten_concurrent_replays_of_one_key_charge_once():
    """The idempotency key has to hold under a real race, not just in sequence."""
    wallet_id = await _committed_wallet(10 * CREDIT)
    key = f"idem:{uuid.uuid4()}"

    async def one() -> bool:
        async with SessionLocal() as db:
            movement = await wallet_repo.debit(
                db, wallet_id=wallet_id, amount_micros=2 * CREDIT,
                idempotency_key=key, ref_type=LedgerRefType.USAGE_EVENT,
            )
            await db.commit()
            return movement.replayed

    replayed = await asyncio.gather(*(one() for _ in range(10)))

    assert replayed.count(False) == 1, "exactly one caller did the work"
    assert replayed.count(True) == 9
    assert await _bucket_total(wallet_id, LedgerBucket.PAID) == 8 * CREDIT

    async with SessionLocal() as db:
        groups = (
            await db.execute(
                select(func.count(func.distinct(LedgerEntry.group_id))).where(
                    LedgerEntry.idempotency_key == key
                )
            )
        ).scalar_one()
        assert groups == 1


async def test_concurrent_wallet_creation_settles_on_one_row():
    """`ensure_wallet` races itself on a user's first two requests."""
    async with SessionLocal() as db:
        user = await make_user(db)
        await db.commit()
        user_id = user.id

    async def one() -> uuid.UUID:
        async with SessionLocal() as db:
            snapshot = await wallet_service.ensure_wallet(db, user_id)
            await db.commit()
            return snapshot.wallet_id

    ids = await asyncio.gather(*(one() for _ in range(8)))

    assert len(set(ids)) == 1
