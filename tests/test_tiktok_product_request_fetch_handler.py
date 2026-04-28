from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

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


def test_tiktok_product_request_fetch_passes_configured_request_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_fetch(product_url: str, *, timeout: int = 30, session=None, request_pacer=None):  # noqa: ANN001
        del product_url, timeout, session
        captured["pacer_config"] = request_pacer.config
        return TikTokProductRecord(
            source_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            resolved_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            normalized_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            product_id="1730964478199763166",
            title="Candy Boxes",
            holiday="",
            price_amount="",
            price_currency="USD",
            price_text="",
            sales_count="",
            shop_name="Candy Shop",
            shop_url="",
            main_image_url="https://cdn.example.com/main.jpg",
        )

    monkeypatch.setattr(handler_module, "fetch_tiktok_product_record", fake_fetch)

    result = handler_module.tiktok_product_request_fetch_handler(
        _context(
            {
                "product_identity": {
                    "product_id": "1730964478199763166",
                    "product_url": "https://www.tiktok.com/shop/pdp/1730964478199763166",
                },
                "tiktok_api_request_delay_min_seconds": 0.2,
                "tiktok_api_request_delay_max_seconds": 0.4,
            }
        )
    )

    assert result.status == "success"
    assert captured["pacer_config"].min_delay_seconds == 0.2
    assert captured["pacer_config"].max_delay_seconds == 0.4


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


class _FakeSliderMouse:
    def __init__(self, page: "_FakeSliderPage") -> None:
        self.page = page
        self.moves: list[tuple[float, float]] = []
        self.down_called = False
        self.up_called = False

    def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    def down(self) -> None:
        self.down_called = True

    def up(self) -> None:
        self.up_called = True
        self.page.slider_visible = False


class _FakeSliderLocator:
    def __init__(self, page: "_FakeSliderPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_FakeSliderLocator":
        return self

    def is_visible(self, timeout: int | None = None) -> bool:
        del timeout
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_SUCCESS_SELECTORS:
            return self.page.slider_success
        if self.selector in (
            product_page.TIKTOK_SLIDER_CAPTCHA_POPUP_SELECTORS
            + product_page.TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS
            + product_page.TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS
            + product_page.TIKTOK_SLIDER_CAPTCHA_HANDLE_SELECTORS
        ):
            return self.page.slider_visible
        return False

    def screenshot(self, timeout: int | None = None) -> bytes:
        del timeout
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS:
            self.page.background_captured_with_target_hidden = any(
                selector in product_page.TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS
                for selector in self.page.hidden_selectors
            )
            return b"background-with-target-hidden" if self.page.background_captured_with_target_hidden else b"background"
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS:
            return b"target"
        return b"not-a-real-image-but-good-enough-for-unit-tests"

    def bounding_box(self, timeout: int | None = None) -> dict[str, float]:
        del timeout
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_BACKGROUND_SELECTORS:
            return {"x": 100.0, "y": 50.0, "width": 300.0, "height": 150.0}
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_TARGET_SELECTORS:
            return {"x": 112.0, "y": 78.0, "width": 40.0, "height": 40.0}
        if self.selector in product_page.TIKTOK_SLIDER_CAPTCHA_HANDLE_SELECTORS:
            return {"x": 105.0, "y": 230.0, "width": 20.0, "height": 20.0}
        return {}

    def click(self, timeout: int | None = None) -> None:
        del timeout
        self.page.refresh_clicked = True

    def evaluate(self, script: str, *args: object, **kwargs: object) -> bool:
        del args, kwargs
        if 'visibility = "hidden"' in script:
            self.page.hidden_selectors.add(self.selector)
            return True
        if "delete element.dataset.tiktokSliderPreviousVisibility" in script:
            self.page.hidden_selectors.discard(self.selector)
            return True
        return False


class _FakeSliderPage:
    def __init__(self) -> None:
        self.slider_visible = True
        self.slider_success = False
        self.refresh_clicked = False
        self.background_captured_with_target_hidden = False
        self.hidden_selectors: set[str] = set()
        self.wait_calls: list[int] = []
        self.mouse = _FakeSliderMouse(self)

    def locator(self, selector: str) -> _FakeSliderLocator:
        return _FakeSliderLocator(self, selector)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)


