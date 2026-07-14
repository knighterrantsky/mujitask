from __future__ import annotations

from typing import Any


AMAZON_FACT_TABLES = [
    "amazon_feishu_bindings",
    "amazon_raw_captures",
    "amazon_product_media_assets",
    "amazon_media_assets",
    "amazon_bsr_snapshots",
    "amazon_product_variants",
    "amazon_offer_snapshots",
    "amazon_product_snapshots",
    "amazon_products",
]

AMAZON_FACT_INDEX_NAMES = [
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
]

AMAZON_FACT_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS amazon_products (
        id TEXT PRIMARY KEY,
        marketplace_code TEXT NOT NULL,
        asin TEXT NOT NULL,
        canonical_url TEXT NOT NULL DEFAULT '',
        parent_asin TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        brand TEXT NOT NULL DEFAULT '',
        category_path_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        latest_snapshot_id TEXT NOT NULL DEFAULT '',
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(marketplace_code, asin)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_products_last_seen_at
        ON amazon_products(marketplace_code, last_seen_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_products_parent_asin
        ON amazon_products(marketplace_code, parent_asin)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_product_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        marketplace_code TEXT NOT NULL,
        asin TEXT NOT NULL,
        run_id TEXT NOT NULL,
        request_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        resolved_asin TEXT NOT NULL DEFAULT '',
        parent_asin TEXT NOT NULL DEFAULT '',
        availability_status TEXT NOT NULL DEFAULT 'unknown',
        title TEXT NOT NULL DEFAULT '',
        brand TEXT NOT NULL DEFAULT '',
        category_path_json TEXT NOT NULL DEFAULT '[]',
        bullet_points_json TEXT NOT NULL DEFAULT '[]',
        description TEXT NOT NULL DEFAULT '',
        technical_details_json TEXT NOT NULL DEFAULT '{}',
        rating DOUBLE PRECISION,
        review_count BIGINT,
        variant_attributes_json TEXT NOT NULL DEFAULT '{}',
        child_asins_json TEXT NOT NULL DEFAULT '[]',
        field_coverage_json TEXT NOT NULL DEFAULT '{}',
        payload_json TEXT NOT NULL DEFAULT '{}',
        content_digest TEXT NOT NULL DEFAULT '',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        UNIQUE(marketplace_code, asin, run_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_product_snapshots_product_collected
        ON amazon_product_snapshots(product_id, collected_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_product_snapshots_asin_collected
        ON amazon_product_snapshots(marketplace_code, asin, collected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_offer_snapshots (
        offer_snapshot_id TEXT PRIMARY KEY,
        product_snapshot_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        offer_key TEXT NOT NULL,
        seller_id TEXT NOT NULL DEFAULT '',
        seller_name TEXT NOT NULL DEFAULT '',
        is_featured_offer BOOLEAN NOT NULL DEFAULT FALSE,
        price_amount NUMERIC(18, 4),
        list_price_amount NUMERIC(18, 4),
        currency TEXT NOT NULL DEFAULT '',
        availability_status TEXT NOT NULL DEFAULT 'unknown',
        fulfillment_channel TEXT NOT NULL DEFAULT 'unknown',
        delivery_text TEXT NOT NULL DEFAULT '',
        coupon_text TEXT NOT NULL DEFAULT '',
        promotions_json TEXT NOT NULL DEFAULT '[]',
        profile_context_digest TEXT NOT NULL DEFAULT '',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_snapshot_id, offer_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_offer_snapshots_product_collected
        ON amazon_offer_snapshots(product_id, collected_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_offer_snapshots_seller
        ON amazon_offer_snapshots(seller_id, collected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_product_variants (
        relation_id TEXT PRIMARY KEY,
        marketplace_code TEXT NOT NULL,
        parent_asin TEXT NOT NULL,
        child_asin TEXT NOT NULL,
        attributes_json TEXT NOT NULL DEFAULT '{}',
        dimensions_json TEXT NOT NULL DEFAULT '{}',
        source_asin TEXT NOT NULL DEFAULT '',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(marketplace_code, parent_asin, child_asin)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_product_variants_child
        ON amazon_product_variants(marketplace_code, child_asin)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_bsr_snapshots (
        bsr_snapshot_id TEXT PRIMARY KEY,
        product_snapshot_id TEXT NOT NULL,
        product_id TEXT NOT NULL,
        category_name TEXT NOT NULL,
        category_path_json TEXT NOT NULL DEFAULT '[]',
        rank_value BIGINT NOT NULL,
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_snapshot_id, category_name, category_path_json)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_bsr_snapshots_product_rank
        ON amazon_bsr_snapshots(product_id, rank_value, collected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_media_assets (
        asset_id TEXT PRIMARY KEY,
        asset_key TEXT NOT NULL UNIQUE,
        source_url TEXT NOT NULL DEFAULT '',
        source_url_digest TEXT NOT NULL DEFAULT '',
        content_digest TEXT NOT NULL DEFAULT '',
        bucket TEXT NOT NULL DEFAULT '',
        object_key TEXT NOT NULL DEFAULT '',
        remote_uri TEXT NOT NULL DEFAULT '',
        file_name TEXT NOT NULL DEFAULT '',
        mime_type TEXT NOT NULL DEFAULT '',
        size_bytes BIGINT NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_media_assets_source_digest
        ON amazon_media_assets(source_url_digest)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_product_media_assets (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        product_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        media_role TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_id, asset_id, media_role, position)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_product_media_assets_product_role
        ON amazon_product_media_assets(product_id, media_role, position)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_raw_captures (
        raw_capture_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL DEFAULT '',
        capture_kind TEXT NOT NULL,
        bucket TEXT NOT NULL,
        object_key TEXT NOT NULL,
        content_digest TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        sanitization_status TEXT NOT NULL DEFAULT 'unknown',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        UNIQUE(bucket, object_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_raw_captures_product_run
        ON amazon_raw_captures(product_id, run_id, capture_kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_raw_captures_request_execution
        ON amazon_raw_captures(request_id, execution_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS amazon_feishu_bindings (
        binding_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        base_id TEXT NOT NULL,
        table_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        source_asin TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        last_synced_snapshot_id TEXT NOT NULL DEFAULT '',
        first_bound_at DOUBLE PRECISION NOT NULL,
        last_synced_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(base_id, table_id, record_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_feishu_bindings_product
        ON amazon_feishu_bindings(product_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_amazon_feishu_bindings_source_asin
        ON amazon_feishu_bindings(source_asin)
    """,
]


def ensure_amazon_fact_schema(connection: Any) -> None:
    """Bootstrap the Amazon Fact schema for local development and isolated tests."""

    for statement in AMAZON_FACT_SCHEMA_STATEMENTS:
        connection.exec_driver_sql(statement)
