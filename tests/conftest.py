from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def runtime_db_url(monkeypatch):
    """Return an isolated Postgres schema URL for runtime/fact store tests."""

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
    engine = create_engine(base_url, future=True, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    url = make_url(base_url)
    query = dict(url.query)
    existing_options = str(query.get("options") or "").strip()
    search_path_option = f"-csearch_path={schema_name}"
    query["options"] = (
        f"{existing_options} {search_path_option}".strip()
        if existing_options
        else search_path_option
    )
    isolated_url = str(url.set(query=query))
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_DB_URL", isolated_url)

    try:
        yield isolated_url
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        engine.dispose()
