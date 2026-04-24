from __future__ import annotations

# ruff: noqa: E402

import pytest

pytest.skip(
    "Legacy TikTok product browser flow was archived under business/flows/achieve during the runtime rewrite.",
    allow_module_level=True,
)

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from automation_business_scaffold.business.flows import (
    TikTokRateLimitError,
    build_feishu_bitable_record,
    download_tiktok_product_main_image,
    extract_tiktok_product_from_html,
    fetch_tiktok_product_record_via_browser,
    infer_tiktok_product_holiday,
    normalize_tiktok_product_url,
)
from automation_business_scaffold.infrastructure.rate_limit.request_pacer import RequestPacer, RequestPacerConfig
from automation_business_scaffold.validators import validate_tiktok_product_url


SAMPLE_ROUTER_DATA = {
    "loaderData": {
        "(region)/pdp/(product_name_slug$)/(product_id)/page": {
            "page_config": {
                "components_map": [
                    {"component_type": "other", "component_name": "other"},
                    {
                        "component_type": "product_info",
                        "component_name": "product_info",
                        "component_data": {
                            "product_info": {
                                "product_model": {
                                    "product_id": "1729732615040962895",
                                    "sold_count": "94151",
                                    "rating_score": "4.7",
                                    "review_count": "1.2K",
                                    "comment_count": "345",
                                    "name": "Sample TikTok Product",
                                    "images": [
                                        {
                                            "height": 1400,
                                            "width": 1400,
                                            "uri": "tos-sample/image-1",
                                            "url_list": [
                                                "https://example.com/main-image.webp",
                                            ],
                                        },
                                        {
                                            "height": 1400,
                                            "width": 1400,
                                            "uri": "tos-sample/image-2",
                                            "url_list": [
                                                "https://example.com/side-image.webp",
                                            ],
                                        }
                                    ],
                                    "sku_property_image_map": {
                                        "Color:Pink": {
                                            "height": 800,
                                            "width": 800,
                                            "uri": "tos-sample/sku-pink",
                                            "url_list": [
                                                "https://example.com/sku-pink.webp",
                                            ],
                                        }
                                    },
                                    "sku_sale_props": [
                                        {
                                            "prop_name": "Color",
                                            "sale_prop_values": [
                                                {
                                                    "prop_value": "Pink",
                                                    "prop_value_id": "pink-id",
                                                }
                                            ],
                                        }
                                    ],
                                    "sku_list": [
                                        {
                                            "sku_id": "sku-pink",
                                            "sku_sale_props": [
                                                {
                                                    "prop_name": "Color",
                                                    "prop_value": "Pink",
                                                    "prop_value_id": "pink-id",
                                                }
                                            ],
                                        }
                                    ],
                                },
                                "promotion_model": {
                                    "promotion_product_price": {
                                        "min_price": {
                                            "currency_name": "USD",
                                            "currency_symbol": "$",
                                            "sale_price_decimal": "24.99",
                                            "sale_price_format": "24.99",
                                            "origin_price_decimal": "49.99",
                                            "discount_format": "50%",
                                        }
                                    }
                                },
                                "seller_model": {
                                    "shop_name": "Sample Shop",
                                },
                            },
                            "shop_info": {
                                "shop_name": "Sample Shop",
                                "shop_link": "https://shop.tiktok.com/us/store/sample-shop/123",
                                "sold_count": 246407,
                                "format_sold_count": "246.4K",
                            },
                        },
                    },
                ]
            }
        }
    },
    "errors": None,
}

SAMPLE_HTML = (
    "<html><body>"
    '<script id="__MODERN_ROUTER_DATA__" type="application/json">'
    f"{json.dumps(SAMPLE_ROUTER_DATA, ensure_ascii=True)}"
    "</script>"
    "</body></html>"
)


class _FakeImageResponse:
    def __init__(self, content: bytes, content_type: str = "image/webp") -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None


class _FakeImageSession:
    def __init__(self, content: bytes, content_type: str = "image/webp") -> None:
        self.response = _FakeImageResponse(content=content, content_type=content_type)

    def get(self, *_args, **_kwargs) -> _FakeImageResponse:
        return self.response

    def close(self) -> None:
        return None


class _FakeProductPageResponse:
    def __init__(self, *, status_code: int = 200, content: bytes | None = None) -> None:
        self.status_code = status_code
        self.url = "https://www.tiktok.com/shop/pdp/1729732615040962895"
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.content = content if content is not None else SAMPLE_HTML.encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise TikTokRateLimitError(f"HTTP {self.status_code}")


class _FakeProductPageSession:
    def __init__(self, response: _FakeProductPageResponse | None = None) -> None:
        self.response = response or _FakeProductPageResponse()
        self.requested_urls: list[str] = []

    def get(self, url: str, *_args, **_kwargs) -> _FakeProductPageResponse:
        self.requested_urls.append(url)
        return self.response


class _FakeLocator:
    def __init__(self, screenshot_bytes: bytes) -> None:
        self._screenshot_bytes = screenshot_bytes
        self.first = self

    def wait_for(self, *, state=None, timeout=None) -> None:
        return None

    def screenshot(self, *, path: str) -> None:
        Path(path).write_bytes(self._screenshot_bytes)


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://www.tiktok.com/shop/pdp/1729732615040962895"
        self.waited_timeouts: list[int] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.url = url

    def wait_for_load_state(self, state: str) -> None:
        assert state == "domcontentloaded"

    def wait_for_timeout(self, timeout_ms: int) -> None:
        assert timeout_ms > 0
        self.waited_timeouts.append(timeout_ms)

    def content(self) -> str:
        return SAMPLE_HTML

    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            return {
                "title_text": "Sample TikTok Product",
                "title_selector": "h1",
                "price_text": "$24.99",
                "price_selector": "[data-e2e='pdp-product-price']",
                "shop_name": "Sample Shop",
                "shop_selector": "[data-e2e='shop-name']",
                "main_image_url": "https://example.com/main-image.webp",
                "main_image_selector": "figure img",
                "main_image_loaded": True,
                "visible_signal_count": 3,
            }
        if "loaded" in script:
            return {"selector": "figure img", "loaded": True}
        raise AssertionError(f"Unexpected evaluate payload: {script}")

    def locator(self, selector: str) -> _FakeLocator:
        assert selector
        return _FakeLocator(b"main-image")

    def screenshot(self, *, path: str, full_page: bool = False) -> None:
        assert full_page is True
        Path(path).write_bytes(b"page-screenshot")


