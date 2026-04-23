from __future__ import annotations

from pathlib import Path

from automation_business_scaffold.business.flows.feishu_tk_selection_mapper import (
    FIELD_COMMENT_COUNT,
    FIELD_GALLERY_IMAGES,
    FIELD_MAIN_IMAGE,
    FIELD_MARKETING_CHART,
    FIELD_PARENT_IMAGE,
    FIELD_PRICE,
    FIELD_PRODUCT_ID,
    FIELD_PRODUCT_LINK,
    FIELD_PRODUCT_STATUS,
    FIELD_RATING,
    FIELD_RECORD_DATE,
    FIELD_SHOP_NAME,
    FIELD_SKU_CHART,
    FIELD_TITLE,
    FIELD_TREND_CHART,
    FIELD_YEAR_SALES,
    FeishuTKSelectionMapper,
    PRODUCT_STATUS_UNAVAILABLE,
    build_feishu_link_value,
    prepare_feishu_writable_fields,
)


PRODUCT_ID = "1732039802895831738"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/hyg-toys-electric-toy-gun-realistic-design/{PRODUCT_ID}"
NORMALIZED_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"


def test_feishu_tk_selection_mapper_matches_url_and_detects_missing_fields() -> None:
    mapper = FeishuTKSelectionMapper()
    record = {
        "record_id": "recvfHCdzsKwVp",
        "fields": {
            FIELD_PRODUCT_LINK: {"text": PRODUCT_URL, "link": PRODUCT_URL},
            FIELD_MAIN_IMAGE: [{"file_token": "img-main"}],
        },
    }

    assert mapper.record_matches_url(record, NORMALIZED_URL) is True
    item = mapper.evaluate_record(record, product_url=NORMALIZED_URL)

    assert item["status"] == "needs_ingest"
    assert item["normalized_url"] == NORMALIZED_URL
    assert item["product_id"] == PRODUCT_ID
    assert FIELD_PRODUCT_ID in item["required_missing_fields"]
    assert FIELD_TITLE in item["required_missing_fields"]


def test_feishu_tk_selection_mapper_skips_when_required_fields_are_complete() -> None:
    mapper = FeishuTKSelectionMapper()
    fields = {
        FIELD_PRODUCT_LINK: {"text": NORMALIZED_URL, "link": NORMALIZED_URL},
        FIELD_MAIN_IMAGE: [{"file_token": "img-main"}],
        FIELD_PRODUCT_ID: PRODUCT_ID,
        FIELD_SHOP_NAME: "TikTok Shop",
        FIELD_TITLE: "TikTok Gift",
        FIELD_PRICE: "$12.99",
        FIELD_COMMENT_COUNT: 123,
        FIELD_RATING: 4.8,
        FIELD_YEAR_SALES: 94151,
        FIELD_MARKETING_CHART: [{"file_token": "chart-marketing"}],
        FIELD_TREND_CHART: [{"file_token": "chart-trend"}],
        FIELD_SKU_CHART: [{"file_token": "chart-sku"}],
        FIELD_RECORD_DATE: 1776787200000,
    }

    item = mapper.evaluate_record({"record_id": "rec-complete", "fields": fields}, product_url=PRODUCT_URL)

    assert item["status"] == "skipped_completed"
    assert item["required_missing_fields"] == []
    assert FIELD_GALLERY_IMAGES not in item["required_for_skip_decision"]
    assert FIELD_PARENT_IMAGE not in item["required_for_skip_decision"]
    assert FIELD_SKU_CHART not in item["required_for_skip_decision"]


def test_feishu_tk_selection_mapper_skips_when_product_status_is_unavailable() -> None:
    mapper = FeishuTKSelectionMapper()
    record = {
        "record_id": "rec-unavailable",
        "fields": {
            FIELD_PRODUCT_LINK: {"text": PRODUCT_URL, "link": PRODUCT_URL},
            FIELD_PRODUCT_STATUS: PRODUCT_STATUS_UNAVAILABLE,
        },
    }

    item = mapper.evaluate_record(record, product_url=NORMALIZED_URL)

    assert item["status"] == "skipped_unavailable"
    assert item["product_status"] == PRODUCT_STATUS_UNAVAILABLE
    assert FIELD_TITLE in item["required_missing_fields"]


