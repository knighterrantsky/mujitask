from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from automation_business_scaffold.capabilities.input_sources.feishu.pagination import (
    scan_feishu_record_pages,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_reading import (
    read_feishu_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.schema_normalization import (
    normalize_raw_rows,
    validate_read_schema,
)
from automation_business_scaffold.capabilities.input_sources.feishu.table_common import (
    FeishuCommonError,
    FeishuTableTarget,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FEISHU_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "capabilities" / "input_sources" / "feishu"
TABLE_COMMON = FEISHU_ROOT / "table_common.py"


class _FakeReadClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_records(self, app_token: str, table_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"app_token": app_token, "table_id": table_id, **kwargs})
        return {
            "data": {
                "items": [
                    {
                        "record_id": "rec-1",
                        "fields": {"Name": "Demo", "Status": "Pending"},
                        "created_time": 1,
                        "last_modified_time": 2,
                    }
                ],
                "has_more": False,
            }
        }


def _target() -> FeishuTableTarget:
    return FeishuTableTarget(
        access_token="access-token",
        app_token="app-token",
        table_id="tbl-token",
        view_id="vew-token",
        table_ref="feishu://source",
    )


def test_read_feishu_records_scans_table_successfully() -> None:
    client = _FakeReadClient()

    rows, pagination = read_feishu_records(
        client,
        _target(),
        {"pagination": {"page_size": 20}, "filter_spec": {"filter_expr": "CurrentValue.[Status] = \"Pending\""}},
    )

    assert rows[0]["record_id"] == "rec-1"
    assert pagination == {"next_page_token": "", "has_more": False}
    assert client.calls == [
        {
            "app_token": "app-token",
            "table_id": "tbl-token",
            "page_size": 20,
            "filter_expr": 'CurrentValue.[Status] = "Pending"',
            "page_token": None,
            "view_id": "vew-token",
        }
    ]


def test_scan_feishu_record_pages_traverses_cursor_until_done() -> None:
    class CursorClient:
        def __init__(self) -> None:
            self.page_tokens: list[str | None] = []

        def list_records(self, _app_token: str, _table_id: str, **kwargs: Any) -> dict[str, Any]:
            token = kwargs.get("page_token")
            self.page_tokens.append(token)
            if token is None:
                return {
                    "data": {
                        "items": [{"record_id": "rec-page-1", "fields": {}}],
                        "has_more": True,
                        "page_token": "cursor-2",
                    }
                }
            return {
                "data": {
                    "items": [{"record_id": "rec-page-2", "fields": {}}],
                    "has_more": False,
                }
            }

    client = CursorClient()

    rows, pagination = scan_feishu_record_pages(
        client,
        app_token="app-token",
        table_id="tbl-token",
        payload={"pagination": {"page_size": 1, "max_pages": 3}},
        view_id="vew-token",
    )

    assert [row["record_id"] for row in rows] == ["rec-page-1", "rec-page-2"]
    assert client.page_tokens == [None, "cursor-2"]
    assert pagination == {"next_page_token": "", "has_more": False}


def test_schema_normalization_filters_fields_and_validates_read_schema() -> None:
    records = [
        {
            "id": "rec-1",
            "fields": {"Name": "Demo", "Status": "Pending", "Ignored": "x"},
            "created_at": 10,
            "modified_time": 20,
        }
    ]

    normalized = normalize_raw_rows(records, field_names=["Name", "Status"])

    assert normalized == [
        {
            "record_id": "rec-1",
            "fields": {"Name": "Demo", "Status": "Pending"},
            "created_time": 10,
            "updated_time": 20,
        }
    ]

    class SchemaClient:
        def list_all_fields(self, _app_token: str, _table_id: str) -> list[dict[str, Any]]:
            return [{"field_name": "Name"}, {"name": "Status"}]

    validate_read_schema(SchemaClient(), _target(), ["Name", "Status"])
    with pytest.raises(FeishuCommonError) as exc_info:
        validate_read_schema(SchemaClient(), _target(), ["Missing"])
    assert exc_info.value.error_code == "feishu_field_missing"
    assert exc_info.value.details == {"missing_fields": ["Missing"], "table_ref": "feishu://source"}


def test_table_common_no_longer_owns_read_side_implementation() -> None:
    source = TABLE_COMMON.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(TABLE_COMMON))
    function_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert {
        "read_feishu_records",
        "normalize_raw_rows",
        "validate_read_schema",
        "_render_filter_expr",
    }.isdisjoint(function_names)
    assert "from automation_business_scaffold.capabilities.input_sources.feishu.row_reading import" in source
    assert "from automation_business_scaffold.capabilities.input_sources.feishu.schema_normalization import" in source
    assert (FEISHU_ROOT / "row_reading.py").is_file()
    assert (FEISHU_ROOT / "pagination.py").is_file()
    assert (FEISHU_ROOT / "schema_normalization.py").is_file()