class _FakeFallbackPage(_FakePage):
    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            return {
                "title_text": "Sample TikTok Product",
                "title_selector": "h1",
                "price_text": "$24.99",
                "price_selector": "[data-e2e='pdp-product-price']",
                "shop_name": "玩具和爱好",
                "shop_selector": "a[href*='/shop/']",
                "main_image_url": "",
                "main_image_selector": "",
                "main_image_loaded": False,
                "visible_signal_count": 2,
            }
        raise AssertionError(f"Unexpected evaluate payload: {script}")


class _FakeRouterCaptureReadyPage(_FakePage):
    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            return {
                "title_text": "Sample TikTok Product",
                "title_selector": "h1",
                "price_text": "",
                "price_selector": "",
                "shop_name": "Sample Shop",
                "shop_selector": "[data-e2e='shop-name']",
                "main_image_url": "",
                "main_image_selector": "",
                "main_image_loaded": False,
                "visible_signal_count": 1,
            }
        raise AssertionError(f"Unexpected evaluate payload: {script}")


class _FakeGenericImageSelectorPage(_FakePage):
    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if isinstance(arg, dict) and "expectedUrl" in arg:
            return {"selector": 'img[data-mujitask-main-image-target="1"]'}
        if "visible_signal_count" in script:
            return {
                "title_text": "Sample TikTok Product",
                "title_selector": "h1",
                "price_text": "$24.99",
                "price_selector": "[data-e2e='pdp-product-price']",
                "shop_name": "Sample Shop",
                "shop_selector": "[data-e2e='shop-name']",
                "main_image_url": "https://example.com/main-image.webp?foo=bar",
                "main_image_selector": "img",
                "main_image_loaded": True,
                "visible_signal_count": 3,
            }
        if "loaded" in script:
            return {"selector": 'img[data-mujitask-main-image-target="1"]', "loaded": True}
        raise AssertionError(f"Unexpected evaluate payload: {script}")


class _FakeSkuDomPage(_FakePage):
    def content(self) -> str:
        return "<html><body><h1>SOFITEN Ascend Toy foam blaster set</h1></body></html>"

    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            if "collectVisibleSkuOptions" in script:
                assert "skuCardForImage" in script
                assert "nearestOptionLabel" in script
                assert "collectVisibleTextSkuOptions" in script
                assert "buildSkuOptionGroups" in script
                assert "sku_images: skuImages" in script
                assert "sku_options: skuOptions" in script
            return {
                "title_text": "SOFITEN Ascend Toy foam blaster set",
                "title_selector": "h1",
                "price_text": "$4*",
                "price_selector": '[data-mujitask-price-fallback="1"]',
                "shop_name": "Sofiten US",
                "shop_selector": "a[href*='/shop/']",
                "main_image_url": "https://example.com/main-white.webp",
                "main_image_selector": '[data-mujitask-main-image-fallback="1"]',
                "main_image_loaded": True,
                "gallery_image_urls": [
                    "https://example.com/gallery-1.webp",
                    "https://example.com/gallery-2.webp",
                ],
                "sku_images": [
                    {
                        "option_name": "Color",
                        "option_value": "WHITE",
                        "sku_property_key": "Color:WHITE",
                        "source_url": "https://example.com/sku-white.webp",
                        "display_order": 0,
                        "selected": True,
                    },
                    {
                        "option_name": "Color",
                        "option_value": "BLACK",
                        "sku_property_key": "Color:BLACK",
                        "source_url": "https://example.com/sku-black.webp",
                        "display_order": 1,
                        "selected": False,
                    },
                    {
                        "option_name": "Color",
                        "option_value": "Army",
                        "sku_property_key": "Color:Army",
                        "source_url": "https://example.com/sku-army.webp",
                        "display_order": 2,
                        "selected": False,
                    },
                ],
                "sku_options": [
                    {
                        "name": "Color",
                        "values": [
                            {
                                "value": "WHITE",
                                "image_url": "https://example.com/sku-white.webp",
                                "sku_property_key": "Color:WHITE",
                                "selected": True,
                            },
                            {
                                "value": "BLACK",
                                "image_url": "https://example.com/sku-black.webp",
                                "sku_property_key": "Color:BLACK",
                                "selected": False,
                            },
                            {
                                "value": "Army",
                                "image_url": "https://example.com/sku-army.webp",
                                "sku_property_key": "Color:Army",
                                "selected": False,
                            },
                        ],
                        "source_platform": "tiktok",
                    }
                ],
                "skus": [
                    {
                        "sku_id": "",
                        "sku_name": "WHITE",
                        "spec_name": "Color: WHITE",
                        "properties": [
                            {
                                "name": "Color",
                                "value": "WHITE",
                                "sku_property_key": "Color:WHITE",
                                "image_url": "https://example.com/sku-white.webp",
                            }
                        ],
                        "sku_property_keys": ["Color:WHITE"],
                        "source_platform": "tiktok",
                    },
                    {
                        "sku_id": "",
                        "sku_name": "BLACK",
                        "spec_name": "Color: BLACK",
                        "properties": [
                            {
                                "name": "Color",
                                "value": "BLACK",
                                "sku_property_key": "Color:BLACK",
                                "image_url": "https://example.com/sku-black.webp",
                            }
                        ],
                        "sku_property_keys": ["Color:BLACK"],
                        "source_platform": "tiktok",
                    },
                ],
                "rating_score": 4.7,
                "review_count": 257,
                "comment_count": 257,
                "sales_count": 4000,
                "visible_signal_count": 3,
            }
        if "loaded" in script:
            return {"selector": '[data-mujitask-main-image-fallback="1"]', "loaded": True}
        raise AssertionError(f"Unexpected evaluate payload: {script}")


