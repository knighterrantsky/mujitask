from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore


class TKFactIngestionService:
    """Normalize collected TK data before writing it into fact tables."""

    def __init__(
        self,
        *,
        runtime_store: Any | None = None,
        fact_store: TKFactStore | None = None,
        db_url: str = "",
        db_path: str | Path = "",
    ):
        self.fact_store = fact_store or TKFactStore(
            runtime_store=runtime_store,
            db_url=db_url,
            db_path=db_path,
        )

    def ingest_tiktok_product_request(
        self,
        *,
        logical_fields: Mapping[str, Any],
        source_item: Mapping[str, Any] | None = None,
        fastmoss_snapshot: Mapping[str, Any] | None = None,
        execution: Any | None = None,
        source_endpoint: str = "tiktok_product.request",
    ) -> dict[str, list[dict[str, Any]]]:
        logical_payload = dict(logical_fields or {})
        item_payload = dict(source_item or {})
        fastmoss_payload = dict(fastmoss_snapshot or {})
        product_id = _first_non_empty(
            item_payload.get("product_id"),
            logical_payload.get("product_id"),
            fastmoss_payload.get("product_id"),
        )
        if not product_id:
            return _empty_ingestion_payload()

        product = {
            "product_id": product_id,
            "product_url": _first_non_empty(item_payload.get("source_url"), logical_payload.get("source_url")),
            "normalized_url": _first_non_empty(
                item_payload.get("normalized_url"),
                logical_payload.get("normalized_url"),
            ),
            "title": _first_non_empty(logical_payload.get("title"), fastmoss_payload.get("product_title")),
            "holiday": _first_non_empty(logical_payload.get("holiday")),
            "seller_name": _first_non_empty(logical_payload.get("shop_name")),
            "source_platform": "tiktok",
            "facts": {
                "fields": dict(item_payload.get("fields") or {})
                if isinstance(item_payload.get("fields"), Mapping)
                else {},
                "logical_fields": logical_payload,
                "fastmoss_snapshot": fastmoss_payload,
            },
            "shop_name": _first_non_empty(logical_payload.get("shop_name")),
            "shop_url": _first_non_empty(logical_payload.get("shop_url")),
        }
        media_assets = [
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": "product_main_image",
                "source_url": logical_payload.get("main_image_url"),
                "local_path": logical_payload.get("main_image_local_path"),
                "file_name": logical_payload.get("main_image_file_name"),
                "mime_type": logical_payload.get("main_image_mime_type"),
                "source_platform": "tiktok",
            },
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": "product_page_screenshot",
                "local_path": logical_payload.get("product_page_screenshot_local_path"),
                "file_name": logical_payload.get("product_page_screenshot_file_name"),
                "mime_type": logical_payload.get("product_page_screenshot_mime_type"),
                "source_platform": "tiktok",
            },
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": "fastmoss_detail_screenshot",
                "local_path": fastmoss_payload.get("detail_page_screenshot_local_path"),
                "file_name": fastmoss_payload.get("detail_page_screenshot_file_name"),
                "mime_type": fastmoss_payload.get("detail_page_screenshot_mime_type"),
                "source_platform": "fastmoss",
            },
        ]
        return self.ingest_api_response(
            source_platform="tiktok",
            source_endpoint=source_endpoint,
            request_url=_first_non_empty(item_payload.get("normalized_url"), logical_payload.get("normalized_url")),
            request_params={"record_id": item_payload.get("record_id"), "product_id": product_id},
            response_payload=item_payload,
            products=[product],
            media_assets=media_assets,
            execution=execution,
        )

    def ingest_api_response(
        self,
        *,
        source_platform: str,
        source_endpoint: str,
        request_url: str = "",
        request_params: Mapping[str, Any] | None = None,
        response_payload: Mapping[str, Any] | None = None,
        status_code: int = 0,
        products: Sequence[Mapping[str, Any]] | None = None,
        product_skus: Sequence[Mapping[str, Any]] | None = None,
        shops: Sequence[Mapping[str, Any]] | None = None,
        creators: Sequence[Mapping[str, Any]] | None = None,
        videos: Sequence[Mapping[str, Any]] | None = None,
        media_assets: Sequence[Mapping[str, Any]] | None = None,
        relations: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        raw_entity_links: Sequence[Mapping[str, Any]] | None = None,
        execution: Any | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        persisted = _empty_ingestion_payload()
        source_platform = _first_non_empty(source_platform)
        source_endpoint = _first_non_empty(source_endpoint)

        raw = self.fact_store.record_raw_api_response(
            source_platform=source_platform,
            source_endpoint=source_endpoint,
            request_url=_first_non_empty(request_url),
            request_params=dict(request_params or {}),
            response_payload=dict(response_payload or {}),
            status_code=int(status_code or 0),
            **_execution_ids(execution),
        )
        _append_dict(persisted["raw_api_responses"], raw)

        related_shop_rows: dict[str, dict[str, Any]] = {}
        product_rows = self._ingest_products(
            products or [],
            source_platform,
            source_endpoint,
            persisted,
            related_shop_rows=related_shop_rows,
        )
        self._ingest_product_skus(product_skus or [], persisted)
        shop_rows = self._ingest_shops(shops or [], source_platform, source_endpoint, persisted)
        shop_rows.update(related_shop_rows)
        creator_rows = self._ingest_creators(creators or [], source_platform, source_endpoint, persisted)
        video_rows = self._ingest_videos(videos or [], source_platform, source_endpoint, persisted)

        self._ingest_relations(
            relations or {},
            source_platform=source_platform,
            products=product_rows,
            shops=shop_rows,
            creators=creator_rows,
            videos=video_rows,
            persisted=persisted,
        )
        self._ingest_media_assets(media_assets or [], persisted)
        self._link_raw_entities(
            raw_response_id=str(raw.get("raw_response_id") or ""),
            entities=[
                *product_rows.values(),
                *shop_rows.values(),
                *creator_rows.values(),
                *video_rows.values(),
            ],
            explicit_links=raw_entity_links or [],
            persisted=persisted,
        )
        return persisted

    def _ingest_products(
        self,
        products: Sequence[Mapping[str, Any]],
        source_platform: str,
        source_endpoint: str,
        persisted: dict[str, list[dict[str, Any]]],
        related_shop_rows: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for product_spec in products:
            product_id = _first_non_empty(product_spec.get("product_id"), product_spec.get("id"))
            if not product_id:
                continue
            product = self.fact_store.upsert_product(
                product_id=product_id,
                product_url=_first_non_empty(product_spec.get("product_url"), product_spec.get("source_url")),
                normalized_url=_first_non_empty(product_spec.get("normalized_url")),
                title=_first_non_empty(product_spec.get("title"), product_spec.get("product_title")),
                holiday=_first_non_empty(product_spec.get("holiday")),
                seller_name=_first_non_empty(product_spec.get("seller_name"), product_spec.get("shop_name")),
                platform=_first_non_empty(product_spec.get("platform")) or "tiktok",
                country_region=_first_non_empty(product_spec.get("country_region")),
                source_platform=_first_non_empty(product_spec.get("source_platform")) or source_platform,
                status=_first_non_empty(product_spec.get("status")) or "active",
                facts=_facts_from_spec(product_spec, source_endpoint=source_endpoint),
            )
            if not product:
                continue
            rows[str(product["product_id"])] = product
            _append_dict(persisted["fact_entities"], product)
            shop = self._upsert_shop_from_product(product_spec, product_id, source_platform, source_endpoint)
            _append_dict(persisted["fact_entities"], shop)
            if shop:
                related_shop_rows[str(shop.get("shop_key") or "")] = shop
                relation = self.fact_store.upsert_product_shop_relation(
                    product_id=product_id,
                    shop_key=str(shop.get("shop_key") or ""),
                    shop_id=str(shop.get("shop_id") or ""),
                    shop_name=str(shop.get("shop_name") or ""),
                    source_platform=source_platform,
                    metadata={"source_endpoint": source_endpoint},
                )
                _append_dict(persisted["fact_relations"], relation)
        return rows

    def _ingest_product_skus(
        self,
        product_skus: Sequence[Mapping[str, Any]],
        persisted: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for sku_spec in product_skus:
            product_id = _first_non_empty(sku_spec.get("product_id"))
            sku = self.fact_store.upsert_product_sku(
                product_id=product_id,
                sku_id=_first_non_empty(sku_spec.get("sku_id")),
                sku_name=_first_non_empty(sku_spec.get("sku_name"), sku_spec.get("name")),
                spec_name=_first_non_empty(sku_spec.get("spec_name")),
                price_text=_first_non_empty(sku_spec.get("price_text")),
                stock_count=sku_spec.get("stock_count") or 0,
                facts=_facts_from_spec(sku_spec),
            )
            if sku:
                rows[str(sku.get("sku_key") or "")] = sku
                _append_dict(persisted["fact_entities"], sku)
        return rows

    def _ingest_shops(
        self,
        shops: Sequence[Mapping[str, Any]],
        source_platform: str,
        source_endpoint: str,
        persisted: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for shop_spec in shops:
            shop = self.fact_store.upsert_shop(
                shop_id=_first_non_empty(shop_spec.get("shop_id"), shop_spec.get("seller_id")),
                shop_name=_first_non_empty(shop_spec.get("shop_name"), shop_spec.get("seller_name")),
                shop_url=_first_non_empty(shop_spec.get("shop_url")),
                platform=_first_non_empty(shop_spec.get("platform")) or "tiktok",
                country_region=_first_non_empty(shop_spec.get("country_region")),
                source_platform=_first_non_empty(shop_spec.get("source_platform")) or source_platform,
                status=_first_non_empty(shop_spec.get("status")) or "active",
                facts=_facts_from_spec(shop_spec, source_endpoint=source_endpoint),
            )
            if shop:
                rows[str(shop.get("shop_key") or "")] = shop
                _append_dict(persisted["fact_entities"], shop)
        return rows

    def _ingest_creators(
        self,
        creators: Sequence[Mapping[str, Any]],
        source_platform: str,
        source_endpoint: str,
        persisted: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for creator_spec in creators:
            creator = self.fact_store.upsert_creator(
                creator_id=_first_non_empty(creator_spec.get("creator_id"), creator_spec.get("influencer_id")),
                uid=_first_non_empty(creator_spec.get("uid"), creator_spec.get("author_uid")),
                unique_id=_first_non_empty(creator_spec.get("unique_id"), creator_spec.get("influencer_id")),
                nickname=_first_non_empty(creator_spec.get("nickname"), creator_spec.get("author_name")),
                profile_url=_first_non_empty(creator_spec.get("profile_url")),
                platform=_first_non_empty(creator_spec.get("platform")) or "tiktok",
                country_region=_first_non_empty(creator_spec.get("country_region")),
                source_platform=_first_non_empty(creator_spec.get("source_platform")) or source_platform,
                status=_first_non_empty(creator_spec.get("status")) or "active",
                facts=_facts_from_spec(creator_spec, source_endpoint=source_endpoint),
            )
            if creator:
                rows[str(creator.get("creator_key") or "")] = creator
                _append_dict(persisted["fact_entities"], creator)
        return rows

    def _ingest_videos(
        self,
        videos: Sequence[Mapping[str, Any]],
        source_platform: str,
        source_endpoint: str,
        persisted: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for video_spec in videos:
            creator_key = _first_non_empty(video_spec.get("creator_key")) or self.fact_store.build_creator_key(
                creator_id=_first_non_empty(video_spec.get("creator_id")),
                uid=_first_non_empty(video_spec.get("uid")),
                unique_id=_first_non_empty(video_spec.get("unique_id")),
            )
            product_id = _first_non_empty(video_spec.get("product_id"))
            video = self.fact_store.upsert_video(
                video_id=_first_non_empty(video_spec.get("video_id"), video_spec.get("id")),
                creator_key=creator_key,
                product_id=product_id,
                title=_first_non_empty(video_spec.get("title"), video_spec.get("video_title")),
                video_url=_first_non_empty(video_spec.get("video_url")),
                cover_url=_first_non_empty(video_spec.get("cover_url")),
                platform=_first_non_empty(video_spec.get("platform")) or "tiktok",
                source_platform=_first_non_empty(video_spec.get("source_platform")) or source_platform,
                status=_first_non_empty(video_spec.get("status")) or "active",
                facts=_facts_from_spec(video_spec, source_endpoint=source_endpoint),
            )
            if not video:
                continue
            rows[str(video.get("video_key") or "")] = video
            _append_dict(persisted["fact_entities"], video)
            if creator_key:
                relation = self.fact_store.upsert_creator_video_relation(
                    creator_key=creator_key,
                    video_key=str(video.get("video_key") or ""),
                    source_platform=source_platform,
                    metadata={"source_endpoint": source_endpoint},
                )
                _append_dict(persisted["fact_relations"], relation)
            if product_id:
                relation = self.fact_store.upsert_video_product_relation(
                    video_key=str(video.get("video_key") or ""),
                    product_id=product_id,
                    source_platform=source_platform,
                    metadata={"source_endpoint": source_endpoint},
                )
                _append_dict(persisted["fact_relations"], relation)
        return rows

    def _upsert_shop_from_product(
        self,
        product_spec: Mapping[str, Any],
        product_id: str,
        source_platform: str,
        source_endpoint: str,
    ) -> dict[str, Any]:
        nested_shop = product_spec.get("shop") if isinstance(product_spec.get("shop"), Mapping) else {}
        shop_id = _first_non_empty(
            product_spec.get("shop_id"),
            product_spec.get("seller_id"),
            nested_shop.get("shop_id"),
            nested_shop.get("seller_id"),
        )
        shop_name = _first_non_empty(
            product_spec.get("shop_name"),
            product_spec.get("seller_name"),
            nested_shop.get("shop_name"),
            nested_shop.get("seller_name"),
        )
        if not (shop_id or shop_name):
            return {}
        return self.fact_store.upsert_shop(
            shop_id=shop_id,
            shop_name=shop_name,
            shop_url=_first_non_empty(product_spec.get("shop_url"), nested_shop.get("shop_url")),
            source_platform=source_platform,
            facts={"source_endpoint": source_endpoint, "product_id": product_id},
        )

    def _ingest_relations(
        self,
        relations: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        source_platform: str,
        products: Mapping[str, Mapping[str, Any]],
        shops: Mapping[str, Mapping[str, Any]],
        creators: Mapping[str, Mapping[str, Any]],
        videos: Mapping[str, Mapping[str, Any]],
        persisted: dict[str, list[dict[str, Any]]],
    ) -> None:
        del products, shops, creators, videos
        for relation_spec in relations.get("product_shops", []):
            shop_key = _first_non_empty(relation_spec.get("shop_key")) or self.fact_store.build_shop_key(
                shop_id=_first_non_empty(relation_spec.get("shop_id"), relation_spec.get("seller_id")),
                shop_name=_first_non_empty(relation_spec.get("shop_name"), relation_spec.get("seller_name")),
            )
            relation = self.fact_store.upsert_product_shop_relation(
                product_id=_first_non_empty(relation_spec.get("product_id")),
                shop_key=shop_key,
                shop_id=_first_non_empty(relation_spec.get("shop_id"), relation_spec.get("seller_id")),
                shop_name=_first_non_empty(relation_spec.get("shop_name"), relation_spec.get("seller_name")),
                relation_role=_first_non_empty(relation_spec.get("relation_role")) or "seller",
                source_platform=source_platform,
                metadata=_metadata_from_relation(relation_spec),
            )
            _append_dict(persisted["fact_relations"], relation)
        for relation_spec in relations.get("creator_products", []):
            creator_key = _first_non_empty(relation_spec.get("creator_key")) or self.fact_store.build_creator_key(
                creator_id=_first_non_empty(relation_spec.get("creator_id"), relation_spec.get("influencer_id")),
                uid=_first_non_empty(relation_spec.get("uid")),
                unique_id=_first_non_empty(relation_spec.get("unique_id"), relation_spec.get("influencer_id")),
            )
            relation = self.fact_store.upsert_creator_product_relation(
                creator_key=creator_key,
                creator_id=_first_non_empty(relation_spec.get("creator_id"), relation_spec.get("influencer_id")),
                product_id=_first_non_empty(relation_spec.get("product_id")),
                source_record_id=_first_non_empty(relation_spec.get("source_record_id")),
                target_record_id=_first_non_empty(relation_spec.get("target_record_id")),
                holiday_name=_first_non_empty(relation_spec.get("holiday_name")),
                sold_count=relation_spec.get("sold_count") or 0,
                source_platform=source_platform,
                metadata=_metadata_from_relation(relation_spec),
            )
            _append_dict(persisted["fact_relations"], relation)
        for relation_spec in relations.get("creator_videos", []):
            creator_key = _first_non_empty(relation_spec.get("creator_key")) or self.fact_store.build_creator_key(
                creator_id=_first_non_empty(relation_spec.get("creator_id")),
                uid=_first_non_empty(relation_spec.get("uid")),
                unique_id=_first_non_empty(relation_spec.get("unique_id")),
            )
            video_key = _first_non_empty(relation_spec.get("video_key")) or _video_key(relation_spec.get("video_id"))
            relation = self.fact_store.upsert_creator_video_relation(
                creator_key=creator_key,
                video_key=video_key,
                source_platform=source_platform,
                metadata=_metadata_from_relation(relation_spec),
            )
            _append_dict(persisted["fact_relations"], relation)
        for relation_spec in relations.get("video_products", []):
            video_key = _first_non_empty(relation_spec.get("video_key")) or _video_key(relation_spec.get("video_id"))
            relation = self.fact_store.upsert_video_product_relation(
                video_key=video_key,
                product_id=_first_non_empty(relation_spec.get("product_id")),
                source_platform=source_platform,
                metadata=_metadata_from_relation(relation_spec),
            )
            _append_dict(persisted["fact_relations"], relation)
        for relation_spec in relations.get("shop_creators", []):
            shop_key = _first_non_empty(relation_spec.get("shop_key")) or self.fact_store.build_shop_key(
                shop_id=_first_non_empty(relation_spec.get("shop_id")),
                shop_name=_first_non_empty(relation_spec.get("shop_name")),
            )
            creator_key = _first_non_empty(relation_spec.get("creator_key")) or self.fact_store.build_creator_key(
                creator_id=_first_non_empty(relation_spec.get("creator_id"), relation_spec.get("influencer_id")),
                uid=_first_non_empty(relation_spec.get("uid")),
                unique_id=_first_non_empty(relation_spec.get("unique_id"), relation_spec.get("influencer_id")),
            )
            relation = self.fact_store.upsert_shop_creator_relation(
                shop_key=shop_key,
                creator_key=creator_key,
                shop_name=_first_non_empty(relation_spec.get("shop_name")),
                creator_id=_first_non_empty(relation_spec.get("creator_id"), relation_spec.get("influencer_id")),
                source_platform=source_platform,
                metadata=_metadata_from_relation(relation_spec),
            )
            _append_dict(persisted["fact_relations"], relation)

    def _ingest_media_assets(
        self,
        media_assets: Sequence[Mapping[str, Any]],
        persisted: dict[str, list[dict[str, Any]]],
    ) -> None:
        for media_spec in media_assets:
            asset = self.fact_store.upsert_media_asset(
                source_url=_first_non_empty(media_spec.get("source_url")),
                file_token=_first_non_empty(media_spec.get("file_token")),
                local_path=_first_non_empty(media_spec.get("local_path"), media_spec.get("path")),
                object_key=_first_non_empty(media_spec.get("object_key")),
                file_name=_first_non_empty(media_spec.get("file_name")),
                mime_type=_first_non_empty(media_spec.get("mime_type"))
                or _infer_mime_type(media_spec.get("local_path") or media_spec.get("path")),
                source_platform=_first_non_empty(media_spec.get("source_platform")),
                metadata=_facts_from_spec(media_spec),
            )
            _append_dict(persisted["fact_media_assets"], asset)
            if asset:
                link = self.fact_store.link_media_asset(
                    entity_type=_first_non_empty(media_spec.get("entity_type")),
                    entity_external_id=_first_non_empty(media_spec.get("entity_external_id")),
                    asset_id=str(asset.get("asset_id") or ""),
                    media_role=_first_non_empty(media_spec.get("media_role")),
                    metadata=_metadata_from_relation(media_spec),
                )
                _append_dict(persisted["fact_media_assets"], link)

    def _link_raw_entities(
        self,
        *,
        raw_response_id: str,
        entities: Sequence[Mapping[str, Any]],
        explicit_links: Sequence[Mapping[str, Any]],
        persisted: dict[str, list[dict[str, Any]]],
    ) -> None:
        if not raw_response_id:
            return
        for entity in entities:
            entity_type, entity_external_id = _entity_identity(entity)
            if not entity_type or not entity_external_id:
                continue
            link = self.fact_store.link_raw_entity(
                raw_response_id=raw_response_id,
                entity_type=entity_type,
                entity_external_id=entity_external_id,
                link_role=f"{entity_type}_entity",
            )
            _append_dict(persisted["raw_api_responses"], link)
        for link_spec in explicit_links:
            link = self.fact_store.link_raw_entity(
                raw_response_id=raw_response_id,
                entity_type=_first_non_empty(link_spec.get("entity_type")),
                entity_external_id=_first_non_empty(link_spec.get("entity_external_id")),
                link_role=_first_non_empty(link_spec.get("link_role")),
                metadata=_metadata_from_relation(link_spec),
            )
            _append_dict(persisted["raw_api_responses"], link)


def _empty_ingestion_payload() -> dict[str, list[dict[str, Any]]]:
    return {
        "fact_entities": [],
        "fact_relations": [],
        "fact_media_assets": [],
        "raw_api_responses": [],
    }


def _append_dict(target: list[dict[str, Any]], value: Mapping[str, Any] | None) -> None:
    if isinstance(value, Mapping) and value:
        target.append(dict(value))


def _execution_ids(execution: Any | None) -> dict[str, str]:
    if execution is None:
        return {"request_id": "", "execution_id": "", "run_id": ""}
    return {
        "request_id": _first_non_empty(getattr(execution, "request_id", "")),
        "execution_id": _first_non_empty(getattr(execution, "execution_id", "")),
        "run_id": _first_non_empty(getattr(execution, "run_id", "")),
    }


def _facts_from_spec(spec: Mapping[str, Any], *, source_endpoint: str = "") -> dict[str, Any]:
    facts = spec.get("facts")
    if isinstance(facts, Mapping):
        payload = dict(facts)
    else:
        payload = {
            key: value
            for key, value in spec.items()
            if key
            not in {
                "metadata",
                "shop",
                "entity_type",
                "entity_external_id",
                "media_role",
                "source_url",
                "file_token",
                "local_path",
                "path",
                "object_key",
                "file_name",
                "mime_type",
            }
        }
    if source_endpoint:
        payload.setdefault("source_endpoint", source_endpoint)
    return payload


def _metadata_from_relation(spec: Mapping[str, Any]) -> dict[str, Any]:
    metadata = spec.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _entity_identity(entity: Mapping[str, Any]) -> tuple[str, str]:
    if entity.get("shop_key"):
        return "shop", str(entity["shop_key"])
    if entity.get("creator_key"):
        return "creator", str(entity["creator_key"])
    if entity.get("video_key"):
        return "video", str(entity["video_key"])
    if entity.get("product_id") and entity.get("id"):
        return "product", str(entity["product_id"])
    return "", ""


def _video_key(video_id: Any) -> str:
    value = _first_non_empty(video_id)
    return f"video:{value}" if value else ""


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _infer_mime_type(path_value: Any) -> str:
    path_text = _first_non_empty(path_value)
    if not path_text:
        return ""
    suffix = Path(path_text).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return ""
