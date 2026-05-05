from __future__ import annotations

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_creator_key,
    build_error,
    build_shop_key,
    bundle_entity_keys,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore
from typing import Any

HANDLER_CODE = "fact_bundle_upsert"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def fact_bundle_upsert_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    request_payload = coerce_mapping(payload.get("request_payload"))
    merged_bundle = merge_fact_bundles(coerce_mapping(payload.get("fact_bundle")))

    entity_keys = bundle_entity_keys(merged_bundle)
    if not entity_keys:
        return skipped_result(
            context,
            summary={"entity_count": 0, "persistence_mode": "skipped"},
            result={"upserted_entities": [], "upserted_relations": [], "observation_refs": []},
            warnings=("Fact bundle was empty; nothing to persist.",),
        )

    persistence_mode = "dry_run"
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
        request_payload.get("db_url"),
        get_execution_control_defaults().fact_db_url,
    )
    if fact_db_url:
        persistence_mode = "database"
    elif _requires_database_persistence(payload, request_payload):
        return failed_result(
            context,
            error=build_error(
                error_type="persistence_configuration_missing",
                error_code="fact_database_persistence_required",
                message="fact_bundle_upsert requires Fact DB persistence, but no fact_db_url was provided.",
                retryable=False,
                details={"required": True, "configured": False},
            ),
            summary={"entity_count": len(entity_keys), "persistence_mode": "missing_database"},
        )

    try:
        if persistence_mode == "database":
            persisted = _persist_fact_bundle(merged_bundle, fact_db_url=fact_db_url)
        else:
            persisted = _plan_fact_bundle_upsert(merged_bundle)
    except Exception as exc:  # pragma: no cover - defensive boundary for worker loop
        error = build_error(
            error_type="persistence_failure",
            error_code="fact_bundle_upsert_failed",
            message=str(exc),
            retryable=True,
            details={"persistence_mode": persistence_mode},
        )
        return failed_result(
            context,
            error=error,
            summary={"entity_count": len(entity_keys), "persistence_mode": persistence_mode},
        )

    result = {
        "upserted_entities": persisted["upserted_entities"],
        "upserted_relations": persisted["upserted_relations"],
        "observation_refs": persisted["observation_refs"],
        "persisted_counts": persisted["persisted_counts"],
        "fact_bundle": merged_bundle,
        "persistence_mode": persistence_mode,
    }
    summary = {
        "entity_count": len(persisted["upserted_entities"]),
        "relation_count": len(persisted["upserted_relations"]),
        "observation_count": len(persisted["observation_refs"]),
        "persistence_mode": persistence_mode,
    }
    return success_result(context, summary=summary, result=result)


def _requires_database_persistence(payload: dict[str, Any], request_payload: dict[str, Any]) -> bool:
    persistence = coerce_mapping(payload.get("persistence"))
    request_persistence = coerce_mapping(request_payload.get("persistence"))
    for source in (payload, request_payload, persistence, request_persistence):
        for key in ("require_database_persistence", "requires_fact_db", "strict_persistence"):
            if key in source and source.get(key) not in (None, ""):
                return _coerce_bool(source.get(key))
    return False


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _plan_fact_bundle_upsert(fact_bundle: dict[str, Any]) -> dict[str, Any]:
    upserted_entities = bundle_entity_keys(fact_bundle)
    upserted_relations: list[str] = []
    for relation_key, items in coerce_mapping(fact_bundle.get("relations")).items():
        for item in coerce_mapping_list(items):
            upserted_relations.append(f"{relation_key}:{first_non_empty(item.get('relation_key'), item.get('product_id'), item.get('video_key'), item.get('shop_key'), item.get('creator_key'))}")
    observation_refs = []
    observation_refs.extend(
        f"product_metric:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_metric_snapshots")), start=1)
    )
    observation_refs.extend(
        f"product_daily:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_daily_metrics")), start=1)
    )
    observation_refs.extend(
        f"distribution:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_distribution_snapshots")), start=1)
    )
    observation_refs.extend(
        f"sku_metric:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_sku_metric_snapshots")), start=1)
    )
    return {
        "upserted_entities": upserted_entities,
        "upserted_relations": upserted_relations,
        "observation_refs": observation_refs,
        "persisted_counts": {
            "products": len(coerce_mapping_list(fact_bundle.get("products"))),
            "product_skus": len(coerce_mapping_list(fact_bundle.get("product_skus"))),
            "shops": len(coerce_mapping_list(fact_bundle.get("shops"))),
            "creators": len(coerce_mapping_list(fact_bundle.get("creators"))),
            "videos": len(coerce_mapping_list(fact_bundle.get("videos"))),
            "media_assets": len(coerce_mapping_list(fact_bundle.get("media_assets"))),
            "relations": len(upserted_relations),
            "observations": len(observation_refs),
        },
    }


