from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import pytest

# Must be set before app.core.config is imported anywhere: `settings` is an
# lru_cache singleton, so the first import wins for the whole session.
#
# TEST_DATABASE_URL lets the money tests run against a real Postgres:
#
#   TEST_DATABASE_URL=postgresql+asyncpg://synora_api:synora_api@localhost/synora_api_test \
#       pytest -m pg
#
# Without it everything runs on SQLite, and the tests marked `pg` skip. That
# is only defensible because the statements under test are portable — see the
# module docstring of app/services/billing/wallet_repo.py.
#
# One database per run. `clean_database` drops and recreates the whole schema
# before every test, so two pytest processes pointed at the same Postgres will
# tear each other's tables down mid-test and fail in ways that look like
# application bugs — duplicate keys, rows that vanish between statements. If
# you want to run two suites at once, give them separate databases.
os.environ.update(
    {
        "ENVIRONMENT": "development",
        # The engine turns on SQL echo when `debug` is true and the dialect is
        # not SQLite, so a Postgres run would print every statement — including
        # the whole schema, twice per test. That buries the actual traceback,
        # which is exactly when you need to read it.
        "DEBUG": "false",
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "sqlite+aiosqlite:///./test_synora.db"
        ),
        "JWT_SECRET": "test-secret-that-is-long-enough-for-hmac-sha256",
        "EXPOSE_DEV_OTP": "true",
        "SMTP_HOST": "",
        "OTP_RESEND_COOLDOWN_SECONDS": "0",
        # Empty on purpose: the whole suite runs the no-Redis fallback path, so
        # every Redis shortcut has a proven Postgres equivalent rather than a
        # theoretical one.
        "REDIS_URL": "",
        "INTERNAL_KEY_SECRET": "test-internal-secret-that-is-long-enough-here",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.base import Base, utcnow  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.billing_enums import (  # noqa: E402
    BillingService,
    PriceBookStatus,
    RoundingMode,
    UsageMetric,
)
from app.models.credit_rate import CreditRate  # noqa: E402
from app.models.billing_enums import CreditRateStatus  # noqa: E402
from app.models.price_book import MODEL_KEY_ANY, Price, PriceBookVersion  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.billing import wallet_repo, wallet_service  # noqa: E402
from app.models.billing_enums import LedgerEntryKind, LedgerRefType  # noqa: E402

DB_FILE = Path("test_synora.db")

IS_POSTGRES = engine.dialect.name == "postgresql"

# Row locking and true concurrency need a real database. `with_for_update()`
# is a silent no-op on SQLite and its writers serialise at the file, so a
# concurrency test there would pass without proving anything.
requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES, reason="needs PostgreSQL; set TEST_DATABASE_URL"
)


# One arbitrary constant, shared by every process that runs this suite.
_SUITE_LOCK_ID = 0x5901_0A11


@pytest.fixture(autouse=True)
async def clean_database() -> AsyncGenerator[None]:
    """A fresh schema per test, so cases cannot leak users into each other.

    On Postgres this also takes an advisory lock for the duration of the test.
    Dropping and recreating the whole schema before every test means two pytest
    processes pointed at the same database tear each other's tables down
    mid-test, and the failures that produces look nothing like the cause: rows
    that vanish between two statements, a duplicate key on an email the test
    just created, and a *different* test failing on each run. So a second
    process queues here instead of interleaving.

    The lock is held on its own connection, created and closed inside this
    fixture. It cannot come from the pool, because `engine.dispose()` below
    would hand it back and release the lock halfway through the test.

    Nothing to do on SQLite: every run has its own file.
    """
    lock_connection = None
    if IS_POSTGRES:
        from sqlalchemy import text

        lock_connection = await engine.connect()
        await lock_connection.execute(
            text("SELECT pg_advisory_lock(:id)"), {"id": _SUITE_LOCK_ID}
        )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield
    finally:
        if lock_connection is not None:
            from sqlalchemy import text

            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:id)"), {"id": _SUITE_LOCK_ID}
            )
            await lock_connection.close()
        # Each test gets its own event loop (`asyncio_default_fixture_loop_scope
        # = function`), and the engine is a module-level singleton with a real
        # pool on Postgres. A pooled connection outliving its loop surfaces as
        # "Event loop is closed" in the *next* test's teardown, which is a
        # thoroughly misleading place to debug. Dropping the pool between tests
        # costs a reconnect and buys an honest failure mode.
        await engine.dispose()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as ac:
        yield ac


@pytest.fixture
async def session() -> AsyncGenerator:
    """A session for talking to the service layer directly.

    Not the request-scoped one — service-layer tests do not go through HTTP,
    and several of them need more than one connection at a time.
    """
    async with SessionLocal() as db:
        yield db


# --- HTTP helpers ----------------------------------------------------------


async def register_and_verify(client, email: str = "ali@example.com", password: str = "Str0ngPassw0rd"):
    """A signed-in user, over HTTP. Returns the token payload.

    Promoted from the copies in `test_auth.py` and `test_password_reset.py`,
    because every wallet and usage test needs it too.
    """
    sent = await client.post("/auth/register", json={"email": email, "password": password})
    code = sent.json()["dev_code"]
    verified = await client.post("/auth/verify-otp", json={"email": email, "code": code})
    return verified.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- billing fixtures ------------------------------------------------------


