from __future__ import annotations

import importlib

import pytest

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)

handler_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.jobs.competitor_row_refresh"
)
flow_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.flows.competitor_row_refresh.pipeline.finalization"
)


def _context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-competitor-row",
        job_id="job-competitor-row",
        handler_code="competitor_row_refresh",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        workflow_code="refresh_current_competitor_table",
        stage_code="collect_product_data",
        job_code="competitor_row_refresh",
        business_key="product:123456789",
        dedupe_key="req-competitor-row:collect_product_data:product:123456789",
        payload=payload,
    )


def test_competitor_row_refresh_handler_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fact_payloads: list[dict] = []
    media_payloads: list[dict] = []
    fastmoss_payloads: list[dict] = []
    write_payloads: list[dict] = []

    def fake_tiktok(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            result={
                "normalized_product_result": {
                    "product": {
                        "product_id": "123456789",
                        "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "normalized_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "title": "Graduation Kit",
                        "holiday": "Graduation",
                        "seller_name": "Graduation Shop",
                        "price_text": "$12.99",
                    },
                    "logical_fields": {
                        "title": "Graduation Kit",
                        "holiday": "Graduation",
                        "shop_name": "Graduation Shop",
                        "price_text": "$12.99",
                        "main_image_url": "https://cdn.example.com/main.jpg",
                    },
                    "media_assets": [
                        {
                            "source_url": "https://cdn.example.com/main.jpg",
                            "entity_type": "product",
                            "entity_external_id": "123456789",
                            "media_role": "product_main_image",
                        },
                        {
                            "source_url": "https://cdn.example.com/main.jpg",
                            "entity_type": "product",
                            "entity_external_id": "123456789",
                            "media_role": "product_sku_image",
                        }
                    ],
                    "fact_bundle": {
                        "products": [
                            {
                                "product_id": "123456789",
                                "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                                "normalized_url": "https://www.tiktok.com/shop/pdp/123456789",
                                "title": "Graduation Kit",
                                "seller_name": "Graduation Shop",
                            }
                        ]
                    },
                },
                "request_attempt": {"attempted": True, "request_source": "live_request"},
            },
        )

    def fake_media(context: HandlerContext) -> HandlerResult:
        media_payloads.append(dict(context.payload))
        return HandlerResult.success(
            context,
            result={
                "synced_assets": [
                    {
                        "source_url": "https://cdn.example.com/main.jpg",
                        "object_key": "runtime/media/main.jpg",
                        "remote_uri": "s3://bucket/runs/job-competitor-row/main.jpg",
                        "source_path": "/tmp/main.jpg",
                        "file_name": "main.jpg",
                        "mime_type": "image/jpeg",
                        "entity_type": "product",
                        "entity_external_id": "123456789",
                        "media_role": "product_main_image",
                    },
                    {
                        "source_url": "https://cdn.example.com/main.jpg",
                        "object_key": "runtime/media/main.jpg",
                        "remote_uri": "s3://bucket/runs/job-competitor-row/main.jpg",
                        "source_path": "/tmp/main.jpg",
                        "file_name": "main.jpg",
                        "mime_type": "image/jpeg",
                        "entity_type": "product",
                        "entity_external_id": "123456789",
                        "media_role": "product_sku_image",
                    }
                ],
                "media_fact_bundle": {
                    "media_assets": [
                        {
                            "source_url": "https://cdn.example.com/main.jpg",
                            "object_key": "runtime/media/main.jpg",
                            "remote_uri": "s3://bucket/runs/job-competitor-row/main.jpg",
                            "source_path": "/tmp/main.jpg",
                            "file_name": "main.jpg",
                            "mime_type": "image/jpeg",
                            "entity_type": "product",
                            "entity_external_id": "123456789",
                            "media_role": "product_main_image",
                        },
                        {
                            "source_url": "https://cdn.example.com/main.jpg",
                            "object_key": "runtime/media/main.jpg",
                            "remote_uri": "s3://bucket/runs/job-competitor-row/main.jpg",
                            "source_path": "/tmp/main.jpg",
                            "file_name": "main.jpg",
                            "mime_type": "image/jpeg",
                            "entity_type": "product",
                            "entity_external_id": "123456789",
                            "media_role": "product_sku_image",
                        }
                    ]
                },
            },
        )

    def fake_fastmoss(context: HandlerContext) -> HandlerResult:
        fastmoss_payloads.append(dict(context.payload))
        return HandlerResult.success(
            context,
            result={
                "product_fact_bundle": {
                    "product_daily_metrics": [
                        {"metric_date": "2026-04-24", "sold_count": 10},
                        {"metric_date": "2026-04-25", "sold_count": 12},
                    ]
                },
                "metrics_snapshot": {
                    "overview": {
                        "real_price": "12.99",
                        "yday_sold_count": "12",
                        "day7_sold_count": "88",
                        "sales_90d": "600",
                    }
                },
            },
        )

    def fake_fact(context: HandlerContext) -> HandlerResult:
        fact_payloads.append(dict(context.payload))
        return HandlerResult.success(
            context,
            result={"upserted_entities": ["product:123456789"]},
        )

    def fake_write(context: HandlerContext) -> HandlerResult:
        write_payloads.append(dict(context.payload))
        return HandlerResult.success(
            context,
            result={"written_count": 1, "target_record_ids": ["row-1"]},
        )

    monkeypatch.setattr(flow_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(flow_module, "media_asset_sync_handler", fake_media)
    monkeypatch.setattr(flow_module, "fastmoss_product_fetch_handler", fake_fastmoss)
    monkeypatch.setattr(flow_module, "fact_bundle_upsert_handler", fake_fact)
    monkeypatch.setattr(flow_module, "feishu_table_write_handler", fake_write)

    result = handler_module.competitor_row_refresh_handler(
        _context(
            {
                "source_record_id": "row-1",
                "source_table_ref": "feishu://mujitask/TK竞品收集",
                "product_identity": {
                    "product_id": "123456789",
                    "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                },
                "request_payload": {
                    "persistence": {"fact_db_configured": True},
                    "artifact_store": {"provider": "minio", "bucket": "pytest-runtime-artifacts"},
                },
                "source_context": {
                    "source_fields": {
                        "SKU-ID": "",
                        "产品链接": "",
                        "图片": "",
                        "标题": "",
                    }
                },
            }
        )
    )

    assert result.status == "success"
    assert [step["step"] for step in result.result["step_timeline"]] == [
        "tiktok_request",
        "browser_fallback",
        "media_sync",
        "fastmoss_fetch",
        "fact_db_upsert",
        "feishu_writeback",
    ]
    assert result.result["writeback_projection"]["fields"]["SKU-ID"] == "123456789"
    assert result.result["writeback_projection"]["fields"]["标题"] == "Graduation Kit"
    assert result.result["writeback_projection"]["fields"]["图片"]["local_path"] == "/tmp/main.jpg"
    assert result.result["writeback_projection"]["fields"]["近90天销量"] == "600"
    assert result.result["runtime_evidence"]["browser_fallback_used"] is False
    assert media_payloads[0]["sync_referenced_files"] is True
    assert media_payloads[0]["require_materialized_assets"] is True
    assert [asset["media_role"] for asset in media_payloads[0]["asset_refs"]] == [
        "product_main_image",
        "product_sku_image",
    ]
    assert fact_payloads[0]["request_payload"]["persistence"]["fact_db_configured"] is True
    assert "execution_control_db_url" not in fact_payloads[0]["request_payload"]
    assert fastmoss_payloads[0]["fastmoss_overview_window_days"] == [7, 28, 90]
    assert str(fastmoss_payloads[0]["fastmoss_window_days"]) == "90"
    assert str(fastmoss_payloads[0]["fastmoss_sku_window_days"]) == "28"
    assert [asset["media_role"] for asset in fact_payloads[0]["fact_bundle"]["media_assets"]] == [
        "product_main_image",
        "product_sku_image",
    ]
    assert write_payloads[0]["records"][0]["projection_fields"]["图片"]["local_path"] == "/tmp/main.jpg"


def test_competitor_row_refresh_handler_returns_tiktok_browser_fallback_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_tiktok(context: HandlerContext) -> HandlerResult:
        return HandlerResult.fallback_required(
            context,
            error=HandlerError(
                error_type="fallback_required",
                error_code="tiktok_browser_fallback_required",
                message="security check",
                retryable=False,
                fallback_allowed=True,
                fallback_reason="request_signal_security_check",
            ),
            result={
                "fallback_required": True,
                "fallback_reason": "request_signal_security_check",
                "fallback_source_job_id": context.job_id,
                "request_attempt": {
                    "attempted": True,
                    "request_source": "live_request",
                    "fallback_signal": True,
                    "fallback_reason": "request_signal_security_check",
                },
            },
        )

    def fail_if_called(context: HandlerContext) -> HandlerResult:
        raise AssertionError(f"{context.handler_code} should not run after fallback_required")

    monkeypatch.setattr(flow_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(flow_module, "fastmoss_product_fetch_handler", fail_if_called)
    monkeypatch.setattr(flow_module, "fact_bundle_upsert_handler", fail_if_called)
    monkeypatch.setattr(flow_module, "feishu_table_write_handler", fail_if_called)

    result = handler_module.competitor_row_refresh_handler(
        _context(
            {
                "source_record_id": "row-1",
                "source_table_ref": "feishu://mujitask/TK竞品收集",
                "product_identity": {
                    "product_id": "123456789",
                    "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                },
                "source_context": {"source_fields": {}},
            }
        )
    )

    assert result.status == "fallback_required"
    assert result.next_action.type == "browser_fallback"
    assert result.summary["fallback_handler"] == "tiktok_product_browser_fetch"
    assert result.result["row_status"] == "fallback_required"
    assert result.result["browser_fallback_payload"]["fallback_source_job_id"].endswith(
        ":tiktok_request"
    )
    assert result.result["step_timeline"][1] == {
        "step": "browser_fallback",
        "status": "fallback_required",
        "fallback_handler": "tiktok_product_browser_fetch",
    }


def test_competitor_row_refresh_returns_fastmoss_security_browser_fallback_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fastmoss_calls: list[dict] = []

    def fake_tiktok(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            result={
                "normalized_product_result": {
                    "product": {
                        "product_id": "123456789",
                        "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "normalized_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "title": "Recovered Product",
                    },
                    "logical_fields": {"title": "Recovered Product"},
                    "fact_bundle": {"products": [{"product_id": "123456789"}]},
                }
            },
        )

    def fake_fastmoss(context: HandlerContext) -> HandlerResult:
        fastmoss_calls.append(dict(context.payload))
        if len(fastmoss_calls) == 1:
            return HandlerResult.fallback_required(
                context,
                error=HandlerError(
                    error_type="security_verification",
                    error_code="fastmoss_security_verification_required",
                    message="FastMoss request failed",
                    retryable=False,
                    fallback_allowed=True,
                    fallback_reason="fastmoss_api_security_verification",
                    details={"response_code": "MSG_SAFE_0001", "path": "/api/goods/v3/base"},
                ),
                result={
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_api_security_verification",
                    "verification_request": {
                        "method": "GET",
                        "path": "/api/goods/v3/base",
                        "params": {"product_id": "123456789"},
                        "region": "US",
                    },
                    "fastmoss": {"phone_env": "FASTMOSS_PHONE"},
                },
            )
        return HandlerResult.success(
            context,
            result={
                "product_fact_bundle": {},
                "metrics_snapshot": {"overview": {"day7_sold_count": "412"}},
            },
        )

    def fake_fact(context: HandlerContext) -> HandlerResult:
        raise AssertionError(f"{context.handler_code} should wait for browser fallback")

    def fake_write(context: HandlerContext) -> HandlerResult:
        raise AssertionError(f"{context.handler_code} should wait for browser fallback")

    monkeypatch.setattr(flow_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(flow_module, "fastmoss_product_fetch_handler", fake_fastmoss)
    monkeypatch.setattr(flow_module, "fact_bundle_upsert_handler", fake_fact)
    monkeypatch.setattr(flow_module, "feishu_table_write_handler", fake_write)

    result = handler_module.competitor_row_refresh_handler(
        _context(
            {
                "source_record_id": "row-1",
                "source_table_ref": "feishu://mujitask/TK竞品收集",
                "product_identity": {
                    "product_id": "123456789",
                    "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                },
                "source_context": {"source_fields": {}},
            }
        )
    )

    assert result.status == "fallback_required"
    assert len(fastmoss_calls) == 1
    assert result.summary["fallback_handler"] == "fastmoss_security_browser_resolve"
    assert result.result["browser_fallback_payload"]["verification_request"]["path"] == "/api/goods/v3/base"
    assert result.result["normalized_product_result"]["product"]["product_id"] == "123456789"
    assert [step["step"] for step in result.result["step_timeline"]] == [
        "tiktok_request",
        "browser_fallback",
        "media_sync",
        "fastmoss_fetch",
        "fastmoss_security_browser_fallback",
    ]


def test_competitor_row_refresh_unavailable_skips_browser_media_and_fastmoss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact_payloads: list[dict] = []
    write_payloads: list[dict] = []

    def fake_tiktok(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            result={
                "normalized_product_result": {
                    "product": {
                        "product_id": "123456789",
                        "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "normalized_url": "https://www.tiktok.com/shop/pdp/123456789",
                        "status": "off_shelf_or_region_unavailable",
                        "facts": {
                            "collection_path": "request",
                            "availability_status": "unavailable",
                            "unavailable_message": "Product not available in this country or region",
                        },
                    },
                    "fact_bundle": {
                        "products": [
                            {
                                "product_id": "123456789",
                                "status": "off_shelf_or_region_unavailable",
                                "facts": {
                                    "availability_status": "unavailable",
                                    "unavailable_message": "Product not available in this country or region",
                                },
                            }
                        ]
                    },
                },
                "fallback_required": False,
                "request_attempt": {
                    "attempted": True,
                    "request_source": "live_request",
                    "fallback_signal": False,
                    "terminal_signal": "product_unavailable",
                },
            },
        )

    def fail_if_called(context: HandlerContext) -> HandlerResult:
        raise AssertionError(f"{context.handler_code} should not be called for unavailable products")

    def fake_fact(context: HandlerContext) -> HandlerResult:
        fact_payloads.append(dict(context.payload))
        return HandlerResult.success(context, result={"upserted_entities": ["product:123456789"]})

    def fake_write(context: HandlerContext) -> HandlerResult:
        write_payloads.append(dict(context.payload))
        return HandlerResult.success(context, result={"written_count": 1})

    monkeypatch.setattr(flow_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(flow_module, "media_asset_sync_handler", fail_if_called)
    monkeypatch.setattr(flow_module, "fastmoss_product_fetch_handler", fail_if_called)
    monkeypatch.setattr(flow_module, "fact_bundle_upsert_handler", fake_fact)
    monkeypatch.setattr(flow_module, "feishu_table_write_handler", fake_write)

    result = handler_module.competitor_row_refresh_handler(
        _context(
            {
                "source_record_id": "row-1",
                "source_table_ref": "feishu://mujitask/TK竞品收集",
                "product_identity": {
                    "product_id": "123456789",
                    "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                },
                "source_context": {"source_fields": {"SKU-ID": "123456789"}},
            }
        )
    )

    assert result.status == "success"
    assert result.result["row_status"] == "unavailable"
    assert result.result["runtime_evidence"]["browser_fallback_used"] is False
    assert [(step["step"], step["status"]) for step in result.result["step_timeline"]] == [
        ("tiktok_request", "success"),
        ("browser_fallback", "skipped"),
        ("media_sync", "skipped"),
        ("fastmoss_fetch", "skipped"),
        ("fact_db_upsert", "success"),
        ("feishu_writeback", "success"),
    ]
    assert fact_payloads[0]["fact_bundle"]["products"][0]["status"] == "off_shelf_or_region_unavailable"
    assert write_payloads[0]["records"][0]["projection_fields"]["商品状态"] == "已下架/区域不可售"
