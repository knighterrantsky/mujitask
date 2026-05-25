from __future__ import annotations

import base64
import hashlib
import io
import re
import time
from typing import Any, Mapping
from urllib.parse import urljoin

from PIL import Image
import requests

from automation_business_scaffold.capabilities.browser.page_primitives import (
    click_first_visible_locator as _click_first_visible_locator,
    drag_slider_handle as _drag_slider_handle,
    locator_bounding_box as _locator_bounding_box,
    locator_image_bytes as _locator_image_bytes,
    locator_screenshot_bytes as _locator_screenshot_bytes,
    safe_wait_for_timeout as _safe_wait_for_timeout,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.coordinate_mapping import (
    _build_fastmoss_mixed_slider_mapping,
    _calculate_slider_drag_distance,
    _image_size,
    _select_fastmoss_shape_anchor_slider_result,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.diagnostics import (
    DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR,
    _capture_page_screenshot_bytes,
    _page_title,
    _persist_fastmoss_slider_artifacts_payload,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.element_state import (
    DEFAULT_FASTMOSS_SLIDER_APPEAR_TIMEOUT_MS,
    DEFAULT_FASTMOSS_SLIDER_CONFIRM_MS,
    DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS,
    DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS,
    DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS,
    DEFAULT_FASTMOSS_SLIDER_POLL_MS,
    DEFAULT_FASTMOSS_SLIDER_REFRESH_WAIT_MS,
    DEFAULT_FASTMOSS_SLIDER_SETTLE_MS,
    FASTMOSS_SLIDER_BACKGROUND_SELECTORS,
    FASTMOSS_SLIDER_HANDLE_SELECTORS,
    FASTMOSS_SLIDER_REFRESH_SELECTORS,
    FASTMOSS_SLIDER_TARGET_SELECTORS,
    _fastmoss_background_css_resource,
    _fastmoss_page_background_css_resource,
    _first_visible_locator,
    _confirm_fastmoss_slider_cleared,
    _read_fastmoss_slider_state,
    _selector_candidates,
    _wait_for_fastmoss_slider_elements,
    _wait_for_fastmoss_slider_loading_cleared,
    _wait_for_fastmoss_slider_post_drag_state,
    _wait_for_fastmoss_slider_ready_for_attempt,
    _wait_for_fastmoss_slider_state,
    _wait_locator_visible,
)
from automation_business_scaffold.contracts.handler.shared import coerce_bool, coerce_mapping, compact_dict, first_non_empty

DEFAULT_FASTMOSS_SLIDER_ATTEMPTS = 3

def _try_resolve_fastmoss_slider_security_check(
    page: Any,
    *,
    automation_page: Any | None = None,
    raw_page: Any | None = None,
    search_url: str,
    max_attempts: int,
    appear_timeout_ms: int,
    settle_ms: int,
    confirm_ms: int,
    audit_dir: str = DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR,
    provider_config: Mapping[str, Any] | None = None,
    resolver_config: Mapping[str, Any] | None = None,
    selectors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if max_attempts <= 0:
        return {"attempted": False, "resolved": False, "reason": "disabled", "attempts": []}
    state = _wait_for_fastmoss_slider_state(page, timeout_ms=appear_timeout_ms)
    if not state.get("visible"):
        return {
            "attempted": False,
            "resolved": True,
            "reason": "slider_not_visible",
            "appear_timeout_ms": max(appear_timeout_ms, 0),
            "attempts": [],
        }
    (
        background_locator,
        background_selector,
        target_locator,
        target_selector,
        handle_locator,
        handle_selector,
    ) = _wait_for_fastmoss_slider_elements(
        page,
        timeout_ms=max(DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS, appear_timeout_ms),
        selector_overrides=selectors,
    )
    if not (background_locator and target_locator and handle_locator):
        return {
            "attempted": True,
            "resolved": False,
            "reason": "missing_slider_elements_after_wait",
            "image_timeout_ms": max(DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS, appear_timeout_ms),
            "slider_state": state,
            "selectors": compact_dict(
                {
                    "background": background_selector,
                    "piece": target_selector,
                    "handle": handle_selector,
                }
            ),
            "attempts": [],
        }
    state = {
        **state,
        "background_selector": background_selector,
        "piece_selector": target_selector,
        "handle_selector": handle_selector,
    }
    try:
        return _resolve_fastmoss_slider_with_framework_captcha(
            automation_page or raw_page or page,
            page=page,
            initial_state=state,
            search_url=search_url,
            max_attempts=max_attempts,
            settle_ms=settle_ms,
            confirm_ms=confirm_ms,
            audit_dir=audit_dir,
            provider_config=provider_config,
            resolver_config=resolver_config,
            selectors=selectors,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "resolved": False,
            "reason": "framework_slider_resolver_failed",
            "error": str(exc),
            "attempts": [],
        }


def _resolve_fastmoss_slider_with_framework_captcha(
    automation_page: Any,
    *,
    page: Any,
    initial_state: Mapping[str, Any],
    search_url: str,
    max_attempts: int,
    settle_ms: int,
    confirm_ms: int,
    audit_dir: str,
    provider_config: Mapping[str, Any] | None,
    resolver_config: Mapping[str, Any] | None,
    selectors: Mapping[str, str] | None,
) -> dict[str, Any]:
    del automation_page
    selector_payload = _resolve_fastmoss_slider_selector_payload(
        page,
        initial_state=initial_state,
        overrides=selectors,
    )
    provider = _build_slider_captcha_provider(provider_config)
    resolver_overrides = dict(resolver_config or {})
    post_drag_poll_ms = max(int(resolver_overrides.pop("after_drag_wait_ms", settle_ms)), 1)
    refresh_wait_ms = max(int(resolver_overrides.pop("refresh_wait_ms", DEFAULT_FASTMOSS_SLIDER_REFRESH_WAIT_MS)), 0)
    image_timeout_ms = max(int(resolver_overrides.pop("image_timeout_ms", DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS)), 1)
    resolver_overrides.pop("success_timeout_ms", None)
    resolver_overrides.pop("max_attempts", None)
    mode = first_non_empty(resolver_overrides.pop("mode", None), "match")
    simple_target = coerce_bool(resolver_overrides.pop("simple_target", None), default=False)
    piece_image_source = first_non_empty(resolver_overrides.pop("piece_image_source", None), "css_visible_crop")
    drag_scale = _float_value(resolver_overrides.pop("drag_scale", None), 1.0)
    drag_offset_x = _float_value(resolver_overrides.pop("drag_offset_x", None), 0.0)
    drag_steps = _positive_int(resolver_overrides.pop("drag_steps", None), DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS)
    drag_step_delay_seconds = _non_negative_float(
        resolver_overrides.pop("drag_step_delay_seconds", None),
        DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS,
    )
    config_payload = {
        "max_attempts": 1,
        "image_timeout_ms": image_timeout_ms,
        "refresh_wait_ms": refresh_wait_ms,
        "after_drag_wait_ms": 0,
        "success_timeout_ms": 0,
        "drag_steps": drag_steps,
        "drag_step_delay_seconds": drag_step_delay_seconds,
        "drag_scale": drag_scale,
        "drag_offset_x": drag_offset_x,
        "mode": mode,
        "simple_target": simple_target,
        "piece_image_source": piece_image_source,
        "capture_page_screenshots": True,
        "capture_image_artifacts": True,
        **resolver_overrides,
    }
    artifact_refs: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    raw_attempts: list[dict[str, Any]] = []
    audit_payload: dict[str, Any] = {
        "config": dict(config_payload),
        "selectors": dict(selector_payload),
        "success": False,
        "attempts": raw_attempts,
    }
    reason = "slider_popup_still_visible"
    resolved = False
    confirmation_wait_ms = max(int(confirm_ms), 1)
    for attempt_index in range(1, max(int(max_attempts), 1) + 1):
        pre_retry_state: dict[str, Any] = {}
        if attempt_index > 1:
            pre_retry_state = _wait_for_fastmoss_slider_loading_cleared(
                page,
                selectors=selector_payload,
                timeout_ms=image_timeout_ms,
            )
            if pre_retry_state.get("loading_visible"):
                attempts.append(
                    {
                        "attempt": attempt_index,
                        "reason": "slider_loading_not_finished_before_retry",
                        "pre_retry_state": pre_retry_state,
                    }
                )
                reason = "slider_loading_not_finished_before_retry"
                break
            _click_first_visible_locator(page, _selector_candidates(str(selector_payload.get("refresh") or ""), FASTMOSS_SLIDER_REFRESH_SELECTORS))
            if refresh_wait_ms:
                _safe_wait_for_timeout(page, refresh_wait_ms)
        current_audit = _resolve_one_fastmoss_mixed_slider_attempt(
            page,
            provider=provider,
            selectors=selector_payload,
            config=config_payload,
            attempt_index=attempt_index,
        )
        artifact_refs.extend(
            _persist_fastmoss_slider_artifacts_payload(
                current_audit,
                audit_dir=audit_dir,
                search_url=f"{search_url}#attempt-{attempt_index}",
            )
        )
        current_raw_attempts = coerce_mapping(current_audit.get("state_dump")).get("attempts")
        for raw_attempt in current_raw_attempts if isinstance(current_raw_attempts, list) else []:
            if isinstance(raw_attempt, Mapping):
                item = dict(raw_attempt)
                item["attempt_index"] = attempt_index
                raw_attempts.append(item)
        current_records = _framework_slider_attempts_from_audit(
            {"attempts": [raw_attempts[-1]]} if raw_attempts else current_audit,
            post_drag_verify_wait_ms=post_drag_poll_ms,
            confirmation_wait_ms=0,
            confirmation_popup_still_visible=None,
        )
        record = current_records[-1] if current_records else {"attempt": attempt_index}
        record["attempt"] = attempt_index
        if pre_retry_state:
            record["pre_retry_state"] = pre_retry_state
        state = _wait_for_fastmoss_slider_post_drag_state(page, timeout_ms=post_drag_poll_ms)
        record["post_drag_verify_wait_ms"] = post_drag_poll_ms
        record["post_drag_wait_elapsed_ms"] = state.get("wait_elapsed_ms")
        record["popup_still_visible"] = bool(state.get("visible"))
        if not state.get("visible") or state.get("success"):
            confirmed_state = _confirm_fastmoss_slider_cleared(page, confirm_ms=confirmation_wait_ms)
            record.update(confirmed_state)
            if confirmed_state.get("confirmation_popup_still_visible"):
                record["reason"] = "slider_reappeared_after_confirmation_wait"
                attempts.append(record)
                reason = "slider_reappeared_after_confirmation_wait"
                continue
            record["reason"] = "slider_cleared"
            attempts.append(record)
            resolved = True
            reason = "slider_cleared"
            break
        record["reason"] = "slider_popup_still_visible"
        attempts.append(record)
        reason = "slider_popup_still_visible"
    audit_payload["success"] = resolved
    return {
        "attempted": True,
        "resolved": resolved,
        "reason": reason,
        "search_url": search_url,
        "attempts": attempts,
        "framework_resolver": "FastMossMixedCssSliderResolver",
        "post_drag_verify_wait_ms": post_drag_poll_ms,
        "confirmation_wait_ms": confirmation_wait_ms,
        "drag_profile": {
            "steps": int(drag_steps),
            "step_delay_seconds": float(drag_step_delay_seconds),
        },
        "audit": audit_payload,
        "artifact_refs": artifact_refs,
    }


def _resolve_one_fastmoss_mixed_slider_attempt(
    page: Any,
    *,
    provider: Any,
    selectors: Mapping[str, str],
    config: Mapping[str, Any],
    attempt_index: int,
) -> dict[str, Any]:
    background_selector = first_non_empty(selectors.get("background"))
    piece_selector = first_non_empty(selectors.get("piece"))
    handle_selector = first_non_empty(selectors.get("handle"))
    before_key = f"slider_attempt_{attempt_index}_before_screenshot"
    target_position_key = f"slider_attempt_{attempt_index}_target_position_screenshot"
    after_key = f"slider_attempt_{attempt_index}_after_screenshot"
    background_key = f"slider_attempt_{attempt_index}_background_image"
    piece_key = f"slider_attempt_{attempt_index}_piece_image"
    raw_piece_key = f"slider_attempt_{attempt_index}_raw_piece_image"
    rendered_piece_key = f"slider_attempt_{attempt_index}_rendered_piece_image"
    extra: dict[str, Any] = {}
    raw_attempt: dict[str, Any] = {
        "attempt_index": attempt_index,
        "match_method": "fastmoss_mixed_css_slider_resolver",
        "mode": first_non_empty(config.get("mode"), "match"),
        "simple_target": coerce_bool(config.get("simple_target"), default=False),
    }
    before_screenshot = _capture_page_screenshot_bytes(page)
    if before_screenshot:
        extra[before_key] = before_screenshot
        raw_attempt["before_screenshot_key"] = before_key
    try:
        ready_state = _wait_for_fastmoss_slider_ready_for_attempt(
            page,
            selectors=selectors,
            timeout_ms=_positive_int(config.get("image_timeout_ms"), DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS),
        )
        raw_attempt["ready_state"] = ready_state
        if not ready_state.get("ready"):
            raise RuntimeError(first_non_empty(ready_state.get("reason"), "FastMoss slider is not ready for matching."))
        background_locator = page.locator(background_selector).first
        piece_locator = page.locator(piece_selector).first
        handle_locator = page.locator(handle_selector).first
        _wait_locator_visible(background_locator, timeout_ms=_positive_int(config.get("image_timeout_ms"), DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS))
        _wait_locator_visible(piece_locator, timeout_ms=_positive_int(config.get("image_timeout_ms"), DEFAULT_FASTMOSS_SLIDER_IMAGE_TIMEOUT_MS))
        background_image = _fastmoss_background_css_image_bytes(background_locator, page=page, selector=background_selector)
        background_box = _locator_bounding_box(background_locator)
        piece_box = _locator_bounding_box(piece_locator)
        handle_box = _locator_bounding_box(handle_locator)
        rendered_piece_image = _locator_screenshot_bytes(piece_locator)
        piece_source_mode = first_non_empty(config.get("piece_image_source"), "css_visible_crop")
        css_piece_payload = (
            _fastmoss_piece_css_visible_crop_image_payload(
                piece_locator,
                page=page,
                selector=piece_selector,
                piece_box=piece_box,
            )
            if piece_source_mode != "locator_screenshot"
            else {}
        )
        piece_image = css_piece_payload.get("crop_image") if isinstance(css_piece_payload.get("crop_image"), bytes) else b""
        piece_source = "css_background_visible_crop" if piece_image else "locator_screenshot"
        if not piece_image:
            piece_image = rendered_piece_image
        if not (background_image and piece_image and background_box and piece_box and handle_box):
            raise RuntimeError("FastMoss slider artifacts are incomplete.")
        background_width, background_height = _image_size(background_image)
        piece_width, piece_height = _image_size(piece_image)
        extra[background_key] = background_image
        extra[piece_key] = piece_image
        raw_piece_image = css_piece_payload.get("raw_image") if isinstance(css_piece_payload.get("raw_image"), bytes) else b""
        if raw_piece_image:
            extra[raw_piece_key] = raw_piece_image
        if rendered_piece_image:
            extra[rendered_piece_key] = rendered_piece_image
        raw_attempt["background"] = {
            "role": "background",
            "selector": background_selector,
            "source": "css_background_image",
            "image_width": background_width,
            "image_height": background_height,
            "rendered_box": background_box,
            "artifact_key": background_key,
            "sha256": hashlib.sha256(background_image).hexdigest(),
        }
        raw_attempt["piece"] = {
            "role": "piece",
            "selector": piece_selector,
            "source": piece_source,
            "image_width": piece_width,
            "image_height": piece_height,
            "rendered_box": piece_box,
            "artifact_key": piece_key,
            "sha256": hashlib.sha256(piece_image).hexdigest(),
            "raw_artifact_key": raw_piece_key if raw_piece_image else "",
            "rendered_artifact_key": rendered_piece_key if rendered_piece_image else "",
            "css_background_crop": coerce_mapping(css_piece_payload.get("metadata")),
            "fallback_source": "locator_screenshot" if piece_source != "css_background_visible_crop" else "",
        }
        if raw_attempt["mode"] == "comparison":
            slider_result = provider.compare_slider(piece_image, background_image)
        else:
            slider_result = provider.match_slider(
                piece_image,
                background_image,
                simple_target=bool(raw_attempt["simple_target"]),
            )
        slider_result = _select_fastmoss_shape_anchor_slider_result(
            slider_result,
            background_image=background_image,
            piece_image=piece_image,
            background_box=background_box,
            piece_box=piece_box,
        )
        mapping = _build_fastmoss_mixed_slider_mapping(
            page,
            slider_result=slider_result,
            background_box=background_box,
            background_image_size=(background_width, background_height),
            piece_image_size=(piece_width, piece_height),
            piece_box=piece_box,
            handle_box=handle_box,
            drag_scale=_float_value(config.get("drag_scale"), 1.0),
            drag_offset_x=_float_value(config.get("drag_offset_x"), 0.0),
        )
        target_position_screenshot = _drag_fastmoss_slider_handle_with_target_capture(
            page,
            mapping=mapping,
            steps=_positive_int(config.get("drag_steps"), DEFAULT_FASTMOSS_SLIDER_DRAG_STEPS),
            step_delay_seconds=_non_negative_float(
                config.get("drag_step_delay_seconds"),
                DEFAULT_FASTMOSS_SLIDER_DRAG_STEP_DELAY_SECONDS,
            ),
        )
        if target_position_screenshot:
            extra[target_position_key] = target_position_screenshot
            raw_attempt["target_position_screenshot_key"] = target_position_key
        raw_attempt["slider_result"] = {
            "target_x": getattr(slider_result, "target_x", None),
            "target_y": getattr(slider_result, "target_y", None),
            "confidence": getattr(slider_result, "confidence", None),
            "raw": getattr(slider_result, "raw", None),
        }
        raw_attempt["mapping"] = mapping
        raw_attempt["success"] = False
    except Exception as exc:  # noqa: BLE001
        raw_attempt["success"] = False
        raw_attempt["error"] = f"{type(exc).__name__}: {exc}"

    after_screenshot = _capture_page_screenshot_bytes(page)
    if after_screenshot:
        extra[after_key] = after_screenshot
        raw_attempt["after_screenshot_key"] = after_key
    audit_payload = {
        "page_url": first_non_empty(getattr(page, "url", "")),
        "page_title": _page_title(page),
        "selectors": dict(selectors),
        "config": dict(config),
        "success": False,
        "attempts": [raw_attempt],
    }
    extra["slider_captcha_audit"] = audit_payload
    return {"state_dump": audit_payload, "extra": extra}

def _fastmoss_background_css_image_bytes(locator: Any, *, page: Any, selector: str) -> bytes:
    resource = _fastmoss_background_css_resource(locator)
    if not resource and page is not None and selector:
        resource = _fastmoss_page_background_css_resource(page, selector=selector)
    return _load_fastmoss_browser_image_resource(resource, page=page)


def _fastmoss_piece_css_visible_crop_image_payload(
    locator: Any,
    *,
    page: Any,
    selector: str,
    piece_box: Mapping[str, float],
) -> dict[str, Any]:
    css_payload = _fastmoss_css_background_payload(locator, page=page, selector=selector)
    resource = first_non_empty(css_payload.get("resource"))
    raw_image = _load_fastmoss_browser_image_resource(resource, page=page)
    if not raw_image:
        return {
            "metadata": {
                **css_payload,
                "status": "raw_foreground_image_unavailable",
            }
        }
    crop_image, crop_metadata = _crop_fastmoss_css_visible_image(
        raw_image,
        background_size=first_non_empty(css_payload.get("backgroundSize")),
        background_position=first_non_empty(css_payload.get("backgroundPosition")),
        element_width=float(piece_box.get("width") or 0.0),
        element_height=float(piece_box.get("height") or 0.0),
    )
    return {
        "raw_image": raw_image,
        "crop_image": crop_image,
        "metadata": {
            **css_payload,
            **crop_metadata,
            "raw_sha256": hashlib.sha256(raw_image).hexdigest(),
            **({"crop_sha256": hashlib.sha256(crop_image).hexdigest()} if crop_image else {}),
        },
    }


def _fastmoss_css_background_payload(locator: Any, *, page: Any, selector: str) -> dict[str, Any]:
    payload = _locator_css_background_payload(locator)
    if not payload and page is not None and selector:
        payload = _page_css_background_payload(page, selector=selector)
    background_image = first_non_empty(payload.get("backgroundImage"))
    return {
        **payload,
        "resource": _extract_css_url(background_image),
    }


def _locator_css_background_payload(locator: Any) -> dict[str, Any]:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return {}
    try:
        payload = evaluate(_FASTMOSS_CSS_BACKGROUND_PAYLOAD_SCRIPT)
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _page_css_background_payload(page: Any, *, selector: str) -> dict[str, Any]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate) or not selector:
        return {}
    try:
        payload = evaluate(
            """
            ([selector, script]) => {
                const element = document.querySelector(selector);
                if (!element) return {};
                return Function("element", `return (${script})(element);`)(element);
            }
            """,
            [selector, _FASTMOSS_CSS_BACKGROUND_PAYLOAD_SCRIPT],
        )
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


_FASTMOSS_CSS_BACKGROUND_PAYLOAD_SCRIPT = """
(element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return {
        backgroundImage: style && style.backgroundImage ? style.backgroundImage : "",
        backgroundSize: style && style.backgroundSize ? style.backgroundSize : "",
        backgroundPosition: style && style.backgroundPosition ? style.backgroundPosition : "",
        backgroundRepeat: style && style.backgroundRepeat ? style.backgroundRepeat : "",
        rect: {
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            scale: window.devicePixelRatio || 1,
        },
    };
}
"""


def _crop_fastmoss_css_visible_image(
    image_bytes: bytes,
    *,
    background_size: str,
    background_position: str,
    element_width: float,
    element_height: float,
) -> tuple[bytes, dict[str, Any]]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            source = image.convert("RGBA")
            raw_width, raw_height = source.size
            scaled_width, scaled_height = _resolve_fastmoss_css_background_size(
                background_size,
                raw_size=(raw_width, raw_height),
            )
            position_x, position_y = _resolve_fastmoss_css_background_position(background_position)
            if scaled_width <= 0 or scaled_height <= 0 or element_width <= 0 or element_height <= 0:
                return b"", {
                    "status": "invalid_css_crop_geometry",
                    "raw_image_width": raw_width,
                    "raw_image_height": raw_height,
                    "background_size": background_size,
                    "background_position": background_position,
                }
            scale_x = scaled_width / float(raw_width)
            scale_y = scaled_height / float(raw_height)
            left = (-position_x) / scale_x
            top = (-position_y) / scale_y
            right = (-position_x + element_width) / scale_x
            bottom = (-position_y + element_height) / scale_y
            crop_box = (
                max(0, min(raw_width, round(left))),
                max(0, min(raw_height, round(top))),
                max(0, min(raw_width, round(right))),
                max(0, min(raw_height, round(bottom))),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                return b"", {
                    "status": "empty_css_crop_box",
                    "raw_image_width": raw_width,
                    "raw_image_height": raw_height,
                    "crop_box": crop_box,
                    "unclamped_crop_box": (left, top, right, bottom),
                }
            cropped = source.crop(crop_box)
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            return output.getvalue(), {
                "status": "success",
                "raw_image_width": raw_width,
                "raw_image_height": raw_height,
                "scaled_background_width": scaled_width,
                "scaled_background_height": scaled_height,
                "background_position_x": position_x,
                "background_position_y": position_y,
                "element_width": element_width,
                "element_height": element_height,
                "crop_box": crop_box,
                "unclamped_crop_box": (left, top, right, bottom),
                "crop_width": cropped.width,
                "crop_height": cropped.height,
            }
    except Exception as exc:  # noqa: BLE001
        return b"", {"status": "css_crop_failed", "error": str(exc)}


def _resolve_fastmoss_css_background_size(value: str, *, raw_size: tuple[int, int]) -> tuple[float, float]:
    raw_width, raw_height = raw_size
    numbers = _css_numeric_values(value)
    text = str(value or "").strip()
    if len(numbers) >= 2:
        if "%" in text:
            return raw_width * numbers[0] / 100.0, raw_height * numbers[1] / 100.0
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        if "%" in text:
            scale = numbers[0] / 100.0
            return raw_width * scale, raw_height * scale
        scaled_width = numbers[0]
        scaled_height = scaled_width * raw_height / max(float(raw_width), 1.0)
        return scaled_width, scaled_height
    return float(raw_width), float(raw_height)


def _resolve_fastmoss_css_background_position(value: str) -> tuple[float, float]:
    numbers = _css_numeric_values(value)
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return numbers[0], 0.0
    return 0.0, 0.0


def _css_numeric_values(value: str) -> list[float]:
    values: list[float] = []
    for item in re.findall(r"-?\d+(?:\.\d+)?", str(value or "")):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def _extract_css_url(value: str) -> str:
    matched = re.search(r"url\((['\"]?)(.*?)\1\)", str(value or "").strip())
    return matched.group(2) if matched else ""

def _load_fastmoss_browser_image_resource(resource: str, *, page: Any) -> bytes:
    source = first_non_empty(resource)
    if not source:
        return b""
    if source.startswith("data:image/"):
        try:
            _prefix, encoded = source.split(",", 1)
            return base64.b64decode(encoded)
        except Exception:
            return b""
    absolute_url = urljoin(first_non_empty(getattr(page, "url", ""), "https://www.fastmoss.com"), source)
    try:
        headers = {
            "User-Agent": _browser_user_agent(page),
            "Referer": first_non_empty(getattr(page, "url", ""), "https://www.fastmoss.com"),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        response = requests.get(
            absolute_url,
            headers=headers,
            cookies=_browser_cookie_map(page, absolute_url),
            timeout=10,
        )
        if response.status_code == 200:
            return bytes(response.content)
    except Exception:
        return b""
    return b""


def _browser_user_agent(page: Any) -> str:
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            return first_non_empty(evaluate("() => navigator.userAgent"), "Mozilla/5.0")
        except Exception:
            return "Mozilla/5.0"
    return "Mozilla/5.0"


def _browser_cookie_map(page: Any, url: str) -> dict[str, str]:
    context = getattr(page, "context", None)
    cookies = getattr(context, "cookies", None)
    if not callable(cookies):
        return {}
    try:
        records = cookies(url)
    except TypeError:
        records = cookies()
    except Exception:
        return {}
    return {
        str(record.get("name")): str(record.get("value"))
        for record in records or []
        if isinstance(record, Mapping) and first_non_empty(record.get("name"))
    }



def _drag_fastmoss_slider_handle_with_target_capture(
    page: Any,
    *,
    mapping: Mapping[str, Any],
    steps: int,
    step_delay_seconds: float,
) -> bytes:
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        raise RuntimeError("FastMoss slider captcha requires page mouse support")
    start_x = float(mapping.get("handle_start_x") or 0.0)
    start_y = float(mapping.get("handle_start_y") or 0.0)
    distance = float(mapping.get("drag_distance") or 0.0)
    mouse.move(start_x, start_y)
    _safe_wait_for_timeout(page, 160)
    mouse.down()
    _safe_wait_for_timeout(page, 120)
    effective_steps = max(1, int(steps))
    for step in range(1, effective_steps + 1):
        progress = step / effective_steps
        eased = 1 - ((1 - progress) ** 2.7)
        y_offset = 0.8 if step % 3 == 0 else -0.5 if step % 3 == 1 else 0.15
        mouse.move(start_x + (distance * eased), start_y + y_offset)
        if step_delay_seconds:
            time.sleep(step_delay_seconds)
    overshoot = 1.5 if distance >= 0 else -1.5
    mouse.move(start_x + distance + overshoot, start_y + 0.4)
    _safe_wait_for_timeout(page, 90)
    mouse.move(start_x + distance, start_y)
    _safe_wait_for_timeout(page, 120)
    target_position_screenshot = _capture_page_screenshot_bytes(page)
    mouse.up()
    return target_position_screenshot



def _resolve_fastmoss_slider_selector_payload(
    page: Any,
    *,
    initial_state: Mapping[str, Any],
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    override_payload = {str(key): str(value) for key, value in dict(overrides or {}).items() if str(value).strip()}
    background_selector = first_non_empty(override_payload.get("background"), initial_state.get("background_selector"))
    target_selector = first_non_empty(override_payload.get("piece"), initial_state.get("piece_selector"))
    handle_selector = first_non_empty(override_payload.get("handle"), initial_state.get("handle_selector"))
    refresh_selector = first_non_empty(override_payload.get("refresh"))
    if not background_selector:
        background_locator, background_selector = _first_visible_locator(page, FASTMOSS_SLIDER_BACKGROUND_SELECTORS)
        del background_locator
    if not target_selector:
        target_locator, target_selector = _first_visible_locator(page, FASTMOSS_SLIDER_TARGET_SELECTORS)
        del target_locator
    if not handle_selector:
        handle_locator, handle_selector = _first_visible_locator(page, FASTMOSS_SLIDER_HANDLE_SELECTORS)
        del handle_locator
    if not refresh_selector:
        refresh_locator, refresh_selector = _first_visible_locator(page, FASTMOSS_SLIDER_REFRESH_SELECTORS, timeout_ms=250)
        del refresh_locator
    payload = {
        "popup": first_non_empty(initial_state.get("selector"), "#captcha-verify-container"),
        "background": background_selector,
        "piece": target_selector,
        "handle": handle_selector,
        "refresh": refresh_selector,
        **override_payload,
    }
    missing = [key for key in ("background", "piece", "handle") if not first_non_empty(payload.get(key))]
    if missing:
        raise RuntimeError(f"FastMoss slider selectors missing: {', '.join(missing)}")
    return {key: value for key, value in payload.items() if first_non_empty(value)}


def _framework_slider_attempts_from_audit(
    audit_payload: Mapping[str, Any],
    *,
    post_drag_verify_wait_ms: int,
    confirmation_wait_ms: int,
    confirmation_popup_still_visible: bool | None,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    raw_attempts = audit_payload.get("attempts")
    for item in raw_attempts if isinstance(raw_attempts, list) else []:
        attempt = item if isinstance(item, Mapping) else {}
        mapping = attempt.get("mapping") if isinstance(attempt.get("mapping"), Mapping) else {}
        slider_result = attempt.get("slider_result") if isinstance(attempt.get("slider_result"), Mapping) else {}
        background = attempt.get("background") if isinstance(attempt.get("background"), Mapping) else {}
        piece = attempt.get("piece") if isinstance(attempt.get("piece"), Mapping) else {}
        record = {
            "attempt": attempt.get("attempt_index"),
            "reason": "" if attempt.get("success") else first_non_empty(attempt.get("error"), "slider_attempt_failed"),
            "match_method": first_non_empty(attempt.get("match_method"), "framework_slider_resolver"),
            "mode": attempt.get("mode"),
            "simple_target": attempt.get("simple_target"),
            "target_x": slider_result.get("target_x"),
            "target_y": slider_result.get("target_y"),
            "confidence": slider_result.get("confidence"),
            "raw_result": slider_result.get("raw"),
            "coordinate_mapping": mapping,
            "drag_distance": mapping.get("drag_distance"),
            "piece_source": piece.get("source"),
            "piece_css_background_crop": piece.get("css_background_crop"),
            "ready_state": attempt.get("ready_state") if isinstance(attempt.get("ready_state"), Mapping) else {},
            "post_drag_verify_wait_ms": post_drag_verify_wait_ms,
            "popup_still_visible": attempt.get("popup_still_visible"),
            "selector_success": attempt.get("selector_success"),
            "artifact_keys": {
                "background": background.get("artifact_key"),
                "piece": piece.get("artifact_key"),
                "raw_piece": piece.get("raw_artifact_key"),
                "rendered_piece": piece.get("rendered_artifact_key"),
                "before_screenshot": attempt.get("before_screenshot_key"),
                "target_position_screenshot": attempt.get("target_position_screenshot_key"),
                "after_screenshot": attempt.get("after_screenshot_key"),
            },
        }
        if attempt.get("success") and confirmation_wait_ms:
            record["confirmation_wait_ms"] = confirmation_wait_ms
            record["confirmation_popup_still_visible"] = bool(confirmation_popup_still_visible)
            if confirmation_popup_still_visible:
                record["reason"] = "slider_reappeared_after_confirmation_wait"
        attempts.append(record)
    return attempts



def _legacy_resolve_fastmoss_slider_security_check(
    page: Any,
    *,
    raw_page: Any | None = None,
    search_url: str,
    max_attempts: int,
    settle_ms: int,
    confirm_ms: int,
) -> dict[str, Any]:
    try:
        captcha_provider = _build_slider_captcha_provider()
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "resolved": False,
            "reason": "captcha_provider_unavailable",
            "error": str(exc),
            "attempts": [],
        }

    attempts: list[dict[str, Any]] = []
    for attempt_index in range(1, max_attempts + 1):
        attempt: dict[str, Any] = {"attempt": attempt_index}
        attempts.append(attempt)
        try:
            if attempt_index > 1:
                _click_first_visible_locator(page, FASTMOSS_SLIDER_REFRESH_SELECTORS)
                _safe_wait_for_timeout(page, 1_500)
            state = _read_fastmoss_slider_state(page)
            if not state.get("visible"):
                attempt["resolved_before_drag"] = True
                return {"attempted": True, "resolved": True, "reason": "slider_already_cleared", "attempts": attempts}

            (
                background_locator,
                background_selector,
                target_locator,
                target_selector,
                handle_locator,
                handle_selector,
            ) = _wait_for_fastmoss_slider_elements(page, timeout_ms=3_000)
            if not (background_locator and target_locator and handle_locator):
                attempt["reason"] = "missing_slider_elements"
                continue
            background_box = _locator_bounding_box(background_locator)
            target_box = _locator_bounding_box(target_locator)
            handle_box = _locator_bounding_box(handle_locator)
            resource_page = raw_page or page
            background_image = _locator_image_bytes(background_locator, page=resource_page, selector=background_selector)
            target_image = _locator_image_bytes(target_locator, page=resource_page, selector=target_selector)
            if not (background_image and target_image and background_box and handle_box):
                attempt["reason"] = "missing_slider_artifacts"
                continue
            background_image_size = _image_size(background_image)
            slider_match = captcha_provider.match_slider(target_image, background_image, simple_target=True)
            drag_distance = _calculate_slider_drag_distance(
                slider_match=slider_match,
                background_box=background_box,
                background_image_size=background_image_size,
                target_image_size=_image_size(target_image),
                target_box=target_box,
                handle_box=handle_box,
            )
            attempt.update(
                {
                    "background_selector": background_selector,
                    "target_selector": target_selector,
                    "handle_selector": handle_selector,
                    "target_x": int(getattr(slider_match, "target_x", 0)),
                    "target_y": int(getattr(slider_match, "target_y", 0)),
                    "confidence": getattr(slider_match, "confidence", None),
                    "background_image_width": background_image_size[0],
                    "drag_distance": round(drag_distance, 2),
                }
            )
            _drag_slider_handle(page, handle_box=handle_box, drag_distance=drag_distance)
            _safe_wait_for_timeout(page, max(settle_ms, 1))
            state = _read_fastmoss_slider_state(page)
            attempt["popup_still_visible"] = bool(state.get("visible"))
            if not state.get("visible"):
                _safe_wait_for_timeout(page, max(confirm_ms, 1))
                confirmed_state = _read_fastmoss_slider_state(page)
                attempt["confirmation_wait_ms"] = max(confirm_ms, 1)
                attempt["confirmation_popup_still_visible"] = bool(confirmed_state.get("visible"))
                if confirmed_state.get("visible"):
                    attempt["reason"] = "slider_reappeared_after_confirmation_wait"
                    continue
                return {"attempted": True, "resolved": True, "reason": "slider_cleared", "search_url": search_url, "attempts": attempts}
        except Exception as exc:  # noqa: BLE001
            attempt["reason"] = "slider_attempt_failed"
            attempt["error"] = str(exc)
    return {"attempted": True, "resolved": False, "reason": "slider_popup_still_visible", "attempts": attempts}


def _build_slider_captcha_provider(provider_config: Mapping[str, Any] | None = None) -> Any:
    from automation_framework.captcha import DdddOcrCaptchaProvider

    return DdddOcrCaptchaProvider(**dict(provider_config or {}))

def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _float_value(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default
