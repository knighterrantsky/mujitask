from __future__ import annotations

from typing import Any

from automation_business_scaffold.business.handlers.api.registry import build_bound_api_handler_registry
from automation_business_scaffold.business.handlers.contract import HandlerContext
from automation_business_scaffold.infrastructure.feishu.api import FeishuAPIError


def _context(handler_code: str, payload: dict[str, Any]) -> HandlerContext:
    return HandlerContext(
        request_id="req-feishu-common",
        job_id=f"job-{handler_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        job_code=handler_code,
    )


def _table_payload(**extra: Any) -> dict[str, Any]:
    return {
        "feishu_table": {"app_token": "app-token", "table_id": "tbl-token", "view_id": "vew-token"},
        "access_token": "access-token",
        **extra,
    }


def test_bound_api_registry_includes_feishu_common_handlers() -> None:
    registry = build_bound_api_handler_registry()

    assert registry.get("feishu_table_read").is_bound
    assert registry.get("feishu_table_write").is_bound


def test_feishu_table_read_adapts_competitor_source_rows(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_records(self, app_token, table_id, **kwargs):
            assert app_token == "app-token"
            assert table_id == "tbl-token"
            assert kwargs["view_id"] == "vew-token"
            return {
                "data": {
                    "items": [
                        {
                            "record_id": "rec-1",
                            "fields": {
                                "产品链接": {"text": "p", "link": "https://www.tiktok.com/shop/pdp/123456789"},
                                "SKU-ID": "123456789",
                                "商品状态": "",
                                "Fastmoss价格": "",
                                "昨日销量": "2",
                                "近7天销量": "",
                                "近90天销量": "9",
                                "记录日期": "",
                            },
                            "created_time": 1,
                            "last_modified_time": 2,
                        },
                        {
                            "record_id": "rec-complete",
                            "fields": {
                                "产品链接": "https://www.tiktok.com/shop/pdp/987654321",
                                "SKU-ID": "987654321",
                                "商品状态": "",
                                "Fastmoss价格": "$1",
                                "昨日销量": "2",
                                "近7天销量": "7",
                                "近90天销量": "9",
                                "记录日期": "2026-04-24",
                            },
                        },
                    ],
                    "has_more": False,
                }
            }

    monkeypatch.setattr(
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-read",
        source_table_ref="feishu://mujitask/TK竞品收集",
        field_names=["产品链接", "SKU-ID", "商品状态", "Fastmoss价格", "昨日销量", "近7天销量", "近90天销量", "记录日期"],
        filter_spec={"candidate_policy": "missing_auto_maintained_fields", "skip_product_status": ["已下架/区域不可售"]},
        adapter_code="competitor_table_source_adapter",
        snapshot_policy={"store_raw_rows": True, "raw_snapshot_namespace": "feishu/competitor/read"},
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    assert result.status == "success"
    assert result.result["raw_rows"][0]["record_id"] == "rec-1"
    assert result.result["source_rows"][0]["source_record_id"] == "rec-1"
    assert result.result["source_rows"][0]["product_identity"]["product_id"] == "123456789"
    assert result.result["candidate_keys"] == ["product:123456789"]
    assert result.result["adapter_summary"]["skipped_complete_count"] == 1
    assert result.result["raw_snapshot_ref"].startswith("artifact://feishu/competitor/read/req-read/")


def test_feishu_table_write_upsert_is_idempotent_on_upsert_key(monkeypatch) -> None:
    class FakeClient:
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, app_token, table_id, page_size=100, view_id=None):
            return list(self.rows)

        def create_record(self, app_token, table_id, fields):
            record_id = f"rec-{len(self.rows) + 1}"
            self.created.append({"record_id": record_id, "fields": dict(fields)})
            self.rows.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            for row in self.rows:
                if row["record_id"] == record_id:
                    row["fields"].update(fields)
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.created = []
    FakeClient.updated = []
    FakeClient.rows = []
    monkeypatch.setattr(
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-write",
        target_table_ref="feishu://mujitask/TK竞品收集",
        write_mode="batch_upsert",
        records=[
            {
                "op": "upsert",
                "business_entity_key": "product:123456789",
                "upsert_key": {"field": "SKU-ID", "value": "123456789"},
                "fields": {"SKU-ID": "123456789", "备注": "first write"},
            }
        ],
    )

    registry = build_bound_api_handler_registry()
    first = registry.dispatch("feishu_table_write", _context("feishu_table_write", payload))
    second = registry.dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert first.status == "success"
    assert second.status == "success"
    assert len(FakeClient.created) == 1
    assert len(FakeClient.updated) == 1
    assert second.result["records"][0]["op"] == "update"
    assert second.result["target_record_ids"] == ["rec-1"]


def test_feishu_table_write_maps_competitor_projection_without_overwriting_manual_fields(monkeypatch) -> None:
    class FakeClient:
        updated: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = [
            {
                "record_id": "rec-1",
                "fields": {
                    "产品链接": {"text": "https://www.tiktok.com/shop/pdp/123456789", "link": "https://www.tiktok.com/shop/pdp/123456789"},
                    "SKU-ID": "123456789",
                    "价格": "$9.99",
                    "标题": "",
                    "Fastmoss价格": "",
                    "近7天销量": "",
                    "记录日期": "",
                },
            }
        ]

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    monkeypatch.setattr(
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK竞品收集",
        write_mode="upsert",
        mapper_code="competitor_table_projection_mapper",
        records=[
            {
                "source_record_id": "rec-1",
                "product_id": "123456789",
                "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                "projection_fields": {
                    "SKU-ID": "123456789",
                    "产品链接": "https://www.tiktok.com/shop/pdp/123456789",
                    "标题": "Graduation Candy Boxes",
                    "价格": "$14.50",
                    "Fastmoss价格": "$14.50",
                    "近7天销量": "412",
                },
                "source_fields": FakeClient.rows[0]["fields"],
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    fields = FakeClient.updated[0]["fields"]
    assert fields["标题"] == "Graduation Candy Boxes"
    assert fields["Fastmoss价格"] == "$14.50"
    assert fields["近7天销量"] == "412"
    assert "记录日期" in fields
    assert "SKU-ID" not in fields
    assert "产品链接" not in fields
    assert fields.get("价格") is None


def test_feishu_table_write_classifies_schema_missing_before_write(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [{"field_name": "SKU-ID"}]

    monkeypatch.setattr(
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK竞品收集",
        write_policy={"validate_schema": True},
        records=[
            {
                "op": "update",
                "record_id": "rec-1",
                "fields": {"SKU-ID": "123456789", "备注": "missing in schema"},
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "schema_missing"
    assert result.error.retryable is False
    assert result.error.details["missing_fields"] == ["备注"]


def test_feishu_table_read_classifies_rate_limit_as_retryable(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_records(self, *args, **kwargs):
            raise FeishuAPIError("too many requests", status=429, code=1254290)

    monkeypatch.setattr(
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(source_table_ref="feishu://mujitask/TK竞品收集")

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "rate_limited"
    assert result.error.error_code == "feishu_rate_limited"
    assert result.error.retryable is True
