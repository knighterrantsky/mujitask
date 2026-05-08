from __future__ import annotations

import base64
import json
import struct
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from automation_framework.captcha import SliderMatchResult
from PIL import Image, ImageDraw

import automation_business_scaffold.capabilities.browser.fastmoss_security_resolve_handler as fastmoss_security_module
from automation_business_scaffold.capabilities.browser.fastmoss_security_resolve_handler import (
    fastmoss_security_browser_resolve_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.domains.tiktok.flows.search_keyword_competitor_products.orchestrator import (
    advance_stage,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.flows.search_keyword_competitor_products.summary import (
    finalize_request,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import keyword_search_parameter_mapper
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import build_tiktok_outbox_message_text
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import build_fastmoss_cookie_cache_context
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

TASK_CODE = "search_keyword_competitor_products"
SEED_TABLE_REF = "tbl_keyword_seed"
SEARCH_QUERY = "water bottle"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + b"fake"
    )


def _png_base64(width: int, height: int) -> str:
    return base64.b64encode(_png_bytes(width, height)).decode("ascii")


def _image_bytes(image: Image.Image, image_format: str) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def _read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_fastmoss_search_security_browser_fallback_design_contract_is_documented() -> None:
    combined = "\n".join(
        [
            _read_repo_text("docs/arch/workflow-competitor-table-design.md"),
            _read_repo_text("contracts/workflow/search_keyword_competitor_products.yaml"),
            _read_repo_text("docs/arch/workflow-design-guidelines.md"),
            _read_repo_text("docs/arch/runtime-db-schema-design.md"),
            _read_repo_text("contracts/harness/architecture-ownership.yaml"),
        ]
    )

    required_tokens = (
        "fastmoss_security_browser_fallback",
        "fastmoss_security_browser_resolve",
        "MSG_SAFE_0001",
        "/api/goods/V2/search",
        "fastmoss_session_cookie_cache",
        "商品详情页不能作为搜索风控解除成功判据",
        "cookies，不是 API token",
        "API worker 不直接驱动浏览器",
        "原始失败的 FastMoss API 请求",
        "/api/goods/v3/base",
        "fastmoss_api_security_verification",
        "verification_request",
        "fastmoss_product_fetch",
        "fastmoss_creator_fetch",
        "fastmoss_shop_fetch",
        "fastmoss_video_fetch",
        "infrastructure/fastmoss",
        "last_auth_failed_at",
        "fastmoss_session_conflict_or_external_login",
        "单点登录或外部登录冲突",
    )

    missing = [token for token in required_tokens if token not in combined]
    assert missing == [], "FastMoss browser fallback design contract is missing tokens:\n" + "\n".join(missing)


def test_security_captcha_handling_strategy_is_documented() -> None:
    combined = "\n".join(
        [
            _read_repo_text("docs/business/requirements/search-keyword-competitor-products.md"),
            _read_repo_text("docs/arch/workflow-competitor-table-design.md"),
            _read_repo_text("docs/arch/workflow-design-guidelines.md"),
            _read_repo_text("docs/arch/handler-contract-design.md"),
            _read_repo_text("contracts/workflow/search_keyword_competitor_products.yaml"),
        ]
    )

    required_tokens = (
        "验证码等待只在已识别风控信号后发生",
        "正常商品页抓取不得进入滑块等待",
        "正常 FastMoss 搜索、正常 TikTok request、正常 TikTok 商品页采集不得进入滑块等待",
        "image_timeout_ms 是元素出现的最大等待，不是固定 sleep",
        "滑动后最多轮询 5 秒验证结果",
        "弹窗消失后延迟 2 秒二次确认",
        "Unable to verify. Please try again.",
        "TikTok 商品页默认 simple_target=false",
        "FastMoss 与 TikTok 商品页验证码逻辑独立",
        "审计必须记录 ddddocr 原始坐标、坐标换算、拖动距离、前后截图",
        "目标位置截图",
        "target_position_screenshot",
    )

    missing = [token for token in required_tokens if token not in combined]
    assert missing == [], "security captcha handling strategy is missing tokens:\n" + "\n".join(missing)


def test_fastmoss_security_browser_resolve_persists_cookie_cache_without_leaking_values(
    runtime_db_url: str,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    result = fastmoss_security_browser_resolve_handler(
        _browser_handler_context(
            {
                "execution_control_db_url": runtime_db_url,
                "search_request": {
                    "keyword": SEARCH_QUERY,
                    "search_query": SEARCH_QUERY,
                    "region": "US",
                    "pagination": {"page": 1, "page_size": 10},
                },
                "fastmoss": {
                    "phone": "18000000000",
                    "base_url": "https://www.fastmoss.com",
                    "region": "US",
                },
                "mock_fastmoss_security_browser_resolve": {
                    "response_code": "200",
                    "ext_is_login": "1",
                    "cookies": [
                        {
                            "name": "fd_tk",
                            "value": "browser-token",
                            "domain": ".fastmoss.com",
                            "path": "/",
                            "secure": True,
                        }
                    ],
                    "slider_resolution": {
                        "attempted": True,
                        "resolved": True,
                        "reason": "slider_cleared",
                        "attempts": [{"attempt": 1, "confirmation_wait_ms": 2000}],
                    },
                },
            }
        )
    )

    assert result.status == "success"
    assert result.result["verified_path"] == "/api/goods/V2/search"
    assert result.result["cookie_cache"]["cookie_count"] == 1
    assert result.result["cookie_cache"]["has_fd_tk"] is True
    assert result.result["slider_resolution"]["attempts"][0]["confirmation_wait_ms"] == 2000
    assert "browser-token" not in json.dumps(result.to_dict(), ensure_ascii=False)

    cache_context = build_fastmoss_cookie_cache_context(
        base_url="https://www.fastmoss.com",
        account_key="18000000000",
        region="US",
    )
    loaded = store.load_fastmoss_cookie_cache(cache_key=str(cache_context["cache_key"]))
    assert loaded is not None
    assert loaded["cookies"][0]["value"] == "browser-token"


def test_fastmoss_security_browser_resolve_supports_non_search_original_request(
    runtime_db_url: str,
) -> None:
    result = fastmoss_security_browser_resolve_handler(
        _browser_handler_context(
            {
                "execution_control_db_url": runtime_db_url,
                "verification_request": {
                    "method": "GET",
                    "path": "/api/goods/v3/base",
                    "params": {"product_id": "1732183420263764252"},
                    "referer": "https://www.fastmoss.com/zh/e-commerce/detail/1732183420263764252",
                    "region": "US",
                    "stage": "product.base",
                },
                "fastmoss": {
                    "phone": "18000000000",
                    "base_url": "https://www.fastmoss.com",
                    "region": "US",
                },
                "mock_fastmoss_security_browser_resolve": {
                    "response_code": "200",
                    "ext_is_login": "1",
                    "cookies": [
                        {
                            "name": "fd_tk",
                            "value": "browser-token",
                            "domain": ".fastmoss.com",
                            "path": "/",
                            "secure": True,
                        }
                    ],
                },
            }
        )
    )

    assert result.status == "success"
    assert result.result["verified_path"] == "/api/goods/v3/base"
    assert result.result["verification"]["verified_path"] == "/api/goods/v3/base"
    assert result.result["cookie_cache"]["verified_path"] == "/api/goods/v3/base"
    assert "browser-token" not in json.dumps(result.to_dict(), ensure_ascii=False)


def test_fastmoss_security_browser_resolve_preserves_audit_on_security_failure(
    runtime_db_url: str,
) -> None:
    result = fastmoss_security_browser_resolve_handler(
        _browser_handler_context(
            {
                "execution_control_db_url": runtime_db_url,
                "fallback_source_job_id": "seed-job-1",
                "search_request": {
                    "keyword": SEARCH_QUERY,
                    "search_query": SEARCH_QUERY,
                    "region": "US",
                    "pagination": {"page": 1, "page_size": 10},
                },
                "fastmoss": {
                    "phone": "18000000000",
                    "base_url": "https://www.fastmoss.com",
                    "region": "US",
                },
                "mock_fastmoss_security_browser_resolve": {
                    "response_code": "MSG_SAFE_0001",
                    "data_id": "299522",
                    "ext_is_login": "1",
                    "cookies": [
                        {
                            "name": "fd_tk",
                            "value": "browser-token",
                            "domain": ".fastmoss.com",
                            "path": "/",
                            "secure": True,
                        }
                    ],
                    "slider_resolution": {
                        "attempted": True,
                        "resolved": False,
                        "reason": "slider_popup_still_visible",
                        "framework_resolver": "SliderCaptchaResolver",
                        "artifact_refs": [
                            {
                                "artifact_key": "slider_captcha_audit",
                                "local_path": "/tmp/slider_captcha_audit.json",
                                "mime_type": "application/json",
                            }
                        ],
                        "attempts": [
                            {
                                "attempt": 1,
                                "match_method": "framework_slider_resolver",
                                "target_x": 136,
                                "target_y": 42,
                                "raw_result": {"target": [136, 42, 190, 96]},
                                "coordinate_mapping": {"scale_x": 0.5, "drag_offset_x": -2},
                                "drag_distance": 84.5,
                                "confidence": 0.88,
                                "artifact_keys": {
                                    "background": "attempt_1_background",
                                    "piece": "attempt_1_piece",
                                    "before_screenshot": "attempt_1_before",
                                    "after_screenshot": "attempt_1_after",
                                },
                            }
                        ],
                    },
                    "slider_captcha_audit_artifact_refs": [
                        {
                            "artifact_key": "slider_captcha_audit",
                            "local_path": "/tmp/slider_captcha_audit.json",
                            "mime_type": "application/json",
                        }
                    ],
                    "browser_diagnostic_artifact_refs": [
                        {
                            "artifact_key": "after_slider_resolution_screenshot",
                            "local_path": "/tmp/after_slider_resolution.png",
                            "mime_type": "image/png",
                        }
                    ],
                },
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "fastmoss_security_verification_required"
    assert result.summary["response_code"] == "MSG_SAFE_0001"
    assert result.summary["slider_artifact_count"] == 1
    assert result.summary["browser_diagnostic_artifact_count"] == 1
    assert result.result["verification"]["data_id"] == "299522"
    assert result.result["fallback_source_job_id"] == "seed-job-1"
    assert result.result["browser_cookie_export"]["cookie_count"] == 1
    assert result.result["browser_cookie_export"]["has_fd_tk"] is True
    attempt = result.result["slider_resolution"]["attempts"][0]
    assert attempt["target_x"] == 136
    assert attempt["coordinate_mapping"]["drag_offset_x"] == -2
    assert attempt["drag_distance"] == 84.5
    assert attempt["artifact_keys"]["after_screenshot"] == "attempt_1_after"
    assert result.result["slider_captcha_audit_artifact_refs"][0]["artifact_key"] == "slider_captcha_audit"
    assert result.result["browser_diagnostic_artifact_refs"][0]["artifact_key"] == "after_slider_resolution_screenshot"
    assert "browser-token" not in json.dumps(result.to_dict(), ensure_ascii=False)


class _FakeFastMossSliderMouse:
    def __init__(self, page: "_FakeFastMossSliderPage") -> None:
        self.page = page
        self.moves: list[tuple[float, float]] = []
        self.down_count = 0
        self.up_count = 0

    def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    def down(self) -> None:
        self.down_count += 1

    def up(self) -> None:
        self.up_count += 1
        if self.page.clear_on_mouse_up:
            self.page.popup_visible = False
        else:
            self.page.loading_reads_remaining = self.page.loading_reads_after_failed_drag


class _FakeFastMossSliderLocator:
    def __init__(self, page: "_FakeFastMossSliderPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_FakeFastMossSliderLocator":
        return self

    def count(self) -> int:
        return 1

    def is_visible(self, timeout: int | None = None) -> bool:
        del timeout
        if self.selector in fastmoss_security_module.FASTMOSS_SLIDER_POPUP_SELECTORS:
            return self.page.popup_visible
        return self.page.popup_visible

    def wait_for(self, *, state: str, timeout: int) -> None:
        del timeout
        if state == "visible" and not self.is_visible():
            raise TimeoutError(f"{self.selector} is not visible")

    def evaluate(self, script: str) -> dict[str, object]:
        del script
        box = self.bounding_box()
        background_image = "none"
        if self.selector in fastmoss_security_module.FASTMOSS_SLIDER_BACKGROUND_SELECTORS:
            background_image = f"url(data:image/png;base64,{_png_base64(672, 390)})"
        return {
            "tag_name": "div",
            "src": "",
            "backgroundImage": background_image,
            "natural_width": box["width"],
            "natural_height": box["height"],
            "rect": box,
        }

    def bounding_box(self, timeout: int | None = None) -> dict[str, float]:
        del timeout
        if self.selector in fastmoss_security_module.FASTMOSS_SLIDER_BACKGROUND_SELECTORS:
            return {"x": 100.0, "y": 50.0, "width": 340.0, "height": 160.0}
        if self.selector in fastmoss_security_module.FASTMOSS_SLIDER_TARGET_SELECTORS:
            return {"x": 120.0, "y": 80.0, "width": 50.0, "height": 50.0}
        if self.selector in fastmoss_security_module.FASTMOSS_SLIDER_HANDLE_SELECTORS:
            return {"x": 90.0, "y": 260.0, "width": 20.0, "height": 20.0}
        return {"x": 80.0, "y": 40.0, "width": 380.0, "height": 280.0}

    def screenshot(self, timeout: int | None = None) -> bytes:
        del timeout
        box = self.bounding_box()
        return _png_bytes(int(box["width"]), int(box["height"]))

    def click(self, timeout: int | None = None) -> None:
        del timeout
        self.page.refresh_count += 1


class _FakeFastMossSliderPage:
    def __init__(self) -> None:
        self.url = "https://www.fastmoss.com/zh/e-commerce/search?words=water%20bottle"
        self.popup_visible = True
        self.clear_on_mouse_up = True
        self.loading_reads_remaining = 0
        self.loading_reads_after_failed_drag = 0
        self.refresh_count = 0
        self.wait_calls: list[int] = []
        self.mouse = _FakeFastMossSliderMouse(self)

    def locator(self, selector: str) -> _FakeFastMossSliderLocator:
        return _FakeFastMossSliderLocator(self, selector)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_calls.append(timeout_ms)

    def screenshot(self, full_page: bool = False) -> bytes:
        del full_page
        return _png_bytes(640, 480)

    def evaluate(self, script: str, arg: object | None = None) -> object:
        if isinstance(arg, dict) and "loadingSelectors" in arg:
            if self.loading_reads_remaining > 0:
                self.loading_reads_remaining -= 1
                return True
            return False
        if "devicePixelRatio" in script:
            return 1.0
        if "navigator.userAgent" in script:
            return "Fake Browser"
        return {}

    def title(self) -> str:
        return "FastMoss Search"


def test_fastmoss_slider_uses_mixed_css_resolver_and_persists_target_position_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    page = _FakeFastMossSliderPage()
    captured_provider_config: dict[str, object] = {}

    class _FakeProvider:
        def match_slider(self, target_image: bytes, background_image: bytes, *, simple_target: bool = False) -> SliderMatchResult:
            assert target_image.startswith(b"\x89PNG")
            assert background_image.startswith(b"\x89PNG")
            assert simple_target is False
            return SliderMatchResult(
                target_x=150,
                target_y=20,
                confidence=0.91,
                raw={"target": [150, 20, 190, 60], "confidence": 0.91},
            )

        def compare_slider(self, target_image: bytes, background_image: bytes) -> SliderMatchResult:
            raise AssertionError("FastMoss slider should use match mode by default")

    def fake_provider(config: dict[str, object] | None = None) -> _FakeProvider:
        captured_provider_config.update(config or {})
        return _FakeProvider()

    monkeypatch.setattr(fastmoss_security_module, "_build_slider_captcha_provider", fake_provider)
    automation_page = SimpleNamespace(
        raw_page=page,
        build_diagnostic_artifacts_payload=lambda **kwargs: {
            "state_dump": kwargs["state_dump"],
            "extra": {"page_diagnostics": {"url": page.url}},
        },
    )

    result = fastmoss_security_module._try_resolve_fastmoss_slider_security_check(
        page,
        automation_page=automation_page,
        raw_page=page,
        search_url=page.url,
        max_attempts=1,
        appear_timeout_ms=1,
        settle_ms=1,
        confirm_ms=2000,
        audit_dir=str(tmp_path),
        provider_config={"import_onnx_path": "/models/fastmoss-slider.onnx"},
        resolver_config={"drag_offset_x": -2},
        selectors={
            "popup": "#tcaptcha_transform_dy",
            "background": ".tencent-captcha-dy__verify-bg-img",
            "piece": ".tencent-captcha-dy__fg-item",
            "handle": ".tencent-captcha-dy__slider-block",
            "refresh": ".tencent-captcha-dy__footer-icon--refresh",
        },
    )

    assert result["resolved"] is True
    assert result["framework_resolver"] == "FastMossMixedCssSliderResolver"
    assert captured_provider_config["import_onnx_path"] == "/models/fastmoss-slider.onnx"
    attempt = result["attempts"][0]
    assert attempt["match_method"] == "fastmoss_mixed_css_slider_resolver"
    assert attempt["simple_target"] is False
    assert attempt["raw_result"]["target"] == [150, 20, 190, 60]
    assert attempt["coordinate_mapping"]["drag_offset_x"] == -2
    assert attempt["coordinate_mapping"]["target_interpretation"] == "target_center_minus_piece_center"
    assert attempt["post_drag_verify_wait_ms"] == 1
    assert attempt["confirmation_wait_ms"] == 2000
    assert result["post_drag_verify_wait_ms"] == 1
    assert result["confirmation_wait_ms"] == 2000
    assert result["drag_profile"] == {
        "steps": fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS,
        "step_delay_seconds": fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS,
    }
    assert fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS <= 36
    assert fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS <= 0.012
    assert result["audit"]["config"]["drag_steps"] == fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS
    assert result["audit"]["config"]["simple_target"] is False
    assert (
        result["audit"]["config"]["drag_step_delay_seconds"]
        == fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS
    )
    assert attempt["artifact_keys"]["target_position_screenshot"] == "slider_attempt_1_target_position_screenshot"
    assert result["artifact_refs"]
    assert all(Path(ref["local_path"]).exists() for ref in result["artifact_refs"])
    target_ref = next(ref for ref in result["artifact_refs"] if ref["artifact_key"] == "slider_attempt_1_target_position_screenshot")
    assert Path(target_ref["local_path"]).read_bytes().startswith(b"\x89PNG")


def test_fastmoss_slider_corrects_low_confidence_ocr_with_outline_bbox_anchor() -> None:
    background = Image.new("RGB", (672, 390), (42, 58, 72))
    draw = ImageDraw.Draw(background)
    target_outline = [
        (420, 182),
        (452, 182),
        (452, 169),
        (476, 169),
        (476, 182),
        (508, 182),
        (508, 246),
        (420, 246),
        (420, 182),
    ]
    draw.line(target_outline, fill=(226, 226, 226), width=5, joint="curve")
    background_bytes = _image_bytes(background, "JPEG")

    piece = Image.new("RGB", (60, 60), (46, 58, 68))
    piece_draw = ImageDraw.Draw(piece)
    piece_draw.line(
        [
            (8, 10),
            (26, 10),
            (26, 2),
            (42, 2),
            (42, 10),
            (54, 10),
            (54, 52),
            (8, 52),
            (8, 10),
        ],
        fill=(235, 235, 235),
        width=4,
        joint="curve",
    )
    piece_bytes = _image_bytes(piece, "PNG")

    ocr_result = SliderMatchResult(
        target_x=508,
        target_y=214,
        confidence=0.18,
        raw={"target": [508, 214], "confidence": 0.18},
    )
    anchored = fastmoss_security_module._select_fastmoss_shape_anchor_slider_result(
        ocr_result,
        background_image=background_bytes,
        piece_image=piece_bytes,
        background_box={"x": 100.0, "y": 50.0, "width": 336.0, "height": 195.0},
        piece_box={"x": 118.0, "y": 200.0, "width": 60.0, "height": 60.0},
    )

    assert anchored.target_x != ocr_result.target_x
    assert anchored.target_x == 464
    shape_anchor = anchored.raw["fastmoss_shape_anchor"]
    assert shape_anchor["selected_box"]["x"] == 418.0
    assert shape_anchor["target_interpretation"] == "fastmoss_outline_bbox_center_minus_piece_outline_anchor"

    mapping = fastmoss_security_module._build_fastmoss_mixed_slider_mapping(
        SimpleNamespace(),
        slider_result=anchored,
        background_box={"x": 100.0, "y": 50.0, "width": 336.0, "height": 195.0},
        background_image_size=(672, 390),
        piece_box={"x": 118.0, "y": 200.0, "width": 60.0, "height": 60.0},
        handle_box={"x": 114.0, "y": 270.0, "width": 48.0, "height": 48.0},
        drag_scale=1.0,
        drag_offset_x=0.0,
    )

    direct_ocr_css_target_x = ocr_result.target_x / 672 * 336
    assert mapping["css_target_x"] < direct_ocr_css_target_x
    assert mapping["target_interpretation"] == "fastmoss_outline_bbox_center_minus_piece_outline_anchor"
    assert mapping["fastmoss_shape_anchor"]["source_target_x"] == 508.0


def test_fastmoss_slider_keeps_ocr_when_outline_candidate_is_far_away() -> None:
    background = Image.new("RGB", (672, 390), (42, 58, 72))
    draw = ImageDraw.Draw(background)
    draw.rectangle((52, 150, 84, 194), outline=(226, 226, 226), width=5)
    background_bytes = _image_bytes(background, "JPEG")

    piece = Image.new("RGB", (60, 60), (46, 58, 68))
    piece_draw = ImageDraw.Draw(piece)
    piece_draw.rectangle((8, 8, 54, 52), outline=(235, 235, 235), width=4)
    piece_bytes = _image_bytes(piece, "PNG")

    ocr_result = SliderMatchResult(
        target_x=568,
        target_y=222,
        confidence=0.31,
        raw={"target": [568, 222], "confidence": 0.31},
    )
    anchored = fastmoss_security_module._select_fastmoss_shape_anchor_slider_result(
        ocr_result,
        background_image=background_bytes,
        piece_image=piece_bytes,
        background_box={"x": 430.0, "y": 358.0, "width": 330.0, "height": 235.7},
        piece_box={"x": 454.5, "y": 427.2, "width": 58.9, "height": 58.9},
    )

    assert anchored is ocr_result


def test_fastmoss_slider_waits_for_loading_to_finish_before_retry(monkeypatch, tmp_path: Path) -> None:
    page = _FakeFastMossSliderPage()
    page.clear_on_mouse_up = False
    page.loading_reads_after_failed_drag = 2

    class _FakeProvider:
        def match_slider(self, target_image: bytes, background_image: bytes, *, simple_target: bool = False) -> SliderMatchResult:
            del target_image, background_image, simple_target
            return SliderMatchResult(
                target_x=150,
                target_y=20,
                confidence=0.91,
                raw={"target": [150, 20, 190, 60], "confidence": 0.91},
            )

        def compare_slider(self, target_image: bytes, background_image: bytes) -> SliderMatchResult:
            raise AssertionError("FastMoss slider should use match mode by default")

    monkeypatch.setattr(fastmoss_security_module, "_build_slider_captcha_provider", lambda _config=None: _FakeProvider())

    result = fastmoss_security_module._try_resolve_fastmoss_slider_security_check(
        page,
        automation_page=SimpleNamespace(raw_page=page),
        raw_page=page,
        search_url=page.url,
        max_attempts=2,
        appear_timeout_ms=1,
        settle_ms=1,
        confirm_ms=2000,
        audit_dir=str(tmp_path),
        provider_config={},
        resolver_config={"refresh_wait_ms": 1, "image_timeout_ms": 1_000},
        selectors={
            "popup": "#tcaptcha_transform_dy",
            "background": ".tencent-captcha-dy__verify-bg-img",
            "piece": ".tencent-captcha-dy__fg-item",
            "handle": ".tencent-captcha-dy__slider-block",
            "refresh": ".tencent-captcha-dy__footer-icon--refresh",
        },
    )

    assert result["resolved"] is False
    assert page.refresh_count == 1
    assert result["attempts"][1]["pre_retry_state"]["loading_visible"] is False
    assert result["attempts"][1]["pre_retry_state"]["wait_elapsed_ms"] >= 500


def test_fastmoss_slider_waits_for_visual_elements_before_framework_resolver(monkeypatch) -> None:
    page = _FakeFastMossSliderPage()
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        fastmoss_security_module,
        "_wait_for_fastmoss_slider_state",
        lambda _page, *, timeout_ms: {"visible": True, "selector": "#tcaptcha_transform_dy"},
    )

    def fake_wait_for_elements(_page, *, timeout_ms, selector_overrides=None):
        calls["timeout_ms"] = timeout_ms
        calls["selector_overrides"] = selector_overrides
        return object(), ".ready-bg", object(), ".ready-piece", object(), ".ready-handle"

    def fake_framework_resolve(
        _automation_page,
        *,
        page,
        initial_state,
        search_url,
        max_attempts,
        settle_ms,
        confirm_ms,
        audit_dir,
        provider_config,
        resolver_config,
        selectors,
    ):
        del page, search_url, max_attempts, settle_ms, confirm_ms, audit_dir, provider_config, resolver_config, selectors
        assert initial_state["background_selector"] == ".ready-bg"
        assert initial_state["piece_selector"] == ".ready-piece"
        assert initial_state["handle_selector"] == ".ready-handle"
        return {"attempted": True, "resolved": True, "reason": "slider_cleared", "attempts": []}

    monkeypatch.setattr(fastmoss_security_module, "_wait_for_fastmoss_slider_elements", fake_wait_for_elements)
    monkeypatch.setattr(
        fastmoss_security_module,
        "_resolve_fastmoss_slider_with_framework_captcha",
        fake_framework_resolve,
    )

    result = fastmoss_security_module._try_resolve_fastmoss_slider_security_check(
        page,
        automation_page=SimpleNamespace(raw_page=page),
        raw_page=page,
        search_url=page.url,
        max_attempts=1,
        appear_timeout_ms=1,
        settle_ms=3000,
        confirm_ms=2000,
        selectors={"background": ".ready-bg"},
    )

    assert result["resolved"] is True
    assert calls["timeout_ms"] == fastmoss_security_module.DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS
    assert calls["selector_overrides"] == {"background": ".ready-bg"}


def test_fastmoss_browser_diagnostic_artifacts_write_state_and_screenshot(tmp_path: Path) -> None:
    page = _FakeFastMossSliderPage()

    refs = fastmoss_security_module._capture_fastmoss_browser_diagnostic_artifacts(
        page,
        raw_page=page,
        audit_dir=str(tmp_path),
        search_url=page.url,
        label="after slider resolution",
        state={"slider_resolution": {"attempts": [{"target_x": 136, "drag_distance": 84.5}]}},
    )

    state_ref = next(ref for ref in refs if ref["artifact_key"] == "after_slider_resolution_state")
    screenshot_ref = next(ref for ref in refs if ref["artifact_key"] == "after_slider_resolution_screenshot")
    state_payload = json.loads(Path(state_ref["local_path"]).read_text(encoding="utf-8"))

    assert Path(screenshot_ref["local_path"]).read_bytes().startswith(b"\x89PNG")
    assert state_payload["page_title"] == "FastMoss Search"
    assert state_payload["state"]["slider_resolution"]["attempts"][0]["target_x"] == 136
    assert state_payload["state"]["slider_resolution"]["attempts"][0]["drag_distance"] == 84.5


def test_keyword_search_parameter_mapper_builds_fastmoss_search_payload() -> None:
    mapped = keyword_search_parameter_mapper(
        {
            "search_keyword": SEARCH_QUERY,
            "filters": {"country_code": "US"},
            "sales_7d_threshold": "200",
            "max_candidates": "5",
            "fastmoss_search_order": "2,2",
        }
    )

    assert mapped["stage_code"] == "keyword_seed_import"
    assert mapped["search_mode"] == "keyword"
    assert mapped["keyword"] == SEARCH_QUERY
    assert mapped["search_query"] == SEARCH_QUERY
    assert mapped["filters"] == {"country_code": "US"}
    assert mapped["limit"] == 5
    assert mapped["sort"] == {"field": "day7_sold_count", "direction": "desc", "source_order": "2,2"}
    assert mapped["output_conditions"]["business_conditions"]["min_day7_sold_count"] == "200"


def test_keyword_search_parameter_mapper_applies_selection_defaults() -> None:
    mapped = keyword_search_parameter_mapper(
        {
            "search_keyword": SEARCH_QUERY,
            "keyword_workflow_mode": "selection",
        }
    )

    assert mapped["output_conditions"]["business_conditions"] == {
        "min_day7_sold_count": "500",
        "min_price_range_max_amount": "10.99",
    }


def test_keyword_search_parameter_mapper_selection_defaults_are_overridable() -> None:
    mapped = keyword_search_parameter_mapper(
        {
            "search_keyword": SEARCH_QUERY,
            "keyword_workflow_mode": "selection",
            "sales_7d_threshold": "800",
            "product_price_threshold": "15.5",
        }
    )

    assert mapped["output_conditions"]["business_conditions"] == {
        "min_day7_sold_count": "800",
        "min_price_range_max_amount": "15.5",
    }


def test_keyword_search_parameter_mapper_keeps_zero_as_unlimited_candidate_limit() -> None:
    mapped = keyword_search_parameter_mapper(
        {
            "search_keyword": SEARCH_QUERY,
            "max_candidates": "0",
        }
    )

    assert mapped["limit"] == 0
    assert mapped["output_conditions"] == {"max_candidates": 0}


def test_keyword_outbox_detail_hides_existing_records() -> None:
    message = build_tiktok_outbox_message_text(
        request_id="req-keyword",
        task_code=TASK_CODE,
        summary={"final_status": "partial_success"},
        result={
            "search_query": "2026 graduation",
            "search_filter_info": {"output_conditions": {"business_conditions": {"min_day7_sold_count": "500"}}},
            "candidate_total_count": 2,
            "seed_write_results": [
                {"product_id": "sku-existing", "status": "skip_existing"},
                {"product_id": "sku-new", "status": "success"},
            ],
            "row_results": [
                {
                    "source_record_id": "rec-existing",
                    "product_id": "sku-existing",
                    "row_status": "failed",
                    "failure_reason": "existing_record",
                },
                {
                    "source_record_id": "rec-new",
                    "product_id": "sku-new",
                    "row_status": "success",
                },
            ],
        },
    )

    assert "种子跳过：1 条" in message
    assert "详情成功：1 条" in message
    assert "详情失败：0 条" in message
    assert "sku-existing" not in message
    assert "1. SKU sku-new" in message


def _store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _submit_keyword_request(runtime_db_url: str) -> tuple[RuntimeStore, object, object]:
    store = _store(runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload={
            "search_query": SEARCH_QUERY,
            "filters": {"country_code": "US"},
            "output_conditions": {"require_product_url": True},
            "max_candidates": 5,
            "seed_table_ref": SEED_TABLE_REF,
            "reply_target": "reply://pytest",
        },
        requested_by="pytest",
        source_channel_code="console",
        reply_target="reply://pytest",
    )
    workflow = get_workflow_definition(TASK_CODE)
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage=workflow.entry_stage_code,
        progress_stage=workflow.entry_stage_code,
    )
    return store, request, workflow


def _latest_stage_job(store: RuntimeStore, *, request_id: str, stage_code: str, job_code: str) -> dict:
    jobs = [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
        and str(job.get("job_code") or "") == job_code
    ]
    assert jobs, f"expected stage job {stage_code}/{job_code}"
    return jobs[-1]


def _latest_stage_execution(store: RuntimeStore, *, request_id: str, stage_code: str, item_code: str):
    executions = [
        execution
        for execution in store.list_task_executions(request_id=request_id)
        if str((execution.payload or {}).get("stage_code") or "") == stage_code
        and str(execution.item_code or "") == item_code
    ]
    assert executions, f"expected stage execution {stage_code}/{item_code}"
    return executions[-1]


def _browser_handler_context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-fastmoss-browser",
        job_id="exec-fastmoss-browser",
        handler_code="fastmoss_security_browser_resolve",
        worker_type="browser_worker",
        runtime_table="task_execution",
        payload=payload,
        workflow_code=TASK_CODE,
        stage_code="fastmoss_security_browser_fallback",
        item_code="fastmoss_security_browser_resolve",
    )


def _mark_search_success(store: RuntimeStore, *, job_id: str, candidate_suffix: str = "1") -> None:
    _mark_api_job_success(
        store,
        job_id=job_id,
        summary={"candidates": 1},
        result={
            "candidates": [
                {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "rank": 1,
                    "title": f"Water bottle {candidate_suffix}",
                }
            ],
            "condition_context": {"normalized": True},
        },
    )


def _mark_api_job_success(
    store: RuntimeStore,
    *,
    job_id: str,
    summary: dict,
    result: dict,
) -> None:
    job = store.load_api_worker_job(job_id=job_id)
    stage_code = str((job.get("payload") or {}).get("stage_code") or "")
    store.update_task_request(
        request_id=str(job["request_id"]),
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=str(job["request_id"]),
        job_code=str(job["job_code"]),
    )
    assert claimed is not None and claimed["job_id"] == job_id
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=str(claimed["run_id"]),
        summary=summary,
        result=result,
    )


def _mark_api_job_fastmoss_security_fallback_required(
    store: RuntimeStore,
    *,
    job_id: str,
) -> None:
    job = store.load_api_worker_job(job_id=job_id)
    stage_code = str((job.get("payload") or {}).get("stage_code") or "")
    store.update_task_request(
        request_id=str(job["request_id"]),
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=str(job["request_id"]),
        job_code=str(job["job_code"]),
    )
    assert claimed is not None and claimed["job_id"] == job_id
    search_request = dict((job.get("payload") or {}).get("search_request") or {})
    handler_result = {
        "status": "fallback_required",
        "handler_code": "keyword_seed_import",
        "request_id": str(job["request_id"]),
        "job_id": job_id,
        "summary": {
            "search_status": "failed",
            "fallback_required": True,
            "fallback_reason": "fastmoss_search_security_verification",
        },
        "result": {
            "fallback_required": True,
            "fallback_reason": "fastmoss_search_security_verification",
            "fallback_source_job_id": job_id,
            "search_request": search_request,
            "security_context": {
                "method": "GET",
                "path": "/api/goods/V2/search",
                "response_code": "MSG_SAFE_0001",
                "data_id": "290777",
                "ext_is_login": "1",
            },
        },
        "warnings": [],
        "next_action": {"type": "browser_fallback", "payload": {}},
        "contract_revision": "product_fact_contract",
        "error": {
            "error_type": "security_verification",
            "error_code": "fastmoss_security_verification_required",
            "message": "FastMoss search security verification is required.",
            "retryable": False,
            "fallback_allowed": True,
            "fallback_reason": "fastmoss_search_security_verification",
            "details": {"response_code": "MSG_SAFE_0001"},
        },
    }
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=str(claimed["run_id"]),
        summary=handler_result["summary"],
        result={
            "handler_result": handler_result,
            **handler_result["result"],
        },
        stage="browser_fallback_required",
    )


def _mark_competitor_row_refresh_fallback_required(
    store: RuntimeStore,
    *,
    job_id: str,
) -> None:
    job = store.load_api_worker_job(job_id=job_id)
    stage_code = str((job.get("payload") or {}).get("stage_code") or "")
    payload = dict(job.get("payload") or {})
    store.update_task_request(
        request_id=str(job["request_id"]),
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=str(job["request_id"]),
        job_code=str(job["job_code"]),
    )
    assert claimed is not None and claimed["job_id"] == job_id
    browser_payload = {
        "product_identity": dict(payload.get("product_identity") or {}),
        "normalized_product_url": payload.get("normalized_product_url") or PRODUCT_URL,
        "source_record_id": payload.get("source_record_id") or "seed-row-1",
        "business_entity_key": payload.get("business_key") or f"product:{PRODUCT_ID}",
        "fallback_source_job_id": job_id,
    }
    handler_result = {
        "status": "fallback_required",
        "handler_code": "competitor_row_refresh",
        "request_id": str(job["request_id"]),
        "job_id": job_id,
        "summary": {
            "row_status": "fallback_required",
            "fallback_required": True,
            "fallback_handler": "tiktok_product_browser_fetch",
        },
        "result": {
            "source_record_id": payload.get("source_record_id") or "seed-row-1",
            "business_entity_key": payload.get("business_key") or f"product:{PRODUCT_ID}",
            "row_status": "fallback_required",
            "fallback_required": True,
            "fallback_handler": "tiktok_product_browser_fetch",
            "fallback_reason": "request_blocked",
            "browser_fallback_payload": browser_payload,
            "step_timeline": [
                {"step": "tiktok_request", "status": "fallback_required"},
                {"step": "browser_fallback", "status": "fallback_required"},
            ],
            "runtime_evidence": {"browser_fallback_used": True},
        },
        "warnings": [],
        "next_action": {"type": "browser_fallback", "payload": browser_payload},
        "contract_revision": "product_fact_contract",
        "error": {
            "error_type": "browser_fallback_required",
            "error_code": "tiktok_product_browser_fetch_required",
            "message": "browser fallback required",
            "retryable": False,
            "fallback_allowed": True,
            "fallback_reason": "request_blocked",
        },
    }
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=str(claimed["run_id"]),
        summary=handler_result["summary"],
        result={"handler_result": handler_result, **handler_result["result"]},
        stage="browser_fallback_required",
    )


def _mark_browser_execution_success(
    store: RuntimeStore,
    *,
    execution_id: str,
    summary: dict,
    result: dict,
) -> None:
    execution = store.load_task_execution(execution_id=execution_id)
    store.update_task_request(
        request_id=execution.request_id,
        status="waiting_children",
        current_stage=str((execution.payload or {}).get("stage_code") or ""),
        progress_stage=str((execution.payload or {}).get("stage_code") or ""),
    )
    claimed = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        worker_pid=123,
        lease_seconds=30.0,
        request_id=execution.request_id,
        item_codes=(execution.item_code,),
    )
    assert claimed is not None and claimed.execution_id == execution_id
    handler_result = {
        "status": "success",
        "handler_code": execution.item_code,
        "request_id": execution.request_id,
        "job_id": execution_id,
        "summary": summary,
        "result": result,
        "warnings": [],
        "next_action": {"type": "none", "payload": {}},
        "contract_revision": "product_fact_contract",
    }
    store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=str(claimed.run_id),
        summary={"handler_status": "success", **summary},
        result={"handler_result": handler_result, **result},
    )


