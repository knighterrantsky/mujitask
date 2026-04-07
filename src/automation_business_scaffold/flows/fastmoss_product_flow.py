from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from automation_business_scaffold.models import FastMossProductSalesSnapshot

from .browser_bridge import open_automation_page

DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL = "https://www.fastmoss.com/zh/account/center"
DEFAULT_FASTMOSS_SEARCH_URL = "https://www.fastmoss.com/zh/e-commerce/search"
DEFAULT_FASTMOSS_STEP_DELAY_SEC = 2.0
DEFAULT_FASTMOSS_LOGIN_SETTLE_SEC = 8.0
DEFAULT_FASTMOSS_DETAIL_SCREENSHOT_DIR = "runtime/downloads/fastmoss_detail_screenshots"


class FastMossStage2Error(RuntimeError):
    pass


def fetch_fastmoss_product_sales_via_browser(
    product_id: str,
    *,
    profile_ref: str | None = None,
    fastmoss_phone: str | None = None,
    fastmoss_password: str | None = None,
    fastmoss_phone_env: str | None = None,
    fastmoss_password_env: str | None = None,
    step_delay_sec: float = DEFAULT_FASTMOSS_STEP_DELAY_SEC,
    login_settle_sec: float = DEFAULT_FASTMOSS_LOGIN_SETTLE_SEC,
    capture_detail_screenshot: bool = True,
) -> FastMossProductSalesSnapshot:
    normalized_product_id = _normalize_fastmoss_product_id(product_id)
    phone = _resolve_fastmoss_secret(fastmoss_phone, fastmoss_phone_env)
    password = _resolve_fastmoss_secret(fastmoss_password, fastmoss_password_env)
    detail_url = _build_fastmoss_detail_url(normalized_product_id)
    search_url = _build_fastmoss_search_url(normalized_product_id)

    with open_automation_page(profile_ref=profile_ref) as browser_page:
        page = browser_page.page
        login_state = _ensure_fastmoss_logged_in(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
        )
        _open_fastmoss_detail_page(page, detail_url, step_delay_sec=step_delay_sec)

        screenshot_path = ""
        screenshot_name = ""
        screenshot_mime = ""
        if capture_detail_screenshot:
            screenshot_path, screenshot_name, screenshot_mime = _capture_fastmoss_detail_screenshot(
                page,
                product_id=normalized_product_id,
            )

        product_title = _extract_fastmoss_product_title(page)
        sales_7d = _extract_fastmoss_period_sales(page, days="7", step_delay_sec=step_delay_sec)
        sales_28d = _extract_fastmoss_period_sales(page, days="28", step_delay_sec=step_delay_sec)
        sales_90d = _extract_fastmoss_period_sales(page, days="90", step_delay_sec=step_delay_sec)
        yesterday_sales = _extract_fastmoss_yesterday_sales(
            page,
            target_date=_yesterday_date_string(),
            step_delay_sec=step_delay_sec,
        )

        return FastMossProductSalesSnapshot(
            product_id=normalized_product_id,
            search_url=search_url,
            detail_url=detail_url,
            product_title=product_title,
            login_state=login_state,
            yesterday_sales=yesterday_sales,
            sales_7d=sales_7d,
            sales_28d=sales_28d,
            sales_90d=sales_90d,
            detail_page_screenshot_local_path=screenshot_path,
            detail_page_screenshot_file_name=screenshot_name,
            detail_page_screenshot_mime_type=screenshot_mime,
        )


