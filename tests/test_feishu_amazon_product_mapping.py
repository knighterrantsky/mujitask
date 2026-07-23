from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    extract_amazon_product_capture,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    map_write_records,
    normalize_write_record,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_reading import (
    read_feishu_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_updates import (
    merge_update_fields,
)
from automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes import (
    attachment_file_token_ref_items,
)
from automation_business_scaffold.capabilities.input_sources.feishu.targets import (
    FeishuTableTarget,
)
from automation_business_scaffold.contracts.handler.domain_mapping import adapt_source_rows
from automation_business_scaffold.contracts.handler.api import (
    build_bound_api_handler_registry,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.domains.amazon.mappers.feishu_product_source_mapper import (
    AMAZON_PRODUCT_SOURCE_FIELDS,
    amazon_product_batch_source_adapter,
    amazon_product_table_source_adapter,
)
from automation_business_scaffold.domains.amazon.projections.feishu_product_projection import (
    AMAZON_PRODUCT_FEISHU_WRITE_FIELDS,
    AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS,
    AMAZON_PRODUCT_PROJECTION_FIELDS,
    amazon_product_projection_mapper,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
OBSERVED_AT = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _capture(name: str = "product_detail_child.html", *, asin: str = "B0CHILD001"):
    return extract_amazon_product_capture(
        _fixture(name),
        requested_asin=asin,
        resolved_url=f"https://www.amazon.com/dp/{asin}",
        observed_at=OBSERVED_AT,
    )


def _source_payload(**overrides):
    return {
        "source_table_ref": "AMAZON_PRODUCTS",
        "target_table_ref": "AMAZON_PRODUCTS",
        "source_record_id": "rec-amazon-1",
        **overrides,
    }


def _raw_row(*, record_id: str = "rec-amazon-1", **field_overrides):
    return {
        "record_id": record_id,
        "fields": {
            "ASIN": " b0child001 ",
            "商品链接": {
                "text": "Amazon product",
                "link": "https://www.amazon.com/example/dp/B0CHILD001?tag=old-20",
            },
            "强制刷新": True,
            "采集状态": "pending",
            "业务备注": "must remain business-owned",
            **field_overrides,
        },
    }


def _materialized_media(capture):
    refs = []
    main_url = capture["media"]["main_image"]["url"]
    refs.append(
        {
            "source_url": main_url,
            "media_role": "main_image",
            "position": 0,
            "sync_state": "uploaded",
            "bucket": "runtime-artifacts",
            "object_key": "mujitask/local/product-media/amazon/us/B0CHILD001/main.jpg",
            "remote_uri": (
                "s3://runtime-artifacts/"
                "mujitask/local/product-media/amazon/us/B0CHILD001/main.jpg"
            ),
            "content_digest": "a" * 64,
            "source_path": "/tmp/amazon-main.jpg",
            "file_name": "main.jpg",
            "mime_type": "image/jpeg",
        }
    )
    for position, image in enumerate(capture["media"]["gallery_images"]):
        if image["url"] == main_url:
            continue
        refs.append(
            {
                "source_url": image["url"],
                "media_role": "gallery_image",
                "position": position,
                "sync_state": "uploaded",
                "bucket": "runtime-artifacts",
                "object_key": (
                    "mujitask/local/product-media/amazon/us/B0CHILD001/"
                    f"gallery-{position}.jpg"
                ),
                "remote_uri": (
                    "s3://runtime-artifacts/mujitask/local/product-media/amazon/us/"
                    f"B0CHILD001/gallery-{position}.jpg"
                ),
                "content_digest": f"{position + 1:064x}",
                "source_path": f"/tmp/amazon-gallery-{position}.jpg",
                "file_name": f"gallery-{position}.jpg",
                "mime_type": "image/jpeg",
            }
        )
    return refs


def _projection_record(capture=None, **overrides):
    capture = capture or _capture()
    record = {
        "source_record_id": "rec-amazon-1",
        "projection_facts": {
            "source_record_id": "rec-amazon-1",
            "requested_asin": capture["requested_asin"],
            "resolved_asin": capture["resolved_asin"],
            "canonical_url": capture["canonical_url"],
            "captured_at": capture["captured_at"],
            "collection_status": capture["collection_status"],
            "product": capture["product"],
            "commerce": capture["commerce"],
            "variants": capture["variants"],
            "rankings": capture["rankings"],
            "media": capture["media"],
            "field_evidence": capture["field_evidence"],
        },
        "materialized_media_assets": [],
    }
    if "materialized_media_assets" not in overrides and capture["media"].get("main_image"):
        record["materialized_media_assets"] = _materialized_media(capture)
    record.update(overrides)
    return record


def test_amazon_mapping_modules_declare_complete_owned_field_sets() -> None:
    assert AMAZON_PRODUCT_SOURCE_FIELDS == (
        "ASIN",
        "采集标签",
        "商品链接",
        "强制刷新",
        "采集状态",
    )
    assert set(AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS) == {
        "ASIN",
        "来源关键词",
        "强制刷新",
    }
    assert AMAZON_PRODUCT_FEISHU_WRITE_FIELDS == (
        "主图",
        "侧边栏图片",
        "30天购买人数",
        "送达日期",
        "包装规格",
        "促销活动记录",
    )
    assert set(AMAZON_PRODUCT_PROJECTION_FIELDS) == {
        "商品链接",
        "采集状态",
        "上次采集时间",
        "采集错误",
        "标题",
        "品牌",
        "类目路径",
        "卖点",
        "描述",
        "主图",
        "侧边栏图片",
        "当前价格",
        "原价",
        "币种",
        "评分",
        "评论数",
        "30天购买人数",
        "库存状态",
        "Parent ASIN",
        "Child ASIN列表",
        "变体属性",
        "卖家",
        "配送方式",
        "送达日期",
        "包装规格",
        "Buy Box卖家",
        "Buy Box价格",
        "优惠券",
        "促销活动记录",
        "BSR排名",
        "技术参数",
        "页面ASIN",
        "字段完整度",
    }


def test_source_adapter_selects_exact_record_and_normalizes_asin_identity() -> None:
    result = amazon_product_table_source_adapter(
        [_raw_row(record_id="ignored"), _raw_row()],
        _source_payload(),
    )

    assert result["candidate_keys"] == ["amazon:US:B0CHILD001"]
    assert result["adapter_summary"] == {
        "adapter_code": "amazon_product_table_source_adapter",
        "input_row_count": 2,
        "source_row_count": 1,
        "matched_row_count": 1,
        "lookup_status": "matched",
        "invalid_asin_count": 0,
        "identity_mismatch_count": 0,
        "unsupported_marketplace_count": 0,
    }
    row = result["source_rows"][0]
    assert row["source_record_id"] == "rec-amazon-1"
    assert row["source_table_ref"] == "AMAZON_PRODUCTS"
    assert row["business_key"] == "amazon:US:B0CHILD001"
    assert row["requested_asin"] == "B0CHILD001"
    assert row["canonical_url"] == "https://www.amazon.com/dp/B0CHILD001"
    assert row["product_identity"] == {
        "marketplace_code": "US",
        "asin": "B0CHILD001",
        "canonical_url": "https://www.amazon.com/dp/B0CHILD001",
    }
    assert row["business_fields"] == {
        "force_refresh": True,
        "collection_status": "pending",
    }
    assert row["writeback_context"] == {
        "target_table_ref": "AMAZON_PRODUCTS",
        "record_id": "rec-amazon-1",
    }
    assert set(row["source_context"]["source_fields"]) == set(
        AMAZON_PRODUCT_SOURCE_FIELDS
    )
    assert "业务备注" not in row["source_context"]["source_fields"]


def test_common_domain_mapping_routes_amazon_source_adapter() -> None:
    result = adapt_source_rows(
        "amazon_product_table_source_adapter",
        [_raw_row()],
        _source_payload(),
    )

    assert result["candidate_keys"] == ["amazon:US:B0CHILD001"]


def test_amazon_batch_source_adapter_selects_only_exact_t_tagged_rows() -> None:
    result = amazon_product_batch_source_adapter(
        [
            _raw_row(record_id="rec-t-1", **{"采集标签": "T"}),
            _raw_row(record_id="rec-a", **{"采集标签": "A"}),
            _raw_row(record_id="rec-lower-t", **{"采集标签": "t"}),
            _raw_row(record_id="rec-empty", **{"采集标签": ""}),
            _raw_row(record_id="rec-invalid", **{"采集标签": "T", "ASIN": "bad"}),
        ],
        {"source_table_ref": "AMAZON_PRODUCTS"},
    )

    assert [row["source_record_id"] for row in result["source_rows"]] == ["rec-t-1"]
    assert result["adapter_summary"] == {
        "adapter_code": "amazon_product_batch_source_adapter",
        "input_row_count": 5,
        "tagged_row_count": 2,
        "source_row_count": 1,
        "selection_field": "采集标签",
        "selection_value": "T",
        "invalid_asin_count": 1,
        "identity_mismatch_count": 0,
        "unsupported_marketplace_count": 0,
        "missing_record_id_count": 0,
    }


def test_common_domain_mapping_routes_amazon_batch_source_adapter() -> None:
    result = adapt_source_rows(
        "amazon_product_batch_source_adapter",
        [_raw_row(**{"采集标签": "T"}), _raw_row(record_id="ignored", **{"采集标签": "A"})],
        {"source_table_ref": "AMAZON_PRODUCTS"},
    )

    assert result["candidate_keys"] == ["amazon:US:B0CHILD001"]


def test_single_row_read_uses_source_record_id_without_scanning_the_table() -> None:
    class Client:
        def get_record(self, app_token, table_id, record_id):
            assert (app_token, table_id, record_id) == (
                "app-token",
                "table-id",
                "rec-amazon-1",
            )
            return {"data": {"record": _raw_row()}}

        def list_all_records(self, *args, **kwargs):
            raise AssertionError("single-row read must not scan the table")

    rows, pagination = read_feishu_records(
        Client(),
        FeishuTableTarget(
            access_token="access-token",
            app_token="app-token",
            table_id="table-id",
        ),
        {"source_record_id": "rec-amazon-1"},
    )

    assert rows == [_raw_row()]
    assert pagination == {
        "next_page_token": "",
        "has_more": False,
        "source": "record_id",
    }


@pytest.mark.parametrize(
    ("fields", "lookup_status", "summary_key"),
    [
        ({"ASIN": "bad"}, "invalid_asin", "invalid_asin_count"),
        (
            {"商品链接": "https://www.amazon.co.uk/dp/B0CHILD001"},
            "unsupported_marketplace",
            "unsupported_marketplace_count",
        ),
        (
            {"商品链接": "https://www.amazon.com/dp/B0OTHER001"},
            "identity_mismatch",
            "identity_mismatch_count",
        ),
    ],
)
def test_source_adapter_returns_typed_lookup_status_for_invalid_identity(
    fields, lookup_status, summary_key
) -> None:
    result = amazon_product_table_source_adapter(
        [_raw_row(**fields)],
        _source_payload(),
    )

    assert result["source_rows"] == []
    assert result["candidate_keys"] == []
    assert result["adapter_summary"]["lookup_status"] == lookup_status
    assert result["adapter_summary"][summary_key] == 1


def test_source_adapter_reports_missing_requested_record_without_guessing() -> None:
    result = amazon_product_table_source_adapter(
        [_raw_row(record_id="other-record")],
        _source_payload(),
    )

    assert result["source_rows"] == []
    assert result["adapter_summary"]["lookup_status"] == "not_found"
    assert result["adapter_summary"]["matched_row_count"] == 0


def test_projection_maps_all_observed_fields_to_same_source_record() -> None:
    capture = _capture()
    command = amazon_product_projection_mapper(
        _projection_record(capture),
        {"mapper_code": "amazon_product_projection_mapper"},
    )

    assert command["op"] == "update"
    assert command["record_id"] == "rec-amazon-1"
    assert command["business_entity_key"] == "amazon:US:B0CHILD001"
    assert command["update_excluded_fields"] == list(
        AMAZON_PRODUCT_MANUAL_PRESERVE_FIELDS
    )
    fields = command["fields"]
    assert set(fields) == set(AMAZON_PRODUCT_PROJECTION_FIELDS)
    assert fields["商品链接"] == {
        "text": "https://www.amazon.com/dp/B0CHILD001",
        "link": "https://www.amazon.com/dp/B0CHILD001",
    }
    assert fields["采集状态"] == "success"
    assert fields["上次采集时间"] == "2026-07-14T08:00:00Z"
    assert fields["采集错误"] == ""
    assert fields["标题"] == "Structured product title"
    assert fields["品牌"] == "Structured Brand"
    assert fields["类目路径"] == "Home & Kitchen > Lighting > Table Lamps"
    assert fields["卖点"] == "Dimmable warm light\nSolid oak base"
    assert fields["描述"] == "Structured product description."
    assert fields["当前价格"] == 29.99
    assert fields["原价"] == 39.99
    assert fields["币种"] == "USD"
    assert fields["评分"] == 4.7
    assert fields["评论数"] == 1234
    assert fields["30天购买人数"] == "500+"
    assert fields["库存状态"] == "in_stock"
    assert fields["Parent ASIN"] == "B0PARENT01"
    assert fields["Child ASIN列表"] == "B0CHILD001\nB0CHILD002"
    assert json.loads(fields["变体属性"]) == {
        "attributes": {"Color": "Blue", "Size": "Large"},
        "dimensions": {
            "Color": ["Blue", "Red"],
            "Size": ["Large", "Small"],
        },
    }
    assert fields["卖家"] == "Structured Seller"
    assert fields["配送方式"] == "Amazon | FREE delivery Friday, July 17"
    assert fields["送达日期"] == "7月17号"
    assert fields["包装规格"] == "没有包装规格"
    assert fields["Buy Box卖家"] == "Structured Seller"
    assert fields["Buy Box价格"] == 29.99
    assert fields["优惠券"] == "Save 10% with coupon"
    assert fields["促销活动记录"] == (
        "coupon | 10% | $26.99\n7-14 16:00"
    )
    assert fields["BSR排名"] == (
        "#7 - Home & Kitchen > Lighting > Table Lamps\n"
        "#321 - Home & Kitchen"
    )
    assert json.loads(fields["技术参数"]) == {
        "Material": "Oak",
        "Product Dimensions": "10 x 10 x 18 inches",
    }
    assert fields["页面ASIN"] == "B0CHILD001"
    assert fields["字段完整度"] == 100.0
    assert fields["主图"] == [
        {
            "bucket": "runtime-artifacts",
            "object_key": (
                "mujitask/local/product-media/amazon/us/B0CHILD001/main.jpg"
            ),
            "content_digest": "a" * 64,
            "file_name": "main.jpg",
            "mime_type": "image/jpeg",
        }
    ]
    assert [item["file_name"] for item in fields["侧边栏图片"]] == [
        "gallery-1.jpg",
        "gallery-2.jpg",
    ]
    assert "ASIN" not in fields
    assert "来源关键词" not in fields
    assert "强制刷新" not in fields


def test_projection_uses_number_of_items_for_packaging_specification() -> None:
    capture = _capture()
    capture["product"]["technical_details"]["Number of Items"] = "2"
    capture["field_evidence"]["product.technical_details"]["value"] = dict(
        capture["product"]["technical_details"]
    )

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert fields["包装规格"] == "2"


def test_projection_places_beijing_promotion_timestamp_on_second_line() -> None:
    record = _projection_record()
    record["projection_facts"]["captured_at"] = "2026-07-19T23:08:00Z"

    fields = amazon_product_projection_mapper(record, {})["fields"]

    assert fields["促销活动记录"] == (
        "coupon | 10% | $26.99\n7-20 07:08"
    )


def test_projection_formats_fixed_amount_coupon_with_calculated_price() -> None:
    capture = _capture()
    offer = capture["commerce"]["featured_offer"]
    offer["promotions"] = [
        {
            "promotion_type": "coupon",
            "label": "Coupon",
            "discount_type": "amount",
            "discount_value": 10.0,
            "deal_price": None,
            "reference_price": None,
            "reference_price_type": None,
            "currency": "USD",
            "prime_only": False,
            "claim_required": True,
            "raw_text": "Apply $10 coupon",
        }
    ]
    capture["field_evidence"]["commerce.featured_offer.promotions"]["value"] = list(
        offer["promotions"]
    )

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert fields["促销活动记录"] == (
        "coupon | $10 | $19.99\n7-14 16:00"
    )


def test_projection_formats_limited_time_deal_without_discount_or_reference_price() -> None:
    capture = _capture()
    offer = capture["commerce"]["featured_offer"]
    offer["promotions"] = [
        {
            "promotion_type": "limited_time_deal",
            "label": "Limited time deal",
            "discount_type": "price_override",
            "discount_value": None,
            "deal_price": 26.99,
            "reference_price": None,
            "reference_price_type": None,
            "currency": "USD",
            "prime_only": False,
            "claim_required": False,
            "raw_text": "Limited time deal | $26.99",
        }
    ]
    capture["field_evidence"]["commerce.featured_offer.promotions"]["value"] = list(
        offer["promotions"]
    )

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert fields["促销活动记录"] == (
        "Limited time deal | $26.99\n7-14 16:00"
    )


def test_projection_writes_dated_no_promotion_snapshot_for_observed_empty_promotions() -> None:
    capture = _capture()
    capture["commerce"]["featured_offer"]["promotions"] = []
    capture["field_evidence"]["commerce.featured_offer.promotions"] = {
        "value": [],
        "status": "observed",
        "source_kind": "semantic_dom",
        "source_locator": "promotion-zones",
        "confidence": 1.0,
    }

    command = amazon_product_projection_mapper(_projection_record(capture), {})

    assert command["fields"]["促销活动记录"] == (
        "当前没有促销活动\n7-14 16:00"
    )
    assert "促销活动记录" not in command["clear_fields"]


def test_projection_preserves_existing_promotion_when_empty_snapshot_is_missing() -> None:
    capture = _capture()
    capture["commerce"]["featured_offer"]["promotions"] = []
    capture["field_evidence"]["commerce.featured_offer.promotions"] = {
        "value": [],
        "status": "missing",
        "source_kind": None,
        "source_locator": None,
        "confidence": 0.0,
    }

    command = amazon_product_projection_mapper(_projection_record(capture), {})

    assert "促销活动记录" not in command["fields"]
    assert "促销活动记录" not in command["clear_fields"]


def test_common_write_mapping_routes_amazon_projection_mapper() -> None:
    mapped = map_write_records(
        {
            "mapper_code": "amazon_product_projection_mapper",
            "records": [_projection_record()],
        }
    )

    assert len(mapped) == 1
    assert mapped[0]["record_id"] == "rec-amazon-1"
    assert mapped[0]["fields"]["标题"] == "Structured product title"
    assert mapped[0]["fields"]["采集错误"] == ""


def test_common_write_mapping_preserves_explicit_unavailable_clear_values() -> None:
    capture = _capture("product_detail_unavailable.html", asin="B0UNAVL001")
    mapped = map_write_records(
        {
            "mapper_code": "amazon_product_projection_mapper",
            "records": [
                _projection_record(capture, materialized_media_assets=[]),
            ],
        }
    )

    assert mapped[0]["fields"]["当前价格"] is None
    assert mapped[0]["fields"]["采集错误"] == ""


def test_common_write_normalization_only_preserves_explicit_clear_fields() -> None:
    normalized = normalize_write_record(
        {
            "op": "update",
            "record_id": "rec-1",
            "clear_fields": ["Amazon清空字段"],
            "fields": {
                "Amazon清空字段": None,
                "旧流程空字符串": "",
                "旧流程空数组": [],
                "保留字段": "value",
            },
        },
        {},
    )

    assert normalized["fields"] == {
        "Amazon清空字段": None,
        "保留字段": "value",
    }


def test_missing_evidence_preserves_existing_fields_even_when_values_are_present() -> None:
    capture = _capture()
    capture["field_evidence"]["product.title"] = {
        "value": capture["product"]["title"],
        "status": "missing",
        "source_kind": "policy",
        "source_locator": "test",
        "confidence": 0,
    }
    capture["field_evidence"]["commerce.featured_offer.price_amount"] = {
        "value": capture["commerce"]["featured_offer"]["price_amount"],
        "status": "missing",
        "source_kind": "policy",
        "source_locator": "test",
        "confidence": 0,
    }
    capture["field_evidence"]["commerce.bought_past_month"]["status"] = "missing"
    capture["field_evidence"]["media.main_image"]["status"] = "missing"

    fields = amazon_product_projection_mapper(
        _projection_record(capture),
        {},
    )["fields"]

    assert "标题" not in fields
    assert "当前价格" not in fields
    assert "30天购买人数" not in fields
    assert "主图" not in fields
    assert fields["品牌"] == "Structured Brand"
    assert fields["字段完整度"] < 100


def test_composite_projection_omits_entire_field_when_any_component_is_missing() -> None:
    capture = _capture()
    capture["field_evidence"]["variants.dimensions"]["status"] = "missing"
    capture["field_evidence"]["commerce.featured_offer.delivery_text"][
        "status"
    ] = "missing"

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert "变体属性" not in fields
    assert "配送方式" not in fields
    assert "送达日期" not in fields


@pytest.mark.parametrize(
    ("delivery_text", "expected"),
    [
        (
            "FREE delivery on orders shipped by Amazon over $35 Wednesday, July 22",
            "7月22号",
        ),
        ("FREE delivery Thursday July 23", "7月23号"),
        ("FREE delivery August 3 - 18", "8月3-18号"),
        ("FREE delivery July 29 - August 2", "7月29号-8月2号"),
        ("FREE delivery Saturday, July 25", "7月25号"),
    ],
)
def test_delivery_date_projection_writes_only_date_or_range(
    delivery_text: str,
    expected: str,
) -> None:
    capture = _capture()
    capture["commerce"]["featured_offer"]["delivery_text"] = delivery_text
    capture["field_evidence"]["commerce.featured_offer.delivery_text"][
        "value"
    ] = delivery_text

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert fields["送达日期"] == expected


def test_delivery_date_projection_preserves_existing_value_when_date_is_unparseable() -> None:
    capture = _capture()
    delivery_text = "FREE delivery with qualifying orders"
    capture["commerce"]["featured_offer"]["delivery_text"] = delivery_text
    capture["field_evidence"]["commerce.featured_offer.delivery_text"][
        "value"
    ] = delivery_text

    fields = amazon_product_projection_mapper(_projection_record(capture), {})["fields"]

    assert "送达日期" not in fields


def test_unavailable_projection_clears_offer_fields_but_preserves_missing_values() -> None:
    capture = _capture("product_detail_unavailable.html", asin="B0UNAVL001")
    command = amazon_product_projection_mapper(
        _projection_record(
            capture,
            materialized_media_assets=[],
        ),
        {},
    )

    fields = command["fields"]
    assert fields["采集状态"] == "unavailable"
    assert fields["库存状态"] == "unavailable"
    assert fields["当前价格"] is None
    assert fields["原价"] is None
    assert fields["币种"] is None
    assert fields["卖家"] is None
    assert fields["配送方式"] is None
    assert fields["Buy Box卖家"] is None
    assert fields["Buy Box价格"] is None
    assert fields["优惠券"] is None
    assert fields["促销活动记录"] is None
    assert "主图" not in fields
    assert "侧边栏图片" not in fields


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("blocked", "captcha_required"), ("failed", "identity_mismatch")],
)
def test_terminal_identity_or_access_failure_writes_status_only(
    status: str,
    error_code: str,
) -> None:
    command = amazon_product_projection_mapper(
        {
            "source_record_id": "rec-amazon-1",
            "requested_asin": "B0CHILD001",
            "resolved_asin": "B0OTHER001",
            "collection_status": status,
            "collected_at": "2026-07-14T08:00:00Z",
            "error_code": error_code,
            "error_message": "redacted failure",
            "product": {"title": "must not write"},
        },
        {},
    )

    assert command["record_id"] == "rec-amazon-1"
    assert command["fields"] == {
        "采集状态": status,
        "上次采集时间": "2026-07-14T08:00:00Z",
        "采集错误": f"{error_code}: redacted failure",
    }


def test_projection_requires_source_record_id_and_never_falls_back_to_upsert() -> None:
    facts = dict(_projection_record()["projection_facts"])
    facts.pop("source_record_id")
    with pytest.raises(ValueError, match="source_record_id"):
        amazon_product_projection_mapper(
            {"projection_facts": facts},
            {},
        )


def test_projection_ignores_media_that_was_not_materialized() -> None:
    record = _projection_record()
    record["materialized_media_assets"][0]["sync_state"] = "referenced"

    fields = amazon_product_projection_mapper(record, {})["fields"]

    assert "主图" not in fields
    assert "侧边栏图片" in fields


def test_materialized_local_attachment_can_replace_existing_feishu_attachment(
    tmp_path,
) -> None:
    local_path = tmp_path / "main.jpg"
    local_path.write_bytes(b"materialized-image")

    merged = merge_update_fields(
        {"主图": [{"local_path": str(local_path), "file_name": "main.jpg"}]},
        existing_fields={"主图": [{"file_token": "old-feishu-token"}]},
        field_schema={"主图": {"type": 17}},
        replace_fields={"主图"},
    )

    assert merged == {
        "主图": [{"local_path": str(local_path), "file_name": "main.jpg"}]
    }


def test_materialized_object_attachment_reads_minio_even_when_local_file_exists(
    monkeypatch,
    tmp_path,
) -> None:
    stored_image = b"stored-image"
    local_path = tmp_path / "stale-main.jpg"
    local_path.write_bytes(b"stale-local-image")
    object_item = {
        "bucket": "runtime-artifacts",
        "object_key": "mujitask/local/product-media/amazon/us/B0CHILD001/main.jpg",
        "content_digest": hashlib.sha256(stored_image).hexdigest(),
        "local_path": str(local_path),
        "file_name": "main.jpg",
        "mime_type": "image/jpeg",
    }
    merged = merge_update_fields(
        {"主图": [object_item]},
        existing_fields={"主图": [{"file_token": "old-feishu-token"}]},
        field_schema={"主图": {"type": 17}},
        replace_fields={"主图"},
    )
    assert merged == {"主图": [object_item]}

    class Store:
        def read_bytes(self, *, bucket, object_key):
            assert bucket == "runtime-artifacts"
            assert object_key == object_item["object_key"]
            return stored_image

    class Client:
        def upload_media(
            self,
            file_name,
            file_data,
            parent_type="bitable_file",
            parent_node="",
            extra=None,
        ):
            assert file_name == "main.jpg"
            assert file_data == b"stored-image"
            assert parent_node == "app-token"
            return "new-feishu-token"

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes.get_execution_control_defaults",
        lambda: SimpleNamespace(
            artifact_store_provider="minio",
            artifact_bucket="runtime-artifacts",
            artifact_object_prefix="mujitask/local",
            minio_endpoint="127.0.0.1:9000",
            minio_access_key="access",
            minio_secret_key="secret",
            minio_secure=False,
            minio_region="",
            minio_create_bucket=False,
        ),
    )
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes.create_artifact_store",
        lambda settings: Store(),
    )

    refs = attachment_file_token_ref_items(
        [object_item],
        client=Client(),
        target=FeishuTableTarget(
            access_token="access-token",
            app_token="app-token",
            table_id="table-id",
        ),
        payload={},
    )

    assert refs == [{"file_token": "new-feishu-token"}]