class _FakeMultiSkuDomPage(_FakeSkuDomPage):
    def evaluate(self, script: str, arg=None):
        payload = super().evaluate(script, arg)
        if not ("visible_signal_count" in script and isinstance(payload, dict)):
            return payload
        payload = dict(payload)
        payload["sku_images"] = [
            image for image in payload["sku_images"] if image["option_value"] in {"WHITE", "BLACK"}
        ]
        payload["sku_options"] = [
            {
                "name": "Color",
                "values": [
                    {
                        "value": "WHITE",
                        "image_url": "https://example.com/sku-white.webp",
                        "sku_property_key": "Color:WHITE",
                        "selected": True,
                    },
                    {
                        "value": "BLACK",
                        "image_url": "https://example.com/sku-black.webp",
                        "sku_property_key": "Color:BLACK",
                        "selected": False,
                    },
                ],
                "source_platform": "tiktok",
            },
            {
                "name": "Quantity",
                "values": [
                    {
                        "value": "1 Pack",
                        "image_url": "",
                        "sku_property_key": "Quantity:1 Pack",
                        "selected": False,
                    },
                    {
                        "value": "2 Pack",
                        "image_url": "",
                        "sku_property_key": "Quantity:2 Pack",
                        "selected": True,
                    },
                ],
                "source_platform": "tiktok",
            },
        ]
        payload["skus"] = []
        return payload


class _FakeLoginToastPage(_FakePage):
    def __init__(self, visibility_sequence: list[bool]) -> None:
        super().__init__()
        self.visibility_sequence = visibility_sequence
        self.login_toast_checks = 0

    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            index = min(self.login_toast_checks, len(self.visibility_sequence) - 1)
            visible = self.visibility_sequence[index] if self.visibility_sequence else False
            self.login_toast_checks += 1
            return {
                "visible": visible,
                "text": "Please login to continue" if visible else "",
                "selector": "[data-e2e='toast-container']",
                "matched_keyword": "login" if visible else "",
            }
        return super().evaluate(script, arg)


class _FakeSecurityCheckPage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://www.tiktok.com/passport/web/challenge"

    def content(self) -> str:
        return "<html><body><div>Security check</div><div>Slide to verify</div></body></html>"

    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            return {
                "title_text": "",
                "title_selector": "",
                "price_text": "",
                "price_selector": "",
                "shop_name": "",
                "shop_selector": "",
                "main_image_url": "",
                "main_image_selector": "",
                "main_image_loaded": False,
                "visible_signal_count": 0,
            }
        raise AssertionError(f"Unexpected evaluate payload: {script}")


class _FakeClock:
    def __init__(self) -> None:
        self.now_ms = 0

    def monotonic(self) -> float:
        return self.now_ms / 1000.0

    def advance(self, timeout_ms: int) -> None:
        self.now_ms += int(timeout_ms)


class _FakeRecoverableSecurityCheckPage(_FakePage):
    def __init__(self, *, clock: _FakeClock, clear_after_ms: int | None) -> None:
        super().__init__()
        self.clock = clock
        self.clear_after_ms = clear_after_ms
        self._source_url = self.url

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self._source_url = url

    @property
    def url(self) -> str:
        if self._security_active():
            return "https://www.tiktok.com/passport/web/challenge"
        return self._source_url

    @url.setter
    def url(self, value: str) -> None:
        self._source_url = value

    def wait_for_timeout(self, timeout_ms: int) -> None:
        super().wait_for_timeout(timeout_ms)
        self.clock.advance(timeout_ms)

    def content(self) -> str:
        if self._security_active():
            return "<html><body><div>Security check</div><div>Slide to verify</div></body></html>"
        return super().content()

    def evaluate(self, script: str, arg=None):
        if self._security_active():
            return _FakeSecurityCheckPage.evaluate(self, script, arg)
        return super().evaluate(script, arg)

    def _security_active(self) -> bool:
        return self.clear_after_ms is None or self.clock.now_ms < self.clear_after_ms


class _FakeNeverReadyPage(_FakePage):
    def __init__(self, *, clock: _FakeClock) -> None:
        super().__init__()
        self.clock = clock

    def wait_for_timeout(self, timeout_ms: int) -> None:
        super().wait_for_timeout(timeout_ms)
        self.clock.advance(timeout_ms)

    def content(self) -> str:
        return "<html><body><div>title only</div></body></html>"

    def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and "toastSelectors" in arg:
            return {
                "visible": False,
                "text": "",
                "selector": "",
                "matched_keyword": "",
            }
        if "visible_signal_count" in script:
            return {
                "title_text": "Sample TikTok Product",
                "title_selector": "h1",
                "price_text": "",
                "price_selector": "",
                "shop_name": "Sample Shop",
                "shop_selector": "[data-e2e='shop-name']",
                "main_image_url": "",
                "main_image_selector": "",
                "main_image_loaded": False,
                "visible_signal_count": 1,
            }
        raise AssertionError(f"Unexpected evaluate payload: {script}")


