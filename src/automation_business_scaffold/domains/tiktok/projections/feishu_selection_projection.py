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


_SKIP_WRITE_IF_EMPTY = frozenset({"记录日期", "商品状态"})


def _map_selection_seed_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_id = _first_non_empty(record.get("product_id"), _extract_product_id(record.get("product_url")))
    product_url = _normalize_product_url(
        _first_non_empty(record.get("product_url"), f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else "")
    )
    search_query = _text(record.get("search_query") or payload.get("search_query"))
    fields = {
        "商品ID": product_id,
        "商品链接": _link_value(product_url),
        "关键词": search_query,
        "备注": f"通过搜索关键字：{search_query}" if search_query else "",
        "记录日期": date.today().isoformat(),
    }
    upsert_key = (
        {"field": "商品ID", "value": product_id}
        if product_id
        else {"field": "商品链接", "value": product_url}
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


def _map_selection_table_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    product_identity = _mapping(payload.get("product_identity")) or _mapping(record.get("product_identity"))
    product_id = _first_non_empty(record.get("product_id"), product_identity.get("product_id"))
    product_url = _normalize_product_url(_first_non_empty(record.get("product_url"), product_identity.get("normalized_product_url"), product_identity.get("product_url")))

    projection_fields = _mapping(record.get("projection_fields"))
    if projection_fields:
        existing_fields = _mapping(record.get("source_fields"))
        fields = _select_missing_selection_fields(
            projection_fields,
            existing_fields=existing_fields,
            product_id=product_id,
            product_url=product_url,
        )
    else:
        fields = {
            "商品ID": product_id,
            "商品链接": _link_value(product_url),
            "记录日期": date.today().isoformat(),
        }
    return _normalize_write_record(
        {
            "op": "update" if _text(record.get("source_record_id")) else "upsert",
            "record_id": _text(record.get("source_record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), _candidate_key({"product_id": product_id, "product_url": product_url})),
            "upsert_key": {"field": "商品ID", "value": product_id} if product_id else {},
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _select_missing_selection_fields(
    projection_fields: Mapping[str, Any],
    *,
    existing_fields: Mapping[str, Any],
    product_id: str,
    product_url: str,
) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    has_content_update = False
    for field_name, value in projection_fields.items():
        if field_name in _SKIP_WRITE_IF_EMPTY:
            continue
        if field_name == "商品ID":
            selected["商品ID"] = product_id or value
            continue
        if field_name == "商品链接":
            selected["商品链接"] = _link_value(product_url) if product_url else _link_value(_text_value(value))
            continue
        if not _field_has_value(value):
            continue
        if _field_has_value(existing_fields.get(field_name)):
            continue
        selected[field_name] = value
        has_content_update = True
    if has_content_update:
        selected["记录日期"] = projection_fields.get("记录日期") or date.today().isoformat()
    return selected


def _field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return True


def _selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    product_identity = _mapping(payload.get("product_identity"))
    request_payload = _mapping(payload.get("request_payload"))
    record_id = _first_non_empty(payload.get("selection_record_id"), request_payload.get("selection_record_id"))
    if not (product_identity or record_id):
        return []
    return [
        {
            "source_record_id": record_id,
            "product_identity": product_identity,
            "product_id": _first_non_empty(product_identity.get("product_id"), payload.get("product_id")),
            "product_url": _first_non_empty(product_identity.get("normalized_product_url"), product_identity.get("product_url"), payload.get("product_url")),
        }
    ]


def selection_seed_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_selection_seed_record(record, payload)


def selection_table_projection_mapper(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return _map_selection_table_record(record, payload)


def selection_writeback_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _selection_writeback_records(payload)


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


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


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
