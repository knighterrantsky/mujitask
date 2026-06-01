from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OUTREACH_READ_FIELD_NAMES = (
    "SKUID",
    "达人ID",
    "视频链接",
    "视频发布时间",
    "检查时间",
    "播放量",
    "视频数量",
    "更新时间",
)


def outreach_source_adapter(
    raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]
) -> dict[str, Any]:
    source_rows: list[dict[str, Any]] = []
    skip_reasons = {
        "missing_product_id": 0,
        "missing_creator_unique_id": 0,
    }
    source_record_ids = set(_list_text(payload.get("source_record_ids")))

    for row in raw_rows:
        record_id = _text(row.get("record_id") or row.get("id"))
        if source_record_ids and record_id not in source_record_ids:
            continue
        fields = _mapping(row.get("fields"))
        product_id = _field_text(fields, "SKUID", "sku_id", "product_id")
        creator_unique_id = _field_text(fields, "达人ID", "creator_unique_id", "unique_id")
        existing_video_url = _field_text(fields, "视频链接", "video_url")
        if not product_id:
            skip_reasons["missing_product_id"] += 1
            continue
        if not creator_unique_id:
            skip_reasons["missing_creator_unique_id"] += 1
            continue
        existing_video_published_date = _normalize_date(
            _field_text(
                fields, "视频发布时间", "existing_video_published_date", "video_published_date"
            )
        )
        existing_play_count = _field_int(fields, "播放量", "existing_play_count", "play_count")
        existing_video_count = _field_int(fields, "视频数量", "existing_video_count", "video_count")
        last_checked_at = _normalize_date(_field_text(fields, "检查时间", "last_checked_at"))
        last_updated_at = _normalize_date(
            _field_text(fields, "更新时间", "last_updated_at", "updated_at")
        )
        source_context = {
            "source_record_id": record_id,
            "source_table_ref": _text(payload.get("source_table_ref")),
            "source_fields": fields,
        }
        source_rows.append(
            {
                "source_record_id": record_id,
                "business_key": f"outreach:{record_id}",
                "product_id": product_id,
                "creator_unique_id": creator_unique_id,
                "existing_video_url": existing_video_url,
                "existing_video_published_date": existing_video_published_date,
                "existing_play_count": existing_play_count,
                "existing_video_count": existing_video_count,
                "last_checked_at": last_checked_at,
                "last_updated_at": last_updated_at,
                "source_fields": fields,
                "writeback_context": {
                    "table_code": "tk_influencer_outreach",
                    "target_table_ref": _first_non_empty(
                        payload.get("target_table_ref"), payload.get("source_table_ref")
                    ),
                    "record_id": record_id,
                },
                "source_context": source_context,
            }
        )

    return {
        "source_rows": source_rows,
        "candidate_keys": [f"outreach:{row['source_record_id']}" for row in source_rows],
        "adapter_summary": {
            "adapter_code": "outreach_source_adapter",
            "input_row_count": len(raw_rows),
            "source_row_count": len(source_rows),
            "skipped_count": sum(skip_reasons.values()),
            "skip_reasons": skip_reasons,
            **skip_reasons,
        },
    }


