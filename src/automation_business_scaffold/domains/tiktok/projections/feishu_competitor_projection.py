from __future__ import annotations
import re
from collections.abc import Mapping
from datetime import date
from typing import Any


_PRODUCT_ID_PATTERNS = (
    re.compile(r"/(?:pdp|product|detail)/(\d+)", re.IGNORECASE),
    re.compile(r"[?&](?:product_id|goods_id)=(\d+)", re.IGNORECASE),
    re.compile(r"\b(\d{8,})\b"),
)


_COMPETITOR_WRITEBACK_EXCLUDED_FIELDS = {"商品状态"}


def _normalize_write_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    write_mode = _text(payload.get("write_mode"))
    record_id = _first_non_empty(record.get("record_id"), record.get("source_record_id"))
    op = _text(record.get("op"))
    if not op:
        if "insert" in write_mode or "append" in write_mode:
            op = "append"
        elif "upsert" in write_mode:
            op = "upsert"
        elif record_id:
            op = "update"
        else:
            op = "append"
    item = {
        "op": op,
        "record_id": record_id,
        "business_entity_key": _first_non_empty(record.get("business_entity_key"), payload.get("business_entity_key")),
        "upsert_key": _mapping(record.get("upsert_key")),
        "fields": _mapping(record.get("fields")),
        "source_context": _mapping(record.get("source_context")) or _source_context_from_record(record, payload),
    }
    return _compact(item)


def _map_competitor_seed_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_id = _first_non_empty(record.get("product_id"), _extract_product_id(record.get("product_url")))
    product_url = _normalize_product_url(
        _first_non_empty(record.get("product_url"), f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else "")
    )
    search_query = _text(record.get("search_query"))
    fields = {
        "SKU-ID": product_id,
        "产品链接": _link_value(product_url),
        "关键词": search_query,
        "备注": f"通过搜索关键字：{search_query}" if search_query else "",
        "达人查找状态": "待查找",
    }
    upsert_key = (
        {"field": "SKU-ID", "value": product_id}
        if product_id
        else {"field": "产品链接", "value": product_url}
    )
    return _normalize_write_record(
        {
            "op": "insert_if_absent",
            "business_entity_key": _candidate_key(
                {
                    "product_id": product_id,
                    "business_entity_key": record.get("business_entity_key"),
                    "product_url": product_url,
                }
            ),
            "upsert_key": upsert_key,
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _map_competitor_table_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_id = _text(record.get("product_id"))
    product_url = _normalize_product_url(record.get("product_url"))
    projection_fields = _mapping(record.get("projection_fields"))
    if projection_fields:
        projection_fields = _normalize_competitor_projection_fields(
            {
                "SKU-ID": product_id,
                "产品链接": product_url,
                **projection_fields,
            }
        )
        fields = _select_missing_competitor_projection_fields(
            projection_fields,
            existing_fields=_mapping(record.get("source_fields")),
        )
    else:
        fields = {
            "SKU-ID": product_id,
            "产品链接": _link_value(product_url),
            "记录日期": date.today().isoformat(),
            "备注": _refresh_note(record),
        }
    return _normalize_write_record(
        {
            "op": "update" if _text(record.get("source_record_id")) else "upsert",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), _candidate_key({"product_id": product_id, "product_url": product_url})),
            "upsert_key": {"field": "SKU-ID", "value": product_id} if product_id else {},
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _normalize_competitor_projection_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field_name, value in fields.items():
        name = _text(field_name)
        if not name or value in (None, "", [], {}):
            continue
        if name == "产品链接":
            normalized[name] = _link_value(_text_value(value)) if _text_value(value) else value
            continue
        if name in {"图片", "前台截图", "Fastmoss截图"}:
            if isinstance(value, Mapping) and any(
                _first_non_empty(value.get(key))
                for key in ("file_token", "local_path", "source_path", "path", "url", "source_url", "remote_uri", "object_key")
            ):
                normalized[name] = dict(value)
                continue
            normalized[name] = _raw_link_value(_text_value(value)) if _text_value(value) else value
            continue
        normalized[name] = value
    return normalized


def _select_missing_competitor_projection_fields(
    projection_fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any],
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for field_name, value in projection_fields.items():
        if field_name in {"记录日期", *_COMPETITOR_WRITEBACK_EXCLUDED_FIELDS}:
            continue
        if not _field_has_value(value):
            continue
        if not _field_has_value(existing_fields.get(field_name)):
            selected[field_name] = value
    if selected:
        selected["记录日期"] = projection_fields.get("记录日期") or date.today().isoformat()
    return selected


def _map_competitor_influencer_status_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    status = _text(record.get("influencer_sync_status"))
    status_text = {
        "success": "已完成",
        "partial_success": "部分完成",
        "failed": "失败重试",
        "skipped": "跳过",
    }.get(status, status or "已完成")
    fields = {
        "达人查找状态": status_text,
        "达人数量": _coerce_int(
            _first_non_empty(
                record.get("influencer_write_success_count"),
                record.get("creator_detail_success_count"),
                record.get("creator_candidate_count"),
            ),
            default=0,
            minimum=0,
            maximum=1_000_000,
        ),
        "备注": _status_note(record),
    }
    return _normalize_write_record(
        {
            "op": "update",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), record.get("product_key"), _candidate_key({"product_id": record.get("product_id")})),
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def competitor_seed_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_seed_record(record, payload)


def competitor_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_table_record(record, payload)


def competitor_influencer_status_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_competitor_influencer_status_record(record, payload)


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


def _link_value(url: str) -> dict[str, str] | str:
    normalized = _normalize_product_url(url)
    if not normalized:
        return ""
    return {"text": normalized, "link": normalized}


def _raw_link_value(url: str) -> dict[str, str] | str:
    normalized = _text(url)
    if not normalized:
        return ""
    return {"text": normalized, "link": normalized}


def _source_context_from_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "source_record_id": _text(record.get("source_record_id") or payload.get("source_record_id")),
            "candidate_key": _text(record.get("candidate_key") or payload.get("candidate_key")),
            "workflow_code": _text(payload.get("workflow_code")),
            "stage_code": _text(payload.get("stage_code")),
            "projection_type": _text(payload.get("mapper_code")),
        }
    )


def _refresh_note(record: Mapping[str, Any]) -> str:
    status = _text(record.get("refresh_status"))
    details = _mapping(record.get("details"))
    if status:
        return f"runtime refresh status: {status}"
    row_status = _text(details.get("row_status"))
    return f"runtime refresh status: {row_status}" if row_status else ""


def _status_note(record: Mapping[str, Any]) -> str:
    warnings = _list_text(record.get("warnings"))
    if warnings:
        return "; ".join(warnings)
    failed = _coerce_int(record.get("creator_detail_failed_count"), default=0, minimum=0, maximum=1000000)
    success = _coerce_int(record.get("influencer_write_success_count"), default=0, minimum=0, maximum=1000000)
    return f"creator_failed={failed}; influencer_written={success}"


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


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


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
