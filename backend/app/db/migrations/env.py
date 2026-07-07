"""Alembic environment — async engine, metadata from the ORM models."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.core.config import settings
from app.db.base import Base

# Import models so their tables register on Base.metadata.
from app.db import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic drives a sync-style URL for offline; for online we build the async
# engine directly from settings so there's one source of truth.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate away from SQL-only artifacts.

    ``*_fts`` GIN expression indexes (migration 0005) exist only in SQL —
    without this filter autogenerate would propose dropping them, the same
    near-miss that once hit the chain-position unique index.
    """
    if type_ == "index" and name and name.endswith("_fts"):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
