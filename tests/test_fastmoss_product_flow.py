from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from automation_business_scaffold.flows.fastmoss_product_flow import (
    _day_before_yesterday_date_string,
    _extract_sales_value_from_overview_text,
    _normalize_fastmoss_product_id,
    _parse_fastmoss_metric_number,
    _preferred_fastmoss_yesterday_dates,
    _yesterday_date_string,
    fetch_fastmoss_product_sales_via_browser,
)


def test_normalize_fastmoss_product_id_accepts_raw_id_and_detail_url():
    assert _normalize_fastmoss_product_id("1732268173492064949") == "1732268173492064949"
    assert (
        _normalize_fastmoss_product_id("https://www.fastmoss.com/zh/e-commerce/detail/1732268173492064949")
        == "1732268173492064949"
    )


def test_extract_sales_value_from_overview_text_reads_summary_card():
    overview_text = """
数据总览
近7天
概览

24

日均3

销量
"""
    assert _extract_sales_value_from_overview_text(overview_text) == "24"


def test_yesterday_date_string_uses_previous_day():
    assert _yesterday_date_string(datetime(2026, 4, 6, 12, 0, 0)) == "2026-04-05"


def test_day_before_yesterday_date_string_uses_two_days_before():
    assert _day_before_yesterday_date_string(datetime(2026, 4, 6, 12, 0, 0)) == "2026-04-04"


def test_preferred_fastmoss_yesterday_dates_prioritizes_yesterday_then_day_before():
    assert _preferred_fastmoss_yesterday_dates(datetime(2026, 4, 6, 12, 0, 0)) == (
        "2026-04-05",
        "2026-04-04",
    )


def test_parse_fastmoss_metric_number_supports_chinese_and_suffix_units():
    assert _parse_fastmoss_metric_number("19.44万") == 194400
    assert _parse_fastmoss_metric_number("12.5k") == 12500
    assert _parse_fastmoss_metric_number("1.2M") == 1200000


def test_fetch_fastmoss_product_sales_via_browser_opens_detail_directly_and_screenshots_before_extraction(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["fetch_fastmoss_product_sales_via_browser"],
    )

    class FakePage:
        def __init__(self) -> None:
            self.detail_gotos: list[str] = []
            self.events: list[str] = []

        def title(self) -> str:
            return "ignored"

    fake_page = FakePage()

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type(
            "_Session",
            (),
            {
                "provider_name": "roxy",
                "target_key": "roxy:84278:sample",
                "profile_ref": "roxy-united-states",
                "session_ref": "roxy-united-states",
                "page": fake_page,
            },
        )()

    monkeypatch.setenv("FASTMOSS_PHONE", "18058996348")
    monkeypatch.setenv("FASTMOSS_PASSWORD", "Tiktok-623")
    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)

    def fake_ensure_login(page, *, phone, password, step_delay_sec, login_settle_sec):
        assert page is fake_page
        assert phone == "18058996348"
        assert password == "Tiktok-623"
        assert step_delay_sec == 0
        assert login_settle_sec == 0
        return "logged_in"

    monkeypatch.setattr(module, "_ensure_fastmoss_logged_in", fake_ensure_login)
    monkeypatch.setattr(
        module,
        "_open_fastmoss_detail_page",
        lambda page, detail_url, step_delay_sec: (
            fake_page.detail_gotos.append(detail_url),
            fake_page.events.append("open_detail"),
        ),
    )
    monkeypatch.setattr(
        module,
        "_capture_fastmoss_detail_screenshot",
        lambda page, *, product_id, screenshot_dir=module.DEFAULT_FASTMOSS_DETAIL_SCREENSHOT_DIR: (
            fake_page.events.append("capture_screenshot"),
            "/tmp/1732268173492064949-fastmoss-detail.png",
            "1732268173492064949-fastmoss-detail.png",
            "image/png",
        )[1:],
    )
    monkeypatch.setattr(
        module,
        "_extract_fastmoss_product_title",
        lambda page: fake_page.events.append("extract_title") or "Example Product",
    )
    monkeypatch.setattr(
        module,
        "_extract_fastmoss_period_sales",
        lambda page, *, days, step_delay_sec: (
            fake_page.events.append(f"extract_{days}d"),
            {"7": "24", "28": "940", "90": "1824"}[days],
        )[1],
    )
    monkeypatch.setattr(
        module,
        "_extract_fastmoss_yesterday_sales",
        lambda page, *, target_date, fallback_target_date=None, step_delay_sec: (
            fake_page.events.append("extract_yesterday") or "0"
        ),
    )

    snapshot = fetch_fastmoss_product_sales_via_browser(
        "https://www.fastmoss.com/zh/e-commerce/detail/1732268173492064949",
        profile_ref="roxy-united-states",
        fastmoss_phone_env="FASTMOSS_PHONE",
        fastmoss_password_env="FASTMOSS_PASSWORD",
        step_delay_sec=0,
        login_settle_sec=0,
    )

    assert fake_page.detail_gotos == ["https://www.fastmoss.com/zh/e-commerce/detail/1732268173492064949"]
    assert fake_page.events == [
        "open_detail",
        "capture_screenshot",
        "extract_title",
        "extract_7d",
        "extract_28d",
        "extract_90d",
        "extract_yesterday",
    ]
    assert snapshot.to_dict() == {
        "product_id": "1732268173492064949",
        "search_url": "https://www.fastmoss.com/zh/e-commerce/search?page=1&words=1732268173492064949",
        "detail_url": "https://www.fastmoss.com/zh/e-commerce/detail/1732268173492064949",
        "product_title": "Example Product",
        "login_state": "logged_in",
        "yesterday_sales": "0",
        "sales_7d": "24",
        "sales_28d": "940",
        "sales_90d": "1824",
        "detail_page_screenshot_local_path": "/tmp/1732268173492064949-fastmoss-detail.png",
        "detail_page_screenshot_file_name": "1732268173492064949-fastmoss-detail.png",
        "detail_page_screenshot_mime_type": "image/png",
    }


