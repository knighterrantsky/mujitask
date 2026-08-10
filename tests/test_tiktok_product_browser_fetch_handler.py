from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import automation_business_scaffold.capabilities.browser.tiktok_product_fetch_handler as browser_handler
from automation_business_scaffold.capabilities.browser.tiktok import product_page
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
    assert normalized["asset_refs"] == normalized["media_assets"]
    assert normalized["fact_bundle"]["media_assets"] == []
    assert normalized["media_assets"][0]["local_path"] == "/tmp/1730964478199763166-main.webp"
    assert result.summary["slider_captcha_attempted"] is True
    assert result.summary["slider_captcha_resolved"] is True
    assert (
        result.result["slider_captcha_resolution"]["attempts"][0]["coordinate_mapping"][
            "drag_distance"
        ]
        == 85.0
    )
    assert (
        result.result["slider_captcha_audit_artifact_refs"][0]["artifact_key"]
        == "slider_attempt_1_background_image"
    )


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
        raise browser_handler.TikTokProductUnavailableError(
            "Product not available in this country or region"
        )

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


def test_tiktok_product_browser_fetch_does_not_retry_exhausted_cdp_recovery(monkeypatch) -> None:
    class RecoveryExhaustedError(RuntimeError):
        code = "chrome_cdp_session_recovery_failed"
        reason = "reconnect_timeout"
        phase = "reconnect"
        recovery_attempted = True
        recovery_count = 1

    def fake_fetch(product_url: str, **kwargs: Any) -> _FakeProduct:
        del product_url, kwargs
        raise RecoveryExhaustedError("sanitized recovery failure")

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

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "tiktok_browser_fetch_failed"
    assert result.error.retryable is False
    assert result.error.details["browser_provider_error_code"] == (
        "chrome_cdp_session_recovery_failed"
    )
    assert result.error.details["browser_provider_reason"] == "reconnect_timeout"
    assert result.error.details["browser_provider_phase"] == "reconnect"
    assert result.error.details["browser_provider_recovery_count"] == 1


def test_tiktok_product_browser_fetch_recognizes_country_region_unprovided_message() -> None:
    message = product_page._extract_unavailable_message("此国家或地区未提供的商品")

    assert message == "TikTok product unavailable: 此国家或地区未提供的商品"


def test_tiktok_browser_operation_log_is_correlated_sanitized_and_flush_safe(
    capsys,
) -> None:
    progress_events: list[tuple[str, dict[str, Any]]] = []
    context = HandlerContext(
        request_id="request-observability",
        job_id="execution-observability",
        handler_code="tiktok_product_browser_fetch",
        worker_type="browser_worker",
        runtime_table="task_execution",
        payload={"run_id": "run-observability"},
        attempt_count=2,
        metadata={
            "progress_callback": lambda stage, **kwargs: progress_events.append(
                (stage, dict(kwargs.get("details") or {}))
            )
        },
    )
    reporter = browser_handler._browser_progress_reporter(context)

    with pytest.raises(RuntimeError):
        with product_page._browser_operation(
            reporter,
            "main_image_download",
            error_state="suppressed_error",
        ):
            raise RuntimeError("sensitive exception body")

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["state"] for event in events] == ["started", "suppressed_error"]
    assert events[0]["operation_id"] == events[1]["operation_id"]
    assert events[1]["error_class"] == "RuntimeError"
    for event in events:
        assert event["execution_id"] == "execution-observability"
        assert event["job_id"] == "execution-observability"
        assert event["run_id"] == "run-observability"
        assert event["attempt_count"] == 2
        assert isinstance(event["child_pid"], int)
        assert event["reported_at"]
        assert "sensitive exception body" not in json.dumps(event)
    assert progress_events[-1][1]["state"] == "suppressed_error"


def test_tiktok_timing_log_drops_urls_selectors_and_paths(capsys) -> None:
    product_page._log_tiktok_fetch_timing(
        trace_id="request-safe",
        step="browser_fetch_start",
        product_url="https://example.test/path?token=secret",
        selector="#account-secret",
        local_path="/tmp/private/browser.png",
        product_id="12345",
    )

    output = capsys.readouterr().out
    assert "product_id=12345" in output
    assert "example.test" not in output
    assert "account-secret" not in output
    assert "/tmp/private" not in output