def test_incomplete_materialized_attachment_reference_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="requires bucket, object_key, and content_digest",
    ):
        attachment_file_token_ref_items(
            [
                {
                    "object_key": (
                        "mujitask/local/product-media/amazon/us/"
                        "B0CHILD001/main.jpg"
                    ),
                    "file_name": "main.jpg",
                }
            ],
            client=object(),
            target=FeishuTableTarget(
                access_token="access-token",
                app_token="app-token",
                table_id="table-id",
            ),
            payload={},
        )


def test_feishu_write_only_transports_six_active_amazon_projection_fields(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeClient:
        uploads = []
        updates = []

        def __init__(self, access_token, request_pacer=None):
            self.access_token = access_token
            self.request_pacer = request_pacer

        def list_all_fields(self, app_token, table_id):
            return [
                {"field_name": "主图", "type": 17},
                {"field_name": "侧边栏图片", "type": 17},
                {"field_name": "30天购买人数", "type": 1},
                {"field_name": "送达日期", "type": 1},
                {"field_name": "包装规格", "type": 1},
                {"field_name": "促销活动记录", "type": 1},
            ]

        def get_record(self, app_token, table_id, record_id):
            return {
                "data": {
                    "record": {
                        "record_id": record_id,
                        "fields": {
                            "主图": [{"file_token": "old-main"}],
                            "侧边栏图片": [{"file_token": "old-gallery"}],
                            "采集错误": "old error",
                        },
                    }
                }
            }

        def upload_media(
            self,
            file_name,
            file_data,
            parent_type="bitable_file",
            parent_node="",
            extra=None,
        ):
            self.uploads.append(
                {
                    "file_name": file_name,
                    "file_data": file_data,
                    "parent_node": parent_node,
                    "extra": extra,
                }
            )
            return f"new-{file_name}"

        def update_record(self, app_token, table_id, record_id, fields):
            self.updates.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeClient.uploads = []
    FakeClient.updates = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )
    record = _projection_record()
    stored_objects: dict[tuple[str, str], bytes] = {}
    for index, asset in enumerate(record["materialized_media_assets"]):
        path = tmp_path / f"asset-{index}.jpg"
        image_bytes = f"image-{index}".encode()
        path.write_bytes(image_bytes)
        asset["source_path"] = str(path)
        asset["file_name"] = path.name
        asset["content_digest"] = hashlib.sha256(image_bytes).hexdigest()
        stored_objects[(asset["bucket"], asset["object_key"])] = image_bytes

    class Store:
        def read_bytes(self, *, bucket, object_key):
            return stored_objects[(bucket, object_key)]

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes.get_execution_control_defaults",
        lambda: SimpleNamespace(
            artifact_store_provider="minio",
            artifact_bucket="runtime-artifacts",
            artifact_object_prefix="mujitask/local",
            minio_endpoint="127.0.0.1:9000",
            minio_access_key="access",
            minio_secret_key="secret",
            minio_secure=False,
            minio_region="",
            minio_create_bucket=False,
        ),
    )
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.field_envelopes.create_artifact_store",
        lambda settings: Store(),
    )

    context = HandlerContext(
        request_id="req-amazon-write",
        job_id="job-amazon-write",
        handler_code="feishu_table_write",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload={
            "target_table_ref": "AMAZON_PRODUCTS",
            "feishu_table": {"app_token": "app-token", "table_id": "table-id"},
            "access_token": "access-token",
            "mapper_code": "amazon_product_projection_mapper",
            "records": [record],
            "write_policy": {
                "ignore_missing_fields": True,
                "field_allowlist": list(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS),
            },
        },
        job_code="feishu_table_write",
    )

    result = build_bound_api_handler_registry().dispatch(
        "feishu_table_write",
        context,
    )

    assert result.status == "success"
    assert len(FakeClient.uploads) == 3
    written = FakeClient.updates[0]
    assert written["record_id"] == "rec-amazon-1"
    assert written["fields"]["主图"] == [{"file_token": "new-asset-0.jpg"}]
    assert written["fields"]["侧边栏图片"] == [
        {"file_token": "new-asset-1.jpg"},
        {"file_token": "new-asset-2.jpg"},
    ]
    assert set(written["fields"]) == set(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS)
    assert written["fields"]["送达日期"] == "7月17号"
    assert written["fields"]["30天购买人数"] == "500+"
    assert written["fields"]["包装规格"] == "没有包装规格"
    assert written["fields"]["促销活动记录"] == (
        "coupon | 10% | $26.99\n7-14 16:00"
    )