def test_extract_tiktok_product_from_html_returns_expected_fields():
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
        resolved_url=(
            "https://shop.tiktok.com/us/pdp/sample-tiktok-product/1729732615040962895"
        ),
    )

    assert product.normalized_url == "https://www.tiktok.com/shop/pdp/1729732615040962895"
    assert product.product_id == "1729732615040962895"
    assert product.title == "Sample TikTok Product"
    assert product.holiday == "其他"
    assert product.main_image_url == "https://example.com/main-image.webp"
    assert product.price_amount == "24.99"
    assert product.price_currency == "USD"
    assert product.price_text == "$24.99"
    assert product.sales_count == 94151
    assert product.rating_score == 4.7
    assert product.review_count == 1200
    assert product.comment_count == 345
    assert [image["source_url"] for image in product.gallery_images] == [
        "https://example.com/main-image.webp",
        "https://example.com/side-image.webp",
    ]
    assert product.sku_images[0]["source_url"] == "https://example.com/sku-pink.webp"
    assert product.sku_images[0]["sku_property_key"] == "Color:Pink"
    assert product.sku_options == [
        {
            "name": "Color",
            "values": [
                {
                    "value": "Pink",
                    "value_id": "pink-id",
                    "image_url": "https://example.com/sku-pink.webp",
                    "sku_property_key": "Color:Pink",
                }
            ],
            "source_platform": "tiktok",
        }
    ]
    assert product.skus == [
        {
            "product_id": "1729732615040962895",
            "sku_id": "sku-pink",
            "sku_name": "Pink",
            "spec_name": "Color: Pink",
            "properties": [
                {
                    "name": "Color",
                    "value": "Pink",
                    "value_id": "pink-id",
                    "sku_property_key": "Color:Pink",
                    "image_url": "",
                }
            ],
            "sku_property_keys": ["Color:Pink"],
            "source_platform": "tiktok",
        }
    ]
    assert product.shop_name == "Sample Shop"
    assert product.shop_url == "https://shop.tiktok.com/us/store/sample-shop/123"


def test_extract_tiktok_product_from_html_prefers_product_rating_label():
    sample_router_data = json.loads(json.dumps(SAMPLE_ROUTER_DATA))
    component_data = sample_router_data["loaderData"]["(region)/pdp/(product_name_slug$)/(product_id)/page"][
        "page_config"
    ]["components_map"][1]["component_data"]
    product_model = component_data["product_info"]["product_model"]
    product_model.pop("rating_score")
    product_model.pop("review_count")
    product_model.pop("comment_count")
    component_data["product_info"]["review_model"] = {
        "product_overall_score": 4.4,
        "product_review_count": "392",
    }
    component_data["review_info"] = {
        "total_reviews": "392",
        "review_ratings": {
            "review_count": "392",
            "overall_score": 4.3,
            "rating_result": {"1": "40", "2": "14", "3": "13", "4": "34", "5": "291"},
        },
    }
    html = (
        "<html><body>"
        '<script id="__MODERN_ROUTER_DATA__" type="application/json">'
        f"{json.dumps(sample_router_data, ensure_ascii=True)}"
        "</script>"
        "</body></html>"
    )

    product = extract_tiktok_product_from_html(
        html,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
    )

    assert product.rating_score == 4.4
    assert product.review_count == 392
    assert product.comment_count == 392


def test_fetch_tiktok_product_record_uses_request_pacer_between_http_requests():
    sleeps: list[float] = []
    pacer = RequestPacer(
        RequestPacerConfig(min_delay_seconds=1.5, max_delay_seconds=1.5),
        sleep_factory=sleeps.append,
        monotonic_factory=lambda: 1.0,
    )
    session = _FakeProductPageSession()

    first = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record"],
    ).fetch_tiktok_product_record(
        "https://www.tiktok.com/shop/pdp/1729732615040962895",
        session=session,
        request_pacer=pacer,
    )
    second = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record"],
    ).fetch_tiktok_product_record(
        "https://www.tiktok.com/shop/pdp/1729732615040962895",
        session=session,
        request_pacer=pacer,
    )

    assert first.product_id == "1729732615040962895"
    assert second.product_id == "1729732615040962895"
    assert sleeps == [1.5]
    assert len(session.requested_urls) == 2


def test_download_tiktok_product_main_image_stores_local_file(tmp_path):
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
    )
    downloaded = download_tiktok_product_main_image(
        product,
        download_dir=str(tmp_path),
        session=_FakeImageSession(content=b"fake-webp-bytes"),
    )

    assert downloaded.main_image_local_path == str(tmp_path / "1729732615040962895-main-image.webp")
    assert downloaded.main_image_file_name == "1729732615040962895-main-image.webp"
    assert downloaded.main_image_mime_type == "image/webp"
    assert Path(downloaded.main_image_local_path).read_bytes() == b"fake-webp-bytes"


def test_build_feishu_bitable_record_uses_local_file_for_main_image(tmp_path):
    product = extract_tiktok_product_from_html(
        SAMPLE_HTML,
        source_url="https://shop.tiktok.com/view/product/1729732615040962895",
    )
    downloaded = download_tiktok_product_main_image(
        product,
        download_dir=str(tmp_path),
        session=_FakeImageSession(content=b"fake-webp-bytes"),
    )

    record = build_feishu_bitable_record(downloaded)

    assert record["logical_fields"]["title"] == "Sample TikTok Product"
    assert record["logical_fields"]["normalized_url"] == "https://www.tiktok.com/shop/pdp/1729732615040962895"
    assert record["logical_fields"]["main_image_local_path"].endswith(
        "1729732615040962895-main-image.webp"
    )
    assert record["fields"] == {
        "产品链接": {
            "text": "https://shop.tiktok.com/view/product/1729732615040962895",
            "link": "https://shop.tiktok.com/view/product/1729732615040962895",
        },
        "SKU-ID": "1729732615040962895",
        "图片": {
            "type": "local_file",
            "path": str(tmp_path / "1729732615040962895-main-image.webp"),
            "file_name": "1729732615040962895-main-image.webp",
            "mime_type": "image/webp",
            "source_url": "https://example.com/main-image.webp",
        },
        "标题": "Sample TikTok Product",
        "节日": "其他",
        "价格": "24.99",
    }


