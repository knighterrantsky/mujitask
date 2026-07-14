from __future__ import annotations

import hashlib
import json
import time
import uuid
from decimal import Decimal
from typing import Any, Mapping, Sequence


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, field_name: str) -> str:
    cleaned = _clean_text(value)
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    return cleaned


def _canonical_json(value: Any, *, default: Any) -> str:
    payload = default if value is None else value
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value).replace(",", ""))


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _timestamp(value: Any) -> float:
    return float(value) if value not in (None, "") else time.time()


class AmazonFactStore:
    """Explicit PostgreSQL persistence for the isolated Amazon Fact tables."""

    def __init__(self, *, runtime_store: Any | None = None, db_url: str = "") -> None:
        if runtime_store is not None:
            self._engine = runtime_store._engine  # noqa: SLF001
            self._text = runtime_store._text  # noqa: SLF001
            return

        try:
            from sqlalchemy import create_engine, text
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency is required in runtime.
            raise RuntimeError("AmazonFactStore requires SQLAlchemy.") from exc

        resolved_db_url = _clean_text(db_url)
        if not resolved_db_url:
            raise RuntimeError("AmazonFactStore requires a configured PostgreSQL Fact DB URL.")
        if resolved_db_url.lower().startswith("sqlite"):
            raise RuntimeError("SQLite is not supported for AmazonFactStore; use PostgreSQL.")
        self._text = text
        self._engine = create_engine(
            resolved_db_url,
            future=True,
            pool_size=2,
            max_overflow=0,
            pool_timeout=10,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    def upsert_product(
        self,
        *,
        marketplace_code: str,
        asin: str,
        canonical_url: str | None = None,
        parent_asin: str | None = None,
        title: str | None = None,
        brand: str | None = None,
        category_path: Sequence[Any] | None = None,
        status: str | None = None,
        latest_snapshot_id: str | None = None,
        facts: Mapping[str, Any] | None = None,
        observed_at: Any = None,
    ) -> dict[str, Any]:
        marketplace = _required_text(marketplace_code, "marketplace_code").upper()
        normalized_asin = _required_text(asin, "asin").upper()
        observed = _timestamp(observed_at)
        now = time.time()
        values = {
            "id": uuid.uuid4().hex,
            "marketplace_code": marketplace,
            "asin": normalized_asin,
            "canonical_url": _clean_text(canonical_url),
            "canonical_url_provided": bool(_clean_text(canonical_url)),
            "parent_asin": _clean_text(parent_asin).upper(),
            "parent_asin_provided": bool(_clean_text(parent_asin)),
            "title": _clean_text(title),
            "title_provided": bool(_clean_text(title)),
            "brand": _clean_text(brand),
            "brand_provided": bool(_clean_text(brand)),
            "category_path_json": _canonical_json(category_path, default=[]),
            "category_path_provided": category_path is not None,
            "status": _clean_text(status) or "active",
            "status_provided": bool(_clean_text(status)),
            "latest_snapshot_id": _clean_text(latest_snapshot_id),
            "latest_snapshot_id_provided": bool(_clean_text(latest_snapshot_id)),
            "facts_json": _canonical_json(facts, default={}),
            "facts_provided": facts is not None,
            "first_seen_at": observed,
            "last_seen_at": observed,
            "created_at": now,
            "updated_at": now,
        }
        sql = """
            INSERT INTO amazon_products (
                id, marketplace_code, asin, canonical_url, parent_asin, title, brand,
                category_path_json, status, latest_snapshot_id, facts_json,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (
                :id, :marketplace_code, :asin, :canonical_url, :parent_asin, :title, :brand,
                :category_path_json, :status, :latest_snapshot_id, :facts_json,
                :first_seen_at, :last_seen_at, :created_at, :updated_at
            )
            ON CONFLICT (marketplace_code, asin) DO UPDATE SET
                canonical_url = CASE WHEN :canonical_url_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.canonical_url ELSE amazon_products.canonical_url END,
                parent_asin = CASE WHEN :parent_asin_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.parent_asin ELSE amazon_products.parent_asin END,
                title = CASE WHEN :title_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.title ELSE amazon_products.title END,
                brand = CASE WHEN :brand_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.brand ELSE amazon_products.brand END,
                category_path_json = CASE WHEN :category_path_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.category_path_json ELSE amazon_products.category_path_json END,
                status = CASE WHEN :status_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.status ELSE amazon_products.status END,
                latest_snapshot_id = CASE WHEN :latest_snapshot_id_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.latest_snapshot_id ELSE amazon_products.latest_snapshot_id END,
                facts_json = CASE WHEN :facts_provided
                    AND EXCLUDED.last_seen_at >= amazon_products.last_seen_at
                    THEN EXCLUDED.facts_json ELSE amazon_products.facts_json END,
                first_seen_at = LEAST(amazon_products.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = GREATEST(amazon_products.last_seen_at, EXCLUDED.last_seen_at),
                updated_at = EXCLUDED.updated_at
            RETURNING *
        """
        return self._execute_returning(sql, values)

    def set_latest_snapshot(
        self,
        *,
        product_id: str,
        snapshot_id: str,
        observed_at: Any = None,
    ) -> dict[str, Any]:
        values = {
            "product_id": _required_text(product_id, "product_id"),
            "snapshot_id": _required_text(snapshot_id, "snapshot_id"),
            "observed_at": _timestamp(observed_at),
            "updated_at": time.time(),
        }
        updated = self._execute_returning(
            """
            UPDATE amazon_products AS product
            SET latest_snapshot_id = :snapshot_id,
                last_seen_at = GREATEST(
                    product.last_seen_at,
                    candidate.collected_at,
                    :observed_at
                ),
                updated_at = :updated_at
            FROM amazon_product_snapshots AS candidate
            WHERE product.id = :product_id
              AND candidate.snapshot_id = :snapshot_id
              AND candidate.product_id = product.id
              AND (
                  product.latest_snapshot_id = ''
                  OR candidate.collected_at >= COALESCE(
                      (
                          SELECT current.collected_at
                          FROM amazon_product_snapshots AS current
                          WHERE current.snapshot_id = product.latest_snapshot_id
                      ),
                      candidate.collected_at
                  )
              )
            RETURNING product.*
            """,
            values,
        )
        if updated:
            return updated
        return self._select_one(
            "SELECT * FROM amazon_products WHERE id = :product_id LIMIT 1",
            values,
        )

    def record_product_snapshot(
        self,
        *,
        product_id: str,
        marketplace_code: str,
        asin: str,
        run_id: str,
        request_id: str = "",
        execution_id: str = "",
        resolved_asin: str = "",
        parent_asin: str = "",
        availability_status: str = "unknown",
        title: str = "",
        brand: str = "",
        category_path: Sequence[Any] | None = None,
        bullet_points: Sequence[Any] | None = None,
        description: str = "",
        technical_details: Mapping[str, Any] | None = None,
        rating: Any = None,
        review_count: Any = None,
        variant_attributes: Mapping[str, Any] | None = None,
        child_asins: Sequence[Any] | None = None,
        field_coverage: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        content_digest: str = "",
        collected_at: Any = None,
    ) -> dict[str, Any]:
        collected = _timestamp(collected_at)
        values = {
            "snapshot_id": uuid.uuid4().hex,
            "product_id": _required_text(product_id, "product_id"),
            "marketplace_code": _required_text(marketplace_code, "marketplace_code").upper(),
            "asin": _required_text(asin, "asin").upper(),
            "run_id": _required_text(run_id, "run_id"),
            "request_id": _clean_text(request_id),
            "execution_id": _clean_text(execution_id),
            "resolved_asin": _clean_text(resolved_asin).upper(),
            "parent_asin": _clean_text(parent_asin).upper(),
            "availability_status": _clean_text(availability_status) or "unknown",
            "title": _clean_text(title),
            "brand": _clean_text(brand),
            "category_path_json": _canonical_json(category_path, default=[]),
            "bullet_points_json": _canonical_json(bullet_points, default=[]),
            "description": _clean_text(description),
            "technical_details_json": _canonical_json(technical_details, default={}),
            "rating": _optional_float(rating),
            "review_count": _optional_int(review_count),
            "variant_attributes_json": _canonical_json(variant_attributes, default={}),
            "child_asins_json": _canonical_json(child_asins, default=[]),
            "field_coverage_json": _canonical_json(field_coverage, default={}),
            "payload_json": _canonical_json(payload, default={}),
            "content_digest": _clean_text(content_digest),
            "collected_at": collected,
            "created_at": time.time(),
        }
        sql = """
            INSERT INTO amazon_product_snapshots (
                snapshot_id, product_id, marketplace_code, asin, run_id, request_id,
                execution_id, resolved_asin, parent_asin, availability_status, title, brand,
                category_path_json, bullet_points_json, description, technical_details_json,
                rating, review_count, variant_attributes_json, child_asins_json,
                field_coverage_json, payload_json, content_digest, collected_at, created_at
            ) VALUES (
                :snapshot_id, :product_id, :marketplace_code, :asin, :run_id, :request_id,
                :execution_id, :resolved_asin, :parent_asin, :availability_status, :title, :brand,
                :category_path_json, :bullet_points_json, :description, :technical_details_json,
                :rating, :review_count, :variant_attributes_json, :child_asins_json,
                :field_coverage_json, :payload_json, :content_digest, :collected_at, :created_at
            )
            ON CONFLICT (marketplace_code, asin, run_id) DO NOTHING
            RETURNING *
        """
        return self._insert_or_select(
            sql,
            values,
            """
            SELECT * FROM amazon_product_snapshots
            WHERE marketplace_code = :marketplace_code AND asin = :asin AND run_id = :run_id
            LIMIT 1
            """,
        )

    def record_featured_offer(
        self,
        *,
        product_snapshot_id: str,
        product_id: str,
        offer_key: str = "featured_offer",
        seller_id: str = "",
        seller_name: str = "",
        is_featured_offer: bool = True,
        price_amount: Any = None,
        list_price_amount: Any = None,
        currency: str = "",
        availability_status: str = "unknown",
        fulfillment_channel: str = "unknown",
        delivery_text: str = "",
        coupon_text: str = "",
        promotions: Sequence[Any] | None = None,
        profile_context_digest: str = "",
        collected_at: Any = None,
    ) -> dict[str, Any]:
        values = {
            "offer_snapshot_id": uuid.uuid4().hex,
            "product_snapshot_id": _required_text(product_snapshot_id, "product_snapshot_id"),
            "product_id": _required_text(product_id, "product_id"),
            "offer_key": _required_text(offer_key, "offer_key"),
            "seller_id": _clean_text(seller_id),
            "seller_name": _clean_text(seller_name),
            "is_featured_offer": bool(is_featured_offer),
            "price_amount": _optional_decimal(price_amount),
            "list_price_amount": _optional_decimal(list_price_amount),
            "currency": _clean_text(currency).upper(),
            "availability_status": _clean_text(availability_status) or "unknown",
            "fulfillment_channel": _clean_text(fulfillment_channel) or "unknown",
            "delivery_text": _clean_text(delivery_text),
            "coupon_text": _clean_text(coupon_text),
            "promotions_json": _canonical_json(promotions, default=[]),
            "profile_context_digest": _clean_text(profile_context_digest),
            "collected_at": _timestamp(collected_at),
            "created_at": time.time(),
        }
        sql = """
            INSERT INTO amazon_offer_snapshots (
                offer_snapshot_id, product_snapshot_id, product_id, offer_key, seller_id,
                seller_name, is_featured_offer, price_amount, list_price_amount, currency,
                availability_status, fulfillment_channel, delivery_text, coupon_text,
                promotions_json, profile_context_digest, collected_at, created_at
            ) VALUES (
                :offer_snapshot_id, :product_snapshot_id, :product_id, :offer_key, :seller_id,
                :seller_name, :is_featured_offer, :price_amount, :list_price_amount, :currency,
                :availability_status, :fulfillment_channel, :delivery_text, :coupon_text,
                :promotions_json, :profile_context_digest, :collected_at, :created_at
            )
            ON CONFLICT (product_snapshot_id, offer_key) DO NOTHING
            RETURNING *
        """
        return self._insert_or_select(
            sql,
            values,
            """
            SELECT * FROM amazon_offer_snapshots
            WHERE product_snapshot_id = :product_snapshot_id AND offer_key = :offer_key
            LIMIT 1
            """,
        )

    def upsert_variant(
        self,
        *,
        marketplace_code: str,
        parent_asin: str,
        child_asin: str,
        attributes: Mapping[str, Any] | None = None,
        dimensions: Mapping[str, Any] | None = None,
        source_asin: str = "",
        observed_at: Any = None,
    ) -> dict[str, Any]:
        observed = _timestamp(observed_at)
        values = {
            "relation_id": uuid.uuid4().hex,
            "marketplace_code": _required_text(marketplace_code, "marketplace_code").upper(),
            "parent_asin": _required_text(parent_asin, "parent_asin").upper(),
            "child_asin": _required_text(child_asin, "child_asin").upper(),
            "attributes_json": _canonical_json(attributes, default={}),
            "attributes_provided": attributes is not None,
            "dimensions_json": _canonical_json(dimensions, default={}),
            "dimensions_provided": dimensions is not None,
            "source_asin": _clean_text(source_asin).upper(),
            "first_seen_at": observed,
            "last_seen_at": observed,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        return self._execute_returning(
            """
            INSERT INTO amazon_product_variants (
                relation_id, marketplace_code, parent_asin, child_asin, attributes_json,
                dimensions_json, source_asin, first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (
                :relation_id, :marketplace_code, :parent_asin, :child_asin, :attributes_json,
                :dimensions_json, :source_asin, :first_seen_at, :last_seen_at, :created_at, :updated_at
            )
            ON CONFLICT (marketplace_code, parent_asin, child_asin) DO UPDATE SET
                attributes_json = CASE WHEN :attributes_provided
                    AND EXCLUDED.last_seen_at >= amazon_product_variants.last_seen_at
                    THEN EXCLUDED.attributes_json ELSE amazon_product_variants.attributes_json END,
                dimensions_json = CASE WHEN :dimensions_provided
                    AND EXCLUDED.last_seen_at >= amazon_product_variants.last_seen_at
                    THEN EXCLUDED.dimensions_json ELSE amazon_product_variants.dimensions_json END,
                source_asin = CASE WHEN EXCLUDED.source_asin <> ''
                    AND EXCLUDED.last_seen_at >= amazon_product_variants.last_seen_at
                    THEN EXCLUDED.source_asin ELSE amazon_product_variants.source_asin END,
                first_seen_at = LEAST(
                    amazon_product_variants.first_seen_at,
                    EXCLUDED.first_seen_at
                ),
                last_seen_at = GREATEST(
                    amazon_product_variants.last_seen_at,
                    EXCLUDED.last_seen_at
                ),
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            values,
        )

    def record_bsr_snapshot(
        self,
        *,
        product_snapshot_id: str,
        product_id: str,
        category_name: str,
        category_path: Sequence[Any] | None,
        rank_value: Any,
        collected_at: Any = None,
    ) -> dict[str, Any]:
        values = {
            "bsr_snapshot_id": uuid.uuid4().hex,
            "product_snapshot_id": _required_text(product_snapshot_id, "product_snapshot_id"),
            "product_id": _required_text(product_id, "product_id"),
            "category_name": _required_text(category_name, "category_name"),
            "category_path_json": _canonical_json(category_path, default=[]),
            "rank_value": int(rank_value),
            "collected_at": _timestamp(collected_at),
            "created_at": time.time(),
        }
        sql = """
            INSERT INTO amazon_bsr_snapshots (
                bsr_snapshot_id, product_snapshot_id, product_id, category_name,
                category_path_json, rank_value, collected_at, created_at
            ) VALUES (
                :bsr_snapshot_id, :product_snapshot_id, :product_id, :category_name,
                :category_path_json, :rank_value, :collected_at, :created_at
            )
            ON CONFLICT (product_snapshot_id, category_name, category_path_json) DO NOTHING
            RETURNING *
        """
        return self._insert_or_select(
            sql,
            values,
            """
            SELECT * FROM amazon_bsr_snapshots
            WHERE product_snapshot_id = :product_snapshot_id
              AND category_name = :category_name
              AND category_path_json = :category_path_json
            LIMIT 1
            """,
        )

    def upsert_media_asset(
        self,
        *,
        source_url: str = "",
        content_digest: str = "",
        bucket: str = "",
        object_key: str = "",
        remote_uri: str = "",
        file_name: str = "",
        mime_type: str = "",
        size_bytes: Any = 0,
        metadata: Mapping[str, Any] | None = None,
        asset_key: str = "",
        observed_at: Any = None,
    ) -> dict[str, Any]:
        normalized_url = _clean_text(source_url)
        source_digest = (
            hashlib.sha256(normalized_url.encode("utf-8")).hexdigest() if normalized_url else ""
        )
        normalized_content_digest = _clean_text(content_digest)
        resolved_asset_key = _clean_text(asset_key)
        if not resolved_asset_key and normalized_content_digest:
            resolved_asset_key = f"content_sha256:{normalized_content_digest}"
        if not resolved_asset_key and source_digest:
            resolved_asset_key = f"source_url_sha256:{source_digest}"
        resolved_asset_key = _required_text(resolved_asset_key, "asset_key")
        observed = _timestamp(observed_at)
        values = {
            "asset_id": uuid.uuid4().hex,
            "asset_key": resolved_asset_key,
            "source_url": normalized_url,
            "source_url_digest": source_digest,
            "content_digest": normalized_content_digest,
            "bucket": _clean_text(bucket),
            "object_key": _clean_text(object_key),
            "remote_uri": _clean_text(remote_uri),
            "file_name": _clean_text(file_name),
            "mime_type": _clean_text(mime_type),
            "size_bytes": max(0, int(size_bytes or 0)),
            "metadata_json": _canonical_json(metadata, default={}),
            "metadata_provided": metadata is not None,
            "first_seen_at": observed,
            "last_seen_at": observed,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        return self._execute_returning(
            """
            INSERT INTO amazon_media_assets (
                asset_id, asset_key, source_url, source_url_digest, content_digest, bucket,
                object_key, remote_uri, file_name, mime_type, size_bytes, metadata_json,
                first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (
                :asset_id, :asset_key, :source_url, :source_url_digest, :content_digest, :bucket,
                :object_key, :remote_uri, :file_name, :mime_type, :size_bytes, :metadata_json,
                :first_seen_at, :last_seen_at, :created_at, :updated_at
            )
            ON CONFLICT (asset_key) DO UPDATE SET
                source_url = CASE WHEN EXCLUDED.source_url <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.source_url ELSE amazon_media_assets.source_url END,
                source_url_digest = CASE WHEN EXCLUDED.source_url_digest <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.source_url_digest ELSE amazon_media_assets.source_url_digest END,
                content_digest = CASE WHEN EXCLUDED.content_digest <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.content_digest ELSE amazon_media_assets.content_digest END,
                bucket = CASE WHEN EXCLUDED.bucket <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.bucket ELSE amazon_media_assets.bucket END,
                object_key = CASE WHEN EXCLUDED.object_key <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.object_key ELSE amazon_media_assets.object_key END,
                remote_uri = CASE WHEN EXCLUDED.remote_uri <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.remote_uri ELSE amazon_media_assets.remote_uri END,
                file_name = CASE WHEN EXCLUDED.file_name <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.file_name ELSE amazon_media_assets.file_name END,
                mime_type = CASE WHEN EXCLUDED.mime_type <> ''
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.mime_type ELSE amazon_media_assets.mime_type END,
                size_bytes = CASE WHEN EXCLUDED.size_bytes > 0
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.size_bytes ELSE amazon_media_assets.size_bytes END,
                metadata_json = CASE WHEN :metadata_provided
                    AND EXCLUDED.last_seen_at >= amazon_media_assets.last_seen_at
                    THEN EXCLUDED.metadata_json ELSE amazon_media_assets.metadata_json END,
                first_seen_at = LEAST(amazon_media_assets.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = GREATEST(amazon_media_assets.last_seen_at, EXCLUDED.last_seen_at),
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            values,
        )

    def link_product_media_asset(
        self,
        *,
        product_id: str,
        asset_id: str,
        media_role: str,
        position: Any = 0,
        metadata: Mapping[str, Any] | None = None,
        observed_at: Any = None,
    ) -> dict[str, Any]:
        product = _required_text(product_id, "product_id")
        asset = _required_text(asset_id, "asset_id")
        role = _required_text(media_role, "media_role")
        normalized_position = int(position or 0)
        relation_key = hashlib.sha256(
            f"{product}\n{asset}\n{role}\n{normalized_position}".encode("utf-8")
        ).hexdigest()
        observed = _timestamp(observed_at)
        values = {
            "relation_id": uuid.uuid4().hex,
            "relation_key": relation_key,
            "product_id": product,
            "asset_id": asset,
            "media_role": role,
            "position": normalized_position,
            "metadata_json": _canonical_json(metadata, default={}),
            "metadata_provided": metadata is not None,
            "first_seen_at": observed,
            "last_seen_at": observed,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        return self._execute_returning(
            """
            INSERT INTO amazon_product_media_assets (
                relation_id, relation_key, product_id, asset_id, media_role, position,
                metadata_json, first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (
                :relation_id, :relation_key, :product_id, :asset_id, :media_role, :position,
                :metadata_json, :first_seen_at, :last_seen_at, :created_at, :updated_at
            )
            ON CONFLICT (product_id, asset_id, media_role, position) DO UPDATE SET
                metadata_json = CASE WHEN :metadata_provided
                    AND EXCLUDED.last_seen_at >= amazon_product_media_assets.last_seen_at
                    THEN EXCLUDED.metadata_json ELSE amazon_product_media_assets.metadata_json END,
                first_seen_at = LEAST(
                    amazon_product_media_assets.first_seen_at,
                    EXCLUDED.first_seen_at
                ),
                last_seen_at = GREATEST(
                    amazon_product_media_assets.last_seen_at,
                    EXCLUDED.last_seen_at
                ),
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            values,
        )

    def record_raw_capture(
        self,
        *,
        product_id: str,
        snapshot_id: str = "",
        capture_kind: str,
        bucket: str,
        object_key: str,
        content_digest: str = "",
        content_type: str = "",
        request_id: str = "",
        execution_id: str = "",
        run_id: str = "",
        sanitization_status: str = "unknown",
        collected_at: Any = None,
    ) -> dict[str, Any]:
        values = {
            "raw_capture_id": uuid.uuid4().hex,
            "product_id": _required_text(product_id, "product_id"),
            "snapshot_id": _clean_text(snapshot_id),
            "capture_kind": _required_text(capture_kind, "capture_kind"),
            "bucket": _required_text(bucket, "bucket"),
            "object_key": _required_text(object_key, "object_key"),
            "content_digest": _clean_text(content_digest),
            "content_type": _clean_text(content_type),
            "request_id": _clean_text(request_id),
            "execution_id": _clean_text(execution_id),
            "run_id": _clean_text(run_id),
            "sanitization_status": _clean_text(sanitization_status) or "unknown",
            "collected_at": _timestamp(collected_at),
            "created_at": time.time(),
        }
        sql = """
            INSERT INTO amazon_raw_captures (
                raw_capture_id, product_id, snapshot_id, capture_kind, bucket, object_key,
                content_digest, content_type, request_id, execution_id, run_id,
                sanitization_status, collected_at, created_at
            ) VALUES (
                :raw_capture_id, :product_id, :snapshot_id, :capture_kind, :bucket, :object_key,
                :content_digest, :content_type, :request_id, :execution_id, :run_id,
                :sanitization_status, :collected_at, :created_at
            )
            ON CONFLICT (bucket, object_key) DO NOTHING
            RETURNING *
        """
        return self._insert_or_select(
            sql,
            values,
            """
            SELECT * FROM amazon_raw_captures
            WHERE bucket = :bucket AND object_key = :object_key
            LIMIT 1
            """,
        )

    def upsert_feishu_binding(
        self,
        *,
        product_id: str,
        base_id: str,
        table_id: str,
        record_id: str,
        source_asin: str = "",
        status: str | None = None,
        last_synced_snapshot_id: str = "",
        observed_at: Any = None,
    ) -> dict[str, Any]:
        observed = _timestamp(observed_at)
        values = {
            "binding_id": uuid.uuid4().hex,
            "product_id": _required_text(product_id, "product_id"),
            "base_id": _required_text(base_id, "base_id"),
            "table_id": _required_text(table_id, "table_id"),
            "record_id": _required_text(record_id, "record_id"),
            "source_asin": _clean_text(source_asin).upper(),
            "status": _clean_text(status) or "active",
            "status_provided": bool(_clean_text(status)),
            "last_synced_snapshot_id": _clean_text(last_synced_snapshot_id),
            "first_bound_at": observed,
            "last_synced_at": observed,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        return self._execute_returning(
            """
            INSERT INTO amazon_feishu_bindings (
                binding_id, product_id, base_id, table_id, record_id, source_asin, status,
                last_synced_snapshot_id, first_bound_at, last_synced_at, created_at, updated_at
            ) VALUES (
                :binding_id, :product_id, :base_id, :table_id, :record_id, :source_asin, :status,
                :last_synced_snapshot_id, :first_bound_at, :last_synced_at, :created_at, :updated_at
            )
            ON CONFLICT (base_id, table_id, record_id) DO UPDATE SET
                product_id = CASE
                    WHEN EXCLUDED.last_synced_at >= amazon_feishu_bindings.last_synced_at
                    THEN EXCLUDED.product_id ELSE amazon_feishu_bindings.product_id END,
                source_asin = CASE WHEN EXCLUDED.source_asin <> ''
                    AND EXCLUDED.last_synced_at >= amazon_feishu_bindings.last_synced_at
                    THEN EXCLUDED.source_asin ELSE amazon_feishu_bindings.source_asin END,
                status = CASE WHEN :status_provided
                    AND EXCLUDED.last_synced_at >= amazon_feishu_bindings.last_synced_at
                    THEN EXCLUDED.status ELSE amazon_feishu_bindings.status END,
                last_synced_snapshot_id = CASE WHEN EXCLUDED.last_synced_snapshot_id <> ''
                    AND EXCLUDED.last_synced_at >= amazon_feishu_bindings.last_synced_at
                    THEN EXCLUDED.last_synced_snapshot_id
                    ELSE amazon_feishu_bindings.last_synced_snapshot_id END,
                first_bound_at = LEAST(
                    amazon_feishu_bindings.first_bound_at,
                    EXCLUDED.first_bound_at
                ),
                last_synced_at = GREATEST(
                    amazon_feishu_bindings.last_synced_at,
                    EXCLUDED.last_synced_at
                ),
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            values,
        )

    def _execute_returning(self, sql: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._engine.begin() as connection:
            row = connection.execute(self._text(sql), dict(values)).mappings().first()
        return self._row_to_dict(row)

    def _select_one(self, sql: str, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._engine.connect() as connection:
            row = connection.execute(self._text(sql), dict(values)).mappings().first()
        return self._row_to_dict(row)

    def _insert_or_select(
        self,
        insert_sql: str,
        values: Mapping[str, Any],
        select_sql: str,
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            row = connection.execute(self._text(insert_sql), dict(values)).mappings().first()
            if row is None:
                row = connection.execute(self._text(select_sql), dict(values)).mappings().first()
        return self._row_to_dict(row)

    @staticmethod
    def _row_to_dict(row: Mapping[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {}
        result = dict(row)
        for key, value in tuple(result.items()):
            if isinstance(value, Decimal):
                result[key] = float(value)
            if key.endswith("_json"):
                result[key[: -len("_json")]] = _decode_json(value)
        return result
