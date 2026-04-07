from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from automation_business_scaffold.flows.fastmoss_product_flow import (
    _extract_sales_value_from_overview_text,
    _normalize_fastmoss_product_id,
    _parse_fastmoss_metric_number,
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
        lambda page, *, target_date, step_delay_sec: fake_page.events.append("extract_yesterday") or "0",
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
