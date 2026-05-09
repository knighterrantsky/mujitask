from __future__ import annotations

from typing import Any, Mapping


def scan_feishu_record_pages(
    client: Any,
    *,
    app_token: str,
    table_id: str,
    payload: Mapping[str, Any],
    view_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pagination = _mapping(payload.get("pagination"))
    page_size = _coerce_int(pagination.get("page_size"), default=100, minimum=1, maximum=500)
    max_pages = _coerce_int(pagination.get("max_pages"), default=20, minimum=1, maximum=1000)
    page_token = _text(pagination.get("cursor") or pagination.get("page_token"))
    filter_expr = render_filter_expr(payload.get("filter_spec"))

    rows: list[dict[str, Any]] = []
    has_more = False
    next_page_token = ""
    for _ in range(max_pages):
        response = client.list_records(
            app_token,
            table_id,
            page_size=page_size,
            filter_expr=filter_expr or None,
            page_token=page_token or None,
            view_id=view_id or None,
        )
        data = _mapping(response.get("data"))
        rows.extend(_mapping_list(data.get("items")))
        has_more = bool(data.get("has_more"))
        next_page_token = _text(data.get("page_token") or data.get("next_page_token"))
        if not has_more or not next_page_token:
            break
        page_token = next_page_token

    return rows, {"next_page_token": next_page_token if has_more else "", "has_more": has_more}


def render_filter_expr(filter_spec: Any) -> str:
    if isinstance(filter_spec, str):
        return filter_spec.strip()
    spec = _mapping(filter_spec)
    return _first_non_empty(spec.get("filter_expr"), spec.get("filter"))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)