def test_feishu_tk_selection_mapper_builds_writeback_fields_and_record_date(tmp_path: Path) -> None:
    mapper = FeishuTKSelectionMapper()
    image_path = tmp_path / "main.webp"
    marketing_chart_path = tmp_path / "marketing_strategy.png"
    trend_chart_path = tmp_path / "overview_trend.png"
    sku_chart_path = tmp_path / "sku_analysis.png"
    image_path.write_bytes(b"image")
    marketing_chart_path.write_bytes(b"png")
    trend_chart_path.write_bytes(b"png")
    sku_chart_path.write_bytes(b"png")
    product_result = {
        "product_id": PRODUCT_ID,
        "tiktok": {
            "product": {
                "product_id": PRODUCT_ID,
                "normalized_url": NORMALIZED_URL,
                "title": "TikTok Gift",
                "shop_name": "TikTok Shop",
                "price_text": "$12.99",
                "comment_count": 392,
                "rating_score": 4.4,
                "sales_count": 3098,
                "main_image_local_path": str(image_path),
                "main_image_file_name": "main.webp",
                "main_image_mime_type": "image/webp",
            }
        },
        "fastmoss": {
            "fastmoss": {
                "overview": {
                    "d_type": 28,
                    "overview": {
                        "d_type": 28,
                        "sold_count": 88,
                    },
                }
            }
        },
        "visualizations": {
            "files": {
                "marketing_strategy": str(marketing_chart_path),
                "overview_trend": str(trend_chart_path),
                "sku_analysis": str(sku_chart_path),
            }
        },
    }

    fields = mapper.build_writeback_fields(product_result)

    assert fields[FIELD_PRODUCT_LINK] == {"text": NORMALIZED_URL, "link": NORMALIZED_URL}
    assert fields[FIELD_PRODUCT_ID] == PRODUCT_ID
    assert fields[FIELD_PRICE] == 12.99
    assert fields[FIELD_COMMENT_COUNT] == 392
    assert fields[FIELD_RATING] == 4.4
    assert fields[FIELD_YEAR_SALES] == 88
    assert fields[FIELD_RECORD_DATE] > 0
    assert fields[FIELD_MAIN_IMAGE]["type"] == "local_file"
    assert fields[FIELD_MARKETING_CHART]["file_name"] == "marketing_strategy.png"
    assert fields[FIELD_TREND_CHART]["file_name"] == "overview_trend.png"
    assert fields[FIELD_SKU_CHART]["file_name"] == "sku_analysis.png"
    assert mapper.validate_writeback_fields(fields) == []


def test_feishu_tk_selection_mapper_writes_parent_sku_only_when_sku_chart_exists(tmp_path: Path) -> None:
    mapper = FeishuTKSelectionMapper()
    main_image_path = tmp_path / "main.webp"
    sku_image_path = tmp_path / "sku-pink.webp"
    marketing_chart_path = tmp_path / "marketing_strategy.png"
    trend_chart_path = tmp_path / "overview_trend.png"
    sku_chart_path = tmp_path / "sku_analysis.png"
    for path in (main_image_path, sku_image_path, marketing_chart_path, trend_chart_path, sku_chart_path):
        path.write_bytes(b"asset")
    base_result = {
        "product_id": PRODUCT_ID,
        "tiktok": {
            "product": {
                "product_id": PRODUCT_ID,
                "normalized_url": NORMALIZED_URL,
                "title": "TikTok Gift",
                "shop_name": "TikTok Shop",
                "price_text": "$12.99",
                "comment_count": 392,
                "rating_score": 4.4,
                "main_image_local_path": str(main_image_path),
                "main_image_file_name": "main.webp",
            }
        },
        "fastmoss": {
            "fastmoss": {
                "overview": {"d_type": 28, "overview": {"sold_count": 88}},
                "sku_distribution": {
                    "best_sku": {
                        "sku_name": "Color",
                        "sku_value": "Pink",
                    }
                },
            }
        },
        "media_upload": {
            "uploaded_media_assets": [
                {
                    "media_role": "product_sku_image",
                    "local_path": str(sku_image_path),
                    "file_name": "sku-pink.webp",
                    "mime_type": "image/webp",
                    "metadata": {"sku_property_key": "Color:Pink"},
                }
            ]
        },
        "persisted": {
            "fact_entities": [
                {
                    "sku_key": f"{PRODUCT_ID}:sku-pink",
                    "sku_id": "sku-pink",
                    "sku_name": "Pink",
                    "spec_name": "Color: Pink",
                    "facts": {"tiktok_spec_name": "Color: Pink"},
                }
            ]
        },
        "visualizations": {
            "files": {
                "marketing_strategy": str(marketing_chart_path),
                "overview_trend": str(trend_chart_path),
            }
        },
    }

    without_sku_chart = mapper.build_writeback_fields(base_result)
    with_sku_chart = mapper.build_writeback_fields(
        {
            **base_result,
            "visualizations": {
                "files": {
                    "marketing_strategy": str(marketing_chart_path),
                    "overview_trend": str(trend_chart_path),
                    "sku_analysis": str(sku_chart_path),
                }
            },
        }
    )

    assert FIELD_PARENT_IMAGE not in without_sku_chart
    assert "父体规格" not in without_sku_chart
    assert with_sku_chart["父体规格"] == "Color: Pink"
    assert with_sku_chart[FIELD_PARENT_IMAGE][0]["file_name"] == "sku-pink.webp"


