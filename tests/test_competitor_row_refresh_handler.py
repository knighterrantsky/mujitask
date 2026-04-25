from __future__ import annotations

import importlib

import pytest

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)

handler_module = importlib.import_module(
    "automation_business_scaffold.capabilities.fact_sources.tiktok.competitor_row_refresh_handler"
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
                        "day90_sold_count": "600",
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

    monkeypatch.setattr(handler_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(handler_module, "media_asset_sync_handler", fake_media)
    monkeypatch.setattr(handler_module, "fastmoss_product_fetch_handler", fake_fastmoss)
    monkeypatch.setattr(handler_module, "fact_bundle_upsert_handler", fake_fact)
    monkeypatch.setattr(handler_module, "feishu_table_write_handler", fake_write)

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
                    "execution_control_db_url": "postgresql+psycopg://runtime",
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
    assert result.result["runtime_evidence"]["browser_fallback_used"] is False
    assert media_payloads[0]["sync_referenced_files"] is True
    assert media_payloads[0]["require_materialized_assets"] is True
    assert [asset["media_role"] for asset in media_payloads[0]["asset_refs"]] == [
        "product_main_image",
        "product_sku_image",
    ]
    assert fact_payloads[0]["request_payload"]["execution_control_db_url"] == "postgresql+psycopg://runtime"
    assert str(fastmoss_payloads[0]["fastmoss_window_days"]) == "90"
    assert [asset["media_role"] for asset in fact_payloads[0]["fact_bundle"]["media_assets"]] == [
        "product_main_image",
        "product_sku_image",
    ]
    assert write_payloads[0]["records"][0]["projection_fields"]["图片"]["local_path"] == "/tmp/main.jpg"


def test_competitor_row_refresh_handler_uses_browser_fallback_inside_row_job(
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

    class _BrowserOutcome:
        def __init__(self, context: HandlerContext) -> None:
            self.worker_result = HandlerResult.success(
                context,
                result={
                    "normalized_product_result": {
                        "product": {
                            "product_id": "123456789",
                            "product_url": "https://www.tiktok.com/shop/pdp/123456789",
                            "normalized_url": "https://www.tiktok.com/shop/pdp/123456789",
                            "title": "Recovered Product",
                            "price_text": "$10.00",
                        },
                        "logical_fields": {
                            "title": "Recovered Product",
                            "price_text": "$10.00",
                        },
                        "fact_bundle": {"products": [{"product_id": "123456789"}]},
                    }
                },
            )
            self.execution_mode = "child_process"
            self.progress_stage = "browser_ready"
            self.child_runner = None

        def to_dict(self) -> dict:
            return {
                "execution_mode": self.execution_mode,
                "progress_stage": self.progress_stage,
            }

    def fake_run_supervised_handler(**kwargs):  # noqa: ANN003
        return _BrowserOutcome(kwargs["context"])

    def fake_fastmoss(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="transport_failure",
                error_code="fastmoss_http_failure",
                message="temporary fastmoss issue",
                retryable=True,
            ),
        )

    def fake_fact(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(context, result={"upserted_entities": ["product:123456789"]})

    def fake_write(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(context, result={"written_count": 1})

    monkeypatch.setattr(handler_module, "tiktok_product_request_fetch_handler", fake_tiktok)
    monkeypatch.setattr(handler_module, "run_supervised_handler", fake_run_supervised_handler)
    monkeypatch.setattr(handler_module, "fastmoss_product_fetch_handler", fake_fastmoss)
    monkeypatch.setattr(handler_module, "fact_bundle_upsert_handler", fake_fact)
    monkeypatch.setattr(handler_module, "feishu_table_write_handler", fake_write)

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

    assert result.status == "partial_success"
    assert result.result["runtime_evidence"]["browser_fallback_used"] is True
    assert result.result["step_timeline"][1]["status"] == "success"
    assert result.result["step_timeline"][3]["status"] == "failed"
