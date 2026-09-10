"""Alembic environment.

The URL comes from `Settings`, never from `alembic.ini`, so a migration can
only ever run against the database the app itself would open.

Both SQLite and Postgres are supported because the test suite runs on SQLite:
`render_as_batch` is switched on for SQLite, which is the only way ALTER TABLE
works there, and it is harmless on Postgres.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base

# Imported for the side effect of registering every mapper on `Base.metadata`.
# Without it autogenerate sees an empty schema and proposes dropping the world.
from app import models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_is_sqlite = settings.database_url.startswith("sqlite")


def _configure(connection: Connection | None = None, **extra: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite cannot ALTER a column; batch mode rewrites the table instead.
        render_as_batch=_is_sqlite,
        **extra,
    )


def run_migrations_offline() -> None:
    _configure(
        url=settings.database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
