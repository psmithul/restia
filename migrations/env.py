"""Alembic environment for the staged Restia schema-authority migration."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from core.database import Base


config = context.config
if config.config_file_name is not None:
    # Alembic runs in-process for the local stamp/status CLI and tests. The
    # logging module's default disables every pre-existing named logger, which
    # silently breaks application diagnostics (and pytest capture) after a
    # migration. Configure Alembic without mutating those logger states.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = os.getenv("DATABASE_URL", "").strip()
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

# Metadata is available for explicit future autogeneration reviews. Revisions
# must contain deterministic operations and may not call create_all(metadata).
target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    """Keep SQLite FTS5 virtual/shadow tables outside ORM autogeneration.

    The 0002 revision manages these optional auxiliary objects explicitly.
    Without this filter, Alembic sees them only in reflection and proposes six
    destructive ``drop_table`` operations on every future autogenerate run.
    """

    if (
        reflected
        and type_ == "table"
        and (name == "chat_messages_fts" or name.startswith("chat_messages_fts_"))
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(
            connection=supplied_connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