def test_fetch_fastmoss_product_sales_via_browser_can_skip_login_verification(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["fetch_fastmoss_product_sales_via_browser"],
    )

    class FakePage:
        def title(self) -> str:
            return "ignored"

    fake_page = FakePage()

    @contextmanager
    def fake_open_automation_page(**_kwargs):
        yield type("_Session", (), {"page": fake_page})()

    monkeypatch.setattr(module, "open_automation_page", fake_open_automation_page)

    def fail_ensure_login(*_args, **_kwargs):
        raise AssertionError("login validation should be skipped")

    monkeypatch.setattr(module, "_ensure_fastmoss_logged_in", fail_ensure_login)
    monkeypatch.setattr(module, "_open_fastmoss_detail_page", lambda page, detail_url, step_delay_sec: None)
    monkeypatch.setattr(module, "_extract_fastmoss_product_title", lambda page: "Example Product")
    monkeypatch.setattr(module, "_extract_fastmoss_period_sales", lambda page, *, days, step_delay_sec: days)
    monkeypatch.setattr(module, "_extract_fastmoss_yesterday_sales", lambda page, **kwargs: "-1")

    snapshot = fetch_fastmoss_product_sales_via_browser(
        "1732268173492064949",
        verify_login=False,
        capture_detail_screenshot=False,
        step_delay_sec=0,
        login_settle_sec=0,
    )

    assert snapshot.login_state == "skipped_login_verification"


def test_extract_fastmoss_yesterday_sales_uses_date_picker_selection(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_extract_fastmoss_yesterday_sales"],
    )

    class FakeLocator:
        def __init__(self, name: str) -> None:
            self.name = name
            self.first = self

        def count(self) -> int:
            return 1

    class FakeOverview:
        def __init__(self) -> None:
            self.start = FakeLocator("start")
            self.end = FakeLocator("end")

        def locator(self, selector: str):
            if selector == "input[placeholder='开始日期']":
                return self.start
            if selector == "input[placeholder='结束日期']":
                return self.end
            raise AssertionError(f"unexpected selector: {selector}")

        def inner_text(self, timeout: int = 5000) -> str:
            return "概览 24 日均3 销量"

    fake_overview = FakeOverview()
    selections: list[tuple[str, str, float]] = []

    monkeypatch.setattr(module, "_fastmoss_overview_locator", lambda page: fake_overview)
    monkeypatch.setattr(module, "_safe_fastmoss_overview_text", lambda overview: "previous")
    monkeypatch.setattr(
        module,
        "_select_fastmoss_overview_date",
        lambda page, overview, *, input_locator, target_date, step_delay_sec: selections.append(
            (input_locator.name, target_date, step_delay_sec)
        )
        or True,
    )
    monkeypatch.setattr(
        module,
        "_wait_for_fastmoss_overview_sales_refresh",
        lambda overview, *, previous_text, min_wait_sec, require_change, timeout_sec=12.0: "24",
    )

    sales = module._extract_fastmoss_yesterday_sales(
        object(),
        target_date="2026-04-06",
        step_delay_sec=0,
    )

    assert sales == "24"
    assert selections == [
        ("start", "2026-04-06", 0),
        ("end", "2026-04-06", 0),
    ]


