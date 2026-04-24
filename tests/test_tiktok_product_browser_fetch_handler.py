from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from automation_business_scaffold.capabilities.browser import tiktok_product_fetch_handler as browser_handler
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
    assert normalized["product"]["facts"]["availability_status"] == "unavailable"
