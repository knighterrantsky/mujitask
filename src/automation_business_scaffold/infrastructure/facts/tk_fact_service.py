from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService


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
    del product_id
    persisted = TKFactIngestionService(runtime_store=store).ingest_tiktok_product_request(
        logical_fields=logical_fields,
        source_item=item,
        fastmoss_snapshot=fastmoss_snapshot,
        execution=execution,
        source_endpoint="single_row_update.result",
    )
    _merge_persisted_payload(result_payload=result_payload, item=item, persisted=persisted)
    return persisted


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


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