def _advance_to_refresh_stage(store: RuntimeStore, request, workflow) -> tuple[object, dict]:
    seed_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="keyword_seed_import",
        job_code="keyword_seed_import",
    )
    _mark_api_job_success(
        store,
        job_id=str(seed_job["job_id"]),
        summary={"candidate_count": 1, "written_count": 1},
        result={
            "normalized_candidates": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "search_rank": 1,
                    "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                }
            ],
            "seed_contexts": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "search_rank": 1,
                    "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                    "source_record_id": "seed-row-1",
                    "seed_status": "success",
                    "target_record_ids": ["seed-row-1"],
                }
            ],
            "seed_write_results": [{"product_id": PRODUCT_ID, "status": "success"}],
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": ["seed-row-1"],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    seed_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_advance["next_stage"] == "dispatch_row_refresh_jobs"

    request = store.load_task_request(request_id=request.request_id)
    dispatch_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_row_refresh_jobs",
    )
    assert dispatch_advance["next_stage"] == "refresh_competitor_rows"

    row_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="refresh_competitor_rows",
        job_code="competitor_row_refresh",
    )
    return request, row_job


def _advance_from_sync_to_finalization(store: RuntimeStore, request, workflow) -> dict:
    request = store.load_task_request(request_id=request.request_id)
    sync_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="sync_media")
    assert sync_waiting["action"] == "waiting"
    media_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="sync_media",
        job_code="media_asset_sync",
    )
    _mark_api_job_success(
        store,
        job_id=str(media_job["job_id"]),
        summary={"synced": 1},
        result={"synced_assets": [{"source_url": "https://cdn.example.com/p1.jpg"}]},
    )

    request = store.load_task_request(request_id=request.request_id)
    sync_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="sync_media")
    assert sync_advance["next_stage"] == "persist_facts"

    request = store.load_task_request(request_id=request.request_id)
    persist_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_waiting["action"] == "waiting"
    fact_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="persist_facts",
        job_code="fact_bundle_upsert",
    )
    _mark_api_job_success(
        store,
        job_id=str(fact_job["job_id"]),
        summary={"upserted": 1},
        result={"upserted_entities": [PRODUCT_ID]},
    )

    request = store.load_task_request(request_id=request.request_id)
    persist_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_advance["next_stage"] == "writeback_competitor_rows"

    request = store.load_task_request(request_id=request.request_id)
    write_waiting = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="writeback_competitor_rows",
    )
    assert write_waiting["action"] == "waiting"
    write_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="writeback_competitor_rows",
        job_code="feishu_table_write",
    )
    _mark_api_job_success(
        store,
        job_id=str(write_job["job_id"]),
        summary={"written": 1},
        result={"written_count": 1, "target_record_ids": ["seed-row-1"]},
    )

    request = store.load_task_request(request_id=request.request_id)
    write_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="writeback_competitor_rows",
    )
    assert write_advance["next_stage"] == "ready_for_summary"

    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    return finalize_request(store=store, request=request, workflow=workflow)