async def make_user(db, *, email: str | None = None) -> User:
    """A verified, active user. Registration is tested elsewhere."""
    user = User(
        email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
        is_verified=True,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def fund(db, user_id: uuid.UUID, *, paid: int = 0, bonus: int = 0, bonus_days: int | None = None):
    """Put credit in a wallet the way a top-up would."""
    snapshot = await wallet_service.ensure_wallet(db, user_id)
    if paid:
        await wallet_repo.credit(
            db,
            wallet_id=snapshot.wallet_id,
            paid_micros=paid,
            kind=LedgerEntryKind.TOPUP,
            ref_type=LedgerRefType.TOPUP,
            idempotency_key=f"test-paid:{uuid.uuid4()}",
        )
    if bonus:
        await wallet_repo.credit(
            db,
            wallet_id=snapshot.wallet_id,
            bonus_micros=bonus,
            bonus_expires_at=utcnow() + timedelta(days=bonus_days) if bonus_days else None,
            kind=LedgerEntryKind.BONUS_GRANT,
            ref_type=LedgerRefType.ADMIN_GRANT,
            idempotency_key=f"test-bonus:{uuid.uuid4()}",
        )
    return await wallet_repo.snapshot_by_id(db, snapshot.wallet_id)


@pytest.fixture
async def wallet(session):
    """A user with one credit of paid balance, committed."""
    user = await make_user(session)
    snapshot = await fund(session, user.id, paid=1_000_000)
    await session.commit()
    return snapshot


@pytest.fixture
async def price_book(session) -> PriceBookVersion:
    """A published price book covering every service and metric.

    Deliberately round numbers rather than realistic ones — a test asserting
    on 1000 micros per minute reads as arithmetic, and a test asserting on
    1_237_000 reads as a magic constant.
    """
    book = PriceBookVersion(
        version=1,
        label="test",
        status=PriceBookStatus.ACTIVE,
        effective_from=utcnow() - timedelta(days=1),
        published_at=utcnow() - timedelta(days=1),
    )
    session.add(book)
    await session.flush()

    def price(service: BillingService, metric: UsageMetric, *, unit: int, rate: int,
              cost: int = 0, minimum: int = 0) -> Price:
        return Price(
            price_book_version_id=book.id,
            service=service,
            model_key=MODEL_KEY_ANY,
            metric=metric,
            unit_size=unit,
            price_micros_per_unit=rate,
            cost_micros_per_unit=cost,
            rounding=RoundingMode.CEIL,
            min_charge_micros=minimum,
        )

    minute = 60_000  # metrics are in milliseconds
    session.add_all(
        [
            price(BillingService.TTS, UsageMetric.TTS_CHARACTERS, unit=1000, rate=250_000, cost=100_000),
            price(BillingService.STT, UsageMetric.STT_AUDIO_MS, unit=minute, rate=1_200_000, cost=500_000),
            # The realtime socket's connection fee, beside the audio. No
            # minimum: CEIL to the started minute is already the floor, which
            # `test_stt_stream.py` asserts on directly.
            price(BillingService.STT, UsageMetric.SESSION_MS, unit=minute, rate=200_000),
            price(BillingService.CHAT, UsageMetric.LLM_INPUT_TOKENS, unit=1000, rate=3_000_000, cost=1_000_000),
            price(BillingService.CHAT, UsageMetric.LLM_CACHED_INPUT_TOKENS, unit=1000, rate=300_000),
            price(BillingService.CHAT, UsageMetric.LLM_OUTPUT_TOKENS, unit=1000, rate=12_000_000, cost=4_000_000),
            # The voice agent is the composite: components plus a connection
            # fee with a floor, so a very short call is not free.
            price(BillingService.VOICE_AGENT, UsageMetric.SESSION_MS, unit=minute, rate=500_000, minimum=250_000),
            price(BillingService.VOICE_AGENT, UsageMetric.STT_AUDIO_MS, unit=minute, rate=1_200_000),
            price(BillingService.VOICE_AGENT, UsageMetric.TTS_CHARACTERS, unit=1000, rate=250_000),
            price(BillingService.VOICE_AGENT, UsageMetric.LLM_INPUT_TOKENS, unit=1000, rate=3_000_000),
            price(BillingService.VOICE_AGENT, UsageMetric.LLM_OUTPUT_TOKENS, unit=1000, rate=12_000_000),
        ]
    )
    await session.commit()
    return book


@pytest.fixture
async def service_key(session):
    """A minted service key with every scope, committed.

    Returns the `MintedKey`, which is the only place the secret is ever
    visible — there is no endpoint that can hand it back.
    """
    from app.models.billing_enums import BillingService
    from app.services.billing import service_key_service

    minted = await service_key_service.mint(
        session, label="test voice agent", service=BillingService.VOICE_AGENT
    )
    await session.commit()
    return minted


def sign_internal(minted, *, method: str, path: str, query: str = "", body: bytes = b"",
                  timestamp: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """Headers for a signed internal request, built the way a client would."""
    import time

    from app.core import signing

    return signing.sign_request(
        key_id=minted.key_id,
        secret=minted.secret,
        method=method,
        path=path,
        query=query,
        body=body,
        timestamp=timestamp if timestamp is not None else int(time.time()),
        nonce=nonce,
    ).headers


@pytest.fixture
async def credit_rate(session) -> CreditRate:
    """150 UZS per credit."""
    rate = CreditRate(
        version=1,
        uzs_per_credit_tiyin=15_000,
        status=CreditRateStatus.ACTIVE,
        effective_from=utcnow() - timedelta(days=1),
        published_at=utcnow() - timedelta(days=1),
    )
    session.add(rate)
    await session.commit()
    return rate


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    # Windows refuses to unlink a file the engine still has open, so close it
    # first; a leftover file is not worth failing the run over either way.
    with suppress(Exception):
        asyncio.run(engine.dispose())
    with suppress(OSError):
        DB_FILE.unlink(missing_ok=True)