def test_tiktok_product_browser_fetch_resolves_visible_slider_with_framework_captcha_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakeSliderPage()
    provider_calls: list[tuple[bytes, bytes, bool]] = []

    class _FakeProvider:
        def match_slider(
            self,
            target_image: bytes,
            background_image: bytes,
            *,
            simple_target: bool = False,
        ) -> SimpleNamespace:
            provider_calls.append((target_image, background_image, simple_target))
            return SimpleNamespace(target_x=72, target_y=12, confidence=0.91)

    monkeypatch.setattr(
        product_page,
        "_build_tiktok_slider_captcha_provider",
        lambda: _FakeProvider(),
    )

    result = product_page._try_resolve_tiktok_slider_security_check(
        page,
        product_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
        max_attempts=1,
        settle_ms=1,
    )

    assert result["resolved"] is True
    assert result["reason"] == "slider_cleared"
    assert provider_calls and provider_calls[0][2] is False
    assert provider_calls[0][0] == b"target"
    assert provider_calls[0][1] == b"background-with-target-hidden"
    assert page.background_captured_with_target_hidden is True
    assert page.hidden_selectors == set()
    assert page.mouse.down_called is True
    assert page.mouse.up_called is True
    assert page.slider_visible is False
    assert page.wait_calls[-1] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS
    assert page.mouse.moves[-1][0] == pytest.approx(175.0)


def test_tiktok_slider_gap_detection_prefers_target_component() -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (340, 213), (135, 205, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 150, 339, 212), fill=(60, 70, 40))
    draw.rectangle((196, 79, 250, 134), fill=(45, 55, 48))
    buffer = BytesIO()
    image.save(buffer, format="PNG")

    gap = product_page._detect_tiktok_slider_gap_from_background(buffer.getvalue())

    assert gap["x"] == 196
    assert gap["width"] == 55
    assert gap["height"] == 56


def test_tiktok_slider_match_prefers_rightmost_ocr_candidate_when_gap_missing() -> None:
    calls: list[bool] = []

    class _FakeProvider:
        def match_slider(
            self,
            target_image: bytes,
            background_image: bytes,
            *,
            simple_target: bool = False,
        ) -> SimpleNamespace:
            del target_image, background_image
            calls.append(simple_target)
            return SimpleNamespace(
                target_x=211 if simple_target else 40,
                target_y=120,
                confidence=0.5,
            )

    slider_match, metadata = product_page._match_tiktok_slider(
        _FakeProvider(),
        b"not-an-image",
        b"not-an-image",
    )

    assert calls == [False, True]
    assert slider_match.target_x == 211
    assert metadata["simple_target"] is True


def test_tiktok_slider_resolution_waits_for_delayed_visible_slider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakeSliderPage()
    page.slider_visible = False

    def fake_wait(timeout_ms: int) -> None:
        page.wait_calls.append(timeout_ms)
        if not page.mouse.down_called:
            page.slider_visible = True

    page.wait_for_timeout = fake_wait  # type: ignore[method-assign]

    class _FakeProvider:
        def match_slider(
            self,
            target_image: bytes,
            background_image: bytes,
            *,
            simple_target: bool = False,
        ) -> SimpleNamespace:
            del target_image, background_image, simple_target
            return SimpleNamespace(target_x=72, target_y=12, confidence=0.91)

    monkeypatch.setattr(
        product_page,
        "_build_tiktok_slider_captcha_provider",
        lambda: _FakeProvider(),
    )

    result = product_page._try_resolve_tiktok_slider_security_check(
        page,
        product_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
        max_attempts=1,
        appear_timeout_ms=10,
        settle_ms=1,
    )

    assert result["resolved"] is True
    assert result["reason"] == "slider_cleared"
    assert page.wait_calls[0] > 0
    assert page.wait_calls[-1] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS


def test_tiktok_slider_resolution_requires_delayed_second_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakeSliderPage()

    def fake_wait(timeout_ms: int) -> None:
        page.wait_calls.append(timeout_ms)
        if timeout_ms == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS:
            page.slider_visible = True

    page.wait_for_timeout = fake_wait  # type: ignore[method-assign]

    class _FakeProvider:
        def match_slider(
            self,
            target_image: bytes,
            background_image: bytes,
            *,
            simple_target: bool = False,
        ) -> SimpleNamespace:
            del target_image, background_image, simple_target
            return SimpleNamespace(target_x=72, target_y=12, confidence=0.91)

    monkeypatch.setattr(
        product_page,
        "_build_tiktok_slider_captcha_provider",
        lambda: _FakeProvider(),
    )

    result = product_page._try_resolve_tiktok_slider_security_check(
        page,
        product_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
        max_attempts=1,
        settle_ms=1,
    )

    assert result["resolved"] is False
    assert result["attempts"][0]["confirmation_wait_ms"] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS
    assert result["attempts"][0]["confirmation_popup_still_visible"] is True
    assert result["attempts"][0]["reason"] == "slider_reappeared_after_confirmation_wait"


