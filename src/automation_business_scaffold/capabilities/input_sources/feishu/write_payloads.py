from __future__ import annotations

import re
from typing import Any, Mapping


def map_write_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = mapping_list(payload.get("records"))
    if not records:
        from automation_business_scaffold.contracts.handler.domain_mapping import (
            selection_writeback_records,
        )

        records = selection_writeback_records(payload)
    mapper_code = text(payload.get("mapper_code"))
    mapped: list[dict[str, Any]] = []
    for record in records:
        if text(record.get("op")) == "delete":
            mapped.append(normalize_write_record(record, payload))
            continue
        if mapping(record.get("fields")):
            mapped.append(normalize_write_record(record, payload))
            continue
        from automation_business_scaffold.contracts.handler.domain_mapping import (
            map_projection_record,
        )

        mapped.append(map_projection_record(mapper_code, record, payload))
    return [record for record in mapped if mapping(record.get("fields")) or text(record.get("op")) == "delete"]


def normalize_write_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    write_mode = text(payload.get("write_mode"))
    record_id = first_non_empty(record.get("record_id"), record.get("source_record_id"))
    op = text(record.get("op"))
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
        "business_entity_key": first_non_empty(record.get("business_entity_key"), payload.get("business_entity_key")),
        "upsert_key": mapping(record.get("upsert_key")),
        "update_excluded_fields": list(record.get("update_excluded_fields") or payload.get("update_excluded_fields") or []),
        "update_replace_fields": list(record.get("update_replace_fields") or payload.get("update_replace_fields") or []),
        "update_accumulate_fields": mapping(record.get("update_accumulate_fields") or payload.get("update_accumulate_fields")),
        "fields": mapping(record.get("fields")),
        "source_context": mapping(record.get("source_context")) or source_context_from_record(record, payload),
    }
    return compact(item)


def write_record_key(record: Mapping[str, Any]) -> str:
    record_id = text(record.get("record_id"))
    if record_id:
        return f"record:{record_id}"
    upsert_key = mapping(record.get("upsert_key"))
    if upsert_key:
        return f"upsert:{text(upsert_key.get('field'))}:{text(upsert_key.get('value'))}"
    entity_key = text(record.get("business_entity_key"))
    if entity_key:
        return f"entity:{entity_key}"
    return ""


def write_result_record(
    record: Mapping[str, Any],
    *,
    status: str,
    record_id: str = "",
    op: str = "",
    message: str = "",
    error_type: str = "",
    error_code: str = "",
) -> dict[str, Any]:
    item = {
        "business_entity_key": text(record.get("business_entity_key")),
        "record_id": first_non_empty(record_id, record.get("record_id")),
        "op": first_non_empty(op, record.get("op")),
        "status": status,
        "fields_written": list(mapping(record.get("fields")).keys()),
    }
    if message:
        item["message"] = message
    if error_type:
        item["error_type"] = error_type
    if error_code:
        item["error_code"] = error_code
    return compact(item)


def raw_result_ref(payload: Mapping[str, Any], key: Any) -> str:
    namespace = first_non_empty(
        mapping(payload.get("snapshot_policy")).get("raw_snapshot_namespace"),
        "feishu/common",
    )
    request_id = first_non_empty(payload.get("request_id"), payload.get("stage_code"), "request")
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", text(key) or "row").strip("-") or "row"
    return f"artifact://{namespace}/{request_id}/{safe_key}.json"


def raw_batch_ref(payload: Mapping[str, Any]) -> str:
    namespace = first_non_empty(
        mapping(payload.get("raw_capture_policy")).get("raw_response_namespace"),
        "feishu/common/write",
    )
    request_id = first_non_empty(payload.get("request_id"), payload.get("stage_code"), "request")
    return f"artifact://{namespace}/{request_id}/batch-1.json"


def compact_raw_result(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    data = mapping(raw_result.get("data"))
    if data:
        return {"code": raw_result.get("code", 0), "data": data}
    return {"code": raw_result.get("code", 0)}


def text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
    if isinstance(value, list):
        return first_non_empty(*(text_value(item) for item in value))
    return text(value)


def source_context_from_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "source_record_id": text(record.get("source_record_id") or payload.get("source_record_id")),
            "candidate_key": text(record.get("candidate_key") or payload.get("candidate_key")),
            "workflow_code": text(payload.get("workflow_code")),
            "stage_code": text(payload.get("stage_code")),
            "projection_type": text(payload.get("mapper_code")),
        }
    )


def mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text(item) for item in value if text(item) or item == ""]
    if isinstance(value, tuple):
        return [text(item) for item in value if text(item) or item == ""]
    item = text(value)
    return [item] if item else []


def first_non_empty(*values: Any) -> str:
    for value in values:
        item = text(value)
        if item:
            return item
    return ""


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                compacted[str(key)] = value.strip()
            continue
        if isinstance(value, Mapping):
            nested = compact(value)
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
