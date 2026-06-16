"""Alembic environment configuration for async SQLAlchemy.

This configures Alembic to work with asyncpg and the OpenZep models.

The database URL is resolved in this priority order:
  1. ``MG_DATABASE_URL`` environment variable
  2. ``sqlalchemy.url`` in ``alembic.ini``
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """Resolve the database URL from env var or alembic.ini."""
    return os.environ.get("MG_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with a connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _db_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"statement_cache_size": 0},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Supports two modes:

    1. **Sync mode** (used by integration tests): A connection is provided
       via ``config.attributes["connection"]``.  The migrations run
       synchronously on that connection.

    2. **Async mode** (default): An async engine is created from the config
       URL and migrations run asynchronously.  This is the normal Alembic
       workflow from the CLI.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        # Sync mode — caller provided a pre-existing connection
        do_run_migrations(connection)
    else:
        # Async mode — create engine from config
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
