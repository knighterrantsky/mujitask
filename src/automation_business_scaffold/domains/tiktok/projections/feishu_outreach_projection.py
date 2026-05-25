from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def outreach_result_projection_mapper(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    existing_video_url = _first_non_empty(record.get("existing_video_url"), _mapping(record.get("source_fields")).get("视频链接"))
    matched = _text(record.get("match_status")) == "matched" or bool(record.get("video_url"))
    fields: dict[str, Any] = {}
    if matched and not existing_video_url:
        fields["视频链接"] = _link_value(record.get("video_url"))
        published_date = _text(record.get("published_date"))
        if published_date:
            fields["视频发布时间"] = published_date
    check_time = _first_non_empty(record.get("checked_at"), payload.get("trigger_date"), payload.get("check_time"))
    if check_time:
        fields["检查时间"] = check_time
    return _normalize_write_record(
        {
            "op": "update",
            "record_id": _first_non_empty(record.get("source_record_id"), record.get("record_id")),
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), f"outreach:{_first_non_empty(record.get('source_record_id'), record.get('record_id'))}"),
            "fields": fields,
            "source_context": _source_context_from_record(record, payload),
        },
        payload,
    )


def _normalize_write_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    record_id = _first_non_empty(record.get("record_id"), record.get("source_record_id"))
    return _compact(
        {
            "op": _first_non_empty(record.get("op"), "update" if record_id else "append"),
            "record_id": record_id,
            "business_entity_key": _first_non_empty(record.get("business_entity_key"), payload.get("business_entity_key")),
            "fields": _mapping(record.get("fields")),
            "source_context": _mapping(record.get("source_context")) or _source_context_from_record(record, payload),
        }
    )


def _link_value(value: Any) -> Any:
    url = _text(value)
    return {"link": url, "text": url} if url else ""


def _source_context_from_record(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return _compact(
        {
            "source_record_id": _first_non_empty(record.get("source_record_id"), record.get("record_id"), payload.get("source_record_id")),
            "product_id": _text(record.get("product_id")),
            "creator_unique_id": _text(record.get("creator_unique_id")),
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


__all__ = ["outreach_result_projection_mapper"]