def test_feishu_tk_selection_mapper_matches_parent_image_by_fastmoss_prop_value_id(tmp_path: Path) -> None:
    mapper = FeishuTKSelectionMapper()
    main_image_path = tmp_path / "main.webp"
    sku_chart_path = tmp_path / "sku_analysis.png"
    marketing_chart_path = tmp_path / "marketing_strategy.png"
    trend_chart_path = tmp_path / "overview_trend.png"
    sand_image_path = tmp_path / "sku-sand.webp"
    black_image_path = tmp_path / "sku-black.webp"
    m4_image_path = tmp_path / "sku-m4.webp"
    for path in (
        main_image_path,
        sku_chart_path,
        marketing_chart_path,
        trend_chart_path,
        sand_image_path,
        black_image_path,
        m4_image_path,
    ):
        path.write_bytes(b"asset")

    fields = mapper.build_writeback_fields(
        {
            "product_id": PRODUCT_ID,
            "tiktok": {
                "product": {
                    "product_id": PRODUCT_ID,
                    "normalized_url": NORMALIZED_URL,
                    "title": "Gel Blaster Electric toy",
                    "shop_name": "HYGToys",
                    "price_text": "$72.00",
                    "comment_count": 393,
                    "rating_score": 4.4,
                    "main_image_local_path": str(main_image_path),
                    "main_image_file_name": "main.webp",
                }
            },
            "fastmoss": {
                "fastmoss": {
                    "overview": {"d_type": 28, "overview": {"sold_count": 330}},
                    "skus": {
                        "best_sku": {
                            "sku_name": "Color",
                            "sku_value": "black",
                            "sold_count": 311,
                        },
                        "sku_list": [
                            {
                                "sku_id": "1732039829297140410",
                                "sku_sale_props": [
                                    {
                                        "prop_name": "Color",
                                        "prop_value": "Sand colored",
                                        "prop_value_id": "7392109364922599173",
                                        "image": "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/cf8d34420bc44b8199870e127cf2b813~tplv-fhlh96nyum-crop-webp:1500:1500.webp?from=2378011839",
                                    }
                                ],
                            },
                            {
                                "sku_id": "1732039829297205946",
                                "sku_sale_props": [
                                    {
                                        "prop_name": "Color",
                                        "prop_value": "black",
                                        "prop_value_id": "7569178036852033294",
                                        "image": "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/75eabb806f724911a2ef60555a44c573~tplv-fhlh96nyum-crop-webp:1500:1500.webp?from=2378011839",
                                    }
                                ],
                            },
                            {
                                "sku_id": "1732039829297271482",
                                "sku_sale_props": [
                                    {
                                        "prop_name": "Color",
                                        "prop_value": "M4A1 Sand colored",
                                        "prop_value_id": "7533835056209594118",
                                        "image": "https://p16-oec-general-useast5.ttcdn-us.com/tos-useast5-i-omjb5zjo8w-tx/8d26188c279a449abc55b6b0362a4565~tplv-fhlh96nyum-crop-webp:1500:1500.webp?from=2378011839",
                                    }
                                ],
                            },
                        ],
                    },
                }
            },
            "media_upload": {
                "uploaded_media_assets": [
                    {
                        "media_role": "product_sku_image",
                        "local_path": str(m4_image_path),
                        "file_name": "sku-m4.webp",
                        "mime_type": "image/webp",
                        "metadata": {"display_order": 0, "sku_property_key": "7533835056209594118"},
                    },
                    {
                        "media_role": "product_sku_image",
                        "local_path": str(sand_image_path),
                        "file_name": "sku-sand.webp",
                        "mime_type": "image/webp",
                        "metadata": {"display_order": 1, "sku_property_key": "7392109364922599173"},
                    },
                    {
                        "media_role": "product_sku_image",
                        "local_path": str(black_image_path),
                        "file_name": "sku-black.webp",
                        "mime_type": "image/webp",
                        "metadata": {"display_order": 2, "sku_property_key": "7569178036852033294"},
                    },
                ]
            },
            "visualizations": {
                "files": {
                    "marketing_strategy": str(marketing_chart_path),
                    "overview_trend": str(trend_chart_path),
                    "sku_analysis": str(sku_chart_path),
                }
            },
        }
    )

    assert fields["父体规格"] == "Color: black"
    assert fields[FIELD_PARENT_IMAGE] == [
        {
            "type": "local_file",
            "path": str(black_image_path),
            "file_name": "sku-black.webp",
            "mime_type": "image/webp",
            "source_url": "",
        }
    ]


