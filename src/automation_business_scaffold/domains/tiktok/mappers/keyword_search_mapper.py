from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


FASTMOSS_SEARCH_PASSTHROUGH_KEYS = (
    "fastmoss_search_response",
    "product_search_response",
    "search_response",
    "mock_fastmoss_search_response",
    "fastmoss_search_pages",
    "product_search_pages",
    "search_pages",
    "mock_fastmoss_search_pages",
)


def keyword_search_parameter_mapper(payload: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _mapping(payload.get("search_request"))
    search_query = _first_text(
        explicit.get("search_query"),
        explicit.get("keyword"),
        payload.get("search_query"),
        payload.get("search_keyword"),
        payload.get("keyword"),
    )
    filters = dict(_mapping(explicit.get("filters")) or _mapping(payload.get("filters")))
    output_conditions = dict(_mapping(explicit.get("output_conditions")) or _mapping(payload.get("output_conditions")))

    sales_7d_threshold = _first_text(explicit.get("sales_7d_threshold"), payload.get("sales_7d_threshold"))
    if sales_7d_threshold:
        business_conditions = dict(_mapping(output_conditions.get("business_conditions")))
        business_conditions.setdefault("min_day7_sold_count", sales_7d_threshold)
        output_conditions["business_conditions"] = business_conditions

    max_candidates = _positive_int(
        _first_text(explicit.get("max_candidates"), payload.get("max_candidates"), output_conditions.get("max_candidates")),
        20,
    )
    search_request = {
        **explicit,
        "stage_code": "keyword_seed_import",
        "search_mode": "keyword",
        "keyword": search_query,
        "search_query": search_query,
        "filters": filters,
        "limit": max_candidates,
        "condition_context": output_conditions,
        "output_conditions": output_conditions,
        "sort": dict(
            _mapping(explicit.get("sort"))
            or {
                "field": "day7_sold_count",
                "direction": "desc",
                "source_order": _first_text(payload.get("fastmoss_search_order"), "2,2"),
            }
        ),
        "pagination": dict(
            _mapping(explicit.get("pagination"))
            or {
                "page": _positive_int(payload.get("fastmoss_search_page"), 1),
                "page_size": _positive_int(payload.get("fastmoss_search_page_size"), 10),
                "max_pages": _positive_int(payload.get("fastmoss_search_max_pages"), 50),
                "stop_when_no_new_product": True,
            }
        ),
        "session_policy": dict(
            _mapping(explicit.get("session_policy"))
            or {
                "require_login": True,
                "degraded_preview_allowed": _bool_param(payload.get("degraded_preview_allowed"), False),
            }
        ),
        "raw_capture_policy": dict(_mapping(explicit.get("raw_capture_policy")) or {"store_raw_response": True}),
        "page_request_delay_seconds": _non_negative_float(payload.get("fastmoss_page_request_delay_seconds"), 1.0),
        "search_digest": _first_text(payload.get("search_digest"), _search_digest(search_query=search_query, filters=filters)),
    }
    for key, value in payload.items():
        if key.startswith("fastmoss_") or key in {
            "fastmoss",
            "browser_cookies",
            "region",
            *FASTMOSS_SEARCH_PASSTHROUGH_KEYS,
        }:
            search_request.setdefault(key, value)
    return {key: value for key, value in search_request.items() if value not in (None, "", [], {})}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _search_digest(*, search_query: str, filters: Mapping[str, Any]) -> str:
    raw = json.dumps({"search_query": search_query, "filters": dict(filters)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


__all__ = ["FASTMOSS_SEARCH_PASSTHROUGH_KEYS", "keyword_search_parameter_mapper"]
