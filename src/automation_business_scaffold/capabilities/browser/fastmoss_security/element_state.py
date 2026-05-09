from __future__ import annotations

import time
from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.page_primitives import (
    extract_css_url as _extract_css_url,
    first_visible_locator as _first_visible_locator,
    locator_bounding_box as _locator_bounding_box,
    locator_image_resource_payload as _locator_image_resource_payload,
    page_image_resource_payload as _page_image_resource_payload,
    safe_wait_for_timeout as _safe_wait_for_timeout,
)
from automation_business_scaffold.contracts.handler.shared import first_non_empty

DEFAULT_FASTMOSS_BROWSER_TIMEOUT_MS = 45_000
DEFAULT_FASTMOSS_SLIDER_CONFIRM_MS = 2_000
DEFAULT_FASTMOSS_SLIDER_APPEAR_TIMEOUT_MS = 8_000
DEFAULT_FASTMOSS_SLIDER_SETTLE_MS = 5_000
DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS = 12_000
DEFAULT_FASTMOSS_SLIDER_REFRESH_WAIT_MS = 2_200
DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS = 36
DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS = 0.012
DEFAULT_FASTMOSS_SLIDER_ATTEMPTS = 3
DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR = "runtime/downloads/fastmoss_slider_captcha_audit"
DEFAULT_FASTMOSS_SLIDER_POLL_MS = 250

FASTMOSS_SLIDER_POPUP_SELECTORS = (
    "#tcaptcha_transform_dy",
    "#tCaptchaDyContent",
    ".tencent-captcha__transform",
    ".tencent-captcha-dy__content",
    "#captcha_container",
    "#captcha-verify-container",
    "#captcha_verify_container",
    "[id*='captcha']",
    "[class*='captcha'][class*='container']",
    "[class*='captcha'][class*='modal']",
    "[class*='secsdk-captcha']",
)
FASTMOSS_SLIDER_BACKGROUND_SELECTORS = (
    ".tencent-captcha-dy__verify-bg-img",
    "#captcha-verify-image",
    ".captcha_verify_img",
    ".captcha-verify-image",
    "[class*='captcha_verify_img']:not([class*='slide'])",
    "[class*='captcha'] img:not([class*='slide'])",
)
FASTMOSS_SLIDER_TARGET_SELECTORS = (
    ".tencent-captcha-dy__fg-item",
    ".captcha_verify_img_slide",
    ".captcha-verify-image-slide",
    "[class*='captcha_verify_img_slide']",
)
FASTMOSS_SLIDER_HANDLE_SELECTORS = (
    ".tencent-captcha-dy__slider-block",
    ".tencent-captcha-dy__verify-slider-area",
    ".secsdk-captcha-drag-icon",
    ".captcha_verify_slide--slidebar",
    ".captcha-verify-slider",
    "[class*='slider'][class*='handle']",
    "[class*='captcha'] [class*='drag']",
)
FASTMOSS_SLIDER_REFRESH_SELECTORS = (
    ".tencent-captcha-dy__footer-icon--refresh",
    "[aria-label='Try a new captcha']",
    ".secsdk_captcha_refresh",
    ".captcha_verify_refresh",
    "[class*='captcha'][class*='refresh']",
)
FASTMOSS_SLIDER_LOADING_SELECTORS = (
    ".tencent-captcha-dy__loading",
    ".tencent-captcha-dy__spinner",
    ".tcaptcha-loading",
    ".tcaptcha-spinner",
    ".captcha-loading",
    ".captcha-spinner",
    "[class*='captcha'][class*='loading']",
    "[class*='captcha'][class*='spinner']",
    "[class*='tcaptcha'][class*='loading']",
    "[class*='tcaptcha'][class*='spinner']",
    "[class*='loading']",
    "[class*='spinner']",
)

def _wait_locator_visible(locator: Any, *, timeout_ms: int) -> None:
    wait_for = getattr(locator, "wait_for", None)
    if callable(wait_for):
        wait_for(state="visible", timeout=timeout_ms)

def _fastmoss_background_css_resource(locator: Any) -> str:
    payload = _locator_image_resource_payload(locator)
    if not isinstance(payload, Mapping):
        return ""
    return _extract_css_url(first_non_empty(payload.get("backgroundImage")))


def _fastmoss_page_background_css_resource(page: Any, *, selector: str) -> str:
    payload = _page_image_resource_payload(page, selector=selector)
    if not isinstance(payload, Mapping):
        return ""
    return _extract_css_url(first_non_empty(payload.get("backgroundImage")))

def _wait_for_fastmoss_slider_loading_cleared(
    page: Any,
    *,
    selectors: Mapping[str, str],
    timeout_ms: int,
    poll_ms: int = DEFAULT_FASTMOSS_SLIDER_POLL_MS,
) -> dict[str, Any]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    elapsed_ms = 0
    last_state: dict[str, Any] = {}
    while True:
        state = _read_fastmoss_slider_readiness(page, selectors=selectors)
        last_state = {**state, "wait_elapsed_ms": elapsed_ms}
        if not state.get("visible") or not state.get("loading_visible") or elapsed_ms >= effective_timeout_ms:
            return last_state
        wait_ms = min(effective_poll_ms, effective_timeout_ms - elapsed_ms)
        if wait_ms <= 0:
            return last_state
        _safe_wait_for_timeout(page, wait_ms)
        elapsed_ms += wait_ms


