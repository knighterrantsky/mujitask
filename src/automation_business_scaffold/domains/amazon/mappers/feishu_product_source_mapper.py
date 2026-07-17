from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    AmazonProductExtractionError,
    InvalidASINError,
    UnsupportedMarketplaceError,
    canonical_amazon_url,
    extract_asin_from_url,
    normalize_asin,
)


AMAZON_PRODUCT_SOURCE_FIELDS = (
    "ASIN",
    "采集标签",
    "商品链接",
    "强制刷新",
    "采集状态",
)


def amazon_product_table_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    source_record_id = _required_text(payload.get("source_record_id"), "source_record_id")
    matched_rows = [
        row for row in raw_rows if _text(row.get("record_id")) == source_record_id
    ]
    counters = {
        "invalid_asin_count": 0,
        "identity_mismatch_count": 0,
        "unsupported_marketplace_count": 0,
    }
    if not matched_rows:
        return _result(
            raw_rows=raw_rows,
            source_rows=[],
            matched_row_count=0,
            lookup_status="not_found",
            counters=counters,
        )
    if len(matched_rows) != 1:
        return _result(
            raw_rows=raw_rows,
            source_rows=[],
            matched_row_count=len(matched_rows),
            lookup_status="ambiguous_match",
            counters=counters,
        )

    row = matched_rows[0]
    fields = _mapping(row.get("fields"))
    try:
        asin = normalize_asin(_field_text(fields.get("ASIN")))
    except InvalidASINError:
        counters["invalid_asin_count"] = 1
        return _result(
            raw_rows=raw_rows,
            source_rows=[],
            matched_row_count=1,
            lookup_status="invalid_asin",
            counters=counters,
        )

    source_url = _field_text(fields.get("商品链接"))
    if source_url:
        try:
            url_asin = extract_asin_from_url(source_url)
        except UnsupportedMarketplaceError:
            counters["unsupported_marketplace_count"] = 1
            return _result(
                raw_rows=raw_rows,
                source_rows=[],
                matched_row_count=1,
                lookup_status="unsupported_marketplace",
                counters=counters,
            )
        except AmazonProductExtractionError:
            counters["identity_mismatch_count"] = 1
            return _result(
                raw_rows=raw_rows,
                source_rows=[],
                matched_row_count=1,
                lookup_status="identity_mismatch",
                counters=counters,
            )
        if url_asin != asin:
            counters["identity_mismatch_count"] = 1
            return _result(
                raw_rows=raw_rows,
                source_rows=[],
                matched_row_count=1,
                lookup_status="identity_mismatch",
                counters=counters,
            )

    canonical_url = canonical_amazon_url(asin)
    business_key = f"amazon:US:{asin}"
    source_table_ref = _text(payload.get("source_table_ref"))
    target_table_ref = _first_non_empty(payload.get("target_table_ref"), source_table_ref)
    source_fields = {
        "ASIN": asin,
        "采集标签": _field_text(fields.get("采集标签")),
        "商品链接": canonical_url,
        "强制刷新": _field_bool(fields.get("强制刷新")),
        "采集状态": _field_text(fields.get("采集状态")),
    }
    source_row = {
        "source_record_id": source_record_id,
        "source_table_ref": source_table_ref,
        "business_key": business_key,
        "requested_asin": asin,
        "canonical_url": canonical_url,
        "product_identity": {
            "marketplace_code": "US",
            "asin": asin,
            "canonical_url": canonical_url,
        },
        "business_fields": {
            "force_refresh": _field_bool(fields.get("强制刷新")),
            "collection_status": _field_text(fields.get("采集状态")),
        },
        "writeback_context": {
            "target_table_ref": target_table_ref,
            "record_id": source_record_id,
        },
        "source_context": {
            "source_record_id": source_record_id,
            "source_table_ref": source_table_ref,
            "source_fields": source_fields,
        },
    }
    return _result(
        raw_rows=raw_rows,
        source_rows=[source_row],
        matched_row_count=1,
        lookup_status="matched",
        counters=counters,
    )


def amazon_product_batch_source_adapter(
    raw_rows: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    tagged_rows = [
        row
        for row in raw_rows
        if _field_text(_mapping(row.get("fields")).get("采集标签")) == "T"
    ]
    source_rows: list[dict[str, Any]] = []
    counters = {
        "invalid_asin_count": 0,
        "identity_mismatch_count": 0,
        "unsupported_marketplace_count": 0,
        "missing_record_id_count": 0,
    }
    for row in tagged_rows:
        source_record_id = _text(row.get("record_id"))
        if not source_record_id:
            counters["missing_record_id_count"] += 1
            continue
        row_result = amazon_product_table_source_adapter(
            [row],
            {**dict(payload), "source_record_id": source_record_id},
        )
        row_summary = _mapping(row_result.get("adapter_summary"))
        for key in (
            "invalid_asin_count",
            "identity_mismatch_count",
            "unsupported_marketplace_count",
        ):
            counters[key] += int(row_summary.get(key) or 0)
        source_rows.extend(row_result.get("source_rows") or [])
    return {
        "source_rows": source_rows,
        "candidate_keys": [row["business_key"] for row in source_rows],
        "adapter_summary": {
            "adapter_code": "amazon_product_batch_source_adapter",
            "input_row_count": len(raw_rows),
            "tagged_row_count": len(tagged_rows),
            "source_row_count": len(source_rows),
            "selection_field": "采集标签",
            "selection_value": "T",
            **counters,
        },
    }


def _result(
    *,
    raw_rows: list[Mapping[str, Any]],
    source_rows: list[dict[str, Any]],
    matched_row_count: int,
    lookup_status: str,
    counters: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "source_rows": source_rows,
        "candidate_keys": [row["business_key"] for row in source_rows],
        "adapter_summary": {
            "adapter_code": "amazon_product_table_source_adapter",
            "input_row_count": len(raw_rows),
            "source_row_count": len(source_rows),
            "matched_row_count": matched_row_count,
            "lookup_status": lookup_status,
            **dict(counters),
        },
    }


def _field_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(
            value.get("link"),
            value.get("text"),
            value.get("value"),
            value.get("name"),
        )
    if isinstance(value, list):
        return _first_non_empty(*(_field_text(item) for item in value))
    return _text(value)


def _field_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on", "是", "已勾选"}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        item = _text(value)
        if item:
            return item
    return ""


def _required_text(value: Any, name: str) -> str:
    item = _text(value)
    if not item:
        raise ValueError(f"{name} is required.")
    return item


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


__all__ = [
    "AMAZON_PRODUCT_SOURCE_FIELDS",
    "amazon_product_batch_source_adapter",
    "amazon_product_table_source_adapter",
]
