from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import automation_business_scaffold.capabilities.browser.tiktok_product_fetch_handler as browser_handler
from automation_business_scaffold.contracts.handler.contract import HandlerContext


@dataclass
class _FakeProduct:
    product_id: str = "1730964478199763166"
    normalized_url: str = "https://www.tiktok.com/shop/pdp/1730964478199763166"

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "source_url": self.normalized_url,
            "resolved_url": self.normalized_url,
            "normalized_url": self.normalized_url,
            "title": "Candy Boxes",
            "holiday": "毕业季",
            "main_image_url": "https://cdn.example.com/main.webp",
            "main_image_local_path": "/tmp/1730964478199763166-main.webp",
            "main_image_file_name": "1730964478199763166-main.webp",
            "main_image_mime_type": "image/webp",
            "price_text": "$13.24",
            "shop_name": "Example Shop",
            "rating_score": 4.8,
            "review_count": 123,
            "comment_count": 45,
            "gallery_images": [
                {
                    "source_url": "https://cdn.example.com/side.webp",
                    "display_order": 1,
                }
            ],
            "sku_images": [
                {
                    "image_url": "https://cdn.example.com/sku.webp",
                    "option_name": "Color",
                    "option_value": "Blue",
                }
            ],
            "skus": [
                {
                    "sku_id": "sku-1",
                    "sku_name": "Blue",
                    "spec_name": "Color: Blue",
                }
            ],
            "slider_captcha_resolution": {
                "attempted": True,
                "resolved": True,
                "reason": "slider_cleared",
                "attempts": [
                    {
                        "attempt": 1,
                        "target_x": 150,
                        "drag_distance": 85.0,
                        "coordinate_mapping": {"drag_distance": 85.0},
                    }
                ],
            },
            "slider_captcha_audit_artifact_refs": [
                {
                    "artifact_key": "slider_attempt_1_background_image",
                    "local_path": "/tmp/slider/background.png",
                    "mime_type": "image/png",
                }
            ],
        }


def _context(payload: dict[str, Any]) -> HandlerContext:
    return HandlerContext(
        request_id="req-browser",
        job_id="browser-job",
        handler_code="tiktok_product_browser_fetch",
        worker_type="browser_worker",
        runtime_table="task_execution",
        item_code="tiktok_product_browser_fetch",
        payload=payload,
    )


def test_tiktok_product_browser_fetch_reuses_legacy_product_fetch(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(product_url: str, **kwargs: Any) -> _FakeProduct:
        captured["product_url"] = product_url
        captured.update(kwargs)
        return _FakeProduct()

    monkeypatch.setattr(browser_handler, "fetch_tiktok_product_record_via_browser", fake_fetch)

    result = browser_handler.tiktok_product_browser_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1730964478199763166",
                    "product_url": "https://www.tiktok.com/shop/pdp/1730964478199763166",
                }
            }
        )
    )

    assert result.status == "success"
    assert captured["capture_page_screenshot"] is False
    assert captured["slider_captcha_audit_dir"] == ""
    normalized = result.result["normalized_product_result"]
    assert normalized["product"]["title"] == "Candy Boxes"
    assert normalized["product"]["facts"]["rating_score"] == "4.8"
    assert normalized["product"]["facts"]["review_count"] == "123"
    assert normalized["product"]["facts"]["comment_count"] == "45"
    assert normalized["product_skus"][0]["sku_id"] == "sku-1"
    assert [asset["media_role"] for asset in normalized["media_assets"]] == [
        "product_main_image",
        "product_gallery_image",
        "product_sku_image",
    ]
    assert normalized["media_assets"][0]["local_path"] == "/tmp/1730964478199763166-main.webp"
    assert result.summary["slider_captcha_attempted"] is True
    assert result.summary["slider_captcha_resolved"] is True
    assert result.result["slider_captcha_resolution"]["attempts"][0]["coordinate_mapping"]["drag_distance"] == 85.0
    assert result.result["slider_captcha_audit_artifact_refs"][0]["artifact_key"] == "slider_attempt_1_background_image"


def test_tiktok_product_browser_fetch_passes_framework_slider_configuration(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(product_url: str, **kwargs: Any) -> _FakeProduct:
        captured["product_url"] = product_url
        captured.update(kwargs)
        return _FakeProduct()

    monkeypatch.setattr(browser_handler, "fetch_tiktok_product_record_via_browser", fake_fetch)

    result = browser_handler.tiktok_product_browser_fetch_handler(
        _context(
            {
                "product_url": "https://www.tiktok.com/shop/pdp/1730964478199763166",
                "slider_captcha_audit_dir": "/tmp/tiktok-slider-audit",
                "slider_captcha_provider_config": {
                    "import_onnx_path": "/models/slider.onnx",
                    "charsets_path": "/models/charsets.json",
                },
                "slider_captcha_resolver_config": {
                    "max_attempts": 2,
                    "simple_target": True,
                    "drag_offset_x": -3,
                },
                "slider_captcha_selectors": {
                    "popup": "#tts_web_captcha_container",
                    "background": "#captcha-verify-image",
                    "piece": ".captcha_verify_img_slide",
                    "handle": ".secsdk-captcha-drag-icon",
                    "refresh": ".secsdk_captcha_refresh",
                },
            }
        )
    )

    assert result.status == "success"
    assert captured["slider_captcha_audit_dir"] == "/tmp/tiktok-slider-audit"
    assert captured["slider_captcha_provider_config"]["import_onnx_path"] == "/models/slider.onnx"
    assert captured["slider_captcha_provider_config"]["charsets_path"] == "/models/charsets.json"
    assert captured["slider_captcha_resolver_config"]["max_attempts"] == 2
    assert captured["slider_captcha_resolver_config"]["simple_target"] is True
    assert captured["slider_captcha_resolver_config"]["drag_offset_x"] == -3
    assert captured["slider_captcha_selectors"]["handle"] == ".secsdk-captcha-drag-icon"


def test_tiktok_product_browser_fetch_returns_unavailable_as_terminal_result(monkeypatch) -> None:
    def fake_fetch(product_url: str, **kwargs: Any) -> _FakeProduct:
        del product_url, kwargs
        raise browser_handler.TikTokProductUnavailableError("Product not available in this country or region")

    monkeypatch.setattr(browser_handler, "fetch_tiktok_product_record_via_browser", fake_fetch)

    result = browser_handler.tiktok_product_browser_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1732308866040173150",
                    "product_url": "https://www.tiktok.com/shop/pdp/1732308866040173150",
                }
            }
        )
    )

    assert result.status == "success"
    assert result.result["availability_status"] == "unavailable"
    normalized = result.result["normalized_product_result"]
    assert normalized["product"]["status"] == "off_shelf_or_region_unavailable"
    assert normalized["product"]["facts"]["availability_status"] == "unavailable"