def _wait_for_fastmoss_slider_ready_for_attempt(
    page: Any,
    *,
    selectors: Mapping[str, str],
    timeout_ms: int,
    poll_ms: int = DEFAULT_FASTMOSS_SLIDER_POLL_MS,
) -> dict[str, Any]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    elapsed_ms = 0
    last_state: dict[str, Any] = {}
    while True:
        state = _read_fastmoss_slider_readiness(page, selectors=selectors)
        last_state = {**state, "wait_elapsed_ms": elapsed_ms}
        if state.get("ready") or not state.get("visible") or elapsed_ms >= effective_timeout_ms:
            return last_state
        wait_ms = min(effective_poll_ms, effective_timeout_ms - elapsed_ms)
        if wait_ms <= 0:
            return last_state
        _safe_wait_for_timeout(page, wait_ms)
        elapsed_ms += wait_ms


def _read_fastmoss_slider_readiness(page: Any, *, selectors: Mapping[str, str]) -> dict[str, Any]:
    state = _read_fastmoss_slider_state(page)
    if not state.get("visible"):
        return {**state, "ready": False, "reason": "slider_not_visible"}

    background_selector = first_non_empty(selectors.get("background"), state.get("selector"))
    piece_selector = first_non_empty(selectors.get("piece"))
    handle_selector = first_non_empty(selectors.get("handle"), state.get("handle_selector"))
    popup_selector = first_non_empty(selectors.get("popup"), state.get("selector"))
    loading_visible = _fastmoss_slider_loading_visible(page, popup_selector=popup_selector)

    background_locator, background_found_selector = _first_visible_locator(
        page,
        _selector_candidates(background_selector, FASTMOSS_SLIDER_BACKGROUND_SELECTORS),
        timeout_ms=250,
    )
    piece_locator, piece_found_selector = _first_visible_locator(
        page,
        _selector_candidates(piece_selector, FASTMOSS_SLIDER_TARGET_SELECTORS),
        timeout_ms=250,
    )
    handle_locator, handle_found_selector = _first_visible_locator(
        page,
        _selector_candidates(handle_selector, FASTMOSS_SLIDER_HANDLE_SELECTORS),
        timeout_ms=250,
    )
    background_box = _locator_bounding_box(background_locator) if background_locator else {}
    piece_box = _locator_bounding_box(piece_locator) if piece_locator else {}
    handle_box = _locator_bounding_box(handle_locator) if handle_locator else {}
    background_resource = _fastmoss_background_css_resource(background_locator) if background_locator else ""
    piece_center_x = _slider_piece_center_x(background_box, piece_box)
    reset_ready = _fastmoss_slider_piece_reset_ready(background_box, piece_box)
    ready = bool(
        background_locator
        and piece_locator
        and handle_locator
        and background_resource
        and not loading_visible
        and reset_ready
    )
    if ready:
        reason = "slider_ready"
    elif loading_visible:
        reason = "slider_loading"
    elif not background_locator or not piece_locator or not handle_locator:
        reason = "missing_slider_elements"
    elif not background_resource:
        reason = "background_image_not_ready"
    elif not reset_ready:
        reason = "slider_not_reset"
    else:
        reason = "slider_not_ready"
    return {
        **state,
        "ready": ready,
        "reason": reason,
        "loading_visible": loading_visible,
        "background_selector": background_found_selector,
        "piece_selector": piece_found_selector,
        "handle_selector": handle_found_selector,
        "background_image_ready": bool(background_resource),
        "piece_reset_ready": reset_ready,
        "piece_center_x": piece_center_x,
        "background_box": background_box,
        "piece_box": piece_box,
        "handle_box": handle_box,
    }


def _fastmoss_slider_loading_visible(page: Any, *, popup_selector: str) -> bool:
    script = """
    (payload) => {
      const root = payload.popupSelector ? document.querySelector(payload.popupSelector) : document;
      if (!root) return false;
      const selectors = payload.loadingSelectors || [];
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none'
          && style.visibility !== 'hidden'
          && Number(style.opacity || '1') > 0.05
          && rect.width > 4
          && rect.height > 4;
      };
      for (const selector of selectors) {
        for (const el of root.querySelectorAll(selector)) {
          if (visible(el)) return true;
        }
      }
      return /loading|verifying/i.test(root.innerText || '');
    }
    """
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            return bool(
                evaluate(
                    script,
                    {
                        "popupSelector": popup_selector,
                        "loadingSelectors": list(FASTMOSS_SLIDER_LOADING_SELECTORS),
                    },
                )
            )
        except TypeError:
            try:
                return bool(evaluate(script))
            except Exception:
                return False
        except Exception:
            return False
    return False


