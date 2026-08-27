from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OUTREACH_READ_FIELD_NAMES = (
    "采集标签",
    "SKUID",
    "达人ID",
    "视频链接",
    "视频发布时间",
    "检查时间",
    "播放量(W)",
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
    for row in raw_rows:
        record_id = _text(row.get("record_id") or row.get("id"))
        raw_fields = _mapping(row.get("fields"))
        fields = {
            field_name: raw_fields[field_name]
            for field_name in OUTREACH_READ_FIELD_NAMES
            if field_name in raw_fields
        }
        if _field_text(fields, "采集标签") != "T":
            raise ValueError(
                "outreach_filter_contract_violation: expected 采集标签=T for every returned row."
            )
        product_id = _field_text(fields, "SKUID")
        creator_unique_id = _field_text(fields, "达人ID")
        existing_video_url = _field_text(fields, "视频链接")
        if not product_id:
            skip_reasons["missing_product_id"] += 1
            continue
        if not creator_unique_id:
            skip_reasons["missing_creator_unique_id"] += 1
            continue
        existing_video_published_date = _normalize_date(
            _field_text(fields, "视频发布时间")
        )
        existing_play_count = _field_optional_number(fields, "播放量(W)")
        existing_video_count = _field_int(fields, "视频数量")
        last_checked_at = _normalize_date(_field_text(fields, "检查时间"))
        last_updated_at = _normalize_date(_field_text(fields, "更新时间"))
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
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        product_id = _text(row.get("product_id"))
        if not product_id:
            continue
        grouped.setdefault(product_id, []).append(dict(row))
    return [
        {
            "product_id": product_id,
            "trigger_date": trigger_date,
            "query_window": build_outreach_query_window(product_rows, trigger_date=trigger_date),
            "rows": [
                {
                    "source_record_id": _text(row.get("source_record_id")),
                    "creator_unique_id": _text(row.get("creator_unique_id")),
                    "existing_video_url": _text_value(row.get("existing_video_url")),
                    "existing_video_published_date": _text(
                        row.get("existing_video_published_date")
                    ),
                    "existing_play_count": _optional_number(row.get("existing_play_count")),
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
) -> dict[str, Any]:
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


def _field_optional_number(fields: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name not in fields:
            continue
        text = _text_value(fields.get(name))
        if not text:
            return None
        return _number(text)
    return None


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _number(value)


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


def _int(value: Any) -> int:
    text = _text_value(value).replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _number(value: Any) -> float | None:
    text = _text_value(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


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
