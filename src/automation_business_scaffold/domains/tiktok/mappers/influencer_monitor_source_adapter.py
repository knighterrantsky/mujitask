from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


INFLUENCER_MONITOR_READ_FIELD_NAMES = (
    "SKU-ID",
    "产品链接",
    "图片",
    "节日",
    "商品状态",
    "达人查找状态",
)

_PRODUCT_ID_PATTERNS = (
    re.compile(r"/(?:pdp|product|detail)/(\d+)", re.IGNORECASE),
    re.compile(r"[?&](?:product_id|goods_id)=(\d+)", re.IGNORECASE),
    re.compile(r"\b(\d{8,})\b"),
)


def influencer_monitor_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    requested_record_ids = set(_list_text(payload.get("source_record_ids")))
    grouped: dict[str, dict[str, Any]] = {}
    invalid_sku_count = 0
    selected_row_count = 0

    for raw_row in raw_rows:
        record_id = _text(raw_row.get("record_id") or raw_row.get("id"))
        if requested_record_ids and record_id not in requested_record_ids:
            continue
        selected_row_count += 1
        fields = _mapping(raw_row.get("fields"))
        product_id = _product_id(fields)
        if not product_id:
            invalid_sku_count += 1
            continue
        item = grouped.setdefault(
            product_id,
            {
                "product_id": product_id,
                "product_identity": {
                    "product_id": product_id,
                    "product_url": _field_text(fields, "产品链接", "商品链接"),
                    "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                    "fastmoss_product_url": (
                        f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}"
                    ),
                },
                "business_key": f"product:{product_id}",
                "source_table_ref": _text(payload.get("source_table_ref")),
                "source_record_ids": [],
                "source_product_images": [],
                "holidays": [],
                "observed_product_statuses": [],
                "observed_influencer_search_statuses": [],
            },
        )
        _append_unique(item["source_record_ids"], record_id)
        for image in _attachment_items(fields.get("图片")):
            _append_unique_mapping(item["source_product_images"], image)
        for holiday in _field_text_values(fields, "节日", "关联节日"):
            _append_unique(item["holidays"], holiday)
        _append_unique(
            item["observed_product_statuses"],
            _field_text(fields, "商品状态", "product_status"),
        )
        _append_unique(
            item["observed_influencer_search_statuses"],
            _field_text(fields, "达人查找状态", "influencer_search_status"),
        )

    source_rows = list(grouped.values())
    for item in source_rows:
        item["source_record_id"] = (
            item["source_record_ids"][0] if item["source_record_ids"] else ""
        )
        item["source_context"] = {
            "source_record_ids": list(item["source_record_ids"]),
            "source_table_ref": item["source_table_ref"],
            "product_id": item["product_id"],
        }

    return {
        "source_rows": source_rows,
        "candidate_keys": [item["business_key"] for item in source_rows],
        "adapter_summary": {
            "adapter_code": "influencer_monitor_source_adapter",
            "input_row_count": len(raw_rows),
            "selected_row_count": selected_row_count,
            "valid_product_row_count": selected_row_count - invalid_sku_count,
            "deduped_product_count": len(source_rows),
            "duplicate_product_row_count": max(
                selected_row_count - invalid_sku_count - len(source_rows),
                0,
            ),
            "invalid_sku_count": invalid_sku_count,
            "status_filtered_count": 0,
        },
    }


def _product_id(fields: Mapping[str, Any]) -> str:
    for value in (
        fields.get("SKU-ID"),
        fields.get("SKU ID"),
        fields.get("商品ID"),
        fields.get("product_id"),
        fields.get("sku_id"),
        fields.get("产品链接"),
        fields.get("商品链接"),
    ):
        text = _text_value(value)
        for pattern in _PRODUCT_ID_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return match.group(1)
    return ""


def _attachment_items(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        compacted = {
            key: item[key]
            for key in (
                "file_token",
                "name",
                "url",
                "tmp_url",
                "bucket",
                "object_key",
                "content_digest",
                "file_name",
                "mime_type",
            )
            if item.get(key) not in (None, "")
        }
        if compacted:
            result.append(compacted)
    return result


def _attachment_identity(item: Mapping[str, Any]) -> str:
    durable_ref = ""
    if item.get("bucket") and item.get("object_key"):
        durable_ref = f"{_text(item.get('bucket'))}/{_text(item.get('object_key'))}"
    return _first_non_empty(
        item.get("file_token"),
        durable_ref,
        item.get("content_digest"),
        item.get("url"),
        item.get("tmp_url"),
        item.get("name"),
        item.get("file_name"),
    )


def _append_unique(values: list[str], value: Any) -> None:
    normalized = _text(value)
    if normalized and normalized not in values:
        values.append(normalized)


def _append_unique_mapping(
    values: list[dict[str, Any]],
    value: Mapping[str, Any],
) -> None:
    identity = _attachment_identity(value)
    if not identity:
        return
    if any(_attachment_identity(existing) == identity for existing in values):
        return
    values.append(dict(value))


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = _text_value(fields.get(name))
        if value:
            return value
    return ""


def _field_text_values(fields: Mapping[str, Any], *names: str) -> list[str]:
    for name in names:
        value = fields.get(name)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            result = []
            for item in value:
                normalized = _text_value(item)
                if normalized and normalized not in result:
                    result.append(normalized)
            return result
        normalized = _text_value(value)
        return [normalized] if normalized else []
    return []


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(
            value.get("text"),
            value.get("value"),
            value.get("name"),
            value.get("link"),
        )
    if isinstance(value, list):
        return _first_non_empty(*(_text_value(item) for item in value))
    return _text(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _list_text(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value if _text(item)]
    normalized = _text(value)
    return [normalized] if normalized else []


def _first_non_empty(*values: Any) -> str:
    for value in values:
        normalized = _text(value)
        if normalized:
            return normalized
    return ""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


__all__ = [
    "INFLUENCER_MONITOR_READ_FIELD_NAMES",
    "influencer_monitor_source_adapter",
]
