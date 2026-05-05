from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.capabilities.input_sources.feishu.table_common import map_write_records
from automation_business_scaffold.domains.tiktok.projections.registry import PROJECTION_MAPPER_CODES
from automation_business_scaffold.domains.tiktok.mappers.registry import SOURCE_ADAPTER_CODES
from automation_business_scaffold.domains.tiktok.mappers.feishu_influencer_source_mapper import (
    influencer_pool_source_adapter,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_selection_row_mapper import (
    selection_table_source_adapter,
)
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


def test_feishu_business_components_have_named_registries() -> None:
    assert SOURCE_ADAPTER_CODES == {
        "competitor_table_source_adapter",
        "influencer_pool_source_adapter",
        "selection_table_source_adapter",
    }
    assert PROJECTION_MAPPER_CODES == {
        "competitor_seed_projection_mapper",
        "competitor_table_projection_mapper",
        "influencer_pool_projection_mapper",
        "competitor_influencer_status_projection_mapper",
        "selection_seed_projection_mapper",
        "selection_table_projection_mapper",
    }


def test_influencer_source_adapter_honors_explicit_source_record_ids() -> None:
    rows = [
        {
            "record_id": "rec-target",
            "fields": {
                "SKU-ID": "1732183562851553564",
                "产品链接": "https://www.tiktok.com/shop/pdp/1732183562851553564",
                "达人查找状态": "已完成",
            },
        },
        {
            "record_id": "rec-other",
            "fields": {
                "SKU-ID": "1729421576968704007",
                "产品链接": "https://www.tiktok.com/shop/pdp/1729421576968704007",
                "达人查找状态": "失败重试",
            },
        },
    ]

    result = influencer_pool_source_adapter(
        rows,
        {
            "source_table_ref": "feishu://source",
            "filter_spec": {
                "source_record_ids": ["rec-target"],
                "candidate_status": ["已完成", "失败重试"],
            },
        },
    )

    assert [row["source_record_id"] for row in result["source_rows"]] == ["rec-target"]


def test_selection_source_adapter_treats_default_parent_spec_as_missing() -> None:
    rows = [
        {
            "record_id": "rec-default-parent",
            "fields": {
                "商品ID": "1732295206515806399",
                "商品链接": "https://www.tiktok.com/shop/pdp/1732295206515806399",
                "店铺名称": "Yuxilio",
                "商品标题": "Pin",
                "商品当前价格": "7.99",
                "商品评论数": "15",
                "商品评分": "5.0",
                "商品描述": "desc",
                "商品主图": [{"file_token": "main"}],
                "商品侧边栏图片": [{"file_token": "gallery"}],
                "今年总销量": "986",
                "出单种类占比图": [{"file_token": "distribution"}],
                "销量趋势图": [{"file_token": "trend"}],
                "SKU销量占比图": [{"file_token": "sku"}],
                "父体规格": "Default",
                "父体图片": [{"file_token": "parent"}],
            },
        }
    ]

    result = selection_table_source_adapter(rows, {"source_table_ref": "feishu://selection"})

    assert result["adapter_summary"]["source_row_count"] == 1
    assert result["source_rows"][0]["source_record_id"] == "rec-default-parent"


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
                                "图片": "",
                                "标题": "Demo product",
                                "节日": "",
                                "卖家": "",
                                "价格": "$9.99",
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
                                "图片": "https://cdn.example.com/987654321.jpg",
                                "标题": "Complete product",
                                "节日": "Halloween",
                                "卖家": "Demo shop",
                                "价格": "$0.99",
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
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-read",
        source_table_ref="feishu://mujitask/TK竞品收集",
        field_names=[
            "产品链接",
            "SKU-ID",
            "图片",
            "标题",
            "节日",
            "卖家",
            "价格",
            "商品状态",
            "Fastmoss价格",
            "昨日销量",
            "近7天销量",
            "近90天销量",
            "记录日期",
        ],
        filter_spec={"candidate_policy": "missing_auto_maintained_fields", "skip_product_status": ["已下架/区域不可售"]},
        adapter_code="competitor_table_source_adapter",
        snapshot_policy={"store_raw_rows": True, "raw_snapshot_namespace": "feishu/competitor/read"},
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    assert result.status == "success"
    assert result.result["raw_rows"][0]["record_id"] == "rec-1"
    assert result.result["source_rows"][0]["source_record_id"] == "rec-1"
    assert result.result["source_rows"][0]["product_identity"]["product_id"] == "123456789"
    assert result.result["source_rows"][0]["missing_auto_fields"] == [
        "图片",
        "节日",
        "卖家",
        "Fastmoss价格",
        "近7天销量",
        "记录日期",
    ]
    assert result.result["candidate_keys"] == ["product:123456789"]
    assert result.result["adapter_summary"]["skipped_complete_count"] == 1
    assert result.result["raw_snapshot_ref"].startswith("artifact://feishu/competitor/read/req-read/")


def test_feishu_table_read_falls_back_to_product_link_when_sku_id_is_not_numeric() -> None:
    payload = _table_payload(
        request_id="req-read-bad-sku",
        raw_rows=[
            {
                "record_id": "rec-bad-sku",
                "fields": {
                    "产品链接": {
                        "text": "https://www.tiktok.com/shop/pdp/1729421577515077639",
                        "link": "https://www.tiktok.com/shop/pdp/1729421577515077639",
                    },
                    "SKU-ID": "复活节",
                    "图片": "",
                    "标题": "Demo product",
                    "节日": "情人节",
                    "卖家": "Demo shop",
                    "价格": "24.99",
                    "商品状态": "",
                    "Fastmoss价格": "",
                    "昨日销量": "0",
                    "近7天销量": "",
                    "近90天销量": "4895",
                    "记录日期": "",
                },
            }
        ],
        filter_spec={"candidate_policy": "missing_auto_maintained_fields"},
        adapter_code="competitor_table_source_adapter",
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    identity = result.result["source_rows"][0]["product_identity"]
    assert result.status == "success"
    assert identity["product_id"] == "1729421577515077639"
    assert identity["fastmoss_product_url"] == "https://www.fastmoss.com/zh/e-commerce/detail/1729421577515077639"
    assert result.result["candidate_keys"] == ["product:1729421577515077639"]


def test_feishu_table_read_can_locate_single_competitor_row_by_product_url() -> None:
    payload = _table_payload(
        request_id="req-read-by-url",
        raw_rows=[
            {
                "record_id": "rec-other",
                "fields": {
                    "产品链接": "https://www.tiktok.com/shop/pdp/111111111",
                    "SKU-ID": "111111111",
                    "商品状态": "",
                },
            },
            {
                "record_id": "rec-target",
                "fields": {
                    "产品链接": {"text": "target", "link": "https://www.tiktok.com/shop/pdp/1732323487665722003"},
                    "SKU-ID": "1732323487665722003",
                    "商品状态": "已下架/区域不可售",
                    "标题": "Complete product",
                    "图片": "https://cdn.example.com/1732323487665722003.jpg",
                    "节日": "Easter",
                    "卖家": "Demo shop",
                    "价格": "$19.99",
                    "Fastmoss价格": "$18.88",
                    "昨日销量": "10",
                    "近7天销量": "30",
                    "近90天销量": "99",
                    "记录日期": "2026-04-25",
                },
            },
        ],
        product_url="https://www.tiktok.com/shop/pdp/1732323487665722003",
        filter_spec={},
        adapter_code="competitor_table_source_adapter",
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    assert result.status == "success"
    assert [row["source_record_id"] for row in result.result["source_rows"]] == ["rec-target"]
    assert result.result["source_rows"][0]["product_identity"]["product_id"] == "1732323487665722003"
    assert result.result["adapter_summary"]["lookup_status"] == "matched"
    assert result.result["adapter_summary"]["matched_row_count"] == 1


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
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
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
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
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
                    "商品状态": "已下架/区域不可售",
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
    assert fields["商品状态"] == "已下架/区域不可售"
    assert fields.get("价格") is None


def test_competitor_projection_keeps_non_terminal_product_status_out_of_auto_writeback() -> None:
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
                    "商品状态": "在售",
                },
                "source_fields": {"SKU-ID": "123456789", "产品链接": "https://www.tiktok.com/shop/pdp/123456789"},
            }
        ],
    )

    mapped = map_write_records(payload)

    assert mapped == []