def test_extract_fastmoss_yesterday_sales_falls_back_to_day_before_when_yesterday_unavailable(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_extract_fastmoss_yesterday_sales"],
    )

    class FakeLocator:
        def __init__(self, name: str) -> None:
            self.name = name
            self.first = self

        def count(self) -> int:
            return 1

    class FakeOverview:
        def __init__(self) -> None:
            self.start = FakeLocator("start")
            self.end = FakeLocator("end")

        def locator(self, selector: str):
            if selector == "input[placeholder='开始日期']":
                return self.start
            if selector == "input[placeholder='结束日期']":
                return self.end
            raise AssertionError(f"unexpected selector: {selector}")

        def inner_text(self, timeout: int = 5000) -> str:
            return "概览 18 日均3 销量"

    selections: list[tuple[str, str]] = []

    def fake_select(_page, _overview, *, input_locator, target_date, step_delay_sec):
        selections.append((input_locator.name, target_date))
        return target_date == "2026-04-05"

    monkeypatch.setattr(module, "_fastmoss_overview_locator", lambda page: FakeOverview())
    monkeypatch.setattr(module, "_safe_fastmoss_overview_text", lambda overview: "previous")
    monkeypatch.setattr(module, "_select_fastmoss_overview_date", fake_select)
    monkeypatch.setattr(
        module,
        "_wait_for_fastmoss_overview_sales_refresh",
        lambda overview, *, previous_text, min_wait_sec, require_change, timeout_sec=12.0: "18",
    )

    sales = module._extract_fastmoss_yesterday_sales(
        object(),
        target_date="2026-04-06",
        fallback_target_date="2026-04-05",
        step_delay_sec=0,
    )

    assert sales == "18"
    assert selections == [
        ("start", "2026-04-06"),
        ("start", "2026-04-05"),
        ("end", "2026-04-05"),
    ]


def test_extract_fastmoss_yesterday_sales_returns_negative_one_when_target_date_unavailable(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_extract_fastmoss_yesterday_sales"],
    )

    class FakeLocator:
        def __init__(self, name: str) -> None:
            self.name = name
            self.first = self

        def count(self) -> int:
            return 1

    class FakeOverview:
        def __init__(self) -> None:
            self.start = FakeLocator("start")
            self.end = FakeLocator("end")

        def locator(self, selector: str):
            if selector == "input[placeholder='开始日期']":
                return self.start
            if selector == "input[placeholder='结束日期']":
                return self.end
            raise AssertionError(f"unexpected selector: {selector}")

        def inner_text(self, timeout: int = 5000) -> str:
            raise AssertionError("inner_text should not be read when target date is unavailable")

    selections: list[tuple[str, str]] = []

    def fake_select(_page, _overview, *, input_locator, target_date, step_delay_sec):
        selections.append((input_locator.name, target_date))
        return False

    monkeypatch.setattr(module, "_fastmoss_overview_locator", lambda page: FakeOverview())
    monkeypatch.setattr(module, "_safe_fastmoss_overview_text", lambda overview: "previous")
    monkeypatch.setattr(module, "_select_fastmoss_overview_date", fake_select)

    sales = module._extract_fastmoss_yesterday_sales(
        object(),
        target_date="2026-04-06",
        fallback_target_date="2026-04-05",
        step_delay_sec=0,
    )

    assert sales == "-1"
    assert selections == [
        ("start", "2026-04-06"),
        ("start", "2026-04-05"),
    ]