def test_login_toast_wait_reports_caught_evaluate_timeout_as_suppressed() -> None:
    events: list[dict[str, object]] = []

    class Page:
        def evaluate(self, *args, **kwargs):
            del args, kwargs
            raise TimeoutError("not persisted")

        def wait_for_timeout(self, timeout_ms: int) -> None:
            del timeout_ms

    def report(operation: str, *, details: dict[str, object]) -> None:
        assert operation == "login_toast_wait"
        events.append(dict(details))

    with product_page._browser_operation(
        report, "login_toast_wait"
    ) as operation_handle:
        product_page._wait_for_login_toast_to_settle(
            Page(),
            settle_ms=1,
            timeout_ms=1,
            poll_ms=1,
            operation_handle=operation_handle,
        )

    assert [event["state"] for event in events] == ["started", "suppressed_error"]
    assert events[-1]["error_class"] == "TimeoutError"


def test_security_check_reports_caught_locator_timeout_as_suppressed() -> None:
    events: list[dict[str, object]] = []

    class Page:
        def locator(self, selector: str):
            del selector
            raise TimeoutError("not persisted")

    def report(operation: str, *, details: dict[str, object]) -> None:
        assert operation == "security_check"
        events.append(dict(details))

    with product_page._browser_operation(report, "security_check"):
        resolution = product_page._try_resolve_tiktok_slider_security_check(
            Page(),
            product_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            appear_timeout_ms=0,
        )

    assert resolution["reason"] == "slider_not_visible"
    assert [event["state"] for event in events] == ["started", "suppressed_error"]
    assert events[0]["operation_id"] == events[1]["operation_id"]
    assert events[-1]["error_class"] == "TimeoutError"


def test_security_check_reports_caught_body_text_timeout_as_suppressed() -> None:
    events: list[dict[str, object]] = []

    class Locator:
        def inner_text(self, *, timeout: int) -> str:
            del timeout
            raise TimeoutError("not persisted")

    class Page:
        def locator(self, selector: str) -> Locator:
            assert selector == "body"
            return Locator()

    def report(operation: str, *, details: dict[str, object]) -> None:
        assert operation == "security_check"
        events.append(dict(details))

    with product_page._browser_operation(report, "security_check"):
        result = product_page._detect_browser_security_check(
            Page(),
            html="",
            resolved_url="https://www.tiktok.com/shop/pdp/1730964478199763166",
            dom_snapshot={},
        )

    assert result is None
    assert [event["state"] for event in events] == ["started", "suppressed_error"]
    assert events[0]["operation_id"] == events[1]["operation_id"]
    assert events[-1]["error_class"] == "TimeoutError"


def test_main_image_capture_reports_failed_candidate_before_fallback_success(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []

    class Locator:
        first: "Locator"

        def __init__(self) -> None:
            self.first = self

        def wait_for(self, *, state: str, timeout: int) -> None:
            assert state == "visible"
            assert timeout == 1_000

        def screenshot(self, *, path: str) -> None:
            assert path == str(tmp_path / "main.png")

    class Page:
        locator_calls = 0

        def locator(self, selector: str) -> Locator:
            del selector
            self.locator_calls += 1
            if self.locator_calls == 1:
                raise TimeoutError("not persisted")
            return Locator()

    def report(operation: str, *, details: dict[str, object]) -> None:
        assert operation == "main_image_capture"
        events.append(dict(details))

    page = Page()
    with product_page._browser_operation(report, "main_image_capture"):
        product_page._capture_locator_screenshot(
            page,
            tmp_path / "main.png",
            selector="#primary-image",
        )

    assert page.locator_calls == 2
    assert [event["state"] for event in events] == ["started", "suppressed_error"]
    assert events[0]["operation_id"] == events[1]["operation_id"]
    assert events[-1]["error_class"] == "TimeoutError"
