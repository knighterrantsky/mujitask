from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_INDEX_NAMES,
    AMAZON_FACT_SCHEMA_STATEMENTS,
    AMAZON_FACT_TABLES,
    ensure_amazon_fact_schema,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "amazon_products",
    "amazon_product_snapshots",
    "amazon_offer_snapshots",
    "amazon_product_variants",
    "amazon_bsr_snapshots",
    "amazon_media_assets",
    "amazon_product_media_assets",
    "amazon_raw_captures",
    "amazon_feishu_bindings",
}
EXPECTED_INDEXES = {
    "idx_amazon_products_last_seen_at",
    "idx_amazon_products_parent_asin",
    "idx_amazon_product_snapshots_product_collected",
    "idx_amazon_product_snapshots_asin_collected",
    "idx_amazon_offer_snapshots_product_collected",
    "idx_amazon_offer_snapshots_seller",
    "idx_amazon_product_variants_child",
    "idx_amazon_bsr_snapshots_product_rank",
    "idx_amazon_media_assets_source_digest",
    "idx_amazon_product_media_assets_product_role",
    "idx_amazon_raw_captures_product_run",
    "idx_amazon_raw_captures_request_execution",
    "idx_amazon_feishu_bindings_product",
    "idx_amazon_feishu_bindings_source_asin",
}


def test_amazon_schema_contract_declares_only_the_nine_governed_tables() -> None:
    assert set(AMAZON_FACT_TABLES) == EXPECTED_TABLES
    assert len(AMAZON_FACT_TABLES) == len(EXPECTED_TABLES)
    assert set(AMAZON_FACT_INDEX_NAMES) == EXPECTED_INDEXES

    schema_sql = "\n".join(AMAZON_FACT_SCHEMA_STATEMENTS).lower()
    assert schema_sql.count("create table if not exists amazon_") == 9
    assert "create table if not exists tk_" not in schema_sql
    assert "alter table" not in schema_sql
    assert "foreign key" not in schema_sql


def test_amazon_schema_freezes_business_and_dedupe_keys() -> None:
    schema_sql = " ".join(" ".join(statement.lower().split()) for statement in AMAZON_FACT_SCHEMA_STATEMENTS)

    required_fragments = {
        "amazon_products": "unique(marketplace_code, asin)",
        "amazon_product_snapshots": "unique(marketplace_code, asin, run_id)",
        "amazon_offer_snapshots": "unique(product_snapshot_id, offer_key)",
        "amazon_product_variants": "unique(marketplace_code, parent_asin, child_asin)",
        "amazon_bsr_snapshots": (
            "unique(product_snapshot_id, category_name, category_path_json)"
        ),
        "amazon_media_assets": "asset_key text not null unique",
        "amazon_product_media_assets": (
            "unique(product_id, asset_id, media_role, position)"
        ),
        "amazon_raw_captures": "unique(bucket, object_key)",
        "amazon_feishu_bindings": "unique(base_id, table_id, record_id)",
    }
    assert all(fragment in schema_sql for fragment in required_fragments.values())


def test_local_bootstrap_executes_only_the_declared_additive_statements() -> None:
    class RecordingConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def exec_driver_sql(self, statement: str) -> None:
            self.statements.append(statement)

    connection = RecordingConnection()

    ensure_amazon_fact_schema(connection)

    assert connection.statements == AMAZON_FACT_SCHEMA_STATEMENTS
    assert all("IF NOT EXISTS" in statement for statement in connection.statements)


def test_migration_revision_is_additive_and_reversible() -> None:
    migration_path = REPO_ROOT / "alembic/versions/20260714_0007_amazon_product_facts.py"
    migration = migration_path.read_text(encoding="utf-8")

    assert 'revision = "20260714_0007"' in migration
    assert 'down_revision = "20260528_0006"' in migration
    assert "AMAZON_FACT_SCHEMA_STATEMENTS" in migration
    assert "AMAZON_FACT_INDEX_NAMES" in migration
    assert "AMAZON_FACT_TABLES" in migration
    assert "DROP INDEX IF EXISTS" in migration
    assert "DROP TABLE IF EXISTS" in migration


def test_local_bootstrap_creates_all_amazon_tables_and_indexes(
    unbootstrapped_runtime_db_url,
) -> None:
    engine = create_engine(unbootstrapped_runtime_db_url, future=True)
    try:
        with engine.begin() as connection:
            ensure_amazon_fact_schema(connection)
            ensure_amazon_fact_schema(connection)

        assert EXPECTED_TABLES <= _list_tables(unbootstrapped_runtime_db_url)
        assert EXPECTED_INDEXES <= _list_indexes(unbootstrapped_runtime_db_url)
    finally:
        engine.dispose()


def test_standard_test_bootstrap_includes_amazon_schema(runtime_db_url) -> None:
    assert EXPECTED_TABLES <= _list_tables(runtime_db_url)


def test_alembic_upgrade_and_targeted_downgrade_only_remove_amazon_tables(
    unbootstrapped_runtime_db_url,
) -> None:
    config = Config("alembic.ini")

    command.upgrade(config, "head")

    upgraded_tables = _list_tables(unbootstrapped_runtime_db_url)
    assert EXPECTED_TABLES <= upgraded_tables
    assert "tk_products" in upgraded_tables
    assert EXPECTED_INDEXES <= _list_indexes(unbootstrapped_runtime_db_url)

    command.downgrade(config, "20260528_0006")

    downgraded_tables = _list_tables(unbootstrapped_runtime_db_url)
    assert EXPECTED_TABLES.isdisjoint(downgraded_tables)
    assert "tk_products" in downgraded_tables


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


def _list_indexes(db_url: str) -> set[str]:
    engine = create_engine(db_url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                    """
                )
            ).mappings().all()
        return {str(row["indexname"]) for row in rows}
    finally:
        engine.dispose()