def test_fetch_tiktok_product_record_via_browser_captures_main_image_and_page_screenshot(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakePage(),
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))
    monkeypatch.setattr(
        module,
        "download_tiktok_product_main_image",
        lambda product, *, download_dir, timeout=30, session=None: replace(
            product,
            main_image_local_path=str(Path(download_dir) / f"{product.product_id}-main-image.webp"),
            main_image_file_name=f"{product.product_id}-main-image.webp",
            main_image_mime_type="image/webp",
        ),
    )
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    (tmp_path / "images" / "1729732615040962895-main-image.webp").write_bytes(b"downloaded-main-image")

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.normalized_url == "https://www.tiktok.com/shop/pdp/1729732615040962895"
    assert product.main_image_local_path.endswith("1729732615040962895-main-image.webp")
    assert product.product_page_screenshot_local_path.endswith("1729732615040962895-product-page.png")
    assert Path(product.main_image_local_path).read_bytes() == b"downloaded-main-image"
    assert Path(product.product_page_screenshot_local_path).read_bytes() == b"page-screenshot"


def test_fetch_tiktok_product_record_via_browser_passes_blocked_handler(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )
    captured_kwargs: dict[str, object] = {}

    @contextmanager
    def fake_open_automation_page(**kwargs):
        captured_kwargs.update(kwargs)
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakePage(),
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))
    monkeypatch.setattr(
        module,
        "download_tiktok_product_main_image",
        lambda product, *, download_dir, timeout=30, session=None: replace(
            product,
            main_image_local_path=str(Path(download_dir) / f"{product.product_id}-main-image.webp"),
            main_image_file_name=f"{product.product_id}-main-image.webp",
            main_image_mime_type="image/webp",
        ),
    )
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    (tmp_path / "images" / "1729732615040962895-main-image.webp").write_bytes(b"downloaded-main-image")

    module.fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    blocked_handling = captured_kwargs["blocked_handling"]
    assert callable(blocked_handling.handler)


def test_handle_tiktok_blocked_context_dismisses_login_promo_with_escape(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_handle_tiktok_blocked_context"],
    )
    delays = iter([960, 340])

    class FakeKeyboard:
        def __init__(self, page) -> None:
            self.page = page

        def press(self, key: str) -> None:
            self.page.pressed_keys.append(key)
            if key == "Escape":
                self.page.promo_visible = False

    class FakePromoPage:
        def __init__(self) -> None:
            self.promo_visible = True
            self.pressed_keys: list[str] = []
            self.waited_timeouts: list[int] = []
            self.keyboard = FakeKeyboard(self)
            self.mouse = None

        def evaluate(self, script: str, arg=None):
            if isinstance(arg, dict) and arg.get("keywords") == ["log in", "create account"]:
                return {
                    "visible": self.promo_visible,
                    "text": "Welcome! Ready for Some Savings? Log in Create Account" if self.promo_visible else "",
                    "selector": "[role='dialog']" if self.promo_visible else "",
                }
            if "visible_signal_count" in script:
                return {"visible_signal_count": 0}
            raise AssertionError(f"Unexpected evaluate payload: {script}")

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waited_timeouts.append(timeout_ms)

    page = FakePromoPage()
    monkeypatch.setattr(
        module,
        "_wait_with_random_delay",
        lambda page, *, min_ms, max_ms: page.wait_for_timeout(next(delays)),
    )
    automation_page = type("_AutomationPage", (), {"raw_page": page, "page": page})()
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": "Welcome! Ready for Some Savings? Log in to see your exclusive discounts. Log in Create Account",
        },
    )()

    resolution = module._handle_tiktok_blocked_context(automation_page, event)

    assert resolution.action == "handled_recheck"
    assert page.pressed_keys == ["Escape"]
    assert page.waited_timeouts == [960, 340]


def test_handle_tiktok_blocked_context_force_continues_after_dismiss_when_product_content_is_visible(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_handle_tiktok_blocked_context"],
    )
    delays = iter([1010, 410])

    class FakeKeyboard:
        def __init__(self, page) -> None:
            self.page = page

        def press(self, key: str) -> None:
            self.page.pressed_keys.append(key)
            if key == "Escape":
                self.page.promo_visible = False

    class FakePromoPage:
        def __init__(self) -> None:
            self.promo_visible = True
            self.pressed_keys: list[str] = []
            self.waited_timeouts: list[int] = []
            self.keyboard = FakeKeyboard(self)
            self.mouse = None

        def evaluate(self, script: str, arg=None):
            if isinstance(arg, dict) and arg.get("keywords") == ["log in", "create account"]:
                return {
                    "visible": self.promo_visible,
                    "text": "Welcome! Ready for Some Savings? Log in Create Account" if self.promo_visible else "",
                    "selector": "[role='dialog']" if self.promo_visible else "",
                }
            if "visible_signal_count" in script:
                return {"visible_signal_count": 3}
            raise AssertionError(f"Unexpected evaluate payload: {script}")

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waited_timeouts.append(timeout_ms)

    page = FakePromoPage()
    monkeypatch.setattr(
        module,
        "_wait_with_random_delay",
        lambda page, *, min_ms, max_ms: page.wait_for_timeout(next(delays)),
    )
    automation_page = type("_AutomationPage", (), {"raw_page": page, "page": page})()
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": "Welcome! Ready for Some Savings? Log in to see your exclusive discounts. Log in Create Account",
        },
    )()

    resolution = module._handle_tiktok_blocked_context(automation_page, event)

    assert resolution.action == "force_continue"
    assert page.pressed_keys == ["Escape"]
    assert page.waited_timeouts == [1010, 410]


def test_is_tiktok_login_promo_blocker_matches_early_body_probe():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_is_tiktok_login_promo_blocker"],
    )
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": (
                "Search Get app Log in TikTok Shop Toys & Hobbies Classic & Novelty Toys "
                "Building Toys Product Detail"
            ),
            "dom_summary": {
                "dialogs": [],
                "body_text_excerpt": (
                    "Search Get app Log in TikTok Shop Toys & Hobbies Classic & Novelty Toys "
                    "Building Toys Product Detail"
                ),
            },
        },
    )()

    assert module._is_tiktok_login_promo_blocker(event) is True


