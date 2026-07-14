from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.pagination import (
    scan_feishu_record_pages,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)


def read_feishu_records(
    client: Any,
    target: FeishuTableTarget,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inline_rows = _mapping_list(payload.get("raw_rows")) or _mapping_list(payload.get("records"))
    if inline_rows:
        return inline_rows, {"next_page_token": "", "has_more": False, "source": "inline"}

    source_record_id = _text(payload.get("source_record_id"))
    if source_record_id:
        response = client.get_record(target.app_token, target.table_id, source_record_id)
        data = _mapping(response.get("data"))
        record = _mapping(data.get("record")) or _mapping(response.get("record"))
        if not record and "fields" in data:
            record = data
        if record and not _text(record.get("record_id") or record.get("id")):
            record["record_id"] = source_record_id
        return (
            [record] if record else [],
            {"next_page_token": "", "has_more": False, "source": "record_id"},
        )

    view_id = _text(
        _mapping(payload.get("feishu_table")).get("view_id")
        or target.view_id
        or payload.get("view_id")
        or payload.get("view_ref")
    )
    return scan_feishu_record_pages(
        client,
        app_token=target.app_token,
        table_id=target.table_id,
        payload=payload,
        view_id=view_id,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return str(value or "").strip()
