from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path

import pytest

# Must be set before app.core.config is imported anywhere.
os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite+aiosqlite:///./test_synora.db",
        "JWT_SECRET": "test-secret-that-is-long-enough-for-hmac-sha256",
        "EXPOSE_DEV_OTP": "true",
        "SMTP_HOST": "",
        "OTP_RESEND_COOLDOWN_SECONDS": "0",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402

DB_FILE = Path("test_synora.db")


@pytest.fixture(autouse=True)
async def clean_database() -> AsyncGenerator[None]:
    """A fresh schema per test, so cases cannot leak users into each other."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test/api/v1") as ac:
        yield ac


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    # Windows refuses to unlink a file the engine still has open, so close it
    # first; a leftover file is not worth failing the run over either way.
    with suppress(Exception):
        asyncio.run(engine.dispose())
    with suppress(OSError):
        DB_FILE.unlink(missing_ok=True)
