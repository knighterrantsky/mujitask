from __future__ import annotations
import re
from collections.abc import Mapping
from typing import Any


_PRODUCT_ID_PATTERNS = (
    re.compile(r"/(?:pdp|product|detail)/(\d+)", re.IGNORECASE),
    re.compile(r"[?&](?:product_id|goods_id)=(\d+)", re.IGNORECASE),
    re.compile(r"\b(\d{8,})\b"),
)


_COMPETITOR_AUTO_FIELDS = (
    "产品链接",
    "SKU-ID",
    "图片",
    "标题",
    "节日",
    "卖家",
    "价格",
    "Fastmoss价格",
    "昨日销量",
    "近7天销量",
    "近90天销量",
    "记录日期",
)


def _adapt_competitor_rows(raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    spec = _mapping(payload.get("filter_spec"))
    skip_statuses = set(_list_text(spec.get("skip_product_status")))
    candidate_policy = _text(spec.get("candidate_policy"))
    auto_fields = tuple(_list_text(spec.get("auto_fields")) or _COMPETITOR_AUTO_FIELDS)
    snapshot_enabled = bool(_mapping(payload.get("snapshot_policy")).get("store_raw_rows"))
    source_rows: list[dict[str, Any]] = []
    skipped_complete = 0
    skipped_unavailable = 0
    dropped_empty = 0

    for row in raw_rows:
        fields = _mapping(row.get("fields"))
        identity = _product_identity_from_fields(fields)
        if not identity:
            dropped_empty += 1
            continue
        product_status = _field_text(fields, "商品状态", "product_status")
        if product_status and product_status in skip_statuses:
            skipped_unavailable += 1
            continue
        missing_auto_fields = [field for field in auto_fields if not _field_has_value(fields.get(field))]
        if candidate_policy == "missing_auto_maintained_fields" and not missing_auto_fields:
            skipped_complete += 1
            continue
        source_rows.append(
            _source_row(
                row,
                payload,
                identity=identity,
                business_fields={"product_status": product_status},
                extra={"missing_auto_fields": missing_auto_fields},
                snapshot_enabled=snapshot_enabled,
            )
        )

    return _adapter_result(
        source_rows,
        input_count=len(raw_rows),
        adapter_code="competitor_table_source_adapter",
        extra_summary={
            "skipped_complete_count": skipped_complete,
            "skipped_unavailable_count": skipped_unavailable,
            "dropped_empty_count": dropped_empty,
        },
    )


def _source_row(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    business_fields: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    snapshot_enabled: bool,
) -> dict[str, Any]:
    record_id = _text(row.get("record_id"))
    source_table_ref = _text(payload.get("source_table_ref"))
    candidate_key = _candidate_key(identity)
    item = {
        "source_record_id": record_id,
        "source_table_ref": source_table_ref,
        "product_identity": dict(identity),
        "product_id": _text(identity.get("product_id")),
        "product_url": _text(identity.get("product_url")),
        "normalized_product_url": _text(identity.get("normalized_product_url")),
        "business_key": candidate_key,
        "business_fields": dict(business_fields or {}),
        "writeback_context": {
            "target_table_ref": _first_non_empty(payload.get("target_table_ref"), source_table_ref),
            "competitor_status_table_ref": _first_non_empty(payload.get("competitor_status_table_ref"), source_table_ref),
            "record_id": record_id,
        },
        "source_context": {
            "source_record_id": record_id,
            "source_table_ref": source_table_ref,
            "product_identity": dict(identity),
            "source_fields": _mapping(row.get("fields")),
        },
    }
    if snapshot_enabled:
        item["source_snapshot_ref"] = _raw_result_ref(payload, record_id)
    if extra:
        item.update(dict(extra))
    return _compact(item)


def _adapter_result(
    source_rows: list[dict[str, Any]],
    *,
    input_count: int,
    adapter_code: str,
    extra_summary: Mapping[str, Any],
) -> dict[str, Any]:
    deduped_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    deduped_count = 0
    for row in source_rows:
        key = _first_non_empty(row.get("business_key"), row.get("source_record_id"))
        if key and key in seen:
            deduped_count += 1
            continue
        if key:
            seen.add(key)
        deduped_rows.append(row)
    return {
        "source_rows": deduped_rows,
        "candidate_keys": [_candidate_key(row.get("product_identity")) for row in deduped_rows],
        "adapter_summary": {
            "adapter_code": adapter_code,
            "input_row_count": input_count,
            "source_row_count": len(deduped_rows),
            "deduped_count": deduped_count,
            **dict(extra_summary),
        },
    }


def competitor_table_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _adapt_competitor_rows(raw_rows, payload)


def _raw_result_ref(payload: Mapping[str, Any], key: Any) -> str:
    namespace = _first_non_empty(
        _mapping(payload.get("snapshot_policy")).get("raw_snapshot_namespace"),
        "feishu/common",
    )
    request_id = _first_non_empty(payload.get("request_id"), payload.get("stage_code"), "request")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", _text(key) or "row").strip("-") or "row"
    return f"artifact://{namespace}/{request_id}/{safe_key}.json"


def _product_identity_from_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    product_url = _field_text(fields, "产品链接", "商品链接", "product_url", "normalized_product_url")
    sku_id = _field_text(fields, "SKU-ID", "SKU ID", "商品ID", "product_id", "sku_id")
    product_id = _first_non_empty(_extract_product_id(sku_id), _extract_product_id(product_url))
    normalized_url = _normalize_product_url(product_url or (f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else ""))
    return _compact(
        {
            "product_id": product_id,
            "product_url": product_url or normalized_url,
            "normalized_product_url": normalized_url,
            "fastmoss_product_url": f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}" if product_id else "",
        }
    )


def _candidate_key(identity: Any) -> str:
    item = _mapping(identity)
    value = _first_non_empty(
        item.get("product_id"),
        _strip_product_key_prefix(item.get("business_entity_key")),
        item.get("normalized_product_url"),
        item.get("product_url"),
    )
    return f"product:{value}" if value else ""


def _strip_product_key_prefix(value: Any) -> str:
    text = _text(value)
    return text.removeprefix("product:") if text.startswith("product:") else text


def _extract_product_id(*values: Any) -> str:
    for value in values:
        text = _text_value(value)
        if not text:
            continue
        for pattern in _PRODUCT_ID_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return match.group(1)
    return ""


def _normalize_product_url(value: Any) -> str:
    text = _text_value(value)
    product_id = _extract_product_id(text)
    if product_id:
        return f"https://www.tiktok.com/shop/pdp/{product_id}"
    return text


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name)
        text = _text_value(value)
        if text:
            return text
    return ""


def _field_has_value(value: Any) -> bool:
    if isinstance(value, list):
        return any(_field_has_value(item) for item in value)
    if isinstance(value, Mapping):
        return any(_field_has_value(item) for item in value.values())
    return bool(_text(value))


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
    if isinstance(value, list):
        return _first_non_empty(*(_text_value(item) for item in value))
    return _text(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item) or item == ""]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item) or item == ""]
    text = _text(value)
    return [text] if text else []


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                compacted[str(key)] = value.strip()
            continue
        if isinstance(value, Mapping):
            nested = _compact(value)
            if nested:
                compacted[str(key)] = nested
            continue
        if isinstance(value, list):
            items = [item for item in value if item not in ("", None, {}, [])]
            if items:
                compacted[str(key)] = items
            continue
        compacted[str(key)] = value
    return compacted
