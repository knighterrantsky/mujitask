from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any


OUTREACH_READ_FIELD_NAMES = ("SKUID", "达人ID", "视频链接", "视频发布时间", "检查时间")


def outreach_source_adapter(raw_rows: list[Mapping[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    source_rows: list[dict[str, Any]] = []
    skip_reasons = {
        "missing_product_id": 0,
        "missing_creator_unique_id": 0,
        "already_has_video_url": 0,
    }
    source_record_ids = set(_list_text(payload.get("source_record_ids")))

    current_product_id = ""
    for row in raw_rows:
        record_id = _text(row.get("record_id") or row.get("id"))
        if source_record_ids and record_id not in source_record_ids:
            continue
        fields = _mapping(row.get("fields"))
        product_id = _field_text(fields, "SKUID", "sku_id", "product_id")
        if product_id:
            current_product_id = product_id
        else:
            product_id = current_product_id
        creator_unique_id = _field_text(fields, "达人ID", "creator_unique_id", "unique_id")
        existing_video_url = _field_text(fields, "视频链接", "video_url")
        if not product_id:
            skip_reasons["missing_product_id"] += 1
            continue
        if not creator_unique_id:
            skip_reasons["missing_creator_unique_id"] += 1
            continue
        if existing_video_url:
            skip_reasons["already_has_video_url"] += 1
            continue
        last_checked_at = _normalize_date(_field_text(fields, "检查时间", "last_checked_at"))
        source_rows.append(
            {
                "source_record_id": record_id,
                "business_key": f"outreach:{record_id}",
                "product_id": product_id,
                "creator_unique_id": creator_unique_id,
                "existing_video_url": existing_video_url,
                "last_checked_at": last_checked_at,
                "writeback_context": {
                    "table_code": "tk_influencer_outreach",
                    "target_table_ref": _first_non_empty(payload.get("target_table_ref"), payload.get("source_table_ref")),
                    "record_id": record_id,
                },
                "source_context": {
                    "source_record_id": record_id,
                    "source_table_ref": _text(payload.get("source_table_ref")),
                    "source_fields": fields,
                },
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


def group_outreach_rows_by_product(rows: list[Mapping[str, Any]], *, trigger_date: str) -> list[dict[str, Any]]:
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
                    "last_checked_at": _text(row.get("last_checked_at")),
                    "writeback_context": _mapping(row.get("writeback_context")),
                }
                for row in product_rows
            ],
        }
        for product_id, product_rows in grouped.items()
    ]


def build_outreach_query_window(rows: list[Mapping[str, Any]], *, trigger_date: str) -> dict[str, Any]:
    checked_dates = [_parse_date(row.get("last_checked_at")) for row in rows]
    valid_dates = [item for item in checked_dates if item is not None]
    if not valid_dates:
        return {"mode": "d_type", "d_type": 0}
    start_date = min(valid_dates).toordinal() - 1
    return {
        "mode": "date_range",
        "start_date": date.fromordinal(start_date).isoformat(),
        "end_date": _normalize_date(trigger_date) or trigger_date,
    }


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name)
        text = _text_value(value)
        if text:
            return text
    return ""


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
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
