"""Create TK fact database tables and remove legacy entity tables.

Revision ID: 20260421_0002
Revises: 20260412_0001
Create Date: 2026-04-21 22:30:00
"""

from __future__ import annotations

from alembic import op

from automation_business_scaffold.infrastructure.facts.tk_fact_store import (
    LEGACY_ENTITY_DROP_STATEMENTS,
    TK_FACT_SCHEMA_STATEMENTS,
)

revision = "20260421_0002"
down_revision = "20260412_0001"
branch_labels = None
depends_on = None


TK_FACT_TABLES = [
    "tk_creator_product_window_performance",
    "tk_video_product_window_performance",
    "tk_product_sku_window_observations",
    "tk_product_sku_window_latest",
    "tk_product_distribution_window_observations",
    "tk_product_distribution_window_latest",
    "tk_product_window_observations",
    "tk_product_window_latest",
    "tk_product_daily_metrics",
    "tk_raw_entity_links",
    "tk_raw_api_responses",
    "tk_shop_creator_relations",
    "tk_video_product_relations",
    "tk_creator_video_relations",
    "tk_creator_product_relations",
    "tk_product_shop_relations",
    "tk_entity_media_assets",
    "tk_media_assets",
    "tk_videos",
    "tk_creators",
    "tk_shops",
    "tk_product_skus",
    "tk_products",
]


LEGACY_ENTITY_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS entity_registry (
        entity_id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        canonical_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        latest_snapshot_id TEXT NOT NULL DEFAULT '',
        first_seen_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_registry_entity_type_canonical_key
        ON entity_registry(entity_type, canonical_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_registry_entity_type_last_seen_at
        ON entity_registry(entity_type, last_seen_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS external_binding (
        binding_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_space TEXT NOT NULL,
        target_id TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_bound_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_external_binding_target
        ON external_binding(target_type, target_space, target_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_external_binding_entity_id_status
        ON external_binding(entity_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_external_binding_source_key
        ON external_binding(source_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        collected_at REAL NOT NULL,
        facts_json TEXT NOT NULL DEFAULT '{}',
        baseline_snapshot_id TEXT NOT NULL DEFAULT '',
        diff_json TEXT NOT NULL DEFAULT '{}',
        request_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_snapshot_entity_id_snapshot_date
        ON entity_snapshot(entity_id, snapshot_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_entity_snapshot_run_id
        ON entity_snapshot(run_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_snapshot_entity_id_run_id
        ON entity_snapshot(entity_id, run_id)
        WHERE run_id <> ''
    """,
]


def upgrade() -> None:
    for statement in TK_FACT_SCHEMA_STATEMENTS:
        op.execute(statement)
    for statement in LEGACY_ENTITY_DROP_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for table_name in TK_FACT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
    for statement in LEGACY_ENTITY_SCHEMA_STATEMENTS:
        op.execute(statement)