def test_is_tiktok_login_promo_blocker_uses_dom_summary_dialog_text():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_is_tiktok_login_promo_blocker"],
    )
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": "Search Get app TikTok Shop",
            "dom_summary": {
                "dialogs": [
                    {
                        "text": (
                            "Welcome! Ready for Some Savings? Log in to see your exclusive discounts. "
                            "Log in Create Account"
                        )
                    }
                ],
                "body_text_excerpt": "Search Get app TikTok Shop",
            },
        },
    )()

    assert module._is_tiktok_login_promo_blocker(event) is True


def test_handle_tiktok_blocked_context_force_continues_when_promo_is_non_blocking(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_handle_tiktok_blocked_context"],
    )
    delays = iter([930, 330, 220, 360])

    class FakeKeyboard:
        def __init__(self, page) -> None:
            self.page = page

        def press(self, key: str) -> None:
            self.page.pressed_keys.append(key)

    class FakeMouse:
        def __init__(self, page) -> None:
            self.page = page

        def click(self, x: int, y: int) -> None:
            self.page.mouse_clicks.append((x, y))

    class FakePromoPage:
        def __init__(self) -> None:
            self.promo_visible = True
            self.pressed_keys: list[str] = []
            self.mouse_clicks: list[tuple[int, int]] = []
            self.waited_timeouts: list[int] = []
            self.keyboard = FakeKeyboard(self)
            self.mouse = FakeMouse(self)

        def evaluate(self, script: str, arg=None):
            if isinstance(arg, dict) and arg.get("keywords") == ["log in", "create account"]:
                return {
                    "visible": self.promo_visible,
                    "text": "Welcome! Ready for Some Savings? Log in Create Account",
                    "selector": "[role='dialog']",
                }
            if "visible_signal_count" in script:
                return {"visible_signal_count": 3}
            raise AssertionError(f"Unexpected evaluate payload: {script}")

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waited_timeouts.append(timeout_ms)

    page = FakePromoPage()
    monkeypatch.setattr(
        module,
        "_wait_with_random_delay",
        lambda page, *, min_ms, max_ms: page.wait_for_timeout(next(delays)),
    )
    automation_page = type("_AutomationPage", (), {"raw_page": page, "page": page})()
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": "Welcome! Ready for Some Savings? Log in to see your exclusive discounts. Log in Create Account",
        },
    )()

    resolution = module._handle_tiktok_blocked_context(automation_page, event)

    assert resolution.action == "force_continue"
    assert page.pressed_keys == ["Escape"]
    assert page.mouse_clicks == [(40, 40)]
    assert page.waited_timeouts == [930, 330, 220, 360]


def test_handle_tiktok_blocked_context_dismisses_early_body_probe_with_blank_click(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_handle_tiktok_blocked_context"],
    )
    delays = iter([880, 310, 210, 350])

    class FakeKeyboard:
        def __init__(self, page) -> None:
            self.page = page

        def press(self, key: str) -> None:
            self.page.pressed_keys.append(key)

    class FakeMouse:
        def __init__(self, page) -> None:
            self.page = page

        def click(self, x: int, y: int) -> None:
            self.page.mouse_clicks.append((x, y))
            self.page.promo_visible = False

    class FakePromoPage:
        def __init__(self) -> None:
            self.promo_visible = True
            self.pressed_keys: list[str] = []
            self.mouse_clicks: list[tuple[int, int]] = []
            self.waited_timeouts: list[int] = []
            self.keyboard = FakeKeyboard(self)
            self.mouse = FakeMouse(self)

        def evaluate(self, script: str, arg=None):
            if isinstance(arg, dict) and arg.get("keywords") == ["log in", "create account"]:
                return {
                    "visible": self.promo_visible,
                    "text": "Welcome! Ready for Some Savings? Log in Create Account" if self.promo_visible else "",
                    "selector": "[role='dialog']" if self.promo_visible else "",
                }
            if "visible_signal_count" in script:
                return {"visible_signal_count": 0}
            raise AssertionError(f"Unexpected evaluate payload: {script}")

        def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waited_timeouts.append(timeout_ms)

    page = FakePromoPage()
    monkeypatch.setattr(
        module,
        "_wait_with_random_delay",
        lambda page, *, min_ms, max_ms: page.wait_for_timeout(next(delays)),
    )
    automation_page = type("_AutomationPage", (), {"raw_page": page, "page": page})()
    event = type(
        "_Event",
        (),
        {
            "page_url": "https://www.tiktok.com/shop/pdp/1729732615040962895",
            "blocker_type": "guide_overlay",
            "summary": "Search Get app Log in TikTok Shop Product Detail",
            "detection_source": "body",
            "dom_summary": {
                "dialogs": [],
                "body_text_excerpt": "Search Get app Log in TikTok Shop Product Detail",
            },
        },
    )()

    resolution = module._handle_tiktok_blocked_context(automation_page, event)

    assert resolution.action == "force_continue"
    assert page.pressed_keys == ["Escape"]
    assert page.mouse_clicks == [(40, 40)]
    assert page.waited_timeouts == [880, 310, 210, 350]


