from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from automation_business_scaffold.flows import (
    build_feishu_bitable_record,
    download_tiktok_product_main_image,
    extract_tiktok_product_from_html,
    fetch_tiktok_product_record_via_browser,
    infer_tiktok_product_holiday,
    normalize_tiktok_product_url,
)
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
                                    "name": "Sample TikTok Product",
                                    "images": [
                                        {
                                            "height": 1400,
                                            "width": 1400,
                                            "uri": "tos-sample/image-1",
                                            "url_list": [
                                                "https://example.com/main-image.webp",
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
    assert product.shop_name == "Sample Shop"
    assert product.shop_url == "https://shop.tiktok.com/us/store/sample-shop/123"


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
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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


def test_handle_tiktok_blocked_context_force_continues_when_promo_is_non_blocking(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_product_flow",
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


def test_fetch_tiktok_product_record_via_browser_falls_back_to_html_data_and_downloaded_main_image(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_product_flow",
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


def test_fetch_tiktok_product_record_via_browser_falls_back_to_matching_dom_image_when_download_fails(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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


def test_fetch_tiktok_product_record_via_browser_waits_for_login_toast_to_clear(
    monkeypatch,
    tmp_path,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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
        "automation_business_scaffold.flows.tiktok_product_flow",
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


def test_fetch_tiktok_product_record_via_browser_continues_when_security_check_clears_within_grace_window(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.flows.tiktok_product_flow",
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
