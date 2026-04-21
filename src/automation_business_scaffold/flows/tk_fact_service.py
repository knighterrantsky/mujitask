from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.flows.tk_fact_store import TKFactStore


def persist_product_fact_bundle(
    *,
    store: Any,
    execution: Any,
    result_payload: dict[str, Any],
    extract_result_item: Any,
) -> dict[str, Any]:
    item = extract_result_item(result_payload)
    if not item:
        return {}
    logical_fields = item.get("logical_fields") if isinstance(item.get("logical_fields"), Mapping) else {}
    if not logical_fields:
        return {}

    product_id = _first_non_empty(item.get("product_id"), logical_fields.get("product_id"))
    if not product_id:
        return {}

    fastmoss_snapshot = (
        dict(item.get("fastmoss_snapshot") or {})
        if isinstance(item.get("fastmoss_snapshot"), Mapping)
        else {}
    )
    fact_store = TKFactStore(runtime_store=store)
    fact_entities: list[dict[str, Any]] = []
    fact_relations: list[dict[str, Any]] = []
    fact_media_assets: list[dict[str, Any]] = []
    raw_api_responses: list[dict[str, Any]] = []

    product = fact_store.upsert_product(
        product_id=product_id,
        product_url=_first_non_empty(item.get("source_url"), logical_fields.get("source_url")),
        normalized_url=_first_non_empty(item.get("normalized_url"), logical_fields.get("normalized_url")),
        title=_first_non_empty(logical_fields.get("title"), fastmoss_snapshot.get("product_title")),
        holiday=_first_non_empty(logical_fields.get("holiday")),
        seller_name=_first_non_empty(logical_fields.get("shop_name")),
        source_platform="tiktok",
        facts={
            "fields": dict(item.get("fields") or {}) if isinstance(item.get("fields"), Mapping) else {},
            "logical_fields": dict(logical_fields),
            "fastmoss_snapshot": dict(fastmoss_snapshot),
        },
    )
    _append_dict(fact_entities, product)

    shop = fact_store.upsert_shop(
        shop_name=_first_non_empty(logical_fields.get("shop_name")),
        shop_url=_first_non_empty(logical_fields.get("shop_url")),
        source_platform="tiktok",
        facts={"source": "single_row_update", "product_id": product_id},
    )
    _append_dict(fact_entities, shop)
    if shop:
        relation = fact_store.upsert_product_shop_relation(
            product_id=product_id,
            shop_key=str(shop.get("shop_key") or ""),
            shop_id=str(shop.get("shop_id") or ""),
            shop_name=str(shop.get("shop_name") or ""),
            source_platform="tiktok",
            metadata={"source": "single_row_update"},
        )
        _append_dict(fact_relations, relation)

    _persist_product_media_assets(
        fact_store=fact_store,
        product_id=product_id,
        logical_fields=logical_fields,
        fastmoss_snapshot=fastmoss_snapshot,
        fact_media_assets=fact_media_assets,
    )

    raw = fact_store.record_raw_api_response(
        source_platform="tiktok",
        source_endpoint="single_row_update.result",
        request_url=_first_non_empty(item.get("normalized_url"), logical_fields.get("normalized_url")),
        request_params={"record_id": item.get("record_id"), "product_id": product_id},
        response_payload=item,
        request_id=str(getattr(execution, "request_id", "") or ""),
        execution_id=str(getattr(execution, "execution_id", "") or ""),
        run_id=str(getattr(execution, "run_id", "") or ""),
    )
    _append_dict(raw_api_responses, raw)
    if raw:
        product_link = fact_store.link_raw_entity(
            raw_response_id=str(raw.get("raw_response_id") or ""),
            entity_type="product",
            entity_external_id=product_id,
            link_role="primary_product",
        )
        _append_dict(raw_api_responses, product_link)
        if shop:
            shop_link = fact_store.link_raw_entity(
                raw_response_id=str(raw.get("raw_response_id") or ""),
                entity_type="shop",
                entity_external_id=str(shop.get("shop_key") or ""),
                link_role="seller_shop",
            )
            _append_dict(raw_api_responses, shop_link)

    persisted = {
        "fact_entities": fact_entities,
        "fact_relations": fact_relations,
        "fact_media_assets": fact_media_assets,
        "raw_api_responses": raw_api_responses,
    }
    _merge_persisted_payload(result_payload=result_payload, item=item, persisted=persisted)
    return persisted


def _persist_product_media_assets(
    *,
    fact_store: TKFactStore,
    product_id: str,
    logical_fields: Mapping[str, Any],
    fastmoss_snapshot: Mapping[str, Any],
    fact_media_assets: list[dict[str, Any]],
) -> None:
    media_specs = [
        {
            "media_role": "product_main_image",
            "source_url": logical_fields.get("main_image_url"),
            "local_path": logical_fields.get("main_image_local_path"),
            "file_name": logical_fields.get("main_image_file_name"),
            "mime_type": logical_fields.get("main_image_mime_type"),
            "source_platform": "tiktok",
        },
        {
            "media_role": "product_page_screenshot",
            "local_path": logical_fields.get("product_page_screenshot_local_path"),
            "file_name": logical_fields.get("product_page_screenshot_file_name"),
            "mime_type": logical_fields.get("product_page_screenshot_mime_type"),
            "source_platform": "tiktok",
        },
        {
            "media_role": "fastmoss_detail_screenshot",
            "local_path": fastmoss_snapshot.get("detail_page_screenshot_local_path"),
            "file_name": fastmoss_snapshot.get("detail_page_screenshot_file_name"),
            "mime_type": fastmoss_snapshot.get("detail_page_screenshot_mime_type"),
            "source_platform": "fastmoss",
        },
    ]
    for spec in media_specs:
        asset = fact_store.upsert_media_asset(
            source_url=_first_non_empty(spec.get("source_url")),
            local_path=_first_non_empty(spec.get("local_path")),
            file_name=_first_non_empty(spec.get("file_name")),
            mime_type=_first_non_empty(spec.get("mime_type")) or _infer_mime_type(spec.get("local_path")),
            source_platform=_first_non_empty(spec.get("source_platform")),
            metadata={"media_role": spec["media_role"]},
        )
        _append_dict(fact_media_assets, asset)
        if asset:
            link = fact_store.link_media_asset(
                entity_type="product",
                entity_external_id=product_id,
                asset_id=str(asset.get("asset_id") or ""),
                media_role=str(spec["media_role"]),
                metadata={"source_platform": spec.get("source_platform")},
            )
            _append_dict(fact_media_assets, link)


def _merge_persisted_payload(
    *,
    result_payload: dict[str, Any],
    item: dict[str, Any],
    persisted: Mapping[str, Any],
) -> None:
    item.update({key: value for key, value in persisted.items() if value})
    if isinstance(result_payload.get("item"), dict):
        result_payload["item"] = item
    items = result_payload.get("items")
    if not isinstance(items, list):
        return
    record_id = _first_non_empty(item.get("record_id"))
    enriched_items: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            enriched_items.append(raw_item)
            continue
        if record_id and _first_non_empty(raw_item.get("record_id")) == record_id:
            merged = dict(raw_item)
            merged.update({key: value for key, value in persisted.items() if value})
            enriched_items.append(merged)
        else:
            enriched_items.append(raw_item)
    result_payload["items"] = enriched_items


def _append_dict(target: list[dict[str, Any]], value: Mapping[str, Any] | None) -> None:
    if isinstance(value, Mapping) and value:
        target.append(dict(value))


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