def test_fetch_tiktok_product_record_via_browser_falls_back_to_html_data_and_downloaded_main_image(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakeFallbackPage(),
            },
        )()

    def fake_download(product, *, download_dir, timeout=30, session=None):
        image_dir = Path(download_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{product.product_id}-main-image.webp"
        image_path.write_bytes(b"downloaded-main-image")
        return replace(
            product,
            main_image_local_path=str(image_path),
            main_image_file_name=image_path.name,
            main_image_mime_type="image/webp",
        )

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.shop_name == "Sample Shop"
    assert product.price_amount == "24.99"
    assert product.main_image_url == "https://example.com/main-image.webp"
    assert product.main_image_local_path.endswith("1729732615040962895-main-image.webp")
    assert Path(product.main_image_local_path).read_bytes() == b"downloaded-main-image"
    assert product.product_page_screenshot_local_path.endswith("1729732615040962895-product-page.png")


def test_fetch_tiktok_product_record_via_browser_exits_early_when_router_data_and_image_are_ready(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )
    page = _FakeRouterCaptureReadyPage()

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": page,
            },
        )()

    def fake_download(product, *, download_dir, timeout=30, session=None):
        image_dir = Path(download_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{product.product_id}-main-image.webp"
        image_path.write_bytes(b"downloaded-main-image")
        return replace(
            product,
            main_image_local_path=str(image_path),
            main_image_file_name=image_path.name,
            main_image_mime_type="image/webp",
        )

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.shop_name == "Sample Shop"
    assert product.price_amount == "24.99"
    assert product.main_image_url == "https://example.com/main-image.webp"
    assert page.waited_timeouts == [250] * 16


def test_fetch_tiktok_product_record_via_browser_falls_back_to_matching_dom_image_when_download_fails(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakeGenericImageSelectorPage(),
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))

    def fake_download(*_args, **_kwargs):
        raise module.TikTokProductExtractionError("download failed")

    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.main_image_local_path.endswith("1729732615040962895-main-image.png")
    assert Path(product.main_image_local_path).read_bytes() == b"main-image"
    assert product.product_page_screenshot_local_path.endswith("1729732615040962895-product-page.png")


def test_build_record_from_browser_state_prefers_router_main_image_url_over_generic_dom_image():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_build_record_from_browser_state"],
    )

    product = module._build_record_from_browser_state(
        html=SAMPLE_HTML,
        dom_snapshot={
            "product_id": "1729732615040962895",
            "title_text": "Sample TikTok Product",
            "main_image_url": "https://example.com/wrong-logo.png",
            "main_image_selector": "img",
            "price_text": "$24.99",
            "shop_name": "Wrong Shop",
        },
        source_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        resolved_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
    )

    assert product.main_image_url == "https://example.com/main-image.webp"


def test_build_record_from_browser_state_extracts_sku_image_mapping_from_dom_snapshot():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_build_record_from_browser_state"],
    )
    dom_snapshot = _FakeSkuDomPage().evaluate("visible_signal_count")

    product = module._build_record_from_browser_state(
        html="<html><body>rendered pdp</body></html>",
        dom_snapshot=dom_snapshot,
        source_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        resolved_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
    )

    assert product.price_amount == "4"
    assert product.rating_score == 4.7
    assert product.review_count == 257
    assert product.sales_count == 4000
    assert [image["sku_property_key"] for image in product.sku_images] == [
        "Color:WHITE",
        "Color:BLACK",
        "Color:Army",
    ]
    assert [image["source_url"] for image in product.sku_images] == [
        "https://example.com/sku-white.webp",
        "https://example.com/sku-black.webp",
        "https://example.com/sku-army.webp",
    ]
    assert product.sku_options == [
        {
            "name": "Color",
            "values": [
                {
                    "value": "WHITE",
                    "image_url": "https://example.com/sku-white.webp",
                    "sku_property_key": "Color:WHITE",
                },
                {
                    "value": "BLACK",
                    "image_url": "https://example.com/sku-black.webp",
                    "sku_property_key": "Color:BLACK",
                },
                {
                    "value": "Army",
                    "image_url": "https://example.com/sku-army.webp",
                    "sku_property_key": "Color:Army",
                },
            ],
            "source_platform": "tiktok",
        }
    ]
    assert [sku["spec_name"] for sku in product.skus] == [
        "Color: WHITE",
        "Color: BLACK",
        "Color: Army",
    ]
    assert product.skus[0]["properties"][0]["image_url"] == "https://example.com/sku-white.webp"


def test_build_record_from_browser_state_preserves_multi_level_sku_options_without_synthetic_combinations():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_build_record_from_browser_state"],
    )
    dom_snapshot = _FakeMultiSkuDomPage().evaluate("visible_signal_count")

    product = module._build_record_from_browser_state(
        html="<html><body>rendered multi sku pdp</body></html>",
        dom_snapshot=dom_snapshot,
        source_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        resolved_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
    )

    assert [image["sku_property_key"] for image in product.sku_images] == [
        "Color:WHITE",
        "Color:BLACK",
    ]
    assert product.sku_options == [
        {
            "name": "Color",
            "values": [
                {
                    "value": "WHITE",
                    "image_url": "https://example.com/sku-white.webp",
                    "sku_property_key": "Color:WHITE",
                },
                {
                    "value": "BLACK",
                    "image_url": "https://example.com/sku-black.webp",
                    "sku_property_key": "Color:BLACK",
                },
            ],
            "source_platform": "tiktok",
        },
        {
            "name": "Quantity",
            "values": [
                {
                    "value": "1 Pack",
                    "image_url": "",
                    "sku_property_key": "Quantity:1 Pack",
                },
                {
                    "value": "2 Pack",
                    "image_url": "",
                    "sku_property_key": "Quantity:2 Pack",
                },
            ],
            "source_platform": "tiktok",
        },
    ]
    assert product.skus == []


def test_fetch_tiktok_product_record_via_browser_preserves_dom_sku_image_mapping(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakeSkuDomPage(),
            },
        )()

    def fake_download(product, *, download_dir, timeout=30, session=None):
        image_dir = Path(download_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{product.product_id}-main-image.webp"
        image_path.write_bytes(b"downloaded-main-image")
        return replace(
            product,
            main_image_local_path=str(image_path),
            main_image_file_name=image_path.name,
            main_image_mime_type="image/webp",
        )

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "download_tiktok_product_main_image", fake_download)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.product_id == "1729732615040962895"
    assert product.main_image_url == "https://example.com/main-white.webp"
    assert product.gallery_images[0]["source_url"] == "https://example.com/gallery-1.webp"
    assert [image["sku_property_key"] for image in product.sku_images] == [
        "Color:WHITE",
        "Color:BLACK",
        "Color:Army",
    ]
    assert product.sku_options[0]["values"][0]["value"] == "WHITE"
    assert product.skus[0]["spec_name"] == "Color: WHITE"
    assert product.skus[0]["sku_property_keys"] == ["Color:WHITE"]
    assert Path(product.main_image_local_path).read_bytes() == b"downloaded-main-image"
    assert product.product_page_screenshot_local_path.endswith("1729732615040962895-product-page.png")