def test_feishu_tk_selection_mapper_blocks_writeback_when_required_fields_missing() -> None:
    mapper = FeishuTKSelectionMapper()
    fields = mapper.build_writeback_fields({"product_id": PRODUCT_ID, "tiktok": {"product": {}}})

    missing = mapper.validate_writeback_fields(fields)

    assert FIELD_TITLE in missing
    assert FIELD_MAIN_IMAGE in missing
    assert FIELD_MARKETING_CHART in missing
    assert FIELD_SKU_CHART not in missing


def test_feishu_tk_selection_mapper_allows_status_only_writeback() -> None:
    mapper = FeishuTKSelectionMapper()
    fields = mapper.build_product_status_writeback_fields(
        {"item": {"status": "product_unavailable", "product_status": PRODUCT_STATUS_UNAVAILABLE}}
    )

    assert fields[FIELD_PRODUCT_STATUS] == PRODUCT_STATUS_UNAVAILABLE
    assert fields[FIELD_RECORD_DATE] > 0
    assert mapper.validate_product_status_writeback_fields(fields) == []
    assert mapper.product_result_is_unavailable(
        {"item": {"status": "product_unavailable", "product_status": PRODUCT_STATUS_UNAVAILABLE}}
    )


def test_feishu_tk_selection_mapper_prepares_product_link_by_field_schema() -> None:
    class FakeClient:
        def __init__(self, ui_type: str):
            self.ui_type = ui_type

        def list_all_fields(self, app_token: str, table_id: str):
            assert app_token == "app"
            assert table_id == "tbl"
            return [{"field_name": FIELD_PRODUCT_LINK, "type": 15 if self.ui_type == "Url" else 1, "ui_type": self.ui_type}]

    link_value = build_feishu_link_value(NORMALIZED_URL)

    url_writable = prepare_feishu_writable_fields(
        client=FakeClient("Url"),
        app_token="app",
        table_id="tbl",
        preview_fields={FIELD_PRODUCT_LINK: link_value},
    )
    text_writable = prepare_feishu_writable_fields(
        client=FakeClient("Text"),
        app_token="app",
        table_id="tbl",
        preview_fields={FIELD_PRODUCT_LINK: link_value},
    )

    assert url_writable[FIELD_PRODUCT_LINK] == link_value
    assert text_writable[FIELD_PRODUCT_LINK] == NORMALIZED_URL