def _slider_piece_center_x(background_box: Mapping[str, Any], piece_box: Mapping[str, Any]) -> float | None:
    if not background_box or not piece_box:
        return None
    return (
        float(piece_box.get("x") or 0.0)
        + (float(piece_box.get("width") or 0.0) / 2)
        - float(background_box.get("x") or 0.0)
    )


def _fastmoss_slider_piece_reset_ready(background_box: Mapping[str, Any], piece_box: Mapping[str, Any]) -> bool:
    piece_center_x = _slider_piece_center_x(background_box, piece_box)
    if piece_center_x is None:
        return False
    background_width = float(background_box.get("width") or 0.0)
    piece_width = float(piece_box.get("width") or 0.0)
    reset_threshold = max(piece_width * 1.6, background_width * 0.35)
    return piece_center_x <= reset_threshold


def _wait_for_fastmoss_slider_post_drag_state(
    page: Any,
    *,
    timeout_ms: int,
    poll_ms: int = DEFAULT_FASTMOSS_SLIDER_POLL_MS,
) -> dict[str, Any]:
    effective_timeout_ms = max(int(timeout_ms), 0)
    effective_poll_ms = max(int(poll_ms), 1)
    elapsed_ms = 0
    last_state: dict[str, Any] = {}
    while True:
        state = _read_fastmoss_slider_state(page)
        last_state = {
            **state,
            "wait_elapsed_ms": elapsed_ms,
        }
        if not state.get("visible") or state.get("success") or elapsed_ms >= effective_timeout_ms:
            return last_state
        wait_ms = min(effective_poll_ms, effective_timeout_ms - elapsed_ms)
        if wait_ms <= 0:
            return last_state
        _safe_wait_for_timeout(page, wait_ms)
        elapsed_ms += wait_ms


def _confirm_fastmoss_slider_cleared(page: Any, *, confirm_ms: int) -> dict[str, Any]:
    wait_ms = max(int(confirm_ms), 1)
    _safe_wait_for_timeout(page, wait_ms)
    confirmed_state = _read_fastmoss_slider_state(page)
    return {
        "confirmation_wait_ms": wait_ms,
        "confirmation_popup_still_visible": bool(confirmed_state.get("visible")),
    }



def _wait_for_fastmoss_slider_state(page: Any, *, timeout_ms: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    last_state: dict[str, Any] = {}
    while True:
        last_state = _read_fastmoss_slider_state(page)
        if last_state.get("visible"):
            return last_state
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return last_state
        _safe_wait_for_timeout(page, min(500, remaining_ms))


def _wait_for_fastmoss_slider_elements(
    page: Any,
    *,
    timeout_ms: int,
    selector_overrides: Mapping[str, str] | None = None,
) -> tuple[Any | None, str, Any | None, str, Any | None, str]:
    overrides = {str(key): str(value) for key, value in dict(selector_overrides or {}).items() if str(value).strip()}
    background_selectors = _selector_candidates(overrides.get("background"), FASTMOSS_SLIDER_BACKGROUND_SELECTORS)
    target_selectors = _selector_candidates(overrides.get("piece"), FASTMOSS_SLIDER_TARGET_SELECTORS)
    handle_selectors = _selector_candidates(overrides.get("handle"), FASTMOSS_SLIDER_HANDLE_SELECTORS)
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    last: tuple[Any | None, str, Any | None, str, Any | None, str] = (None, "", None, "", None, "")
    while True:
        background_locator, background_selector = _first_visible_locator(page, background_selectors)
        target_locator, target_selector = _first_visible_locator(page, target_selectors)
        handle_locator, handle_selector = _first_visible_locator(page, handle_selectors)
        last = (
            background_locator,
            background_selector,
            target_locator,
            target_selector,
            handle_locator,
            handle_selector,
        )
        if background_locator and target_locator and handle_locator:
            return last
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return last
        _safe_wait_for_timeout(page, min(500, remaining_ms))


def _selector_candidates(primary: str | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    normalized = first_non_empty(primary)
    if not normalized:
        return fallback
    return (normalized, *tuple(selector for selector in fallback if selector != normalized))


def _read_fastmoss_slider_state(page: Any) -> dict[str, Any]:
    popup_locator, popup_selector = _first_visible_locator(page, FASTMOSS_SLIDER_POPUP_SELECTORS, timeout_ms=250)
    if popup_locator:
        return {"visible": True, "selector": popup_selector}
    background_locator, background_selector = _first_visible_locator(page, FASTMOSS_SLIDER_BACKGROUND_SELECTORS, timeout_ms=250)
    handle_locator, handle_selector = _first_visible_locator(page, FASTMOSS_SLIDER_HANDLE_SELECTORS, timeout_ms=250)
    return {
        "visible": bool(background_locator and handle_locator),
        "selector": background_selector if background_locator else "",
        "handle_selector": handle_selector if handle_locator else "",
    }
