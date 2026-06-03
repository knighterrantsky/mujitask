from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def outreach_result_projection_mapper(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    existing_video_url = _first_non_empty(record.get("existing_video_url"), _mapping(record.get("source_fields")).get("视频链接"))
    existing_published_date = _first_non_empty(
        record.get("existing_video_published_date"),
        _mapping(record.get("source_fields")).get("视频发布时间"),
    )
    fields: dict[str, Any] = {}
    video_url = _first_non_empty(record.get("highest_play_video_url"), record.get("video_url"))
    check_time = _first_non_empty(record.get("checked_at"), payload.get("trigger_date"), payload.get("check_time"))
    if not video_url:
        if check_time and not existing_video_url:
            fields["检查时间"] = check_time
        return _normalize_write_record(
            {
                "op": "update",
                "record_id": _first_non_empty(record.get("source_record_id"), record.get("record_id")),
                "business_entity_key": _first_non_empty(
                    record.get("business_entity_key"),
                    f"outreach:{_first_non_empty(record.get('source_record_id'), record.get('record_id'))}",
                ),
                "fields": fields,
                "source_context": _source_context_from_record(record, payload),
            },
            payload,
        )

    if video_url and video_url != _text_value(existing_video_url):
        fields["视频链接"] = _link_value(video_url)
    published_date = _first_non_empty(record.get("earliest_published_date"), record.get("published_date"))
    if published_date and not _text_value(existing_published_date):
        fields["视频发布时间"] = published_date
    if "total_play_count" in record or "play_count" in record:
        play_count = _format_feishu_play_count(_first_non_empty(record.get("total_play_count"), record.get("play_count")))
        if play_count != _existing_play_count_display(record):
            fields["播放量"] = play_count
    if "video_count" in record:
        video_count = _int(record.get("video_count"))
        if video_count != _int(_first_non_empty(record.get("existing_video_count"), _mapping(record.get("source_fields")).get("视频数量"))):
            fields["视频数量"] = video_count
    if fields:
        updated_at = _first_non_empty(record.get("updated_at"), payload.get("trigger_date"), payload.get("check_time"), check_time)
        if updated_at:
            fields["更新时间"] = updated_at
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


def _format_feishu_play_count(value: Any) -> str:
    play_count = max(0, _int(value))
    if play_count < 10000:
        return "<1W"
    return f"{play_count // 10000}W"


def _existing_play_count_display(record: Mapping[str, Any]) -> str:
    source_fields = _mapping(record.get("source_fields"))
    if "播放量" in source_fields:
        return _text_value(source_fields.get("播放量"))
    existing_play_count = record.get("existing_play_count")
    if existing_play_count not in (None, ""):
        return _format_feishu_play_count(existing_play_count)
    return ""


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
    if isinstance(value, list):
        return _first_non_empty(*(_text_value(item) for item in value))
    return _text(value)


def _int(value: Any) -> int:
    text = _text_value(value).replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


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
