from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base

logger = logging.getLogger("synora.db")

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug and not _is_sqlite,
    future=True,
    # SQLite's async driver holds a single file handle; pooling options that
    # suit Postgres are either ignored or actively unhelpful there.
    **({} if _is_sqlite else {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20}),
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on failure.

    Never commits — every service function owns its own commit. And never use
    it inside a streaming response or a background task: FastAPI closes the
    dependency when the *request* ends, which for a stream is before the
    generator has finished. Those callers open `SessionLocal()` themselves.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create any missing tables — SQLite only.

    Alembic is the schema authority now. `create_all` stays for local SQLite
    development and for the test suite, where a fresh file per run is the whole
    point; on anything else it is actively dangerous, because it would create
    tables Alembic does not know it created and then never alter them again.
    """
    if not _is_sqlite:
        logger.info("Skipping create_all: Alembic owns this schema. Run `alembic upgrade head`.")
        return

    # Imported for the side effect of registering the mappers on `Base.metadata`.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()
