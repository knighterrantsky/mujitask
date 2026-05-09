from __future__ import annotations

from typing import Any

from automation_business_scaffold.capabilities.browser.page_primitives import (
    click_first_visible_locator,
    first_visible_locator,
    locator_bounding_box,
    page_goto,
    safe_wait_for_timeout,
)


class _Locator:
    def __init__(self, *, visible: bool = False, click_uses_timeout: bool = True) -> None:
        self.visible = visible
        self.click_uses_timeout = click_uses_timeout
        self.clicked = False

    @property
    def first(self) -> "_Locator":
        return self

    def is_visible(self, **kwargs: Any) -> bool:
        del kwargs
        return self.visible

    def click(self, **kwargs: Any) -> None:
        if not self.click_uses_timeout and kwargs:
            raise TypeError("timeout is unsupported")
        self.clicked = True

    def bounding_box(self, **kwargs: Any) -> dict[str, float]:
        del kwargs
        return {"x": 1, "y": 2, "width": 3, "height": 4}


class _Page:
    def __init__(self) -> None:
        self.selectors: dict[str, _Locator] = {}
        self.goto_calls: list[dict[str, Any]] = []
        self.waits: list[int] = []

    def locator(self, selector: str) -> _Locator:
        if selector == ".raises":
            raise RuntimeError("selector failed")
        return self.selectors.get(selector, _Locator())

    def goto(self, url: str, **kwargs: Any) -> None:
        if kwargs:
            raise TypeError("legacy page only accepts url")
        self.goto_calls.append({"url": url, "kwargs": kwargs})

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


def test_page_primitives_probe_visible_locator_after_selector_failures() -> None:
    page = _Page()
    page.selectors[".challenge"] = _Locator(visible=True)

    locator, selector = first_visible_locator(page, (".raises", ".missing", ".challenge"))

    assert selector == ".challenge"
    assert locator is page.selectors[".challenge"]
    assert locator_bounding_box(locator) == {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}


def test_page_primitives_preserve_legacy_navigation_and_click_fallbacks() -> None:
    page = _Page()
    page.selectors[".slider-handle"] = _Locator(visible=True, click_uses_timeout=False)

    page_goto(page, "https://www.fastmoss.com/security", timeout_ms=123)
    clicked = click_first_visible_locator(page, (".missing", ".slider-handle"))
    safe_wait_for_timeout(page, 0)

    assert page.goto_calls == [{"url": "https://www.fastmoss.com/security", "kwargs": {}}]
    assert clicked is True
    assert page.selectors[".slider-handle"].clicked is True
    assert page.waits == [1]
