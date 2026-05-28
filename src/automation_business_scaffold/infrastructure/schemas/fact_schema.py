from __future__ import annotations

from typing import Any

TK_FACT_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS tk_products (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL UNIQUE,
        product_url TEXT NOT NULL DEFAULT '',
        normalized_url TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        holiday TEXT NOT NULL DEFAULT '',
        seller_name TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT 'tiktok',
        country_region TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_products_last_seen_at ON tk_products(last_seen_at)",
    """
    CREATE TABLE IF NOT EXISTS tk_product_skus (
        id TEXT PRIMARY KEY,
        sku_key TEXT NOT NULL UNIQUE,
        product_id TEXT NOT NULL,
        sku_id TEXT NOT NULL DEFAULT '',
        sku_name TEXT NOT NULL DEFAULT '',
        spec_name TEXT NOT NULL DEFAULT '',
        price_text TEXT NOT NULL DEFAULT '',
        stock_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_product_skus_product_id ON tk_product_skus(product_id)",
    """
    CREATE TABLE IF NOT EXISTS tk_shops (
        id TEXT PRIMARY KEY,
        shop_key TEXT NOT NULL UNIQUE,
        shop_id TEXT NOT NULL DEFAULT '',
        shop_name TEXT NOT NULL DEFAULT '',
        shop_url TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT 'tiktok',
        country_region TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_shops_shop_name ON tk_shops(shop_name)",
    """
    CREATE TABLE IF NOT EXISTS tk_creators (
        id TEXT PRIMARY KEY,
        creator_key TEXT NOT NULL UNIQUE,
        creator_id TEXT NOT NULL DEFAULT '',
        uid TEXT NOT NULL DEFAULT '',
        unique_id TEXT NOT NULL DEFAULT '',
        nickname TEXT NOT NULL DEFAULT '',
        profile_url TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT 'tiktok',
        country_region TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_creators_unique_id ON tk_creators(unique_id)",
    """
    CREATE TABLE IF NOT EXISTS tk_videos (
        id TEXT PRIMARY KEY,
        video_key TEXT NOT NULL UNIQUE,
        video_id TEXT NOT NULL DEFAULT '',
        creator_key TEXT NOT NULL DEFAULT '',
        creator_uid TEXT NOT NULL DEFAULT '',
        creator_unique_id TEXT NOT NULL DEFAULT '',
        product_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        video_url TEXT NOT NULL DEFAULT '',
        cover_url TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT 'tiktok',
        source_platform TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        facts_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_videos_creator_key ON tk_videos(creator_key)",
    "CREATE INDEX IF NOT EXISTS idx_tk_videos_creator_unique_id ON tk_videos(creator_unique_id)",
    "CREATE INDEX IF NOT EXISTS idx_tk_videos_product_id ON tk_videos(product_id)",
    """
    CREATE TABLE IF NOT EXISTS tk_media_assets (
        asset_id TEXT PRIMARY KEY,
        asset_key TEXT NOT NULL UNIQUE,
        source_url TEXT NOT NULL DEFAULT '',
        file_token TEXT NOT NULL DEFAULT '',
        local_path TEXT NOT NULL DEFAULT '',
        object_key TEXT NOT NULL DEFAULT '',
        file_name TEXT NOT NULL DEFAULT '',
        mime_type TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_entity_media_assets (
        link_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        entity_type TEXT NOT NULL,
        entity_external_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        media_role TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_entity_media_assets_entity
        ON tk_entity_media_assets(entity_type, entity_external_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_shop_relations (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        product_id TEXT NOT NULL,
        shop_key TEXT NOT NULL,
        shop_id TEXT NOT NULL DEFAULT '',
        shop_name TEXT NOT NULL DEFAULT '',
        relation_role TEXT NOT NULL DEFAULT 'seller',
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_product_shop_product ON tk_product_shop_relations(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_tk_product_shop_shop ON tk_product_shop_relations(shop_key)",
    """
    CREATE TABLE IF NOT EXISTS tk_creator_product_relations (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        creator_key TEXT NOT NULL,
        creator_id TEXT NOT NULL DEFAULT '',
        product_id TEXT NOT NULL,
        source_record_id TEXT NOT NULL DEFAULT '',
        target_record_id TEXT NOT NULL DEFAULT '',
        holiday_name TEXT NOT NULL DEFAULT '',
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_creator_product_creator
        ON tk_creator_product_relations(creator_key, product_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_creator_product_product
        ON tk_creator_product_relations(product_id, sold_count)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_creator_video_relations (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        creator_key TEXT NOT NULL,
        video_key TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_video_product_relations (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        video_key TEXT NOT NULL,
        product_id TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tk_video_product_unique
        ON tk_video_product_relations(video_key, product_id, source_platform)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_video_product_product_video
        ON tk_video_product_relations(product_id, video_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_video_product_video_product
        ON tk_video_product_relations(video_key, product_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_shop_creator_relations (
        relation_id TEXT PRIMARY KEY,
        relation_key TEXT NOT NULL UNIQUE,
        shop_key TEXT NOT NULL,
        creator_key TEXT NOT NULL,
        shop_name TEXT NOT NULL DEFAULT '',
        creator_id TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_raw_api_responses (
        raw_response_id TEXT PRIMARY KEY,
        source_platform TEXT NOT NULL DEFAULT '',
        source_endpoint TEXT NOT NULL DEFAULT '',
        request_url TEXT NOT NULL DEFAULT '',
        request_params_json TEXT NOT NULL DEFAULT '{}',
        response_payload_json TEXT NOT NULL DEFAULT '{}',
        status_code INTEGER NOT NULL DEFAULT 0,
        request_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_raw_api_responses_endpoint
        ON tk_raw_api_responses(source_platform, source_endpoint, collected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_raw_entity_links (
        raw_link_id TEXT PRIMARY KEY,
        raw_response_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_external_id TEXT NOT NULL,
        link_role TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_raw_entity_links_entity
        ON tk_raw_entity_links(entity_type, entity_external_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_daily_metrics (
        metric_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        metric_date TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        price_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_id, metric_date, source_platform)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tk_product_daily_product_date ON tk_product_daily_metrics(product_id, metric_date)",
    """
    CREATE TABLE IF NOT EXISTS tk_product_window_latest (
        latest_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        source_endpoint TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        window_start TEXT NOT NULL DEFAULT '',
        window_end TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_id, source_platform, source_endpoint, window_days)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_window_observations (
        observation_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        source_endpoint TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        window_start TEXT NOT NULL DEFAULT '',
        window_end TEXT NOT NULL DEFAULT '',
        observation_reason TEXT NOT NULL DEFAULT '',
        is_persisted_snapshot INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_distribution_window_latest (
        latest_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        distribution_type TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        metric_value DOUBLE PRECISION NOT NULL DEFAULT 0,
        metric_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_id, distribution_type, source_key, source_platform, window_days)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_distribution_window_observations (
        observation_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        distribution_type TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        metric_value DOUBLE PRECISION NOT NULL DEFAULT 0,
        metric_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        observation_reason TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_sku_window_latest (
        latest_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        sku_key TEXT NOT NULL DEFAULT '',
        sku_id TEXT NOT NULL DEFAULT '',
        sku_name TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        stock_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(product_id, sku_key, source_platform, window_days)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_product_sku_window_observations (
        observation_id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL,
        sku_key TEXT NOT NULL DEFAULT '',
        sku_id TEXT NOT NULL DEFAULT '',
        sku_name TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        stock_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        observation_reason TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_video_metric_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        video_key TEXT NOT NULL,
        video_id TEXT NOT NULL DEFAULT '',
        creator_key TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        source_endpoint TEXT NOT NULL DEFAULT '',
        play_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        digg_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        comment_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        share_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_video_metric_snapshots_video_collected
        ON tk_video_metric_snapshots(video_key, collected_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tk_video_metric_snapshots_creator_collected
        ON tk_video_metric_snapshots(creator_key, collected_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_video_product_window_performance (
        performance_id TEXT PRIMARY KEY,
        video_key TEXT NOT NULL,
        product_id TEXT NOT NULL,
        creator_key TEXT NOT NULL DEFAULT '',
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tk_creator_product_window_performance (
        performance_id TEXT PRIMARY KEY,
        creator_key TEXT NOT NULL,
        product_id TEXT NOT NULL,
        source_platform TEXT NOT NULL DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 0,
        sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        collected_at DOUBLE PRECISION NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
]


LEGACY_ENTITY_DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS entity_snapshot",
    "DROP TABLE IF EXISTS external_binding",
    "DROP TABLE IF EXISTS entity_registry",
]


def ensure_tk_fact_schema(connection: Any, *, drop_legacy_entity_tables: bool = True) -> None:
    for statement in TK_FACT_SCHEMA_STATEMENTS:
        connection.exec_driver_sql(statement)
    if drop_legacy_entity_tables:
        for statement in LEGACY_ENTITY_DROP_STATEMENTS:
            connection.exec_driver_sql(statement)

