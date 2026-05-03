from __future__ import annotations

import pytest

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.domains.tiktok.flows import selection_row_refresh


PRODUCT_ID = "1732355931137544633"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"


def _context(*, writeback_enabled: bool = False) -> HandlerContext:
    return HandlerContext(
        request_id="req-selection-safety",
        job_id="job-selection-safety",
        handler_code="selection_row_refresh",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        workflow_code="tiktok_fastmoss_product_ingest",
        stage_code="collect_selection_rows",
        job_code="selection_row_refresh",
        payload={
            "request_payload": {
                "product_url": PRODUCT_URL,
                "selection_table_ref": "https://example.feishu.cn/base/app?table=tbl",
                "writeback_enabled": writeback_enabled,
            },
            "source_record_id": "rec-1",
            "target_table_ref": "https://example.feishu.cn/base/app?table=tbl",
            "product_identity": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
                "normalized_product_url": PRODUCT_URL,
            },
            "writeback_enabled": writeback_enabled,
        },
    )


def test_selection_row_refresh_writeback_disabled_skips_feishu_write(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"feishu_write": 0}

    def fake_tiktok_fetch(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            result={
                "normalized_product_result": {
                    "product_id": PRODUCT_ID,
                    "normalized_product_url": PRODUCT_URL,
                    "logical_fields": {"title": "Sample product"},
                    "fact_bundle": {"products": [{"product_id": PRODUCT_ID, "product_url": PRODUCT_URL}]},
                }
            },
        )

    def fake_fastmoss_fetch(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(context, result={"product_fact_bundle": {"product_id": PRODUCT_ID}})

    def fake_fact_upsert(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(context, result={"persistence_mode": "dry_run"})

    def fail_feishu_write(context: HandlerContext) -> HandlerResult:
        called["feishu_write"] += 1
        raise AssertionError("feishu_table_write must not run when writeback_enabled=false")

    monkeypatch.setattr(selection_row_refresh, "tiktok_product_request_fetch_handler", fake_tiktok_fetch)
    monkeypatch.setattr(selection_row_refresh, "fastmoss_product_fetch_handler", fake_fastmoss_fetch)
    monkeypatch.setattr(selection_row_refresh, "fact_bundle_upsert_handler", fake_fact_upsert)
    monkeypatch.setattr(selection_row_refresh, "feishu_table_write_handler", fail_feishu_write)

    result = selection_row_refresh.run_selection_row_refresh_flow(_context(writeback_enabled=False))

    assert result.status == "success"
    assert called["feishu_write"] == 0
    assert any(
        step.get("step") == "feishu_writeback" and step.get("reason") == "writeback_disabled"
        for step in result.result["step_timeline"]
    )


def test_selection_row_refresh_url_invalid_writeback_disabled_skips_status_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"feishu_write": 0}

    def fake_tiktok_fetch(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="invalid_input",
                error_code="url_invalid_domain",
                message="invalid domain",
                retryable=False,
            ),
        )

    def fail_feishu_write(context: HandlerContext) -> HandlerResult:
        called["feishu_write"] += 1
        raise AssertionError("invalid URL status write must not run when writeback_enabled=false")

    monkeypatch.setattr(selection_row_refresh, "tiktok_product_request_fetch_handler", fake_tiktok_fetch)
    monkeypatch.setattr(selection_row_refresh, "feishu_table_write_handler", fail_feishu_write)

    result = selection_row_refresh.run_selection_row_refresh_flow(_context(writeback_enabled=False))

    assert result.status == "success"
    assert result.result["row_status"] == "url_invalid"
    assert called["feishu_write"] == 0
    assert any(
        step.get("step") == "feishu_writeback_url_invalid" and step.get("reason") == "writeback_disabled"
        for step in result.result["step_timeline"]
    )


def test_selection_projection_skips_parent_fields_and_sku_chart_without_effective_best_sku() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "best_sku": {"sku_value": "", "sold_count": 0},
                        "sku_list": [
                            {
                                "sku_id": "sku-default",
                                "sku_name": "Default",
                                "sku_sale_props": [{"prop_value": "Default", "image": "https://example.com/default.jpg"}],
                            }
                        ],
                    },
                }
            ],
            "product_skus": [
                {
                    "sku_id": "sku-default",
                    "spec_name": "Default",
                    "media_assets": [{"source_url": "https://example.com/default.jpg"}],
                }
            ],
        },
        chart_image_paths={"sku_chart": [{"file_token": "sku-chart-token"}]},
    )

    assert "SKU销量占比图" not in fields
    assert "父体规格" not in fields
    assert "父体图片" not in fields


def test_selection_projection_skips_default_best_sku_even_with_sales() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "best_sku": {"sku_value": "Default", "sold_count": 10},
                        "sku_units_sold": {
                            "Specification": {
                                "list": [
                                    {"source": "Default", "sold_count": 10},
                                    {"source": "Other", "sold_count": 1},
                                ]
                            }
                        },
                        "sku_list": [
                            {
                                "sku_id": "sku-default",
                                "sku_name": "Default",
                                "sku_sale_props": [{"prop_value": "Default", "image": "https://example.com/default.jpg"}],
                            }
                        ],
                    },
                }
            ],
            "product_skus": [{"sku_id": "sku-default", "spec_name": "Default"}],
        },
        chart_image_paths={"sku_chart": [{"file_token": "sku-chart-token"}]},
    )

    assert "SKU销量占比图" not in fields
    assert "父体规格" not in fields
    assert "父体图片" not in fields