def group_outreach_rows_by_product(
    rows: list[Mapping[str, Any]],
    *,
    trigger_date: str,
    request_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    resolved_request_payload = _request_payload_from_rows(rows, request_payload)
    for row in rows:
        product_id = _text(row.get("product_id"))
        if not product_id:
            continue
        grouped.setdefault(product_id, []).append(dict(row))
    return [
        {
            "product_id": product_id,
            "trigger_date": trigger_date,
            "query_window": build_outreach_query_window(
                product_rows, trigger_date=trigger_date, request_payload=resolved_request_payload
            ),
            "rows": [
                {
                    "source_record_id": _text(row.get("source_record_id")),
                    "creator_unique_id": _text(row.get("creator_unique_id")),
                    "existing_video_url": _text_value(row.get("existing_video_url")),
                    "existing_video_published_date": _text(
                        row.get("existing_video_published_date")
                    ),
                    "existing_play_count": _int(row.get("existing_play_count")),
                    "existing_video_count": _int(row.get("existing_video_count")),
                    "last_checked_at": _text(row.get("last_checked_at")),
                    "last_updated_at": _text(row.get("last_updated_at")),
                    "source_fields": _mapping(row.get("source_fields")),
                    "source_context": _mapping(row.get("source_context")),
                    "writeback_context": _mapping(row.get("writeback_context")),
                }
                for row in product_rows
            ],
        }
        for product_id, product_rows in grouped.items()
    ]


def build_outreach_query_window(
    rows: list[Mapping[str, Any]],
    *,
    trigger_date: str,
    request_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_request_payload = _request_payload_from_rows(rows, request_payload)
    request_window = _request_query_window(resolved_request_payload)
    if request_window:
        return request_window

    rows_for_window = [row for row in rows if not _text_value(row.get("existing_video_url"))]
    if rows_for_window:
        dates = [_parse_date(row.get("last_checked_at")) for row in rows_for_window]
        valid_dates = [item for item in dates if item is not None]
        if not valid_dates:
            return {"mode": "d_type", "d_type": 0}
        start_date = min(valid_dates).toordinal() - 1
        return _date_range_window(date.fromordinal(start_date), trigger_date)

    updated_dates = [_parse_date(row.get("last_updated_at")) for row in rows]
    valid_dates = [item for item in updated_dates if item is not None]
    if not valid_dates:
        return {"mode": "d_type", "d_type": 0}
    start_date = max(valid_dates).toordinal() - 1
    return _date_range_window(date.fromordinal(start_date), trigger_date)


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name)
        text = _text_value(value)
        if text:
            return text
    return ""


def _field_int(fields: Mapping[str, Any], *names: str) -> int:
    return _int(_field_text(fields, *names))


def _request_query_window(payload: Mapping[str, Any]) -> dict[str, Any]:
    if _coerce_bool(payload.get("force_full")):
        return {"mode": "d_type", "d_type": 0}
    start_date = _normalize_date(payload.get("start_date"))
    end_date = _normalize_date(payload.get("end_date"))
    if start_date and end_date:
        return {"mode": "date_range", "start_date": start_date, "end_date": end_date}
    return {}


def _request_payload_from_rows(
    rows: list[Mapping[str, Any]], request_payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    normalized = _normalize_request_payload(request_payload)
    if normalized:
        return normalized
    for row in rows:
        normalized = _normalize_request_payload(row.get("request_payload"))
        if normalized:
            return normalized
        normalized = _normalize_request_payload(
            _mapping(row.get("source_context")).get("request_payload")
        )
        if normalized:
            return normalized
    return {}


def _normalize_request_payload(value: Any) -> dict[str, Any]:
    payload = _mapping(value)
    nested = _mapping(payload.get("request_payload"))
    merged = {**nested, **payload}
    merged.pop("request_payload", None)
    return merged


def _date_range_window(start_date: date, trigger_date: str) -> dict[str, Any]:
    return {
        "mode": "date_range",
        "start_date": start_date.isoformat(),
        "end_date": _normalize_date(trigger_date) or trigger_date,
    }


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(
            value.get("link"), value.get("text"), value.get("value"), value.get("name")
        )
    if isinstance(value, list):
        return _first_non_empty(*(_text_value(item) for item in value))
    return _text(value)


def _normalize_date(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else ""


def _parse_date(value: Any) -> date | None:
    text = _text_value(value)
    if not text:
        return None
    timestamp_date = _parse_timestamp_date(text)
    if timestamp_date is not None:
        return timestamp_date
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_timestamp_date(text: str) -> date | None:
    try:
        timestamp = float(text)
    except ValueError:
        return None
    if timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=_feishu_date_timezone()).date()
    except (OSError, OverflowError, ValueError):
        return None


def _feishu_date_timezone() -> tzinfo:
    zone_name = os.environ.get("FEISHU_DATE_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    return [text] if text else []


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _text(value).lower() in {"1", "true", "yes", "y", "on"}


def _int(value: Any) -> int:
    text = _text_value(value).replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


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


__all__ = [
    "OUTREACH_READ_FIELD_NAMES",
    "build_outreach_query_window",
    "group_outreach_rows_by_product",
    "outreach_source_adapter",
]
