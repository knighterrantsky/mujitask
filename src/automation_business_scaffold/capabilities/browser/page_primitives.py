from __future__ import annotations

import base64
import re
import time
from typing import Any, Mapping

import requests


def page_goto(page: Any, url: str, *, timeout_ms: int) -> None:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        return
    try:
        goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except TypeError:
        goto(url)


def safe_wait_for_timeout(page: Any, timeout_ms: int) -> None:
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        wait(max(int(timeout_ms), 1))
        return
    time.sleep(max(float(timeout_ms), 1.0) / 1000.0)


def first_visible_locator(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 500) -> tuple[Any | None, str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            target = getattr(locator, "first", locator)
            if locator_is_visible(target, timeout_ms=timeout_ms):
                return target, selector
        except Exception:
            continue
    return None, ""


def locator_is_visible(locator: Any, *, timeout_ms: int = 500) -> bool:
    is_visible = getattr(locator, "is_visible", None)
    if not callable(is_visible):
        return False
    try:
        return bool(is_visible(timeout=timeout_ms))
    except TypeError:
        try:
            return bool(is_visible())
        except Exception:
            return False
    except Exception:
        return False


def click_first_visible_locator(page: Any, selectors: tuple[str, ...]) -> bool:
    locator, _selector = first_visible_locator(page, selectors, timeout_ms=250)
    click = getattr(locator, "click", None)
    if not callable(click):
        return False
    try:
        click(timeout=1_000)
    except TypeError:
        click()
    except Exception:
        evaluate = getattr(locator, "evaluate", None)
        if not callable(evaluate):
            raise
        evaluate("element => element.click()")
    return True


def locator_image_bytes(locator: Any, *, page: Any | None = None, selector: str = "") -> bytes:
    resource = locator_image_resource(locator, page=page, selector=selector)
    if resource:
        payload = load_image_resource_bytes(resource)
        if payload:
            return payload
    return locator_screenshot_bytes(locator)


def locator_image_resource(locator: Any, *, page: Any | None = None, selector: str = "") -> str:
    payload = locator_image_resource_payload(locator)
    if not payload and page is not None and selector:
        payload = page_image_resource_payload(page, selector=selector)
    if not isinstance(payload, Mapping):
        return ""
    background_image = str(payload.get("backgroundImage") or "")
    background_url = extract_css_url(background_image)
    if background_url:
        return background_url
    return str(payload.get("src") or "").strip()


def locator_image_resource_payload(locator: Any) -> dict[str, Any]:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return {}
    try:
        payload = evaluate(
            """
            (element) => {
                const style = window.getComputedStyle(element);
                return {
                    backgroundImage: style && style.backgroundImage ? style.backgroundImage : "",
                    src: element.currentSrc || element.src || ""
                };
            }
            """
        )
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def page_image_resource_payload(page: Any, *, selector: str) -> dict[str, Any]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {}
    try:
        payload = evaluate(
            """
            (selector) => {
                const element = document.querySelector(selector);
                if (!element) {
                    return {};
                }
                const style = window.getComputedStyle(element);
                return {
                    backgroundImage: style && style.backgroundImage ? style.backgroundImage : "",
                    src: element.currentSrc || element.src || ""
                };
            }
            """,
            selector,
        )
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def extract_css_url(value: str) -> str:
    text = str(value or "").strip()
    if not text or text == "none":
        return ""
    matched = re.search(r"url\((['\"]?)(.*?)\1\)", text)
    return matched.group(2) if matched else ""


def load_image_resource_bytes(resource: str) -> bytes:
    source = str(resource or "").strip()
    if not source:
        return b""
    if source.startswith("data:image/"):
        try:
            _prefix, encoded = source.split(",", 1)
            return base64.b64decode(encoded)
        except Exception:
            return b""
    if source.startswith(("http://", "https://")):
        try:
            response = requests.get(
                source,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if response.status_code == 200:
                return response.content
        except Exception:
            return b""
    return b""


def locator_screenshot_bytes(locator: Any) -> bytes:
    screenshot = getattr(locator, "screenshot", None)
    if not callable(screenshot):
        return b""
    try:
        payload = screenshot(timeout=3_000)
    except TypeError:
        payload = screenshot()
    return payload if isinstance(payload, bytes) else b""


def locator_bounding_box(locator: Any) -> dict[str, float]:
    bounding_box = getattr(locator, "bounding_box", None)
    if not callable(bounding_box):
        return {}
    try:
        box = bounding_box(timeout=3_000)
    except TypeError:
        box = bounding_box()
    if not isinstance(box, Mapping):
        return {}
    return {
        "x": float(box.get("x") or 0),
        "y": float(box.get("y") or 0),
        "width": float(box.get("width") or 0),
        "height": float(box.get("height") or 0),
    }


def drag_slider_handle(page: Any, *, handle_box: Mapping[str, float], drag_distance: float) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        raise RuntimeError("Slider captcha requires page mouse support")
    start_x = float(handle_box.get("x") or 0) + float(handle_box.get("width") or 0) / 2
    start_y = float(handle_box.get("y") or 0) + float(handle_box.get("height") or 0) / 2
    steps = max(12, min(28, int(abs(drag_distance) // 8) or 12))
    mouse.move(start_x, start_y)
    mouse.down()
    for step in range(1, steps + 1):
        progress = step / steps
        eased = 1 - (1 - progress) * (1 - progress)
        mouse.move(start_x + drag_distance * eased, start_y)
    mouse.move(start_x + drag_distance, start_y)
    mouse.up()