def discover_fastmoss_keyword_candidates_via_browser(
    search_keyword: str,
    *,
    sales_7d_threshold: float,
    profile_ref: str | None = None,
    fastmoss_phone: str | None = None,
    fastmoss_password: str | None = None,
    fastmoss_phone_env: str | None = None,
    fastmoss_password_env: str | None = None,
    step_delay_sec: float = DEFAULT_FASTMOSS_STEP_DELAY_SEC,
    login_settle_sec: float = DEFAULT_FASTMOSS_LOGIN_SETTLE_SEC,
    max_pages: int = 0,
) -> dict[str, Any]:
    normalized_keyword = str(search_keyword or "").strip()
    if not normalized_keyword:
        raise ValueError("search_keyword is required")

    phone = _resolve_fastmoss_secret(fastmoss_phone, fastmoss_phone_env)
    password = _resolve_fastmoss_secret(fastmoss_password, fastmoss_password_env)

    with open_automation_page(profile_ref=profile_ref) as browser_page:
        page = browser_page.page
        login_state = _ensure_fastmoss_logged_in(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
        )
        search_url = _execute_fastmoss_keyword_search(
            page,
            normalized_keyword,
            step_delay_sec=step_delay_sec,
        )
        _ensure_fastmoss_sales_7d_sort_desc(page, step_delay_sec=step_delay_sec)

        items: list[dict[str, Any]] = []
        seen_product_ids: set[str] = set()
        pages_scanned = 0
        rows_scanned = 0

        while True:
            rows = _fastmoss_search_result_rows(page)
            page_candidates = _extract_fastmoss_search_page_candidates(
                rows,
                search_keyword=normalized_keyword,
            )
            pages_scanned += 1
            rows_scanned += len(page_candidates)

            has_threshold_match = False
            for candidate in page_candidates:
                product_id = str(candidate.get("product_id", "")).strip()
                if not product_id or product_id in seen_product_ids:
                    continue
                seen_product_ids.add(product_id)
                if float(candidate.get("sales_7d_value", 0.0) or 0.0) <= sales_7d_threshold:
                    continue
                has_threshold_match = True
                items.append(candidate)

            if max_pages > 0 and pages_scanned >= max_pages:
                break
            if not has_threshold_match:
                break
            if not _click_fastmoss_next_page(page, step_delay_sec=step_delay_sec):
                break

        return {
            "search_keyword": normalized_keyword,
            "search_url": search_url,
            "login_state": login_state,
            "sales_7d_threshold": sales_7d_threshold,
            "pages_scanned": pages_scanned,
            "rows_scanned": rows_scanned,
            "items": items,
        }


def _ensure_fastmoss_logged_in(
    page: Any,
    *,
    phone: str,
    password: str,
    step_delay_sec: float,
    login_settle_sec: float,
) -> str:
    page.goto(DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL, wait_until="domcontentloaded", timeout=60000)
    _sleep(step_delay_sec)
    if _is_fastmoss_account_logged_in(page):
        return "already_logged_in"

    if not phone or not password:
        raise FastMossStage2Error("FastMoss login required but phone/password were not provided")

    guest_modal = page.locator(".ant-modal-wrap").first
    if guest_modal.count():
        guest_modal.get_by_text("登录/注册", exact=True).click()
        _sleep(step_delay_sec)

    login_modal = page.locator(".ant-modal-wrap").nth(1)
    if not login_modal.count():
        raise FastMossStage2Error("FastMoss login modal did not appear")

    if login_modal.get_by_text("手机号登录/注册", exact=True).count():
        login_modal.get_by_text("手机号登录/注册", exact=True).click()
        _sleep(step_delay_sec)
    if login_modal.get_by_text("密码登录", exact=True).count():
        login_modal.get_by_text("密码登录", exact=True).click()
        _sleep(step_delay_sec)

    phone_input = login_modal.locator("input[placeholder='输入您的手机号码']").first
    password_input = login_modal.locator("input[placeholder='输入密码']").first
    if not phone_input.count() or not password_input.count():
        raise FastMossStage2Error("FastMoss phone/password login inputs were not found")

    phone_input.fill(phone)
    _sleep(step_delay_sec)
    password_input.fill(password)
    _sleep(step_delay_sec)
    login_modal.get_by_text("注册/登录", exact=True).click()
    _sleep(login_settle_sec)

    page.goto(DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL, wait_until="domcontentloaded", timeout=60000)
    _sleep(step_delay_sec)
    if not _is_fastmoss_account_logged_in(page):
        raise FastMossStage2Error("FastMoss login did not reach the account center")
    return "logged_in"