def test_keyword_runtime_module_is_loadable_and_happy_path_finalizes(runtime_db_url: str) -> None:
    runtime = load_workflow_runtime(TASK_CODE)
    assert runtime is not None
    assert runtime.advance_stage is advance_stage
    assert runtime.finalize_request.__module__.endswith(".search_keyword_competitor_products.orchestrator")
    assert runtime.release_request_after_child_completion is release_request_after_child_completion

    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, row_job = _advance_to_refresh_stage(store, request, workflow)

    assert row_job["payload"]["source_record_id"] == "seed-row-1"
    _mark_api_job_success(
        store,
        job_id=str(row_job["job_id"]),
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "success"},
                {"step": "browser_fallback", "status": "skipped"},
                {"step": "media_sync", "status": "success"},
                {"step": "fastmoss_fetch", "status": "success"},
                {"step": "fact_db_upsert", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["action"] == "finalized"
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["candidate_total_count"] == 1
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"
    message_text = finalized["outbox"][0]["payload"]["message_text"]
    assert "关键词搜索竞品写入完成" in message_text
    assert f"关键词：{SEARCH_QUERY}" in message_text
    assert "候选：1 条" in message_text
    assert "详情成功：1 条" in message_text
    assert "1. SKU 123456789" in message_text


def test_keyword_runtime_zero_candidates_finalizes_success(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    seed_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="keyword_seed_import",
        job_code="keyword_seed_import",
    )
    _mark_api_job_success(
        store,
        job_id=str(seed_job["job_id"]),
        summary={"candidate_count": 0, "written_count": 0},
        result={
            "normalized_candidates": [],
            "seed_contexts": [],
            "seed_write_results": [],
            "written_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    seed_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_advance["next_stage"] == "dispatch_row_refresh_jobs"
    request = store.load_task_request(request_id=request.request_id)
    dispatch_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="dispatch_row_refresh_jobs")
    assert dispatch_advance["next_stage"] == "refresh_competitor_rows"
    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)

    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["summary"]["search_query"] == SEARCH_QUERY
    assert finalized["result"]["candidate_total_count"] == 0


def test_keyword_runtime_fastmoss_security_browser_fallback_retries_original_search(
    runtime_db_url: str,
) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    seed_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="keyword_seed_import",
        job_code="keyword_seed_import",
    )
    _mark_api_job_fastmoss_security_fallback_required(store, job_id=str(seed_job["job_id"]))

    request = store.load_task_request(request_id=request.request_id)
    fallback_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert fallback_advance["next_stage"] == "fastmoss_security_browser_fallback"

    request = store.load_task_request(request_id=request.request_id)
    browser_wait = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="fastmoss_security_browser_fallback",
    )
    assert browser_wait["action"] == "waiting"
    execution = _latest_stage_execution(
        store,
        request_id=request.request_id,
        stage_code="fastmoss_security_browser_fallback",
        item_code="fastmoss_security_browser_resolve",
    )
    assert execution.payload["search_request"]["search_query"] == SEARCH_QUERY
    assert execution.payload["security_context"]["response_code"] == "MSG_SAFE_0001"
    _mark_browser_execution_success(
        store,
        execution_id=execution.execution_id,
        summary={"resolved": True, "verified_path": "/api/goods/V2/search"},
        result={
            "verified_path": "/api/goods/V2/search",
            "cookie_cache": {"status": "saved", "cookie_count": 1, "has_fd_tk": True},
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    browser_done = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="fastmoss_security_browser_fallback",
    )
    assert browser_done["next_stage"] == "keyword_seed_import"

    request = store.load_task_request(request_id=request.request_id)
    retry_wait = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert retry_wait["action"] == "waiting"
    seed_jobs = [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request.request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == "keyword_seed_import"
    ]
    assert len(seed_jobs) == 2
    retry_job = seed_jobs[-1]
    assert retry_job["payload"]["fastmoss_security_browser_fallback_attempt"] == 1
    assert retry_job["dedupe_key"].endswith(":after-fastmoss-security-browser-fallback")
    _mark_api_job_success(
        store,
        job_id=str(retry_job["job_id"]),
        summary={"candidate_count": 1, "written_count": 1},
        result={
            "normalized_candidates": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "search_rank": 1,
                    "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                }
            ],
            "seed_contexts": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "source_record_id": "seed-row-1",
                    "seed_status": "success",
                }
            ],
            "seed_write_results": [{"product_id": PRODUCT_ID, "status": "success"}],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    seed_done = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_done["next_stage"] == "dispatch_row_refresh_jobs"
    assert seed_done["details"]["candidate_total_count"] == 1