def test_tiktok_framework_slider_requires_page_state_confirmation_and_humanized_drag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import automation_framework.captcha as captcha_module

    wait_calls: list[int] = []
    captured: dict[str, object] = {}

    class _FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            captured["provider_config"] = kwargs

    class _FakeAudit:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {
                "attempts": [
                    {
                        "attempt_index": 1,
                        "success": True,
                        "mode": "match",
                        "simple_target": True,
                        "popup_still_visible": False,
                        "slider_result": {
                            "target_x": 234,
                            "target_y": 169,
                            "confidence": 0.55,
                            "raw": {"target_x": 234, "target_y": 169},
                        },
                        "mapping": {"drag_distance": 101.25},
                    }
                ]
            }

    class _FakeResolver:
        def __init__(self, *, provider: object, selectors: object, config: object) -> None:
            captured["provider"] = provider
            captured["selectors"] = selectors
            captured["config"] = config

        def resolve(self, automation_page: object) -> SimpleNamespace:
            captured["automation_page"] = automation_page
            return SimpleNamespace(
                success=True,
                audit=_FakeAudit(),
                artifacts_payload={},
            )

    page = SimpleNamespace(wait_for_timeout=lambda timeout_ms: wait_calls.append(timeout_ms))
    automation_page = object()

    monkeypatch.setattr(captcha_module, "DdddOcrCaptchaProvider", _FakeProvider)
    monkeypatch.setattr(captcha_module, "SliderCaptchaResolver", _FakeResolver)
    monkeypatch.setattr(product_page, "_read_tiktok_slider_captcha_state", lambda _page: {"visible": True})
    monkeypatch.setattr(
        product_page,
        "_wait_for_tiktok_slider_post_drag_state",
        lambda _page, *, timeout_ms: {"visible": False, "wait_elapsed_ms": 1200},
    )

    result = product_page._resolve_tiktok_slider_with_framework_captcha(
        automation_page,
        page=page,
        product_url="https://www.tiktok.com/view/product/1729492379807814341",
        max_attempts=1,
        settle_ms=product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_SETTLE_MS,
        confirm_ms=product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS,
        audit_dir=str(tmp_path),
        provider_config={"import_onnx_path": "/models/tiktok-slider.onnx"},
        resolver_config=None,
        selectors=None,
        trace_id="trace-1",
    )

    config = captured["config"]
    assert getattr(config, "image_timeout_ms") == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_IMAGE_TIMEOUT_MS
    assert getattr(config, "after_drag_wait_ms") == 0
    assert getattr(config, "success_timeout_ms") == 0
    assert getattr(config, "refresh_wait_ms") == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_REFRESH_SETTLE_MS
    assert getattr(config, "max_attempts") == 1
    assert getattr(config, "drag_steps") == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEPS
    assert (
        getattr(config, "drag_step_delay_seconds")
        == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEP_DELAY_SECONDS
    )
    assert getattr(config, "simple_target") is product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_SIMPLE_TARGET
    assert result["resolved"] is False
    assert result["reason"] == "slider_reappeared_after_confirmation_wait"
    assert result["post_drag_verify_wait_ms"] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_SETTLE_MS
    assert result["confirmation_wait_ms"] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS
    assert result["drag_profile"]["steps"] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_DRAG_STEPS
    assert result["attempts"][0]["reason"] == "slider_reappeared_after_confirmation_wait"
    assert result["attempts"][0]["post_drag_verify_wait_ms"] == product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_SETTLE_MS
    assert result["attempts"][0]["post_drag_wait_elapsed_ms"] == 1200
    assert result["attempts"][0]["confirmation_popup_still_visible"] is True
    assert wait_calls == [product_page.DEFAULT_TIKTOK_SLIDER_CAPTCHA_CONFIRM_MS]


def test_tiktok_blocked_handler_tries_slider_for_product_security_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_resolve(page: object, *, product_url: str, **kwargs: object) -> dict[str, object]:
        del page, kwargs
        captured["product_url"] = product_url
        return {"attempted": True, "resolved": True, "reason": "slider_cleared", "attempts": [{}]}

    monkeypatch.setattr(product_page, "_try_resolve_tiktok_slider_security_check", fake_resolve)

    resolution = product_page._handle_tiktok_blocked_context(
        SimpleNamespace(raw_page=object()),
        SimpleNamespace(
            page_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            blocker_type="security_challenge",
            summary="slide to verify",
            dom_summary={},
        ),
    )

    assert resolution.action == "handled_recheck"
    assert captured["product_url"] == "https://www.tiktok.com/shop/pdp/1730964478199763166"
