from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from automation_business_scaffold.capabilities.input_sources.feishu.batch_write import (
    execute_write_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_updates import (
    execute_one_write,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)
from automation_business_scaffold.capabilities.input_sources.feishu.transport_errors import (
    classify_feishu_exception,
)
from automation_business_scaffold.infrastructure.feishu.api import FeishuAPIError


REPO_ROOT = Path(__file__).resolve().parents[1]
FEISHU_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "capabilities" / "input_sources" / "feishu"
TABLE_COMMON = FEISHU_ROOT / "table_common.py"


def _target() -> FeishuTableTarget:
    return FeishuTableTarget(
        access_token="access-token",
        app_token="app-token",
        table_id="tbl-token",
        view_id="vew-token",
        table_ref="feishu://target",
    )


def test_batch_write_creates_record_successfully() -> None:
    class FakeWriteClient:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []

        def list_all_fields(self, _app_token: str, _table_id: str) -> list[dict[str, Any]]:
            return [{"field_name": "Name", "type": 1}]

        def create_record(self, app_token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
            self.created.append({"app_token": app_token, "table_id": table_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": "rec-created"}}}

    client = FakeWriteClient()

    result = execute_write_records(
        client,
        _target(),
        [{"op": "append", "fields": {"Name": "Demo"}}],
        {"target_table_ref": "feishu://target"},
    )

    assert result["written_count"] == 1
    assert result["target_record_ids"] == ["rec-created"]
    assert client.created == [{"app_token": "app-token", "table_id": "tbl-token", "fields": {"Name": "Demo"}}]


def test_row_update_merges_partial_update_fields() -> None:
    class FakeUpdateClient:
        def __init__(self) -> None:
            self.updated: list[dict[str, Any]] = []

        def list_all_records(self, _app_token: str, _table_id: str, page_size: int = 100, view_id: str | None = None) -> list[dict[str, Any]]:
            del page_size, view_id
            return [
                {
                    "record_id": "rec-existing",
                    "fields": {
                        "Tags": ["old"],
                        "Images": [{"file_token": "old-file"}],
                        "Ignored": "manual",
                    },
                }
            ]

        def update_record(self, _app_token: str, _table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    client = FakeUpdateClient()

    raw, record_id, op = execute_one_write(
        client,
        _target(),
        {
            "op": "update",
            "record_id": "rec-existing",
            "update_excluded_fields": ["Ignored"],
            "fields": {
                "Tags": ["new"],
                "Images": [{"file_token": "new-file"}],
                "Ignored": "computed",
            },
        },
        field_schema={"Tags": {"type": 4}, "Images": {"type": 17}, "Ignored": {"type": 1}},
    )

    assert raw["code"] == 0
    assert record_id == "rec-existing"
    assert op == "update"
    assert client.updated[0]["record_id"] == "rec-existing"
    assert client.updated[0]["fields"]["Tags"] == ["old", "new"]
    assert [item["file_token"] for item in client.updated[0]["fields"]["Images"]] == ["old-file", "new-file"]
    assert "Ignored" not in client.updated[0]["fields"]


def test_write_failure_normalization_marks_rate_limit_retryable() -> None:
    class RateLimitedClient:
        def list_all_fields(self, _app_token: str, _table_id: str) -> list[dict[str, Any]]:
            return [{"field_name": "Name", "type": 1}]

        def create_record(self, _app_token: str, _table_id: str, _fields: dict[str, Any]) -> dict[str, Any]:
            raise FeishuAPIError("too many requests", status=429, code=1254290)

    result = execute_write_records(
        RateLimitedClient(),
        _target(),
        [{"op": "append", "fields": {"Name": "Demo"}}],
        {"target_table_ref": "feishu://target"},
    )
    error = classify_feishu_exception(FeishuAPIError("too many requests", status=429, code=1254290))

    assert result["failed_count"] == 1
    assert result["records"][0]["error_type"] == "rate_limited"
    assert result["records"][0]["error_code"] == "feishu_rate_limited"
    assert error.retryable is True


def test_table_common_no_longer_owns_write_side_implementation() -> None:
    source = TABLE_COMMON.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(TABLE_COMMON))
    function_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert {
        "validate_write_schema",
        "map_write_records",
        "execute_write_records",
        "classify_feishu_exception",
        "_emit_write_progress",
        "_write_progress_message",
        "_prepare_fields_for_write",
        "_normalize_write_record",
        "_execute_one_write",
        "_fields_for_update",
        "_find_existing_record_id",
        "_attachment_write_items",
        "_upload_attachment_item",
    }.isdisjoint(function_names)
    assert "from automation_business_scaffold.capabilities.input_sources.feishu.batch_write import" in source
    assert "from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import" in source
    assert (FEISHU_ROOT / "batch_write.py").is_file()
    assert (FEISHU_ROOT / "row_updates.py").is_file()
    assert (FEISHU_ROOT / "field_envelopes.py").is_file()
    assert (FEISHU_ROOT / "transport_errors.py").is_file()