def test_fetch_tiktok_product_record_via_browser_waits_for_login_toast_to_clear(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )
    page = _FakeLoginToastPage([True, True, False, False])

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": page,
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)
    monkeypatch.setattr(module, "DEFAULT_IMAGE_DOWNLOAD_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "DEFAULT_PAGE_SCREENSHOT_DIR", str(tmp_path / "pages"))

    product = fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895?source=product_detail",
        profile_ref="local-chrome",
    )

    assert product.product_id == "1729732615040962895"
    assert page.login_toast_checks == 4
    assert page.waited_timeouts[:3] == [250, 250, 250]


def test_wait_for_login_toast_to_settle_raises_when_toast_never_disappears():
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_wait_for_login_toast_to_settle"],
    )
    page = _FakeLoginToastPage([True, True, True, True, True])

    with pytest.raises(module.TikTokProductExtractionError, match="login toast"):
        module._wait_for_login_toast_to_settle(
            page,
            settle_ms=250,
            timeout_ms=750,
            poll_ms=250,
            stable_absent_polls=2,
        )


def test_fetch_tiktok_product_record_via_browser_raises_security_check_error_when_challenge_page_detected(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    clock = _FakeClock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakeRecoverableSecurityCheckPage(clock=clock, clear_after_ms=None),
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)

    with pytest.raises(module.TikTokSecurityCheckError, match="security check"):
        module.fetch_tiktok_product_record_via_browser(
            "https://www.tiktok.com/shop/pdp/1729732615040962895",
            profile_ref="local-chrome",
            timeout_ms=500,
            security_check_grace_ms=1000,
        )


def test_detect_browser_security_check_ignores_html_only_captcha_loader_on_product_page(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_detect_browser_security_check"],
    )
    monkeypatch.setattr(
        module,
        "_safe_body_text",
        lambda _page: "Search Get app Log in TikTok Shop Product Detail Reviews",
    )

    message = module._detect_browser_security_check(
        object(),
        html=(
            '<script id="__MODERN_ROUTER_DATA__" type="application/json">{}</script>'
            '<script id="lucifer-captcha-loader-js" '
            'src="https://example.com/captcha/index"></script>'
        ),
        resolved_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        dom_snapshot={"visible_signal_count": 1},
    )

    assert message is None


def test_detect_browser_security_check_uses_html_fallback_when_page_is_not_product_like(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_detect_browser_security_check"],
    )
    monkeypatch.setattr(module, "_safe_body_text", lambda _page: "")

    message = module._detect_browser_security_check(
        object(),
        html=(
            '<script id="lucifer-captcha-loader-js" '
            'src="https://example.com/captcha/index"></script>'
        ),
        resolved_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        dom_snapshot={"visible_signal_count": 0},
    )

    assert message == "TikTok security check detected: lucifer-captcha"


def test_fetch_tiktok_product_record_via_browser_continues_when_security_check_clears_within_grace_window(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["fetch_tiktok_product_record_via_browser"],
    )

    clock = _FakeClock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "chrome_cdp",
                "target_key": "chrome_cdp:none:local-chrome",
                "profile_ref": "local-chrome",
                "session_ref": "local-chrome",
                "page": _FakeRecoverableSecurityCheckPage(clock=clock, clear_after_ms=1000),
            },
        )()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)

    product = module.fetch_tiktok_product_record_via_browser(
        "https://www.tiktok.com/shop/pdp/1729732615040962895",
        profile_ref="local-chrome",
        timeout_ms=500,
        capture_page_screenshot=False,
        security_check_grace_ms=1000,
    )

    assert product.product_id == "1729732615040962895"
    assert product.title == "Sample TikTok Product"
    assert clock.now_ms >= 1000


def test_wait_for_product_page_ready_preserves_timeout_when_capture_is_never_ready(monkeypatch):
    module = __import__(
        "automation_business_scaffold.business.flows.tiktok_product_flow",
        fromlist=["_wait_for_product_page_ready"],
    )

    clock = _FakeClock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    page = _FakeNeverReadyPage(clock=clock)

    snapshot = module._wait_for_product_page_ready(
        page,
        timeout_ms=500,
        source_url="https://www.tiktok.com/shop/pdp/1729732615040962895",
        trace_id="rec-timeout",
    )

    assert snapshot["visible_signal_count"] == 1
    assert page.waited_timeouts == [250, 250, 250, 250]
    assert clock.now_ms == 1000


def test_normalize_tiktok_product_url_strips_parameters():
    assert (
        normalize_tiktok_product_url(
            "https://www.tiktok.com/shop/pdp/1730573078867972103?source=product_detail"
        )
        == "https://www.tiktok.com/shop/pdp/1730573078867972103"
    )


def test_infer_tiktok_product_holiday_matches_known_options_and_fallback():
    assert infer_tiktok_product_holiday("Valentine's Day Heart Garland") == "情人节"
    assert infer_tiktok_product_holiday("Halloween Pumpkin Lights") == "万圣节"
    assert infer_tiktok_product_holiday("Generic Party Supplies") == "其他"


def test_validate_tiktok_product_url_accepts_expected_product_links():
    validate_tiktok_product_url("https://shop.tiktok.com/view/product/1729732615040962895")
    validate_tiktok_product_url(
        "https://shop.tiktok.com/us/pdp/sample-tiktok-product/1729732615040962895"
    )
