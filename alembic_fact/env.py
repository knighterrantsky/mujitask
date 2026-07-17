from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from automation_business_scaffold.project_env import bootstrap_project_env
from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_VERSION_TABLE,
)

bootstrap_project_env()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

_FACT_MIGRATION_DB_URL_ENV = "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL"
_VERSION_TABLE = AMAZON_FACT_VERSION_TABLE


def _database_url() -> str:
    db_url = os.getenv(_FACT_MIGRATION_DB_URL_ENV, "").strip()
    if not db_url:
        raise RuntimeError(f"{_FACT_MIGRATION_DB_URL_ENV} is required for Fact DB migrations.")
    try:
        backend_name = make_url(db_url).get_backend_name()
    except ArgumentError as exc:
        raise RuntimeError(f"{_FACT_MIGRATION_DB_URL_ENV} is not a valid database URL.") from exc
    if backend_name != "postgresql":
        raise RuntimeError(f"{_FACT_MIGRATION_DB_URL_ENV} must identify a PostgreSQL database.")
    return db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=_VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table=_VERSION_TABLE,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
