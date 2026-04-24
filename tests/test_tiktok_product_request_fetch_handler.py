from __future__ import annotations

import pytest

from automation_business_scaffold.business.flows.achieve.tiktok_product_flow import (
    TikTokSecurityCheckError,
)
from automation_business_scaffold.capabilities.fact_sources.tiktok import product_request_fetch_handler as handler_module
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


def test_tiktok_product_request_fetch_keeps_inline_request_payload_on_request_path() -> None:
    result = handler_module.tiktok_product_request_fetch_handler(
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
    assert result.result["request_attempt"]["attempted"] is False
    assert result.result["request_attempt"]["request_source"] == "inline_payload"


def test_tiktok_product_request_fetch_live_request_falls_back_only_after_explicit_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(product_url: str, *, timeout: int = 30, session=None, request_pacer=None):  # noqa: ANN001
        del timeout, session, request_pacer
        raise TikTokSecurityCheckError(f"captcha required for {product_url}")

    monkeypatch.setattr(handler_module, "fetch_tiktok_product_record", fake_fetch)

    result = handler_module.tiktok_product_request_fetch_handler(
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
    assert result.result["fallback_reason"] == "request_signal_security_check"
    assert result.result["request_attempt"]["attempted"] is True
    assert result.result["request_attempt"]["fallback_signal"] is True


def test_tiktok_product_request_fetch_network_failure_stays_retryable_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(product_url: str, *, timeout: int = 30, session=None, request_pacer=None):  # noqa: ANN001
        del product_url, timeout, session, request_pacer
        raise ConnectionError("temporary network issue")

    monkeypatch.setattr(handler_module, "fetch_tiktok_product_record", fake_fetch)

    result = handler_module.tiktok_product_request_fetch_handler(
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

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.retryable is True
    assert result.error.error_code == "tiktok_request_transport_failed"
    assert result.result["request_attempt"]["attempted"] is True
