from __future__ import annotations

from automation_business_scaffold.capabilities.fact_sources.tiktok.product_request_fetch_handler import (
    tiktok_product_request_fetch_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext


def _context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-tiktok-product",
        job_id="job-tiktok-product",
        handler_code="tiktok_product_request_fetch",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        job_code="tiktok_product_request_fetch",
        payload=payload,
    )


def test_tiktok_product_request_fetch_requests_fallback_when_detail_payload_missing() -> None:
    result = tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1730964478199763166",
                    "product_url": "https://www.tiktok.com/shop/pdp/1730964478199763166",
                },
                "fallback_allowed": True,
            }
        )
    )

    assert result.status == "fallback_required"
    assert result.result["fallback_required"] is True
    assert result.result["fallback_reason"] == "request_payload_missing_product_detail"


def test_tiktok_product_request_fetch_keeps_inline_request_payload_on_request_path() -> None:
    result = tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {"product_id": "1730964478199763166"},
                "raw_request_result": {
                    "product": {
                        "product_id": "1730964478199763166",
                        "title": "Candy Boxes",
                        "main_image_url": "https://cdn.example.com/main.jpg",
                        "main_image_local_path": "/tmp/main.jpg",
                        "main_image_file_name": "main.jpg",
                        "main_image_mime_type": "image/jpeg",
                    }
                },
            }
        )
    )

    assert result.status == "success"
    normalized = result.result["normalized_product_result"]
    assert normalized["product"]["title"] == "Candy Boxes"
    assert normalized["media_assets"][0]["source_url"] == "https://cdn.example.com/main.jpg"
    assert normalized["media_assets"][0]["local_path"] == "/tmp/main.jpg"
