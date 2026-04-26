from __future__ import annotations

import pytest

from automation_business_scaffold.infrastructure.tiktok import product_page
from automation_business_scaffold.infrastructure.tiktok.product_page import (
    SHOP_CANDIDATE_SELECTORS,
    TikTokProductExtractionError,
    TikTokProductUnavailableError,
    TikTokSecurityCheckError,
)
from automation_business_scaffold.models import TikTokProductRecord
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


def test_tiktok_product_request_fetch_promotes_all_request_metrics_to_product_fields() -> None:
    result = handler_module.tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {"product_id": "1730964478199763166"},
                "raw_request_result": {
                    "product": {
                        "product_id": "1730964478199763166",
                        "title": "Candy Boxes",
                        "shop_name": "Candy Shop",
                        "main_image_url": "https://cdn.example.com/main.jpg",
                        "price_text": "$13.24",
                        "price_amount": "13.24",
                        "price_currency": "USD",
                        "sales_count": 12000,
                        "rating_score": 4.8,
                        "review_count": 123,
                        "comment_count": 45,
                        "gallery_images": [{"source_url": "https://cdn.example.com/side-1.jpg"}],
                        "sku_images": [{"source_url": "https://cdn.example.com/sku-1.jpg"}],
                    }
                },
            }
        )
    )

    normalized = result.result["normalized_product_result"]
    product = normalized["product"]
    assert product["price_text"] == "$13.24"
    assert product["price_amount"] == "13.24"
    assert product["price_currency"] == "USD"
    assert product["sales_count"] == "12000"
    assert product["rating_score"] == "4.8"
    assert product["review_count"] == "123"
    assert product["comment_count"] == "45"
    assert len(product["gallery_images"]) == 1
    assert len(product["sku_images"]) == 1
    assert normalized["logical_fields"]["price_amount"] == "13.24"
    assert normalized["logical_fields"]["sales_count"] == "12000"
    assert len(normalized["logical_fields"]["gallery_images"]) == 1
    assert len(normalized["media_assets"]) == 3


def test_tiktok_product_request_fetch_cleans_sold_by_shop_label() -> None:
    result = handler_module.tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {"product_id": "1729421576968704007"},
                "raw_request_result": {
                    "product": {
                        "product_id": "1729421576968704007",
                        "title": "Squishball",
                        "shop_name": "Sold by JOYIN",
                        "main_image_url": "https://cdn.example.com/main.jpg",
                        "price_text": "$13.24",
                        "price_amount": "13.24",
                    }
                },
            }
        )
    )

    normalized = result.result["normalized_product_result"]
    assert normalized["product"]["shop_name"] == "JOYIN"
    assert normalized["logical_fields"]["shop_name"] == "JOYIN"


def test_browser_masked_price_is_not_promoted_to_numeric_amount() -> None:
    from automation_business_scaffold.infrastructure.tiktok.product_page import _normalize_price_amount

    assert _normalize_price_amount("$1*") == ""
    assert _normalize_price_amount("$13.24") == "13.24"


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


def test_tiktok_product_request_fetch_missing_router_data_requests_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(product_url: str, *, timeout: int = 30, session=None, request_pacer=None):  # noqa: ANN001
        del product_url, timeout, session, request_pacer
        raise TikTokProductExtractionError("failed to locate script tag: __MODERN_ROUTER_DATA__")

    monkeypatch.setattr(handler_module, "fetch_tiktok_product_record", fake_fetch)

    result = handler_module.tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1730892854181139253",
                    "product_url": "https://www.tiktok.com/shop/pdp/1730892854181139253",
                },
                "fallback_allowed": True,
            }
        )
    )

    assert result.status == "fallback_required"
    assert result.result["fallback_required"] is True
    assert result.result["fallback_reason"] == "request_signal_missing_router_data"
    assert result.result["request_attempt"]["attempted"] is True
    assert result.result["request_attempt"]["fallback_signal"] is True


def test_tiktok_product_request_fetch_unavailable_is_terminal_without_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(product_url: str, *, timeout: int = 30, session=None, request_pacer=None):  # noqa: ANN001
        del timeout, session, request_pacer
        raise TikTokProductUnavailableError(f"TikTok product unavailable: Product not available in this country or region: {product_url}")

    monkeypatch.setattr(handler_module, "fetch_tiktok_product_record", fake_fetch)

    result = handler_module.tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1730730317348636561",
                    "product_url": "https://www.tiktok.com/shop/pdp/1730730317348636561",
                },
                "fallback_allowed": True,
            }
        )
    )

    assert result.status == "success"
    assert result.result["fallback_required"] is False
    assert result.result["request_attempt"]["attempted"] is True
    assert result.result["request_attempt"]["fallback_signal"] is False
    assert result.result["request_attempt"]["terminal_signal"] == "product_unavailable"
    normalized = result.result["normalized_product_result"]
    assert normalized["product"]["status"] == "off_shelf_or_region_unavailable"
    assert normalized["product"]["facts"]["availability_status"] == "unavailable"


def test_browser_shop_selectors_do_not_match_category_breadcrumb_links() -> None:
    assert "a[href*='/shop/']" not in SHOP_CANDIDATE_SELECTORS
    assert "a[href*='/shop/store/']" in SHOP_CANDIDATE_SELECTORS


def test_browser_record_prefers_dom_main_image_over_router_image(monkeypatch: pytest.MonkeyPatch) -> None:
    router_record = TikTokProductRecord(
        source_url="https://www.tiktok.com/shop/pdp/1729743550572237626",
        resolved_url="https://www.tiktok.com/shop/pdp/1729743550572237626",
        normalized_url="https://www.tiktok.com/shop/pdp/1729743550572237626",
        product_id="1729743550572237626",
        title="Router Product",
        holiday="",
        main_image_url="https://cdn.example.com/router-or-description.webp",
        price_amount="12.99",
        price_currency="USD",
        price_text="$12.99",
        sales_count=0,
        shop_name="Router Shop",
        shop_url="",
    )

    monkeypatch.setattr(product_page, "extract_tiktok_product_from_html", lambda *args, **kwargs: router_record)

    record = product_page._build_record_from_browser_state(
        html="<html></html>",
        source_url="https://www.tiktok.com/shop/pdp/1729743550572237626",
        resolved_url="https://www.tiktok.com/shop/pdp/1729743550572237626",
        dom_snapshot={
            "product_id": "1729743550572237626",
            "title_text": "DOM Product",
            "main_image_url": "https://cdn.example.com/first-screen-current-slide.webp",
            "price_text": "$12.99",
            "shop_name": "Sold by DOM Shop",
        },
    )

    assert record.main_image_url == "https://cdn.example.com/first-screen-current-slide.webp"
