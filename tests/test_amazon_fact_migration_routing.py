from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_TABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FACT_MIGRATION_DB_URL_ENV = "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL"
FACT_RUNTIME_ROLE_ENV = "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE"
EXPECTED_AMAZON_TABLES = set(AMAZON_FACT_TABLES)


def test_fact_migration_requires_its_explicit_postgres_url_without_fallback(
    monkeypatch,
) -> None:
    monkeypatch.delenv(FACT_MIGRATION_DB_URL_ENV, raising=False)
    monkeypatch.setenv(
        "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL",
        "postgresql+psycopg://ignored:ignored@127.0.0.1/ignored",
    )
    monkeypatch.setenv(
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "postgresql+psycopg://ignored:ignored@127.0.0.1/ignored",
    )

    with pytest.raises(RuntimeError, match=f"^{FACT_MIGRATION_DB_URL_ENV} is required"):
        command.upgrade(Config(str(REPO_ROOT / "alembic_fact.ini")), "head")


def test_runtime_revision_is_noop_and_fact_revision_owns_exactly_nine_tables(
    monkeypatch,
) -> None:
    runtime_revision = _load_revision(
        REPO_ROOT / "alembic/versions/20260714_0007_amazon_product_facts.py",
        "runtime_amazon_compatibility_revision",
    )
    fact_revision = _load_revision(
        REPO_ROOT / "alembic_fact/versions/20260714_0007_amazon_product_facts.py",
        "fact_amazon_revision",
    )

    assert runtime_revision.revision == "20260714_0007"
    assert runtime_revision.down_revision == "20260528_0006"
    assert runtime_revision.upgrade() is None
    assert runtime_revision.downgrade() is None

    class Bind:
        dialect = postgresql.dialect()

    statements: list[str] = []
    monkeypatch.setenv(FACT_RUNTIME_ROLE_ENV, "mujitask_fact_runtime")
    monkeypatch.setattr(fact_revision.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(fact_revision.op, "execute", statements.append)

    fact_revision.upgrade()

    schema_sql = "\n".join(statements)
    created_tables = set(
        re.findall(r"CREATE TABLE IF NOT EXISTS (amazon_[a-z0-9_]+)", schema_sql)
    )
    assert fact_revision.revision == "20260714_0007"
    assert fact_revision.down_revision is None
    assert created_tables == EXPECTED_AMAZON_TABLES
    assert len(created_tables) == 9

    grants = [statement for statement in statements if statement.startswith("GRANT ")]
    assert len(grants) == 3
    assert grants[0] == 'GRANT USAGE ON SCHEMA "public" TO "mujitask_fact_runtime"'
    assert grants[1].startswith("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ")
    assert all(f'"public"."{table_name}"' in grants[1] for table_name in AMAZON_FACT_TABLES)
    assert grants[1].endswith(' TO "mujitask_fact_runtime"')
    assert grants[2] == (
        'GRANT SELECT ON TABLE "public"."fact_alembic_version" '
        'TO "mujitask_fact_runtime"'
    )
    assert re.search(r"\b(CREATE|ALTER|DROP|TRUNCATE)\b", "\n".join(grants)) is None


@pytest.mark.parametrize(
    "role",
    [
        "9runtime",
        "runtime-role",
        "runtime role",
        'runtime"role',
        "runtime;drop_role",
        "r" * 64,
    ],
)
def test_fact_runtime_role_is_validated_before_any_ddl(
    monkeypatch,
    role: str,
) -> None:
    fact_revision = _load_revision(
        REPO_ROOT / "alembic_fact/versions/20260714_0007_amazon_product_facts.py",
        f"fact_amazon_invalid_role_{abs(hash(role))}",
    )
    statements: list[str] = []
    monkeypatch.setenv(FACT_RUNTIME_ROLE_ENV, role)
    monkeypatch.setattr(fact_revision.op, "execute", statements.append)

    with pytest.raises(RuntimeError, match=f"^{FACT_RUNTIME_ROLE_ENV} must be"):
        fact_revision.upgrade()

    assert statements == []


def test_runtime_and_fact_alembic_routes_are_physically_isolated(
    unbootstrapped_runtime_db_url,
    monkeypatch,
) -> None:
    runtime_config = Config(str(REPO_ROOT / "alembic.ini"))
    fact_config = Config(str(REPO_ROOT / "alembic_fact.ini"))
    monkeypatch.setenv(FACT_MIGRATION_DB_URL_ENV, unbootstrapped_runtime_db_url)
    monkeypatch.delenv(FACT_RUNTIME_ROLE_ENV, raising=False)

    command.upgrade(runtime_config, "head")

    runtime_tables = _list_tables(unbootstrapped_runtime_db_url)
    assert not {table for table in runtime_tables if table.startswith("amazon_")}
    assert "alembic_version" in runtime_tables
    assert "fact_alembic_version" not in runtime_tables

    command.upgrade(fact_config, "head")

    fact_tables = _list_tables(unbootstrapped_runtime_db_url)
    assert {table for table in fact_tables if table.startswith("amazon_")} == (
        EXPECTED_AMAZON_TABLES
    )
    assert "alembic_version" in fact_tables
    assert "fact_alembic_version" in fact_tables

    command.downgrade(fact_config, "base")

    downgraded_tables = _list_tables(unbootstrapped_runtime_db_url)
    assert not {table for table in downgraded_tables if table.startswith("amazon_")}
    assert "task_request" in downgraded_tables


def test_fact_upgrade_runner_selects_the_fact_alembic_config() -> None:
    runner = (
        REPO_ROOT / "scripts/execution_control/run_fact_alembic_upgrade.sh"
    ).read_text(encoding="utf-8")

    assert FACT_MIGRATION_DB_URL_ENV in runner
    assert "-c alembic_fact.ini upgrade head" in runner
    assert "run_alembic_upgrade.sh" not in runner
    assert (REPO_ROOT / "alembic_fact/script.py.mako").is_file()


def _load_revision(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _list_tables(db_url: str) -> set[str]:
    engine = create_engine(db_url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = current_schema()
                    """
                )
            ).mappings().all()
        return {str(row["table_name"]) for row in rows}
    finally:
        engine.dispose()