def test_selection_projection_uses_best_sku_value_not_first_product_sku() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "best_sku": {"sku_value": "Blue - 12 Pack", "sold_count": 35},
                        "sku_units_sold": {
                            "Style": {
                                "list": [
                                    {"source": "Golden - 12 Pack", "sold_count": 10},
                                    {"source": "Blue - 12 Pack", "sold_count": 35},
                                ]
                            }
                        },
                        "sku_list": [
                            {
                                "sku_id": "sku-golden",
                                "sku_name": "Golden - 12 Pack",
                                "sku_sale_props": [
                                    {
                                        "prop_value": "Golden - 12 Pack",
                                        "prop_value_id": "prop-golden",
                                        "image": "https://example.com/golden.jpg",
                                    }
                                ],
                            },
                            {
                                "sku_id": "sku-blue",
                                "sku_name": "Blue - 12 Pack",
                                "sku_sale_props": [
                                    {
                                        "prop_value": "Blue - 12 Pack",
                                        "prop_value_id": "prop-blue",
                                        "image": "https://example.com/blue.jpg",
                                    }
                                ],
                            },
                        ],
                    },
                }
            ],
            "product_skus": [
                {
                    "sku_id": "sku-golden",
                    "spec_name": "Golden - 12 Pack",
                    "media_assets": [{"source_url": "https://example.com/golden-media.jpg"}],
                },
                {"sku_id": "sku-blue", "spec_name": "Blue - 12 Pack"},
            ],
        },
        chart_image_paths={"sku_chart": [{"file_token": "sku-chart-token"}]},
    )

    assert fields["SKU销量占比图"] == [{"file_token": "sku-chart-token"}]
    assert fields["父体规格"] == "Blue - 12 Pack"
    assert fields["父体图片"] == "https://example.com/blue.jpg"


def test_selection_projection_can_read_best_sku_from_sku_distribution_payload() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "data": {
                            "sku_list": [
                                {
                                    "sku_id": "sku-golden",
                                    "sku_name": "Golden - 12 Pack",
                                    "sku_sale_props": [{"prop_value": "Golden - 12 Pack"}],
                                },
                                {
                                    "sku_id": "sku-blue",
                                    "sku_name": "Blue - 12 Pack",
                                    "sku_sale_props": [{"prop_value": "Blue - 12 Pack", "image": "https://example.com/blue.jpg"}],
                                },
                            ],
                        }
                    },
                },
                {
                    "source_endpoint": "goods.sku_distribution",
                    "response_payload": {
                        "data": {
                            "best_sku": {"sku_value": "Blue - 12 Pack", "sold_count": 35},
                            "sku_units_sold": {
                                "Style": {
                                    "list": [
                                        {"source": "Golden - 12 Pack", "sold_count": 10},
                                        {"source": "Blue - 12 Pack", "sold_count": 35},
                                    ]
                                }
                            },
                        }
                    },
                },
            ],
            "product_skus": [],
        },
    )

    assert fields["父体规格"] == "Blue - 12 Pack"
    assert fields["父体图片"] == "https://example.com/blue.jpg"


def test_selection_projection_writes_best_sku_spec_without_unmatched_image() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "best_sku": {"sku_value": "Blue - 12 Pack", "sold_count": 35},
                        "sku_units_sold": {
                            "Style": {
                                "list": [
                                    {"source": "Golden - 12 Pack", "sold_count": 10},
                                    {"source": "Blue - 12 Pack", "sold_count": 35},
                                ]
                            }
                        },
                        "sku_list": [
                            {
                                "sku_id": "sku-golden",
                                "sku_name": "Golden - 12 Pack",
                                "sku_sale_props": [{"prop_value": "Golden - 12 Pack", "image": "https://example.com/golden.jpg"}],
                            },
                            {
                                "sku_id": "sku-blue",
                                "sku_name": "Blue - 12 Pack",
                                "sku_sale_props": [{"prop_value": "Blue - 12 Pack"}],
                            },
                        ],
                    },
                }
            ],
            "product_skus": [{"sku_id": "sku-blue", "spec_name": "Blue - 12 Pack"}],
        },
    )

    assert fields["父体规格"] == "Blue - 12 Pack"
    assert "父体图片" not in fields


def test_selection_projection_skips_single_sku_best_sku_as_non_distinct_analysis() -> None:
    fields = _projection_fields(
        fastmoss_bundle={
            "raw_api_responses": [
                {
                    "source_endpoint": "goods.skus",
                    "response_payload": {
                        "best_sku": {"sku_value": "2 Pack(AK-M4)", "sold_count": 40},
                        "sku_units_sold": {
                            "Specification Name": {
                                "list": [{"source": "2 Pack(AK-M4)", "sold_count": 40}],
                            }
                        },
                        "sku_list": [
                            {
                                "sku_id": "sku-single",
                                "sku_name": "CG-AKM4-2TZ",
                                "sku_sale_props": [
                                    {
                                        "prop_name": "Specification Name",
                                        "prop_value": "2 Pack(AK-M4)",
                                        "image": "https://example.com/single.jpg",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
            "product_skus": [{"sku_id": "sku-single", "spec_name": "2 Pack(AK-M4)"}],
        },
        chart_image_paths={"sku_chart": [{"file_token": "sku-chart-token"}]},
    )

    assert "SKU销量占比图" not in fields
    assert "父体规格" not in fields
    assert "父体图片" not in fields


def _projection_fields(
    *,
    fastmoss_bundle: dict[str, object],
    chart_image_paths: dict[str, object] | None = None,
) -> dict[str, object]:
    return selection_row_refresh._build_selection_projection_fields(
        source_context={},
        normalized_product_result={
            "product": {"product_id": PRODUCT_ID, "normalized_url": PRODUCT_URL},
            "logical_fields": {},
        },
        fastmoss_result={
            "product_fact_bundle": fastmoss_bundle,
            "metrics_snapshot": {"overview": {}},
        },
        media_result={},
        chart_image_paths=chart_image_paths,
    )