def _is_fastmoss_account_logged_in(page: Any) -> bool:
    body_text = page.locator("body").inner_text(timeout=5000)
    if "账号ID：" in body_text or "会员有效期至" in body_text:
        return True
    if "游客身份" in body_text or "登录/注册查看您的账户信息" in body_text:
        return False
    return False


def _search_fastmoss_product_detail_url(
    page: Any,
    product_id: str,
    *,
    step_delay_sec: float,
) -> tuple[str, str]:
    page.goto(DEFAULT_FASTMOSS_SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    _sleep(step_delay_sec)

    search_input = page.locator("input[placeholder='商品搜索']").first
    if not search_input.count():
        raise FastMossStage2Error("FastMoss search input was not found")

    search_input.fill(product_id)
    _sleep(step_delay_sec)
    search_input.press("Enter")
    page.wait_for_url(re.compile(rf".*words={re.escape(product_id)}"), timeout=30000)
    _sleep(step_delay_sec)

    detail_link = page.locator(f"a[href='/zh/e-commerce/detail/{product_id}']").first
    try:
        detail_link.wait_for(state="visible", timeout=10000)
    except Exception:
        detail_link = page.locator(f"a[href$='/detail/{product_id}']").first
        detail_link.wait_for(state="visible", timeout=10000)
    if not detail_link.count():
        raise FastMossStage2Error(f"FastMoss detail link for product_id={product_id} was not found")

    href = str(detail_link.get_attribute("href") or "").strip()
    if not href:
        raise FastMossStage2Error("FastMoss detail link did not expose an href")

    return page.url, urljoin(DEFAULT_FASTMOSS_SEARCH_URL, href)


def _open_fastmoss_detail_page(page: Any, detail_url: str, *, step_delay_sec: float) -> None:
    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
    _fastmoss_overview_locator(page).wait_for(state="visible", timeout=30000)
    _sleep(step_delay_sec)


def _build_fastmoss_detail_url(product_id: str) -> str:
    return f"https://www.fastmoss.com/zh/e-commerce/detail/{product_id}"


def _build_fastmoss_search_url(search_keyword: str) -> str:
    return f"https://www.fastmoss.com/zh/e-commerce/search?page=1&words={search_keyword}"


def _execute_fastmoss_keyword_search(
    page: Any,
    search_keyword: str,
    *,
    step_delay_sec: float,
) -> str:
    page.goto(DEFAULT_FASTMOSS_SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    _sleep(step_delay_sec)

    search_input = page.locator("input[placeholder='商品搜索']").first
    if not search_input.count():
        raise FastMossStage2Error("FastMoss search input was not found")

    search_input.fill(search_keyword)
    _sleep(step_delay_sec)
    search_input.press("Enter")
    _sleep(step_delay_sec * 2)
    _wait_for_fastmoss_search_table(page)
    return page.url


def _ensure_fastmoss_sales_7d_sort_desc(page: Any, *, step_delay_sec: float) -> None:
    sort_header = _find_fastmoss_header_by_text(page, "近7天销量")
    if sort_header is None:
        raise FastMossStage2Error("FastMoss 7-day sales sort header was not found")

    for _ in range(3):
        sort_state = str(sort_header.get_attribute("aria-sort") or "").strip().lower()
        if sort_state == "descending":
            return
        sort_header.click()
        _sleep(step_delay_sec)
        _wait_for_fastmoss_search_table(page)

    raise FastMossStage2Error("FastMoss 7-day sales sort could not be switched to descending")


def _find_fastmoss_header_by_text(page: Any, target_text: str):
    headers = page.locator("thead th")
    header_count = headers.count()
    for index in range(header_count):
        header = headers.nth(index)
        normalized_text = "".join(str(header.inner_text(timeout=3000) or "").split())
        if normalized_text == target_text:
            return header
    return None


def _wait_for_fastmoss_search_table(page: Any) -> None:
    table = page.locator("table").first
    if table.count():
        table.wait_for(state="visible", timeout=30000)


def _fastmoss_search_result_rows(page: Any) -> Any:
    _wait_for_fastmoss_search_table(page)
    return page.locator("tr[data-row-key]")


def _extract_fastmoss_search_page_candidates(rows: Any, *, search_keyword: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    row_count = rows.count()
    for index in range(row_count):
        row = rows.nth(index)
        candidate = _extract_fastmoss_search_row_candidate(
            row,
            search_keyword=search_keyword,
            page_index=None,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _extract_fastmoss_search_row_candidate(
    row: Any,
    *,
    search_keyword: str,
    page_index: int | None,
) -> dict[str, Any] | None:
    raw_product_id = str(row.get_attribute("data-row-key") or "").strip() or str(
        row.locator("a[href*='/zh/e-commerce/detail/']").first.get_attribute("href") or ""
    ).strip()
    try:
        product_id = _normalize_fastmoss_product_id(raw_product_id)
    except ValueError:
        return None

    detail_link = row.locator("a[href*='/zh/e-commerce/detail/']").first
    detail_href = str(detail_link.get_attribute("href") or "").strip()
    detail_url = urljoin(DEFAULT_FASTMOSS_SEARCH_URL, detail_href) if detail_href else _build_fastmoss_detail_url(product_id)
    title = ""
    if detail_link.count():
        title = str(detail_link.locator("h3").first.inner_text(timeout=3000) or "").strip()
    if not title:
        title = str(detail_link.inner_text(timeout=3000) or "").strip().splitlines()[0]

    sales_7d_text = _extract_fastmoss_search_row_sales_text(row)
    sales_7d_value = _parse_fastmoss_metric_number(sales_7d_text)

    candidate: dict[str, Any] = {
        "search_keyword": search_keyword,
        "product_id": product_id,
        "detail_url": detail_url,
        "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
        "product_title": title,
        "sales_7d": sales_7d_text,
        "sales_7d_value": sales_7d_value,
    }
    if page_index is not None:
        candidate["page_index"] = page_index
    return candidate


def _extract_fastmoss_search_row_sales_text(row: Any) -> str:
    sales_cell = row.locator("td.ant-table-column-sort").first
    if sales_cell.count():
        sales_text = str(sales_cell.inner_text(timeout=3000) or "").strip()
        if sales_text:
            return sales_text

    fallback_cell = row.locator("td").nth(4)
    if fallback_cell.count():
        sales_text = str(fallback_cell.inner_text(timeout=3000) or "").strip()
        if sales_text:
            return sales_text

    raise FastMossStage2Error("FastMoss search row 7-day sales cell could not be parsed")


def _click_fastmoss_next_page(page: Any, *, step_delay_sec: float) -> bool:
    next_button = page.locator(".ant-pagination-next").first
    if not next_button.count():
        return False
    if str(next_button.get_attribute("aria-disabled") or "").strip().lower() == "true":
        return False

    previous_first_key = ""
    rows = _fastmoss_search_result_rows(page)
    if rows.count():
        previous_first_key = str(rows.nth(0).get_attribute("data-row-key") or "").strip()

    next_button.click()
    _sleep(step_delay_sec)
    if previous_first_key:
        try:
            page.wait_for_function(
                """
                ([selector, previousKey]) => {
                  const row = document.querySelector(selector);
                  return !!row && row.getAttribute('data-row-key') !== previousKey;
                }
                """,
                arg=["tr[data-row-key]", previous_first_key],
                timeout=15000,
            )
        except Exception:
            pass
    _wait_for_fastmoss_search_table(page)
    return True


def _extract_fastmoss_period_sales(page: Any, *, days: str, step_delay_sec: float) -> str:
    label_text = f"近{days}天"
    overview = _fastmoss_overview_locator(page)
    range_label = overview.locator(f"label:has-text('{label_text}')").first
    if not range_label.count():
        raise FastMossStage2Error(f"FastMoss overview range label '{label_text}' was not found")
    range_label.click()
    _sleep(step_delay_sec)
    return _extract_sales_value_from_overview_text(overview.inner_text(timeout=5000))


def _extract_fastmoss_yesterday_sales(page: Any, *, target_date: str, step_delay_sec: float) -> str:
    overview = _fastmoss_overview_locator(page)
    start_input = overview.locator("input[placeholder='开始日期']").first
    end_input = overview.locator("input[placeholder='结束日期']").first
    if not start_input.count() or not end_input.count():
        raise FastMossStage2Error("FastMoss overview date-range inputs were not found")

    start_input.click()
    _sleep(step_delay_sec)
    start_input.fill(target_date)
    start_input.press("Enter")
    _sleep(step_delay_sec)

    end_input.click()
    _sleep(step_delay_sec)
    end_input.fill(target_date)
    end_input.press("Enter")
    _sleep(step_delay_sec * 2)

    return _extract_sales_value_from_overview_text(overview.inner_text(timeout=5000))


def _fastmoss_overview_locator(page: Any):
    overview = page.locator("#overview").first
    if not overview.count():
        raise FastMossStage2Error("FastMoss overview section was not found on the detail page")
    return overview


def _extract_sales_value_from_overview_text(overview_text: str) -> str:
    match = re.search(r"概览\s*([^\s]+)\s*日均[^\s]+\s*销量", overview_text)
    if not match:
        raise FastMossStage2Error("FastMoss overview sales metric could not be parsed")
    return match.group(1).strip()


def _extract_fastmoss_product_title(page: Any) -> str:
    heading = page.locator("h1").first
    if heading.count():
        heading_text = str(heading.inner_text(timeout=3000) or "").strip()
        if heading_text:
            return heading_text
    title_parts = str(page.title() or "").split(" TikTok", 1)
    return title_parts[0].strip()


def _capture_fastmoss_detail_screenshot(
    page: Any,
    *,
    product_id: str,
    screenshot_dir: str = DEFAULT_FASTMOSS_DETAIL_SCREENSHOT_DIR,
) -> tuple[str, str, str]:
    output_dir = Path(screenshot_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / f"{product_id}-fastmoss-detail.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    return str(screenshot_path), screenshot_path.name, "image/png"


def _resolve_fastmoss_secret(direct_value: str | None, env_name: str | None) -> str:
    value = str(direct_value or "").strip()
    if value:
        return value
    name = str(env_name or "").strip()
    if not name:
        return ""
    return str(os.getenv(name, "")).strip()


def _normalize_fastmoss_product_id(value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("product_id is required")
    match = re.search(r"(\d{10,})", raw_value)
    if not match:
        raise ValueError("product_id must contain digits")
    return match.group(1)


def _parse_fastmoss_metric_number(value: str) -> float:
    raw_value = str(value or "").strip().replace(",", "")
    if not raw_value:
        return 0.0

    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kKmM万亿]?)", raw_value)
    if not match:
        raise FastMossStage2Error(f"FastMoss metric could not be converted to number: {value}")

    amount = float(match.group(1))
    unit = match.group(2)
    if unit in {"k", "K"}:
        return amount * 1_000
    if unit in {"m", "M"}:
        return amount * 1_000_000
    if unit == "万":
        return amount * 10_000
    if unit == "亿":
        return amount * 100_000_000
    return amount


def _yesterday_date_string(now: datetime | None = None) -> str:
    reference = now or datetime.now()
    return (reference.date() - timedelta(days=1)).isoformat()


def _sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(seconds)