def test_navigate_fastmoss_datepicker_to_month_clicks_next_until_target_month_visible(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_navigate_fastmoss_datepicker_to_month"],
    )

    class FakeMissingLocator:
        def count(self) -> int:
            return 0

    class FakeLocatorGroup:
        def __init__(self, items):
            self._items = items

        def count(self) -> int:
            return len(self._items)

        def nth(self, index: int):
            return self._items[index]

        @property
        def first(self):
            return self._items[0] if self._items else FakeMissingLocator()

        @property
        def last(self):
            return self._items[-1] if self._items else FakeMissingLocator()

    class FakeHeaderView:
        def __init__(self, picker, header_index: int) -> None:
            self.picker = picker
            self.header_index = header_index

        def inner_text(self, timeout: int = 1000) -> str:
            year, month = self.picker.states[self.picker.state_index][self.header_index]
            return f"{year}年{month}月"

    class FakeButton:
        def __init__(self, picker, direction: str) -> None:
            self.picker = picker
            self.direction = direction

        def count(self) -> int:
            return 1

        def click(self) -> None:
            self.picker.history.append(self.direction)
            if self.direction == "next":
                self.picker.state_index = min(self.picker.state_index + 1, len(self.picker.states) - 1)
            else:
                self.picker.state_index = max(self.picker.state_index - 1, 0)

    class FakePicker:
        def __init__(self) -> None:
            self.states = [
                [(2026, 1), (2026, 2)],
                [(2026, 2), (2026, 3)],
                [(2026, 3), (2026, 4)],
            ]
            self.state_index = 0
            self.history: list[str] = []

        def locator(self, selector: str):
            if selector == ".ant-picker-header-view":
                return FakeLocatorGroup([FakeHeaderView(self, 0), FakeHeaderView(self, 1)])
            if selector == ".ant-picker-header-next-btn":
                return FakeLocatorGroup([FakeButton(self, "next")])
            if selector == ".ant-picker-header-prev-btn":
                return FakeLocatorGroup([FakeButton(self, "prev")])
            raise AssertionError(f"unexpected selector: {selector}")

    picker = FakePicker()
    monkeypatch.setattr(module, "_page_click", lambda page, target: target.click())
    monkeypatch.setattr(module, "_sleep", lambda seconds: None)

    moved = module._navigate_fastmoss_datepicker_to_month(
        object(),
        picker,
        target_date="2026-04-06",
        step_delay_sec=0,
    )

    assert moved is True
    assert picker.history == ["next", "next"]
    assert picker.state_index == 2


def test_find_fastmoss_date_cell_skips_disabled_candidates():
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_find_fastmoss_date_cell"],
    )

    class FakeLocatorGroup:
        def __init__(self, items):
            self._items = items

        def count(self) -> int:
            return len(self._items)

        def nth(self, index: int):
            return self._items[index]

    class FakeCell:
        def __init__(self, class_name: str, name: str) -> None:
            self.class_name = class_name
            self.name = name

        def get_attribute(self, name: str):
            if name == "class":
                return self.class_name
            return ""

    enabled_cell = FakeCell("ant-picker-cell ant-picker-cell-in-view", "enabled")
    disabled_cell = FakeCell("ant-picker-cell ant-picker-cell-disabled", "disabled")

    class FakePicker:
        def locator(self, selector: str):
            if selector == ".ant-picker-cell.ant-picker-cell-in-view[title='2026-04-06']":
                return FakeLocatorGroup([disabled_cell, enabled_cell])
            if selector == ".ant-picker-cell[title='2026-04-06']":
                return FakeLocatorGroup([disabled_cell, enabled_cell])
            raise AssertionError(f"unexpected selector: {selector}")

    selected = module._find_fastmoss_date_cell(FakePicker(), target_date="2026-04-06")

    assert selected is enabled_cell


def test_extract_fastmoss_period_sales_waits_for_overview_refresh(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_extract_fastmoss_period_sales"],
    )

    class FakeLabel:
        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            return 1

        def get_attribute(self, name: str):
            if name == "aria-checked":
                return "false"
            if name == "class":
                return ""
            return ""

    class FakeOverview:
        def __init__(self) -> None:
            self.label = FakeLabel()

        def locator(self, selector: str):
            if selector == "label:has-text('近7天')":
                return self.label
            raise AssertionError(f"unexpected selector: {selector}")

    clicked: list[str] = []
    wait_args: dict[str, object] = {}

    monkeypatch.setattr(module, "_fastmoss_overview_locator", lambda page: FakeOverview())
    monkeypatch.setattr(module, "_safe_fastmoss_overview_text", lambda overview: "before")
    monkeypatch.setattr(module, "_page_click", lambda page, target: clicked.append("clicked"))
    monkeypatch.setattr(module, "_sleep", lambda seconds: None)

    def fake_wait(overview, *, previous_text, min_wait_sec, require_change, timeout_sec=12.0):
        wait_args.update(
            {
                "previous_text": previous_text,
                "min_wait_sec": min_wait_sec,
                "require_change": require_change,
            }
        )
        return "24"

    monkeypatch.setattr(module, "_wait_for_fastmoss_overview_sales_refresh", fake_wait)

    sales = module._extract_fastmoss_period_sales(object(), days="7", step_delay_sec=0)

    assert sales == "24"
    assert clicked == ["clicked"]
    assert wait_args == {
        "previous_text": "before",
        "min_wait_sec": 0.4,
        "require_change": True,
    }


