from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping, Sequence


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


def _json_dumps(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":"))


def _load_json_dict(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class TKFactStore:
    def __init__(self, *, runtime_store: Any | None = None, db_url: str = ""):
        if runtime_store is not None:
            self._engine = runtime_store._engine  # noqa: SLF001
            self._text = runtime_store._text  # noqa: SLF001
            return

        try:
            from sqlalchemy import create_engine, text
        except ModuleNotFoundError as exc:
            raise RuntimeError("TKFactStore requires SQLAlchemy.") from exc

        resolved_db_url = _clean_text(db_url)
        if not resolved_db_url:
            raise RuntimeError(
                "TKFactStore requires db_url or runtime_store. Fill "
                "BUSINESS_EXECUTION_CONTROL_DB_URL / EXECUTION_CONTROL_DB_URL in "
                "scripts/execution_control/executor.local.env or pass fact_db_url explicitly. "
                "SQLite/db_path fallback has been removed."
            )
        if resolved_db_url.lower().startswith("sqlite"):
            raise RuntimeError("SQLite is no longer supported for TKFactStore; use Postgres.")
        self._text = text
        self._engine = create_engine(resolved_db_url, future=True, pool_pre_ping=True)
        with self._engine.begin() as connection:
            ensure_tk_fact_schema(connection)

    def upsert_product(
        self,
        *,
        product_id: str,
        product_url: str = "",
        normalized_url: str = "",
        title: str = "",
        holiday: str = "",
        seller_name: str = "",
        platform: str = "tiktok",
        country_region: str = "",
        source_platform: str = "",
        status: str = "active",
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        if not product_id:
            return {}
        return self._upsert_by_unique(
            table_name="tk_products",
            unique_column="product_id",
            unique_value=product_id,
            values={
                "id": uuid.uuid4().hex,
                "product_id": product_id,
                "product_url": _clean_text(product_url),
                "normalized_url": _clean_text(normalized_url),
                "title": _clean_text(title),
                "holiday": _clean_text(holiday),
                "seller_name": _clean_text(seller_name),
                "platform": _clean_text(platform) or "tiktok",
                "country_region": _clean_text(country_region),
                "source_platform": _clean_text(source_platform),
                "status": _clean_text(status) or "active",
                "facts_json": _json_dumps(facts),
            },
        )

    def upsert_product_sku(
        self,
        *,
        product_id: str,
        sku_id: str = "",
        sku_name: str = "",
        spec_name: str = "",
        price_text: str = "",
        stock_count: Any = 0,
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        sku_id = _clean_text(sku_id)
        sku_name = _clean_text(sku_name)
        if not product_id or not (sku_id or sku_name):
            return {}
        sku_key = f"{product_id}:{sku_id or sku_name}"
        return self._upsert_by_unique(
            table_name="tk_product_skus",
            unique_column="sku_key",
            unique_value=sku_key,
            values={
                "id": uuid.uuid4().hex,
                "sku_key": sku_key,
                "product_id": product_id,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "spec_name": _clean_text(spec_name),
                "price_text": _clean_text(price_text),
                "stock_count": _coerce_float(stock_count),
                "facts_json": _json_dumps(facts),
            },
        )

    def upsert_shop(
        self,
        *,
        shop_id: str = "",
        shop_name: str = "",
        shop_url: str = "",
        platform: str = "tiktok",
        country_region: str = "",
        source_platform: str = "",
        status: str = "active",
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        shop_id = _clean_text(shop_id)
        shop_name = _clean_text(shop_name)
        shop_key = self.build_shop_key(shop_id=shop_id, shop_name=shop_name)
        if not shop_key:
            return {}
        return self._upsert_by_unique(
            table_name="tk_shops",
            unique_column="shop_key",
            unique_value=shop_key,
            values={
                "id": uuid.uuid4().hex,
                "shop_key": shop_key,
                "shop_id": shop_id,
                "shop_name": shop_name,
                "shop_url": _clean_text(shop_url),
                "platform": _clean_text(platform) or "tiktok",
                "country_region": _clean_text(country_region),
                "source_platform": _clean_text(source_platform),
                "status": _clean_text(status) or "active",
                "facts_json": _json_dumps(facts),
            },
        )

    def upsert_creator(
        self,
        *,
        creator_id: str = "",
        uid: str = "",
        unique_id: str = "",
        nickname: str = "",
        profile_url: str = "",
        platform: str = "tiktok",
        country_region: str = "",
        source_platform: str = "",
        status: str = "active",
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        creator_id = _clean_text(creator_id)
        uid = _clean_text(uid)
        unique_id = _clean_text(unique_id)
        creator_key = self.build_creator_key(creator_id=creator_id, uid=uid, unique_id=unique_id)
        if not creator_key:
            return {}
        return self._upsert_by_unique(
            table_name="tk_creators",
            unique_column="creator_key",
            unique_value=creator_key,
            values={
                "id": uuid.uuid4().hex,
                "creator_key": creator_key,
                "creator_id": creator_id,
                "uid": uid,
                "unique_id": unique_id,
                "nickname": _clean_text(nickname),
                "profile_url": _clean_text(profile_url),
                "platform": _clean_text(platform) or "tiktok",
                "country_region": _clean_text(country_region),
                "source_platform": _clean_text(source_platform),
                "status": _clean_text(status) or "active",
                "facts_json": _json_dumps(facts),
            },
        )

    def upsert_video(
        self,
        *,
        video_id: str = "",
        creator_key: str = "",
        product_id: str = "",
        title: str = "",
        video_url: str = "",
        cover_url: str = "",
        platform: str = "tiktok",
        source_platform: str = "",
        status: str = "active",
        facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        video_id = _clean_text(video_id)
        video_key = f"video:{video_id}" if video_id else ""
        if not video_key:
            return {}
        return self._upsert_by_unique(
            table_name="tk_videos",
            unique_column="video_key",
            unique_value=video_key,
            values={
                "id": uuid.uuid4().hex,
                "video_key": video_key,
                "video_id": video_id,
                "creator_key": _clean_text(creator_key),
                "product_id": _clean_text(product_id),
                "title": _clean_text(title),
                "video_url": _clean_text(video_url),
                "cover_url": _clean_text(cover_url),
                "platform": _clean_text(platform) or "tiktok",
                "source_platform": _clean_text(source_platform),
                "status": _clean_text(status) or "active",
                "facts_json": _json_dumps(facts),
            },
        )

    def upsert_media_asset(
        self,
        *,
        source_url: str = "",
        file_token: str = "",
        local_path: str = "",
        object_key: str = "",
        file_name: str = "",
        mime_type: str = "",
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        asset_key = self.build_asset_key(
            source_url=source_url,
            file_token=file_token,
            local_path=local_path,
            object_key=object_key,
        )
        if not asset_key:
            return {}
        return self._upsert_by_unique(
            table_name="tk_media_assets",
            unique_column="asset_key",
            unique_value=asset_key,
            values={
                "asset_id": uuid.uuid4().hex,
                "asset_key": asset_key,
                "source_url": _clean_text(source_url),
                "file_token": _clean_text(file_token),
                "local_path": _clean_text(local_path),
                "object_key": _clean_text(object_key),
                "file_name": _clean_text(file_name),
                "mime_type": _clean_text(mime_type),
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def link_media_asset(
        self,
        *,
        entity_type: str,
        entity_external_id: str,
        asset_id: str,
        media_role: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_type = _clean_text(entity_type)
        entity_external_id = _clean_text(entity_external_id)
        asset_id = _clean_text(asset_id)
        media_role = _clean_text(media_role)
        if not entity_type or not entity_external_id or not asset_id:
            return {}
        relation_key = f"{entity_type}:{entity_external_id}:{media_role}:{asset_id}"
        return self._upsert_by_unique(
            table_name="tk_entity_media_assets",
            unique_column="relation_key",
            unique_value=relation_key,
            values={
                "link_id": uuid.uuid4().hex,
                "relation_key": relation_key,
                "entity_type": entity_type,
                "entity_external_id": entity_external_id,
                "asset_id": asset_id,
                "media_role": media_role,
                "metadata_json": _json_dumps(metadata),
            },
        )

    def upsert_product_shop_relation(
        self,
        *,
        product_id: str,
        shop_key: str,
        shop_id: str = "",
        shop_name: str = "",
        relation_role: str = "seller",
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        shop_key = _clean_text(shop_key)
        if not product_id or not shop_key:
            return {}
        relation_key = f"{product_id}:{shop_key}:{_clean_text(relation_role) or 'seller'}"
        return self._upsert_relation(
            table_name="tk_product_shop_relations",
            relation_key=relation_key,
            values={
                "product_id": product_id,
                "shop_key": shop_key,
                "shop_id": _clean_text(shop_id),
                "shop_name": _clean_text(shop_name),
                "relation_role": _clean_text(relation_role) or "seller",
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def upsert_creator_product_relation(
        self,
        *,
        creator_key: str,
        product_id: str,
        creator_id: str = "",
        source_record_id: str = "",
        target_record_id: str = "",
        holiday_name: str = "",
        sold_count: Any = 0,
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        creator_key = _clean_text(creator_key)
        product_id = _clean_text(product_id)
        if not creator_key or not product_id:
            return {}
        relation_key = f"{creator_key}:{product_id}"
        return self._upsert_relation(
            table_name="tk_creator_product_relations",
            relation_key=relation_key,
            values={
                "creator_key": creator_key,
                "creator_id": _clean_text(creator_id),
                "product_id": product_id,
                "source_record_id": _clean_text(source_record_id),
                "target_record_id": _clean_text(target_record_id),
                "holiday_name": _clean_text(holiday_name),
                "sold_count": _coerce_float(sold_count),
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def upsert_creator_video_relation(
        self,
        *,
        creator_key: str,
        video_key: str,
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        creator_key = _clean_text(creator_key)
        video_key = _clean_text(video_key)
        if not creator_key or not video_key:
            return {}
        return self._upsert_relation(
            table_name="tk_creator_video_relations",
            relation_key=f"{creator_key}:{video_key}",
            values={
                "creator_key": creator_key,
                "video_key": video_key,
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def upsert_video_product_relation(
        self,
        *,
        video_key: str,
        product_id: str,
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        video_key = _clean_text(video_key)
        product_id = _clean_text(product_id)
        if not video_key or not product_id:
            return {}
        return self._upsert_relation(
            table_name="tk_video_product_relations",
            relation_key=f"{video_key}:{product_id}",
            values={
                "video_key": video_key,
                "product_id": product_id,
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def upsert_shop_creator_relation(
        self,
        *,
        shop_key: str,
        creator_key: str,
        shop_name: str = "",
        creator_id: str = "",
        source_platform: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        shop_key = _clean_text(shop_key)
        creator_key = _clean_text(creator_key)
        if not shop_key or not creator_key:
            return {}
        return self._upsert_relation(
            table_name="tk_shop_creator_relations",
            relation_key=f"{shop_key}:{creator_key}",
            values={
                "shop_key": shop_key,
                "creator_key": creator_key,
                "shop_name": _clean_text(shop_name),
                "creator_id": _clean_text(creator_id),
                "source_platform": _clean_text(source_platform),
                "metadata_json": _json_dumps(metadata),
            },
        )

    def record_raw_api_response(
        self,
        *,
        source_platform: str = "",
        source_endpoint: str = "",
        request_url: str = "",
        request_params: Mapping[str, Any] | None = None,
        response_payload: Mapping[str, Any] | None = None,
        status_code: int = 0,
        request_id: str = "",
        execution_id: str = "",
        run_id: str = "",
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        raw_response_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    INSERT INTO tk_raw_api_responses (
                        raw_response_id, source_platform, source_endpoint, request_url,
                        request_params_json, response_payload_json, status_code,
                        request_id, execution_id, run_id, collected_at, created_at
                    ) VALUES (
                        :raw_response_id, :source_platform, :source_endpoint, :request_url,
                        :request_params_json, :response_payload_json, :status_code,
                        :request_id, :execution_id, :run_id, :collected_at, :created_at
                    )
                    """
                ),
                {
                    "raw_response_id": raw_response_id,
                    "source_platform": _clean_text(source_platform),
                    "source_endpoint": _clean_text(source_endpoint),
                    "request_url": _clean_text(request_url),
                    "request_params_json": _json_dumps(request_params),
                    "response_payload_json": _json_dumps(response_payload),
                    "status_code": int(status_code or 0),
                    "request_id": _clean_text(request_id),
                    "execution_id": _clean_text(execution_id),
                    "run_id": _clean_text(run_id),
                    "collected_at": float(collected_at or now),
                    "created_at": now,
                },
            )
        return self.get_raw_api_response(raw_response_id=raw_response_id)

    def record_product_window_observation(
        self,
        *,
        product_id: str,
        source_platform: str = "",
        source_endpoint: str = "",
        window_days: int = 0,
        window_start: str = "",
        window_end: str = "",
        observation_reason: str = "",
        is_persisted_snapshot: bool = True,
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        if not product_id:
            return {}
        now = time.time()
        observation_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_window_observations (
                            observation_id, product_id, source_platform, source_endpoint,
                            window_days, window_start, window_end, observation_reason,
                            is_persisted_snapshot, payload_json, collected_at, created_at
                        ) VALUES (
                            :observation_id, :product_id, :source_platform, :source_endpoint,
                            :window_days, :window_start, :window_end, :observation_reason,
                            :is_persisted_snapshot, :payload_json, :collected_at, :created_at
                        )
                        RETURNING *
                        """
                    ),
                    {
                        "observation_id": observation_id,
                        "product_id": product_id,
                        "source_platform": _clean_text(source_platform),
                        "source_endpoint": _clean_text(source_endpoint),
                        "window_days": int(window_days or 0),
                        "window_start": _clean_text(window_start),
                        "window_end": _clean_text(window_end),
                        "observation_reason": _clean_text(observation_reason),
                        "is_persisted_snapshot": 1 if is_persisted_snapshot else 0,
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def upsert_product_window_latest(
        self,
        *,
        product_id: str,
        source_platform: str = "",
        source_endpoint: str = "",
        window_days: int = 0,
        window_start: str = "",
        window_end: str = "",
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        if not product_id:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_window_latest (
                            latest_id, product_id, source_platform, source_endpoint,
                            window_days, window_start, window_end, payload_json,
                            collected_at, created_at, updated_at
                        ) VALUES (
                            :latest_id, :product_id, :source_platform, :source_endpoint,
                            :window_days, :window_start, :window_end, :payload_json,
                            :collected_at, :created_at, :updated_at
                        )
                        ON CONFLICT(product_id, source_platform, source_endpoint, window_days)
                        DO UPDATE SET
                            window_start = EXCLUDED.window_start,
                            window_end = EXCLUDED.window_end,
                            payload_json = EXCLUDED.payload_json,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING *
                        """
                    ),
                    {
                        "latest_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "source_platform": _clean_text(source_platform),
                        "source_endpoint": _clean_text(source_endpoint),
                        "window_days": int(window_days or 0),
                        "window_start": _clean_text(window_start),
                        "window_end": _clean_text(window_end),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def upsert_product_daily_metric(
        self,
        *,
        product_id: str,
        metric_date: str,
        source_platform: str = "",
        sold_count: Any = 0,
        sale_amount: Any = 0,
        price_amount: Any = 0,
        currency: str = "",
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        metric_date = _clean_text(metric_date)
        source_platform = _clean_text(source_platform)
        if not product_id or not metric_date:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_daily_metrics (
                            metric_id, product_id, metric_date, source_platform,
                            sold_count, sale_amount, price_amount, currency,
                            payload_json, collected_at, created_at, updated_at
                        ) VALUES (
                            :metric_id, :product_id, :metric_date, :source_platform,
                            :sold_count, :sale_amount, :price_amount, :currency,
                            :payload_json, :collected_at, :created_at, :updated_at
                        )
                        ON CONFLICT(product_id, metric_date, source_platform)
                        DO UPDATE SET
                            sold_count = EXCLUDED.sold_count,
                            sale_amount = EXCLUDED.sale_amount,
                            price_amount = EXCLUDED.price_amount,
                            currency = EXCLUDED.currency,
                            payload_json = EXCLUDED.payload_json,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING *
                        """
                    ),
                    {
                        "metric_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "metric_date": metric_date,
                        "source_platform": source_platform,
                        "sold_count": _coerce_float(sold_count),
                        "sale_amount": _coerce_float(sale_amount),
                        "price_amount": _coerce_float(price_amount),
                        "currency": _clean_text(currency),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def record_product_distribution_window_observation(
        self,
        *,
        product_id: str,
        distribution_type: str,
        source_key: str = "",
        source_name: str = "",
        source_platform: str = "",
        window_days: int = 0,
        metric_value: Any = 0,
        metric_amount: Any = 0,
        observation_reason: str = "",
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        distribution_type = _clean_text(distribution_type)
        source_key = _clean_text(source_key)
        if not product_id or not distribution_type or not source_key:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_distribution_window_observations (
                            observation_id, product_id, distribution_type, source_key,
                            source_name, source_platform, window_days, metric_value,
                            metric_amount, observation_reason, payload_json,
                            collected_at, created_at
                        ) VALUES (
                            :observation_id, :product_id, :distribution_type, :source_key,
                            :source_name, :source_platform, :window_days, :metric_value,
                            :metric_amount, :observation_reason, :payload_json,
                            :collected_at, :created_at
                        )
                        RETURNING *
                        """
                    ),
                    {
                        "observation_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "distribution_type": distribution_type,
                        "source_key": source_key,
                        "source_name": _clean_text(source_name),
                        "source_platform": _clean_text(source_platform),
                        "window_days": int(window_days or 0),
                        "metric_value": _coerce_float(metric_value),
                        "metric_amount": _coerce_float(metric_amount),
                        "observation_reason": _clean_text(observation_reason),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def upsert_product_distribution_window_latest(
        self,
        *,
        product_id: str,
        distribution_type: str,
        source_key: str = "",
        source_name: str = "",
        source_platform: str = "",
        window_days: int = 0,
        metric_value: Any = 0,
        metric_amount: Any = 0,
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        distribution_type = _clean_text(distribution_type)
        source_key = _clean_text(source_key)
        if not product_id or not distribution_type or not source_key:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_distribution_window_latest (
                            latest_id, product_id, distribution_type, source_key,
                            source_name, source_platform, window_days, metric_value,
                            metric_amount, payload_json, collected_at, created_at, updated_at
                        ) VALUES (
                            :latest_id, :product_id, :distribution_type, :source_key,
                            :source_name, :source_platform, :window_days, :metric_value,
                            :metric_amount, :payload_json, :collected_at, :created_at, :updated_at
                        )
                        ON CONFLICT(product_id, distribution_type, source_key, source_platform, window_days)
                        DO UPDATE SET
                            source_name = EXCLUDED.source_name,
                            metric_value = EXCLUDED.metric_value,
                            metric_amount = EXCLUDED.metric_amount,
                            payload_json = EXCLUDED.payload_json,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING *
                        """
                    ),
                    {
                        "latest_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "distribution_type": distribution_type,
                        "source_key": source_key,
                        "source_name": _clean_text(source_name),
                        "source_platform": _clean_text(source_platform),
                        "window_days": int(window_days or 0),
                        "metric_value": _coerce_float(metric_value),
                        "metric_amount": _coerce_float(metric_amount),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def record_product_sku_window_observation(
        self,
        *,
        product_id: str,
        sku_key: str = "",
        sku_id: str = "",
        sku_name: str = "",
        source_platform: str = "",
        window_days: int = 0,
        sold_count: Any = 0,
        sale_amount: Any = 0,
        stock_count: Any = 0,
        observation_reason: str = "",
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        sku_id = _clean_text(sku_id)
        sku_name = _clean_text(sku_name)
        sku_key = _clean_text(sku_key) or f"{product_id}:{sku_id or sku_name}"
        if not product_id or not sku_key:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_sku_window_observations (
                            observation_id, product_id, sku_key, sku_id, sku_name,
                            source_platform, window_days, sold_count, sale_amount,
                            stock_count, observation_reason, payload_json,
                            collected_at, created_at
                        ) VALUES (
                            :observation_id, :product_id, :sku_key, :sku_id, :sku_name,
                            :source_platform, :window_days, :sold_count, :sale_amount,
                            :stock_count, :observation_reason, :payload_json,
                            :collected_at, :created_at
                        )
                        RETURNING *
                        """
                    ),
                    {
                        "observation_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "sku_key": sku_key,
                        "sku_id": sku_id,
                        "sku_name": sku_name,
                        "source_platform": _clean_text(source_platform),
                        "window_days": int(window_days or 0),
                        "sold_count": _coerce_float(sold_count),
                        "sale_amount": _coerce_float(sale_amount),
                        "stock_count": _coerce_float(stock_count),
                        "observation_reason": _clean_text(observation_reason),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def upsert_product_sku_window_latest(
        self,
        *,
        product_id: str,
        sku_key: str = "",
        sku_id: str = "",
        sku_name: str = "",
        source_platform: str = "",
        window_days: int = 0,
        sold_count: Any = 0,
        sale_amount: Any = 0,
        stock_count: Any = 0,
        payload: Mapping[str, Any] | None = None,
        collected_at: float | None = None,
    ) -> dict[str, Any]:
        product_id = _clean_text(product_id)
        sku_id = _clean_text(sku_id)
        sku_name = _clean_text(sku_name)
        sku_key = _clean_text(sku_key) or f"{product_id}:{sku_id or sku_name}"
        if not product_id or not sku_key:
            return {}
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        INSERT INTO tk_product_sku_window_latest (
                            latest_id, product_id, sku_key, sku_id, sku_name,
                            source_platform, window_days, sold_count, sale_amount,
                            stock_count, payload_json, collected_at, created_at, updated_at
                        ) VALUES (
                            :latest_id, :product_id, :sku_key, :sku_id, :sku_name,
                            :source_platform, :window_days, :sold_count, :sale_amount,
                            :stock_count, :payload_json, :collected_at, :created_at, :updated_at
                        )
                        ON CONFLICT(product_id, sku_key, source_platform, window_days)
                        DO UPDATE SET
                            sku_id = EXCLUDED.sku_id,
                            sku_name = EXCLUDED.sku_name,
                            sold_count = EXCLUDED.sold_count,
                            sale_amount = EXCLUDED.sale_amount,
                            stock_count = EXCLUDED.stock_count,
                            payload_json = EXCLUDED.payload_json,
                            collected_at = EXCLUDED.collected_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING *
                        """
                    ),
                    {
                        "latest_id": uuid.uuid4().hex,
                        "product_id": product_id,
                        "sku_key": sku_key,
                        "sku_id": sku_id,
                        "sku_name": sku_name,
                        "source_platform": _clean_text(source_platform),
                        "window_days": int(window_days or 0),
                        "sold_count": _coerce_float(sold_count),
                        "sale_amount": _coerce_float(sale_amount),
                        "stock_count": _coerce_float(stock_count),
                        "payload_json": _json_dumps(payload),
                        "collected_at": float(collected_at or now),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def link_raw_entity(
        self,
        *,
        raw_response_id: str,
        entity_type: str,
        entity_external_id: str,
        link_role: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_response_id = _clean_text(raw_response_id)
        entity_type = _clean_text(entity_type)
        entity_external_id = _clean_text(entity_external_id)
        if not raw_response_id or not entity_type or not entity_external_id:
            return {}
        raw_link_id = uuid.uuid4().hex
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    INSERT INTO tk_raw_entity_links (
                        raw_link_id, raw_response_id, entity_type, entity_external_id,
                        link_role, metadata_json, created_at
                    ) VALUES (
                        :raw_link_id, :raw_response_id, :entity_type, :entity_external_id,
                        :link_role, :metadata_json, :created_at
                    )
                    """
                ),
                {
                    "raw_link_id": raw_link_id,
                    "raw_response_id": raw_response_id,
                    "entity_type": entity_type,
                    "entity_external_id": entity_external_id,
                    "link_role": _clean_text(link_role),
                    "metadata_json": _json_dumps(metadata),
                    "created_at": now,
                },
            )
            row = (
                connection.execute(
                    self._text("SELECT * FROM tk_raw_entity_links WHERE raw_link_id = :raw_link_id LIMIT 1"),
                    {"raw_link_id": raw_link_id},
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def creator_has_product(self, *, creator_id: str = "", uid: str = "", unique_id: str = "", product_id: str) -> bool:
        creator_key = self.build_creator_key(creator_id=creator_id, uid=uid, unique_id=unique_id)
        product_id = _clean_text(product_id)
        if not creator_key or not product_id:
            return False
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT 1
                        FROM tk_creator_product_relations
                        WHERE creator_key = :creator_key
                          AND product_id = :product_id
                        LIMIT 1
                        """
                    ),
                    {"creator_key": creator_key, "product_id": product_id},
                )
                .first()
            )
        return row is not None

    def get_product(self, *, product_id: str) -> dict[str, Any]:
        return self._get_by_unique("tk_products", "product_id", _clean_text(product_id))

    def get_creator(self, *, creator_key: str) -> dict[str, Any]:
        return self._get_by_unique("tk_creators", "creator_key", _clean_text(creator_key))

    def get_raw_api_response(self, *, raw_response_id: str) -> dict[str, Any]:
        return self._get_by_unique("tk_raw_api_responses", "raw_response_id", _clean_text(raw_response_id))

    @staticmethod
    def build_shop_key(*, shop_id: str = "", shop_name: str = "") -> str:
        shop_id = _clean_text(shop_id)
        shop_name = _clean_text(shop_name)
        if shop_id:
            return f"shop_id:{shop_id}"
        if shop_name:
            return f"shop_name:{shop_name}"
        return ""

    @staticmethod
    def build_creator_key(*, creator_id: str = "", uid: str = "", unique_id: str = "") -> str:
        creator_id = _clean_text(creator_id)
        uid = _clean_text(uid)
        unique_id = _clean_text(unique_id)
        if creator_id:
            return f"creator_id:{creator_id}"
        if uid:
            return f"uid:{uid}"
        if unique_id:
            return f"unique_id:{unique_id}"
        return ""

    @staticmethod
    def build_asset_key(
        *,
        source_url: str = "",
        file_token: str = "",
        local_path: str = "",
        object_key: str = "",
    ) -> str:
        for prefix, value in (
            ("file_token", file_token),
            ("object_key", object_key),
            ("local_path", local_path),
            ("source_url", source_url),
        ):
            cleaned = _clean_text(value)
            if cleaned:
                return f"{prefix}:{cleaned}"
        return ""

    def table_names(self) -> set[str]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                self._text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = current_schema()
                    """
                )
            ).mappings().all()
            return {str(row["table_name"]) for row in rows}

    def _upsert_relation(
        self,
        *,
        table_name: str,
        relation_key: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        data = {
            "relation_id": uuid.uuid4().hex,
            "relation_key": relation_key,
            **dict(values),
        }
        return self._upsert_by_unique(
            table_name=table_name,
            unique_column="relation_key",
            unique_value=relation_key,
            values=data,
        )

    def _upsert_by_unique(
        self,
        *,
        table_name: str,
        unique_column: str,
        unique_value: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT *
                        FROM {table_name}
                        WHERE {unique_column} = :unique_value
                        LIMIT 1
                        """
                    ),
                    {"unique_value": unique_value},
                )
                .mappings()
                .first()
            )
            data = dict(values)
            if existing is None:
                data.setdefault("created_at", now)
                data.setdefault("updated_at", now)
                data.setdefault("first_seen_at", now)
                data.setdefault("last_seen_at", now)
                self._insert_row(connection, table_name=table_name, data=data)
            else:
                update_data = self._merge_update_data(existing, data)
                update_data["updated_at"] = now
                update_data["last_seen_at"] = now
                self._update_row(
                    connection,
                    table_name=table_name,
                    unique_column=unique_column,
                    unique_value=unique_value,
                    data=update_data,
                )
            row = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT *
                        FROM {table_name}
                        WHERE {unique_column} = :unique_value
                        LIMIT 1
                        """
                    ),
                    {"unique_value": unique_value},
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def _get_by_unique(self, table_name: str, unique_column: str, unique_value: str) -> dict[str, Any]:
        if not unique_value:
            return {}
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT *
                        FROM {table_name}
                        WHERE {unique_column} = :unique_value
                        LIMIT 1
                        """
                    ),
                    {"unique_value": unique_value},
                )
                .mappings()
                .first()
            )
        return self._row_to_dict(row) if row is not None else {}

    def _insert_row(self, connection: Any, *, table_name: str, data: Mapping[str, Any]) -> None:
        columns = list(data.keys())
        placeholders = [f":{column}" for column in columns]
        connection.execute(
            self._text(
                f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
            ),
            dict(data),
        )

    def _update_row(
        self,
        connection: Any,
        *,
        table_name: str,
        unique_column: str,
        unique_value: str,
        data: Mapping[str, Any],
    ) -> None:
        update_data = {key: value for key, value in data.items() if key not in {"id", "asset_id", "link_id", "relation_id"}}
        if not update_data:
            return
        assignments = [f"{column} = :{column}" for column in update_data]
        update_data = dict(update_data)
        update_data["unique_value"] = unique_value
        connection.execute(
            self._text(
                f"""
                UPDATE {table_name}
                SET {', '.join(assignments)}
                WHERE {unique_column} = :unique_value
                """
            ),
            update_data,
        )

    def _merge_update_data(self, existing: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for key, value in data.items():
            if key in {"id", "asset_id", "link_id", "relation_id", "created_at", "first_seen_at"}:
                continue
            if str(key).endswith("_json"):
                merged[key] = value if _non_empty(_load_json_dict(str(value or ""))) else existing.get(key, value)
                continue
            if _non_empty(value):
                merged[key] = value
            elif key in existing:
                merged[key] = existing[key]
        return merged

    def _row_to_dict(self, row: Mapping[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        for key in list(payload.keys()):
            if str(key).endswith("_json"):
                public_key = str(key)[: -len("_json")]
                payload[public_key] = _load_json_dict(str(payload.get(key) or ""))
        return payload


def extract_fact_payloads(items: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    fact_entities: list[dict[str, Any]] = []
    fact_relations: list[dict[str, Any]] = []
    fact_media_assets: list[dict[str, Any]] = []
    fact_metric_observations: list[dict[str, Any]] = []
    raw_api_responses: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {
        "entities": set(),
        "relations": set(),
        "media": set(),
        "metrics": set(),
        "raw": set(),
    }

    for item in items:
        for entity in item.get("fact_entities", []) if isinstance(item.get("fact_entities"), list) else []:
            key = _fact_identity(entity)
            if key and key not in seen["entities"]:
                seen["entities"].add(key)
                fact_entities.append(dict(entity))
        for relation in item.get("fact_relations", []) if isinstance(item.get("fact_relations"), list) else []:
            key = _fact_identity(relation)
            if key and key not in seen["relations"]:
                seen["relations"].add(key)
                fact_relations.append(dict(relation))
        for asset in item.get("fact_media_assets", []) if isinstance(item.get("fact_media_assets"), list) else []:
            key = _fact_identity(asset)
            if key and key not in seen["media"]:
                seen["media"].add(key)
                fact_media_assets.append(dict(asset))
        for metric in (
            item.get("fact_metric_observations", [])
            if isinstance(item.get("fact_metric_observations"), list)
            else []
        ):
            key = _fact_identity(metric)
            if key and key not in seen["metrics"]:
                seen["metrics"].add(key)
                fact_metric_observations.append(dict(metric))
        for raw_response in item.get("raw_api_responses", []) if isinstance(item.get("raw_api_responses"), list) else []:
            key = _fact_identity(raw_response)
            if key and key not in seen["raw"]:
                seen["raw"].add(key)
                raw_api_responses.append(dict(raw_response))

    return {
        "fact_entities": fact_entities,
        "fact_relations": fact_relations,
        "fact_media_assets": fact_media_assets,
        "fact_metric_observations": fact_metric_observations,
        "raw_api_responses": raw_api_responses,
    }


def _fact_identity(payload: Mapping[str, Any]) -> str:
    for key in (
        "id",
        "product_id",
        "shop_key",
        "creator_key",
        "video_key",
        "asset_id",
        "latest_id",
        "observation_id",
        "relation_key",
        "raw_response_id",
        "raw_link_id",
    ):
        value = _clean_text(payload.get(key))
        if value:
            return f"{key}:{value}"
    return ""
