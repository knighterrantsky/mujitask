from __future__ import annotations

import time
from typing import Any

from automation_business_scaffold.extend_script.feishu_api import parse_table_url
from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore


def build_product_canonical_key(item: dict[str, Any]) -> str:
    product_id = str(item.get("product_id", "") or "").strip()
    if product_id:
        return f"tiktok_product:{product_id}"
    normalized_url = str(item.get("normalized_url", "") or "").strip()
    if normalized_url:
        return f"normalized_pdp_url:{normalized_url}"
    return ""


def build_product_snapshot_facts(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(item.get("record_id", "") or "").strip(),
        "source_url": str(item.get("source_url", "") or "").strip(),
        "normalized_url": str(item.get("normalized_url", "") or "").strip(),
        "product_id": str(item.get("product_id", "") or "").strip(),
        "status": str(item.get("status", "") or "").strip(),
        "fields": dict(item.get("fields") or {}) if isinstance(item.get("fields"), dict) else {},
        "logical_fields": (
            dict(item.get("logical_fields") or {})
            if isinstance(item.get("logical_fields"), dict)
            else {}
        ),
        "fastmoss_snapshot": (
            dict(item.get("fastmoss_snapshot") or {})
            if isinstance(item.get("fastmoss_snapshot"), dict)
            else {}
        ),
    }


def build_snapshot_date(item: dict[str, Any], *, collected_at: float) -> str:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    record_date = fields.get("记录日期") if isinstance(fields, dict) else None
    if record_date not in (None, ""):
        try:
            timestamp = float(record_date)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return time.strftime("%Y-%m-%d", time.localtime(timestamp))
        except (TypeError, ValueError):
            pass
    return time.strftime("%Y-%m-%d", time.localtime(collected_at))


def build_snapshot_diff(
    baseline_facts: dict[str, Any],
    current_facts: dict[str, Any],
) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key in sorted(set(baseline_facts) | set(current_facts)):
        before = baseline_facts.get(key)
        after = current_facts.get(key)
        if before != after:
            diff[key] = {"before": before, "after": after}
    return diff


def build_binding_target_space(table_url: str) -> str:
    try:
        table_meta = parse_table_url(table_url)
    except Exception:
        return ""
    app_token = str(table_meta.get("app_token", "") or "").strip()
    table_id = str(table_meta.get("table_id", "") or "").strip()
    if app_token and table_id:
        return f"{app_token}.{table_id}"
    return app_token or table_id


def extract_entity_payloads(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    seen_entity_ids: set[str] = set()
    seen_binding_ids: set[str] = set()
    seen_snapshot_ids: set[str] = set()
    for item in items:
        entity_payload = item.get("entity")
        if isinstance(entity_payload, dict):
            entity_id = str(entity_payload.get("entity_id", "") or "").strip()
            if entity_id and entity_id not in seen_entity_ids:
                seen_entity_ids.add(entity_id)
                entities.append(entity_payload)
        binding_payload = item.get("binding")
        if isinstance(binding_payload, dict):
            binding_id = str(binding_payload.get("binding_id", "") or "").strip()
            if binding_id and binding_id not in seen_binding_ids:
                seen_binding_ids.add(binding_id)
                bindings.append(binding_payload)
        snapshot_payload = item.get("entity_snapshot")
        if isinstance(snapshot_payload, dict):
            snapshot_id = str(snapshot_payload.get("snapshot_id", "") or "").strip()
            if snapshot_id and snapshot_id not in seen_snapshot_ids:
                seen_snapshot_ids.add(snapshot_id)
                snapshots.append(snapshot_payload)
    return entities, bindings, snapshots


def persist_product_entity_snapshot(
    *,
    store: Phase1RuntimeStore,
    execution: Any,
    result_payload: dict[str, Any],
    extract_result_item: Any,
) -> dict[str, Any]:
    item = extract_result_item(result_payload)
    if not item:
        return {}
    canonical_key = build_product_canonical_key(item)
    if not canonical_key:
        return {}
    logical_fields = item.get("logical_fields") if isinstance(item.get("logical_fields"), dict) else {}
    if not logical_fields:
        return {}

    entity = store.get_or_create_entity(
        entity_type="product",
        canonical_key=canonical_key,
    )

    binding = None
    record_id = str(item.get("record_id", "") or "").strip()
    table_url = str(execution.payload.get("table_url", "") or "").strip()
    target_space = build_binding_target_space(table_url)
    if record_id and target_space:
        binding = store.upsert_external_binding(
            entity_id=entity.entity_id,
            target_type="feishu_record",
            target_space=target_space,
            target_id=record_id,
            source_key=str(item.get("normalized_url", "") or item.get("product_id", "") or "").strip(),
            metadata={
                "table_url": table_url,
                "item_code": execution.item_code,
            },
        )

    collected_at = time.time()
    facts = build_product_snapshot_facts(item)
    baseline_snapshot = store.load_latest_entity_snapshot(entity_id=entity.entity_id)
    baseline_snapshot_id = ""
    diff: dict[str, Any] = {}
    if baseline_snapshot is not None:
        baseline_snapshot_id = baseline_snapshot.snapshot_id
        diff = build_snapshot_diff(baseline_snapshot.facts, facts)
    snapshot = store.create_entity_snapshot(
        entity_id=entity.entity_id,
        snapshot_date=build_snapshot_date(item, collected_at=collected_at),
        collected_at=collected_at,
        facts=facts,
        baseline_snapshot_id=baseline_snapshot_id,
        diff=diff,
        request_id=execution.request_id,
        execution_id=execution.execution_id,
        run_id=str(execution.run_id or f"managed-{execution.execution_id}"),
    )

    entity_payload = entity.to_dict()
    entity_payload["latest_snapshot_id"] = snapshot.snapshot_id
    persisted = {
        "entity": entity_payload,
        "binding": binding.to_dict() if binding is not None else {},
        "entity_snapshot": snapshot.to_dict(),
    }
    item.update(persisted)
    if isinstance(result_payload.get("item"), dict):
        result_payload["item"] = item
    items = result_payload.get("items")
    if isinstance(items, list):
        enriched_items: list[dict[str, Any]] = []
        record_id = str(item.get("record_id", "") or "").strip()
        for raw_item in items:
            if not isinstance(raw_item, dict):
                enriched_items.append(raw_item)
                continue
            if str(raw_item.get("record_id", "") or "").strip() == record_id:
                merged = dict(raw_item)
                merged.update(persisted)
                enriched_items.append(merged)
            else:
                enriched_items.append(raw_item)
        result_payload["items"] = enriched_items
    return persisted