def _persist_fact_bundle(fact_bundle: dict[str, Any], *, fact_db_url: str) -> dict[str, Any]:
    store = TKFactStore(db_url=fact_db_url)
    upserted_entities: list[str] = []
    upserted_relations: list[str] = []
    observation_refs: list[str] = []
    asset_id_by_key: dict[str, str] = {}

    for product in coerce_mapping_list(fact_bundle.get("products")):
        row = store.upsert_product(
            product_id=coerce_str(product.get("product_id")),
            product_url=coerce_str(product.get("product_url")),
            normalized_url=coerce_str(product.get("normalized_url")),
            title=coerce_str(product.get("title")),
            holiday=coerce_str(product.get("holiday")),
            seller_name=coerce_str(first_non_empty(product.get("seller_name"), product.get("shop_name"))),
            source_platform=coerce_str(product.get("source_platform")),
            status=coerce_str(first_non_empty(product.get("status"), _product_status_from_facts(product.get("facts")), "active")),
            facts=coerce_mapping(product.get("facts")),
        )
        if row:
            upserted_entities.append(f"product:{row.get('product_id')}")

    for sku in coerce_mapping_list(fact_bundle.get("product_skus")):
        row = store.upsert_product_sku(
            product_id=coerce_str(sku.get("product_id")),
            sku_id=coerce_str(sku.get("sku_id")),
            sku_name=coerce_str(sku.get("sku_name")),
            spec_name=coerce_str(sku.get("spec_name")),
            price_text=coerce_str(sku.get("price_text")),
            stock_count=sku.get("stock_count"),
            facts=coerce_mapping(sku.get("facts")),
        )
        if row:
            upserted_entities.append(f"product_sku:{row.get('sku_key')}")

    for shop in coerce_mapping_list(fact_bundle.get("shops")):
        row = store.upsert_shop(
            shop_id=coerce_str(shop.get("shop_id")),
            shop_name=coerce_str(shop.get("shop_name")),
            shop_url=coerce_str(shop.get("shop_url")),
            source_platform=coerce_str(shop.get("source_platform")),
            facts=coerce_mapping(shop.get("facts")),
        )
        if row:
            upserted_entities.append(f"shop:{row.get('shop_key')}")

    for creator in coerce_mapping_list(fact_bundle.get("creators")):
        row = store.upsert_creator(
            creator_id=coerce_str(creator.get("creator_id")),
            uid=coerce_str(creator.get("uid")),
            unique_id=coerce_str(creator.get("unique_id")),
            nickname=coerce_str(creator.get("nickname")),
            profile_url=coerce_str(creator.get("profile_url")),
            source_platform=coerce_str(creator.get("source_platform")),
            facts=coerce_mapping(creator.get("facts")),
        )
        if row:
            upserted_entities.append(f"creator:{row.get('creator_key')}")

    for video in coerce_mapping_list(fact_bundle.get("videos")):
        row = store.upsert_video(
            video_id=coerce_str(video.get("video_id")),
            creator_key=coerce_str(video.get("creator_key")),
            product_id=coerce_str(video.get("product_id")),
            title=coerce_str(video.get("title")),
            video_url=coerce_str(video.get("video_url")),
            cover_url=coerce_str(video.get("cover_url")),
            source_platform=coerce_str(video.get("source_platform")),
            facts=coerce_mapping(video.get("facts")),
        )
        if row:
            upserted_entities.append(f"video:{row.get('video_key')}")

    for asset in coerce_mapping_list(fact_bundle.get("media_assets")):
        row = store.upsert_media_asset(
            source_url=coerce_str(asset.get("source_url")),
            file_token=coerce_str(asset.get("file_token")),
            local_path=coerce_str(first_non_empty(asset.get("source_path"), asset.get("local_path"))),
            object_key=coerce_str(asset.get("object_key")),
            file_name=coerce_str(asset.get("file_name")),
            mime_type=coerce_str(asset.get("mime_type")),
            source_platform=coerce_str(asset.get("source_platform")),
            metadata=coerce_mapping(asset.get("metadata")),
        )
        if row:
            asset_key = first_non_empty(row.get("asset_key"))
            asset_id_by_key[asset_key] = coerce_str(row.get("asset_id"))
            upserted_entities.append(f"asset:{asset_key}")
            entity_type = coerce_str(asset.get("entity_type"))
            entity_external_id = coerce_str(asset.get("entity_external_id"))
            media_role = coerce_str(asset.get("media_role"))
            if entity_type and entity_external_id and media_role:
                linked = store.link_media_asset(
                    entity_type=entity_type,
                    entity_external_id=entity_external_id,
                    asset_id=coerce_str(row.get("asset_id")),
                    media_role=media_role,
                    metadata=coerce_mapping(asset.get("metadata")),
                )
                if linked:
                    upserted_relations.append(f"entity_media_asset:{linked.get('relation_key')}")

    relations = coerce_mapping(fact_bundle.get("relations"))
    for relation in coerce_mapping_list(relations.get("product_shops")):
        row = store.upsert_product_shop_relation(
            product_id=coerce_str(relation.get("product_id")),
            shop_key=first_non_empty(
                relation.get("shop_key"),
                build_shop_key(shop_id=coerce_str(relation.get("shop_id")), shop_name=coerce_str(relation.get("shop_name"))),
            ),
            shop_id=coerce_str(relation.get("shop_id")),
            shop_name=coerce_str(relation.get("shop_name")),
            relation_role=coerce_str(first_non_empty(relation.get("relation_role"), "seller")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"product_shop:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("creator_products")):
        row = store.upsert_creator_product_relation(
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            product_id=coerce_str(relation.get("product_id")),
            creator_id=coerce_str(relation.get("creator_id")),
            source_record_id=coerce_str(relation.get("source_record_id")),
            target_record_id=coerce_str(relation.get("target_record_id")),
            holiday_name=coerce_str(relation.get("holiday_name")),
            sold_count=relation.get("sold_count"),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"creator_product:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("creator_videos")):
        row = store.upsert_creator_video_relation(
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            video_key=first_non_empty(relation.get("video_key"), f"video:{coerce_str(relation.get('video_id'))}"),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"creator_video:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("video_products")):
        row = store.upsert_video_product_relation(
            video_key=first_non_empty(relation.get("video_key"), f"video:{coerce_str(relation.get('video_id'))}"),
            product_id=coerce_str(relation.get("product_id")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"video_product:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("shop_creators")):
        row = store.upsert_shop_creator_relation(
            shop_key=first_non_empty(
                relation.get("shop_key"),
                build_shop_key(shop_id=coerce_str(relation.get("shop_id")), shop_name=coerce_str(relation.get("shop_name"))),
            ),
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            shop_name=coerce_str(relation.get("shop_name")),
            creator_id=coerce_str(relation.get("creator_id")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"shop_creator:{row.get('relation_key')}")

    raw_id_by_key: dict[str, str] = {}
    for raw_response in coerce_mapping_list(fact_bundle.get("raw_api_responses")):
        row = store.record_raw_api_response(
            source_platform=coerce_str(raw_response.get("source_platform")),
            source_endpoint=coerce_str(raw_response.get("source_endpoint")),
            request_url=coerce_str(raw_response.get("request_url")),
            request_params=coerce_mapping(raw_response.get("request_params")),
            response_payload=coerce_mapping(raw_response.get("response_payload")),
            status_code=int(raw_response.get("status_code", 0) or 0),
        )
        if row:
            raw_key = f"{row.get('source_platform')}:{row.get('source_endpoint')}:{row.get('request_url')}"
            raw_id_by_key[raw_key] = coerce_str(row.get("raw_response_id"))

    for observation in coerce_mapping_list(fact_bundle.get("product_metric_snapshots")):
        latest = store.upsert_product_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            source_platform=coerce_str(observation.get("source_platform")),
            source_endpoint=coerce_str(observation.get("source_endpoint")),
            window_days=int(observation.get("window_days", 0) or 0),
            window_start=coerce_str(observation.get("window_start")),
            window_end=coerce_str(observation.get("window_end")),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            source_platform=coerce_str(observation.get("source_platform")),
            source_endpoint=coerce_str(observation.get("source_endpoint")),
            window_days=int(observation.get("window_days", 0) or 0),
            window_start=coerce_str(observation.get("window_start")),
            window_end=coerce_str(observation.get("window_end")),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"product_window_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"product_window_observation:{observed.get('observation_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_daily_metrics")):
        row = store.upsert_product_daily_metric(
            product_id=coerce_str(observation.get("product_id")),
            metric_date=coerce_str(observation.get("metric_date")),
            source_platform=coerce_str(observation.get("source_platform")),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            price_amount=observation.get("price_amount"),
            currency=coerce_str(observation.get("currency")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if row:
            observation_refs.append(f"product_daily:{row.get('metric_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_distribution_snapshots")):
        latest = store.upsert_product_distribution_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            distribution_type=coerce_str(observation.get("distribution_type")),
            source_key=coerce_str(observation.get("source_key")),
            source_name=coerce_str(observation.get("source_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            metric_value=observation.get("metric_value"),
            metric_amount=observation.get("metric_amount"),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_distribution_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            distribution_type=coerce_str(observation.get("distribution_type")),
            source_key=coerce_str(observation.get("source_key")),
            source_name=coerce_str(observation.get("source_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            metric_value=observation.get("metric_value"),
            metric_amount=observation.get("metric_amount"),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"distribution_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"distribution_observation:{observed.get('observation_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_sku_metric_snapshots")):
        latest = store.upsert_product_sku_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            sku_key=coerce_str(observation.get("sku_key")),
            sku_id=coerce_str(observation.get("sku_id")),
            sku_name=coerce_str(observation.get("sku_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            stock_count=observation.get("stock_count"),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_sku_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            sku_key=coerce_str(observation.get("sku_key")),
            sku_id=coerce_str(observation.get("sku_id")),
            sku_name=coerce_str(observation.get("sku_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            stock_count=observation.get("stock_count"),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"sku_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"sku_observation:{observed.get('observation_id')}")

    persisted_counts = {
        "products": sum(1 for key in upserted_entities if key.startswith("product:")),
        "product_skus": sum(1 for key in upserted_entities if key.startswith("product_sku:")),
        "shops": sum(1 for key in upserted_entities if key.startswith("shop:")),
        "creators": sum(1 for key in upserted_entities if key.startswith("creator:")),
        "videos": sum(1 for key in upserted_entities if key.startswith("video:")),
        "media_assets": sum(1 for key in upserted_entities if key.startswith("asset:")),
        "relations": len(upserted_relations),
        "observations": len(observation_refs),
    }
    return {
        "upserted_entities": upserted_entities,
        "upserted_relations": upserted_relations,
        "observation_refs": observation_refs,
        "persisted_counts": persisted_counts,
    }


def _product_status_from_facts(facts: Any) -> str:
    fact_payload = coerce_mapping(facts)
    availability_status = coerce_str(fact_payload.get("availability_status")).strip().lower()
    if availability_status == "unavailable":
        return "off_shelf_or_region_unavailable"
    return ""


__all__ = ["CONTRACT", "HANDLER_CODE", "fact_bundle_upsert_handler"]