def test_extract_fastmoss_period_sales_allows_steady_metric_after_range_selection_changes(
    monkeypatch,
):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_extract_fastmoss_period_sales"],
    )

    class FakeLabel:
        def __init__(self) -> None:
            self.first = self
            self.selected = False

        def count(self) -> int:
            return 1

        def get_attribute(self, name: str):
            if name == "aria-checked":
                return "true" if self.selected else "false"
            if name == "class":
                return "checked" if self.selected else ""
            return ""

    class FakeOverview:
        def __init__(self) -> None:
            self.label = FakeLabel()

        def locator(self, selector: str):
            if selector == "label:has-text('近7天')":
                return self.label
            raise AssertionError(f"unexpected selector: {selector}")

    wait_args: dict[str, object] = {}
    overview = FakeOverview()

    monkeypatch.setattr(module, "_fastmoss_overview_locator", lambda page: overview)
    monkeypatch.setattr(module, "_safe_fastmoss_overview_text", lambda overview: "before")

    def fake_click(page, target):
        overview.label.selected = True

    monkeypatch.setattr(module, "_page_click", fake_click)
    monkeypatch.setattr(module, "_sleep", lambda seconds: None)

    def fake_wait(overview, *, previous_text, min_wait_sec, require_change, timeout_sec=12.0):
        wait_args.update(
            {
                "previous_text": previous_text,
                "min_wait_sec": min_wait_sec,
                "require_change": require_change,
            }
        )
        return "24"

    monkeypatch.setattr(module, "_wait_for_fastmoss_overview_sales_refresh", fake_wait)

    sales = module._extract_fastmoss_period_sales(object(), days="7", step_delay_sec=0)

    assert sales == "24"
    assert wait_args == {
        "previous_text": "before",
        "min_wait_sec": 0.4,
        "require_change": False,
    }


def test_wait_for_fastmoss_overview_sales_refresh_requires_metric_refresh(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_wait_for_fastmoss_overview_sales_refresh"],
    )

    class FakeOverview:
        def __init__(self, texts):
            self._texts = iter(texts)

        def inner_text(self, timeout: int = 5000) -> str:
            return next(self._texts)

    texts = [
        "近90天 概览 100 日均1 销量",
        "近90天 概览 100 日均1 销量",
        "近7天 概览 24 日均3 销量",
        "近7天 概览 24 日均3 销量",
    ]
    overview = FakeOverview(texts)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    refreshed = module._wait_for_fastmoss_overview_sales_refresh(
        overview,
        previous_text="近90天 概览 100 日均1 销量",
        min_wait_sec=0,
        require_change=True,
        timeout_sec=1,
    )

    assert refreshed == "24"


def test_wait_for_fastmoss_overview_sales_refresh_waits_for_loading_to_clear(monkeypatch):
    module = __import__(
        "automation_business_scaffold.flows.fastmoss_product_flow",
        fromlist=["_wait_for_fastmoss_overview_sales_refresh"],
    )

    class FakeOverview:
        def __init__(self, texts):
            self._texts = iter(texts)

        def inner_text(self, timeout: int = 5000) -> str:
            return next(self._texts)

    loading_states = iter([True, True, False, False])
    loading_checks: list[bool] = []

    def fake_has_loading(_overview) -> bool:
        state = next(loading_states)
        loading_checks.append(state)
        return state

    overview = FakeOverview(
        [
            "近7天 概览 24 日均3 销量",
            "近7天 概览 24 日均3 销量",
            "近7天 概览 24 日均3 销量",
            "近7天 概览 24 日均3 销量",
        ]
    )
    monkeypatch.setattr(module, "_fastmoss_overview_has_loading", fake_has_loading)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    refreshed = module._wait_for_fastmoss_overview_sales_refresh(
        overview,
        previous_text="近90天 概览 100 日均1 销量",
        min_wait_sec=0,
        require_change=True,
        timeout_sec=1,
    )

    assert refreshed == "24"
    assert loading_checks == [True, True, False, False]