def test_keyword_runtime_row_browser_fallback_resumes_before_summary(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, row_job = _advance_to_refresh_stage(store, request, workflow)

    _mark_competitor_row_refresh_fallback_required(store, job_id=str(row_job["job_id"]))

    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "browser_fallback"

    request = store.load_task_request(request_id=request.request_id)
    browser_wait = advance_stage(store=store, request=request, workflow=workflow, stage_code="browser_fallback")
    assert browser_wait["action"] == "waiting"
    execution = _latest_stage_execution(
        store,
        request_id=request.request_id,
        stage_code="browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )
    assert execution.payload["source_record_id"] == "seed-row-1"
    assert execution.payload["business_entity_key"] == f"product:{PRODUCT_ID}"

    _mark_browser_execution_success(
        store,
        execution_id=execution.execution_id,
        summary={"transport": "browser"},
        result={
            "normalized_product_result": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
                "source": "browser",
            }
        },
    )

    store.update_task_request(
        request_id=request.request_id,
        status="pending",
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    recovered = release_request_after_child_completion(store, request_id=request.request_id)
    assert recovered == [
        {
            "request_id": request.request_id,
            "stage_code": "resume_competitor_rows_after_browser_fallback",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

    request = store.load_task_request(request_id=request.request_id)
    resume_wait = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="resume_competitor_rows_after_browser_fallback",
    )
    assert resume_wait["action"] == "waiting"
    resume_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="resume_competitor_rows_after_browser_fallback",
        job_code="competitor_row_refresh",
    )
    assert resume_job["payload"]["normalized_product_result"]["source"] == "browser"
    assert resume_job["payload"]["browser_fallback_resolved"] is True

    _mark_api_job_success(
        store,
        job_id=str(resume_job["job_id"]),
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "success"},
                {"step": "browser_fallback", "status": "success"},
                {"step": "media_sync", "status": "success"},
                {"step": "fastmoss_fetch", "status": "success"},
                {"step": "fact_db_upsert", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    resume_done = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="resume_competitor_rows_after_browser_fallback",
    )
    assert resume_done["next_stage"] == "ready_for_summary"

    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["result"]["row_results"][0]["browser_status"] == "success"
    assert finalized["result"]["stage_summary"]["refresh_competitor_rows"]["statuses"]["fallback_required"] == 1


def test_keyword_runtime_browser_fallback_path_finalizes(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, row_job = _advance_to_refresh_stage(store, request, workflow)

    _mark_api_job_success(
        store,
        job_id=str(row_job["job_id"]),
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "fallback_required"},
                {"step": "browser_fallback", "status": "success"},
                {"step": "media_sync", "status": "success"},
                {"step": "fastmoss_fetch", "status": "success"},
                {"step": "fact_db_upsert", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_results"][0]["browser_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
