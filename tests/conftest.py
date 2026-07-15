from __future__ import annotations

import os
import uuid

import pytest
from automation_business_scaffold.project_env import bootstrap_project_env
from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_SCHEMA_REVISION,
    AMAZON_FACT_VERSION_TABLE,
    ensure_amazon_fact_schema,
)
from automation_business_scaffold.infrastructure.schemas.fact_schema import ensure_tk_fact_schema
from automation_business_scaffold.infrastructure.schemas.runtime_schema import ensure_runtime_schema

bootstrap_project_env()

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
except ModuleNotFoundError:  # pragma: no cover - exercised only in lean local envs.
    create_engine = None
    text = None
    make_url = None


def _merge_pg_options(base_url: str, *new_options: str) -> str:
    if make_url is None:  # pragma: no cover - protected by fixture gate.
        return base_url

    url = make_url(base_url)
    query = dict(url.query)
    existing_options = str(query.get("options") or "").strip()
    parts = [part for part in [existing_options, *new_options] if part]
    query["options"] = " ".join(parts)
    return str(url.set(query=query))


@pytest.fixture
def runtime_db_url(monkeypatch):
    """Return an isolated Postgres schema URL for runtime/fact store tests."""

    yield from _isolated_runtime_db_url(monkeypatch, bootstrap_schema=True)


@pytest.fixture
def unbootstrapped_runtime_db_url(monkeypatch):
    """Return an isolated Postgres schema URL for migration/bootstrap tests."""

    yield from _isolated_runtime_db_url(monkeypatch, bootstrap_schema=False)


def _isolated_runtime_db_url(monkeypatch, *, bootstrap_schema: bool):
    """Return an isolated Postgres schema URL, optionally bootstrapped for runtime tests."""

    if create_engine is None or text is None or make_url is None:
        pytest.skip("SQLAlchemy is required for runtime/fact store tests.")

    base_url = (
        os.getenv("TEST_DATABASE_URL", "").strip()
        or os.getenv("BUSINESS_EXECUTION_CONTROL_DB_URL", "").strip()
        or os.getenv("EXECUTION_CONTROL_DB_URL", "").strip()
    )
    if not base_url:
        pytest.skip("Postgres DB URL is required; set TEST_DATABASE_URL or BUSINESS_EXECUTION_CONTROL_DB_URL.")
    if base_url.lower().startswith("sqlite"):
        pytest.skip("SQLite is no longer supported for runtime tests.")

    schema_name = f"test_{uuid.uuid4().hex}"
    base_url = _merge_pg_options(base_url, "-cclient_encoding=UTF8")
    engine = create_engine(base_url, future=True, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    isolated_url = _merge_pg_options(base_url, f"-csearch_path={schema_name}")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_DB_URL", isolated_url)
    if bootstrap_schema:
        schema_engine = create_engine(isolated_url, future=True, pool_pre_ping=True)
        try:
            ensure_runtime_schema(schema_engine)
            with schema_engine.begin() as connection:
                ensure_tk_fact_schema(connection)
                ensure_amazon_fact_schema(connection)
                connection.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {AMAZON_FACT_VERSION_TABLE} "
                        "(version_num VARCHAR(32) NOT NULL)"
                    )
                )
                connection.execute(text(f"DELETE FROM {AMAZON_FACT_VERSION_TABLE}"))
                connection.execute(
                    text(
                        f"INSERT INTO {AMAZON_FACT_VERSION_TABLE} (version_num) VALUES (:revision)"
                    ),
                    {"revision": AMAZON_FACT_SCHEMA_REVISION},
                )
        finally:
            schema_engine.dispose()


    try:
        yield isolated_url
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        engine.dispose()