def test_competitor_projection_keeps_image_url_as_raw_link() -> None:
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
                    "图片": "https://p16-oec.example.com/image.webp?from=2378011839",
                },
                "source_fields": {"图片": ""},
            }
        ],
    )

    records = map_write_records(payload)

    assert records[0]["fields"]["图片"] == {
        "text": "https://p16-oec.example.com/image.webp?from=2378011839",
        "link": "https://p16-oec.example.com/image.webp?from=2378011839",
    }


def test_feishu_table_write_uploads_attachment_file_before_write(monkeypatch, tmp_path) -> None:
    class FakeClient:
        updated: list[dict[str, Any]] = []
        uploads: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [
                {"field_name": "图片", "type": 17},
                {"field_name": "记录日期", "type": 5},
            ]

        def upload_media(self, file_name, file_data, parent_type="bitable_file", parent_node="", extra=None):
            self.uploads.append(
                {
                    "file_name": file_name,
                    "file_data": file_data,
                    "parent_type": parent_type,
                    "parent_node": parent_node,
                    "extra": extra,
                }
            )
            return "file-token-main"

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    FakeClient.uploads = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    image_path = tmp_path / "main.webp"
    image_path.write_bytes(b"webp-bytes")
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK竞品收集",
        records=[
            {
                "op": "update",
                "record_id": "rec-1",
                "fields": {
                    "图片": {
                        "local_path": str(image_path),
                        "file_name": "main.webp",
                        "mime_type": "image/webp",
                    },
                    "记录日期": "2026-04-24",
                },
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert FakeClient.uploads[0]["file_name"] == "main.webp"
    assert FakeClient.uploads[0]["file_data"] == b"webp-bytes"
    assert FakeClient.updated[0]["fields"] == {
        "图片": [{"file_token": "file-token-main"}],
        "记录日期": 1776960000000,
    }
    assert result.result["records"][0]["fields_written"] == ["图片", "记录日期"]


def test_feishu_table_write_uploads_tiktok_uri_attachment_before_write(monkeypatch, tmp_path) -> None:
    class FakeClient:
        updated: list[dict[str, Any]] = []
        uploads: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [{"field_name": "图片", "type": 17}]

        def upload_media(self, file_name, file_data, parent_type="bitable_file", parent_node="", extra=None):
            self.uploads.append(
                {
                    "file_name": file_name,
                    "file_data": file_data,
                    "parent_type": parent_type,
                    "parent_node": parent_node,
                    "extra": extra,
                }
            )
            return "feishu-file-token"

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    FakeClient.uploads = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    image_path = tmp_path / "main.webp"
    image_path.write_bytes(b"webp-bytes")
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK竞品收集",
        records=[
            {
                "op": "update",
                "record_id": "rec-1",
                "fields": {
                    "图片": {
                        "file_token": "tiktok_uri:tos-useast8-i-example/image",
                        "local_path": str(image_path),
                        "file_name": "main.webp",
                        "mime_type": "image/webp",
                    },
                },
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert FakeClient.uploads[0]["file_name"] == "main.webp"
    assert FakeClient.updated[0]["fields"] == {
        "图片": [{"file_token": "feishu-file-token"}],
    }


def test_feishu_table_write_uses_remote_image_name_for_attachment_upload(monkeypatch) -> None:
    class FakeResponse:
        content = b"jpeg-bytes"
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            return None

    class FakeClient:
        updated: list[dict[str, Any]] = []
        uploads: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [{"field_name": "达人头像", "type": 17}]

        def upload_media(self, file_name, file_data, parent_type="bitable_file", parent_node="", extra=None):
            self.uploads.append({"file_name": file_name, "file_data": file_data})
            return "avatar-file-token"

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    FakeClient.uploads = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK达人池",
        records=[
            {
                "op": "update",
                "record_id": "rec-1",
                "fields": {
                    "达人头像": {"url": "https://s.500fd.com/tt_author/avatar~c5_300x300.jpeg"},
                },
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert FakeClient.uploads[0]["file_name"] == "avatar~c5_300x300.jpeg"
    assert FakeClient.updated[0]["fields"]["达人头像"] == [{"file_token": "avatar-file-token"}]


def test_competitor_seed_projection_mapper_creates_keyword_seed_row(monkeypatch) -> None:
    class FakeClient:
        created: list[dict[str, Any]] = []
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

    FakeClient.created = []
    FakeClient.rows = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-keyword-seed",
        target_table_ref="feishu://mujitask/TK竞品收集",
        mapper_code="competitor_seed_projection_mapper",
        write_mode="insert_if_absent",
        records=[
            {
                "product_id": "123456789",
                "search_query": "water bottle",
                "search_rank": 1,
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert result.result["target_record_ids"] == ["rec-1"]
    assert FakeClient.created[0]["fields"] == {
        "SKU-ID": "123456789",
        "产品链接": {
            "text": "https://www.tiktok.com/shop/pdp/123456789",
            "link": "https://www.tiktok.com/shop/pdp/123456789",
        },
        "备注": "通过搜索关键字：water bottle",
    }
    assert result.result["records"][0]["op"] == "append"


def test_selection_seed_projection_mapper_creates_keyword_seed_row(monkeypatch) -> None:
    class FakeClient:
        created: list[dict[str, Any]] = []
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

    FakeClient.created = []
    FakeClient.rows = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-selection-keyword-seed",
        target_table_ref="feishu://mujitask/TK选品收集",
        mapper_code="selection_seed_projection_mapper",
        write_mode="insert_if_absent",
        records=[
            {
                "product_id": "123456789",
                "search_query": "water bottle",
                "search_rank": 1,
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert result.result["target_record_ids"] == ["rec-1"]
    assert FakeClient.created[0]["fields"] == {
        "商品ID": "123456789",
        "商品链接": {
            "text": "https://www.tiktok.com/shop/pdp/123456789",
            "link": "https://www.tiktok.com/shop/pdp/123456789",
        },
        "关键词": "water bottle",
        "备注": "通过搜索关键字：water bottle",
        "记录日期": FakeClient.created[0]["fields"]["记录日期"],
    }
    assert result.result["records"][0]["op"] == "append"


def test_selection_seed_projection_mapper_skips_existing_product_without_rewrite(monkeypatch) -> None:
    class FakeClient:
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, app_token, table_id, page_size=100, view_id=None):
            return list(self.rows)

        def create_record(self, app_token, table_id, fields):
            self.created.append({"fields": dict(fields)})
            raise AssertionError("existing selection seed rows must not be recreated")

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            raise AssertionError("existing selection seed rows must not be updated")

    FakeClient.created = []
    FakeClient.updated = []
    FakeClient.rows = [
        {
            "record_id": "rec-existing",
            "fields": {
                "商品ID": "123456789",
                "备注": "manual note",
            },
        }
    ]
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-selection-keyword-seed",
        target_table_ref="feishu://mujitask/TK选品收集",
        mapper_code="selection_seed_projection_mapper",
        write_mode="insert_if_absent",
        records=[
            {
                "product_id": "123456789",
                "product_url": "https://www.fastmoss.com/zh/e-commerce/detail/123456789",
                "search_query": "water bottle",
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "skipped"
    assert FakeClient.created == []
    assert FakeClient.updated == []
    assert result.result["written_count"] == 0
    assert result.result["skipped_count"] == 1
    assert result.result["records"][0]["record_id"] == "rec-existing"
    assert result.result["records"][0]["op"] == "skip_existing"


def test_competitor_seed_projection_mapper_skips_existing_product_without_rewrite(monkeypatch) -> None:
    class FakeClient:
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, app_token, table_id, page_size=100, view_id=None):
            return list(self.rows)

        def create_record(self, app_token, table_id, fields):
            self.created.append({"fields": dict(fields)})
            raise AssertionError("existing keyword seed rows must not be recreated")

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            raise AssertionError("existing keyword seed rows must not be updated")

    FakeClient.created = []
    FakeClient.updated = []
    FakeClient.rows = [
        {
            "record_id": "rec-existing",
            "fields": {
                "SKU-ID": "123456789",
                "备注": "manual note",
            },
        }
    ]
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        request_id="req-keyword-seed",
        target_table_ref="feishu://mujitask/TK竞品收集",
        mapper_code="competitor_seed_projection_mapper",
        write_mode="insert_if_absent",
        records=[
            {
                "product_id": "123456789",
                "product_url": "https://www.fastmoss.com/zh/e-commerce/detail/123456789",
                "search_query": "water bottle",
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "skipped"
    assert FakeClient.created == []
    assert FakeClient.updated == []
    assert result.result["written_count"] == 0
    assert result.result["skipped_count"] == 1
    assert result.result["records"][0]["record_id"] == "rec-existing"
    assert result.result["records"][0]["op"] == "skip_existing"


def test_feishu_table_write_classifies_schema_missing_before_write(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [{"field_name": "SKU-ID"}]

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
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


def test_feishu_table_write_filters_and_merges_existing_multi_select_options(monkeypatch) -> None:
    class FakeClient:
        updated: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [
                {
                    "field_name": "达人ID",
                    "type": 1,
                },
                {
                    "field_name": "合作店铺",
                    "type": 4,
                    "property": {
                        "options": [
                            {"name": "Existing Shop"},
                            {"name": "Allowed Shop"},
                        ]
                    },
                },
            ]

        def list_all_records(self, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            del app_token, table_id, page_size, filter_expr, view_id
            return [
                {
                    "record_id": "rec-creator",
                    "fields": {
                        "达人ID": "creator-1",
                        "合作店铺": ["Existing Shop"],
                    },
                }
            ]

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK达人池",
        write_mode="upsert",
        records=[
            {
                "op": "upsert",
                "upsert_key": {"field": "达人ID", "value": "creator-1"},
                "fields": {
                    "达人ID": "creator-1",
                    "合作店铺": ["Allowed Shop", "Not Configured Shop"],
                },
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert FakeClient.updated[0]["fields"]["合作店铺"] == ["Existing Shop", "Allowed Shop"]


def test_feishu_table_write_replaces_configured_attachment_field_on_update(monkeypatch) -> None:
    class FakeClient:
        updated: list[dict[str, Any]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_fields(self, app_token, table_id):
            return [{"field_name": "达人头像", "type": 17}, {"field_name": "达人ID", "type": 1}]

        def list_all_records(self, app_token, table_id, page_size=100, filter_expr=None, view_id=None):
            del app_token, table_id, page_size, filter_expr, view_id
            return [
                {
                    "record_id": "rec-creator",
                    "fields": {
                        "达人ID": "creator-1",
                        "达人头像": [{"file_token": "old-bin-token"}],
                    },
                }
            ]

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.updated = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(
        target_table_ref="feishu://mujitask/TK达人池",
        write_mode="upsert",
        records=[
            {
                "op": "upsert",
                "upsert_key": {"field": "达人ID", "value": "creator-1"},
                "update_replace_fields": ["达人头像"],
                "fields": {
                    "达人ID": "creator-1",
                    "达人头像": [{"file_token": "new-image-token"}],
                },
            }
        ],
    )

    result = build_bound_api_handler_registry().dispatch("feishu_table_write", _context("feishu_table_write", payload))

    assert result.status == "success"
    assert FakeClient.updated[0]["fields"]["达人头像"] == [{"file_token": "new-image-token"}]


def test_competitor_influencer_status_writeback_does_not_touch_remark() -> None:
    records = map_write_records(
        {
            "mapper_code": "competitor_influencer_status_projection_mapper",
            "write_mode": "upsert",
            "records": [
                {
                    "source_record_id": "rec-1",
                    "product_id": "1732183562851553564",
                    "product_key": "rec-1:1732183562851553564",
                    "influencer_sync_status": "success",
                    "creator_detail_failed_count": 0,
                    "influencer_write_success_count": 3,
                    "warnings": [],
                }
            ],
        }
    )

    assert records[0]["fields"] == {"达人查找状态": "已完成"}


def test_feishu_table_read_classifies_rate_limit_as_retryable(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_records(self, *args, **kwargs):
            raise FeishuAPIError("too many requests", status=429, code=1254290)

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    payload = _table_payload(source_table_ref="feishu://mujitask/TK竞品收集")

    result = build_bound_api_handler_registry().dispatch("feishu_table_read", _context("feishu_table_read", payload))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "rate_limited"
    assert result.error.error_code == "feishu_rate_limited"
    assert result.error.retryable is True
