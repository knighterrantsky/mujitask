from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from automation_framework.browser import (
    BlockedContext,
    BlockedHandlingConfig,
    BlockedResolution,
    BlockerRule,
    BlockerRulesConfig,
)

from automation_business_scaffold.models import FastMossProductSalesSnapshot

from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page

DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL = "https://www.fastmoss.com/zh/account/center"
DEFAULT_FASTMOSS_SEARCH_URL = "https://www.fastmoss.com/zh/e-commerce/search"
DEFAULT_FASTMOSS_STEP_DELAY_SEC = 2.0
DEFAULT_FASTMOSS_LOGIN_SETTLE_SEC = 8.0
DEFAULT_FASTMOSS_DETAIL_SCREENSHOT_DIR = "runtime/downloads/fastmoss_detail_screenshots"
FASTMOSS_SESSION_RECOVERED_LOGIN_STATE = "relogged_in_after_session_loss"
FASTMOSS_OVERVIEW_LOADING_SELECTORS = (
    ".ant-spin-spinning",
    ".ant-spin-dot",
    ".ant-skeleton",
    ".ant-skeleton-active",
    ".anticon-loading",
)
FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC = 0.2
FASTMOSS_BLOCKER_DETECT_SELECTORS = (
    '[role="dialog"]',
    '[aria-modal="true"]',
    "dialog",
    ".ant-modal-root",
    ".ant-modal-wrap",
    ".ant-modal-mask",
    '[class*="modal"]',
    '[class*="overlay"]',
    '[class*="interstitial"]',
    '[data-testid*="modal"]',
    '[data-testid*="popup"]',
    '[data-testid*="overlay"]',
)
FASTMOSS_SECURITY_BLOCKER_DETECT_SELECTORS = (
    '[class*="captcha"]',
    '[id*="captcha"]',
    'iframe[src*="captcha"]',
    'iframe[src*="challenge"]',
    'iframe[src*="checkpoint"]',
)
FASTMOSS_BLOCKER_DISMISS_SELECTORS = (
    '[aria-label*="close" i]',
    '[data-testid*="close" i]',
    '[class*="close" i]',
    '[id*="close" i]',
    ".ant-modal-close",
    ".ant-modal-close-x",
)
FASTMOSS_BLOCKER_DISMISS_KEYWORDS = (
    "close",
    "dismiss",
    "cancel",
    "skip",
    "not now",
    "no thanks",
    "got it",
    "later",
    "关闭",
    "取消",
    "知道了",
    "稍后",
    "以后再说",
)
FASTMOSS_SECURITY_BLOCKER_KEYWORDS = (
    "captcha",
    "verify",
    "security",
    "challenge",
    "checkpoint",
    "access denied",
    "blocked",
    "验证",
    "风控",
    "安全",
)
FASTMOSS_LOGIN_PROMPT_MARKERS = (
    "游客身份",
    "游客权限不足",
    "权限不足，请登录/注册",
    "登录/注册查看您的账户信息",
    "手机号登录/注册",
    "密码登录",
    "输入您的手机号码",
    "输入密码",
    "注册/登录",
)


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
    verify_login: bool = False,
) -> FastMossProductSalesSnapshot:
    fetch_started_at = time.time()
    normalized_product_id = _normalize_fastmoss_product_id(product_id)
    phone = _resolve_fastmoss_secret(fastmoss_phone, fastmoss_phone_env)
    password = _resolve_fastmoss_secret(fastmoss_password, fastmoss_password_env)
    detail_url = _build_fastmoss_detail_url(normalized_product_id)
    search_url = _build_fastmoss_search_url(normalized_product_id)
    _log_fastmoss_timing(product_id=normalized_product_id, phase="fetch_start")

    with open_automation_page(
        profile_ref=profile_ref,
        blocked_handling=_fastmoss_blocked_handling(),
        blocker_rules=_fastmoss_blocker_rules(),
    ) as browser_page:
        page = browser_page.page
        login_state = _resolve_fastmoss_login_state(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            verify_login=verify_login,
        )
        session_recovered = False
        _, recovered_during_open = _run_timed_fastmoss_action_with_relogin(
            step_name="open_detail",
            product_id=normalized_product_id,
            action=lambda: _open_fastmoss_detail_page(page, detail_url, step_delay_sec=step_delay_sec),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
        )
        session_recovered = session_recovered or recovered_during_open

        screenshot_path = ""
        screenshot_name = ""
        screenshot_mime = ""
        if capture_detail_screenshot:
            (
                (screenshot_path, screenshot_name, screenshot_mime),
                recovered_during_screenshot,
            ) = _run_timed_fastmoss_action_with_relogin(
                step_name="detail_screenshot",
                product_id=normalized_product_id,
                action=lambda: _capture_fastmoss_detail_screenshot(
                    page,
                    product_id=normalized_product_id,
                ),
                page=page,
                phone=phone,
                password=password,
                step_delay_sec=step_delay_sec,
                login_settle_sec=login_settle_sec,
                restore_after_relogin=lambda: _open_fastmoss_detail_page(
                    page,
                    detail_url,
                    step_delay_sec=step_delay_sec,
                ),
            )
            session_recovered = session_recovered or recovered_during_screenshot

        product_title, recovered_during_title = _run_timed_fastmoss_action_with_relogin(
            step_name="product_title",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_product_title(page),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_title
        fastmoss_price_amount, recovered_during_price = _run_timed_fastmoss_action_with_relogin(
            step_name="price_amount",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_price_amount(page),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_price
        sales_7d, recovered_during_sales_7d = _run_timed_fastmoss_action_with_relogin(
            step_name="sales_7d",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_period_sales(page, days="7", step_delay_sec=step_delay_sec),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_sales_7d
        sales_28d, recovered_during_sales_28d = _run_timed_fastmoss_action_with_relogin(
            step_name="sales_28d",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_period_sales(page, days="28", step_delay_sec=step_delay_sec),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_sales_28d
        sales_90d, recovered_during_sales_90d = _run_timed_fastmoss_action_with_relogin(
            step_name="sales_90d",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_period_sales(page, days="90", step_delay_sec=step_delay_sec),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_sales_90d
        preferred_yesterday_date, fallback_yesterday_date = _preferred_fastmoss_yesterday_dates()
        yesterday_sales, recovered_during_yesterday = _run_timed_fastmoss_action_with_relogin(
            step_name="yesterday_sales",
            product_id=normalized_product_id,
            action=lambda: _extract_fastmoss_yesterday_sales(
                page,
                target_date=preferred_yesterday_date,
                fallback_target_date=fallback_yesterday_date,
                step_delay_sec=step_delay_sec,
            ),
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=lambda: _open_fastmoss_detail_page(
                page,
                detail_url,
                step_delay_sec=step_delay_sec,
            ),
        )
        session_recovered = session_recovered or recovered_during_yesterday

        if session_recovered:
            login_state = FASTMOSS_SESSION_RECOVERED_LOGIN_STATE
        snapshot = FastMossProductSalesSnapshot(
            product_id=normalized_product_id,
            search_url=search_url,
            detail_url=detail_url,
            product_title=product_title,
            login_state=login_state,
            fastmoss_price_amount=fastmoss_price_amount,
            yesterday_sales=yesterday_sales,
            sales_7d=sales_7d,
            sales_28d=sales_28d,
            sales_90d=sales_90d,
            detail_page_screenshot_local_path=screenshot_path,
            detail_page_screenshot_file_name=screenshot_name,
            detail_page_screenshot_mime_type=screenshot_mime,
        )
        _log_fastmoss_timing(
            product_id=normalized_product_id,
            phase="fetch_ready",
            elapsed_ms=int((time.time() - fetch_started_at) * 1000),
            login_state=login_state,
            session_recovered=session_recovered,
        )
        return snapshot


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _log_fastmoss_timing(*, product_id: str, phase: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "kind": "fastmoss_timing",
        "ts_ms": int(time.time() * 1000),
        "product_id": str(product_id or "").strip(),
        "phase": str(phase or "").strip(),
    }
    for key, value in extra.items():
        if value in (None, ""):
            continue
        payload[str(key)] = value
    print(_json_dumps(payload), flush=True)


def _run_timed_fastmoss_action_with_relogin(
    step_name: str,
    product_id: str,
    action: Callable[[], Any],
    *,
    page: Any,
    phone: str | None,
    password: str | None,
    step_delay_sec: float,
    login_settle_sec: float,
    restore_after_relogin: Callable[[], Any] | None = None,
) -> tuple[Any, bool]:
    _log_fastmoss_timing(product_id=product_id, phase=f"{step_name}_start")
    started_at = time.time()
    try:
        result, session_recovered = _run_fastmoss_action_with_relogin(
            action,
            page=page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=restore_after_relogin,
        )
    except Exception as exc:
        _log_fastmoss_timing(
            product_id=product_id,
            phase=f"{step_name}_failed",
            elapsed_ms=int((time.time() - started_at) * 1000),
            error=str(exc),
        )
        raise
    _log_fastmoss_timing(
        product_id=product_id,
        phase=f"{step_name}_ready",
        elapsed_ms=int((time.time() - started_at) * 1000),
        session_recovered=session_recovered,
    )
    return result, session_recovered


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
    verify_login: bool = True,
) -> dict[str, Any]:
    normalized_keyword = str(search_keyword or "").strip()
    if not normalized_keyword:
        raise ValueError("search_keyword is required")

    phone = _resolve_fastmoss_secret(fastmoss_phone, fastmoss_phone_env)
    password = _resolve_fastmoss_secret(fastmoss_password, fastmoss_password_env)

    with open_automation_page(
        profile_ref=profile_ref,
        blocked_handling=_fastmoss_blocked_handling(),
        blocker_rules=_fastmoss_blocker_rules(),
    ) as browser_page:
        page = browser_page.page
        login_state = _resolve_fastmoss_login_state(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            verify_login=verify_login,
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


def validate_fastmoss_login_via_browser(
    *,
    profile_ref: str | None = None,
    fastmoss_phone: str | None = None,
    fastmoss_password: str | None = None,
    fastmoss_phone_env: str | None = None,
    fastmoss_password_env: str | None = None,
    step_delay_sec: float = DEFAULT_FASTMOSS_STEP_DELAY_SEC,
    login_settle_sec: float = DEFAULT_FASTMOSS_LOGIN_SETTLE_SEC,
) -> dict[str, Any]:
    phone = _resolve_fastmoss_secret(fastmoss_phone, fastmoss_phone_env)
    password = _resolve_fastmoss_secret(fastmoss_password, fastmoss_password_env)

    with open_automation_page(
        profile_ref=profile_ref,
        blocked_handling=_fastmoss_blocked_handling(),
        blocker_rules=_fastmoss_blocker_rules(),
    ) as browser_page:
        page = browser_page.page
        login_state = _ensure_fastmoss_logged_in(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
        )
        return {
            "login_state": login_state,
            "profile_ref": str(
                getattr(browser_page, "profile_ref", "") or profile_ref or ""
            ).strip(),
            "provider_name": str(getattr(browser_page, "provider_name", "") or "").strip(),
            "target_key": str(getattr(browser_page, "target_key", "") or "").strip(),
        }


def _ensure_fastmoss_logged_in(
    page: Any,
    *,
    phone: str,
    password: str,
    step_delay_sec: float,
    login_settle_sec: float,
) -> str:
    _page_navigate(page, DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL)
    _sleep(step_delay_sec)
    if _is_fastmoss_account_logged_in(page):
        return "already_logged_in"

    if not phone or not password:
        raise FastMossStage2Error("FastMoss login required but phone/password were not provided")

    login_modal = _open_fastmoss_login_modal(page, step_delay_sec=step_delay_sec)
    login_modal = _switch_fastmoss_login_modal_to_password_mode(
        page,
        login_modal,
        step_delay_sec=step_delay_sec,
    )

    phone_input, password_input = _fastmoss_login_inputs(page, login_modal)
    if not phone_input.count() or not password_input.count():
        raise FastMossStage2Error("FastMoss phone/password login inputs were not found")

    _page_type_text(page, phone_input, phone)
    _sleep(step_delay_sec)
    _page_type_text(page, password_input, password)
    _sleep(step_delay_sec)
    submit_button = _find_fastmoss_text_button(login_modal, "注册/登录")
    if submit_button is None:
        submit_button = _find_fastmoss_text_button(page, "注册/登录")
    if submit_button is None:
        raise FastMossStage2Error("FastMoss login submit button was not found")
    _click_fastmoss_precise_target(page, submit_button)
    _sleep(login_settle_sec)

    _page_navigate(page, DEFAULT_FASTMOSS_ACCOUNT_CENTER_URL)
    _sleep(step_delay_sec)
    if not _is_fastmoss_account_logged_in(page):
        raise FastMossStage2Error("FastMoss login did not reach the account center")
    return "logged_in"


def _open_fastmoss_login_modal(page: Any, *, step_delay_sec: float) -> Any:
    existing_modal = _find_fastmoss_login_modal(page, require_inputs=False)
    if existing_modal is not None and _fastmoss_modal_has_login_inputs(existing_modal):
        return existing_modal

    login_button = _find_fastmoss_login_button(page, existing_modal)
    if login_button is None:
        raise FastMossStage2Error("FastMoss login modal did not appear")
    _click_fastmoss_precise_target(page, login_button)
    _sleep(step_delay_sec)

    login_modal = _wait_for_fastmoss_login_modal(page, require_inputs=False)
    if login_modal is None:
        raise FastMossStage2Error("FastMoss login modal did not appear")
    return login_modal


def _switch_fastmoss_login_modal_to_password_mode(
    page: Any,
    login_modal: Any,
    *,
    step_delay_sec: float,
) -> Any:
    phone_login_button = _find_fastmoss_text_button(login_modal, "手机号登录/注册")
    if phone_login_button is None:
        phone_login_button = _find_fastmoss_text_button(page, "手机号登录/注册")
    if phone_login_button is not None:
        _click_fastmoss_precise_target(page, phone_login_button)
        _sleep(step_delay_sec)
        refreshed_modal = _wait_for_fastmoss_login_modal(page, require_inputs=False)
        if refreshed_modal is not None:
            login_modal = refreshed_modal

    password_login_button = _find_fastmoss_text_button(login_modal, "密码登录")
    if password_login_button is None:
        password_login_button = _find_fastmoss_text_button(page, "密码登录")
    if password_login_button is not None:
        _click_fastmoss_precise_target(page, password_login_button)
        _sleep(step_delay_sec)

    refreshed_modal = _wait_for_fastmoss_login_modal(page, require_inputs=True)
    if refreshed_modal is not None:
        return refreshed_modal

    for label in ("手机号登录/注册", "密码登录"):
        button = _find_fastmoss_text_button(login_modal, label)
        if button is None:
            button = _find_fastmoss_text_button(page, label)
        if button is None:
            continue
        _click_fastmoss_precise_target(page, button)
        _sleep(step_delay_sec)

    refreshed_modal = _wait_for_fastmoss_login_modal(page, require_inputs=True)
    if refreshed_modal is not None:
        return refreshed_modal
    return login_modal


def _wait_for_fastmoss_login_modal(page: Any, *, require_inputs: bool) -> Any | None:
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        login_modal = _find_fastmoss_login_modal(page, require_inputs=require_inputs)
        if login_modal is not None:
            return login_modal
        _sleep(0.2)
    return None


def _find_fastmoss_login_modal(page: Any, *, require_inputs: bool) -> Any | None:
    modals = page.locator(".ant-modal-wrap")
    fallback_modal = None
    for index in range(modals.count()):
        modal = modals.nth(index)
        if _fastmoss_modal_has_login_inputs(modal):
            return modal
        if _is_fastmoss_login_modal_candidate(modal):
            fallback_modal = modal
    if require_inputs:
        return None
    return fallback_modal


def _is_fastmoss_login_modal_candidate(modal: Any) -> bool:
    normalized_text = _normalize_fastmoss_browser_text(_safe_fastmoss_locator_text(modal))
    if not normalized_text:
        return False
    modal_markers = FASTMOSS_LOGIN_PROMPT_MARKERS + (
        "登录/注册",
        "手机号登录/注册",
        "密码登录",
    )
    return any(_normalize_fastmoss_browser_text(marker) in normalized_text for marker in modal_markers)


def _fastmoss_modal_has_login_inputs(modal: Any) -> bool:
    phone_input = modal.locator("input[placeholder='输入您的手机号码']").first
    password_input = modal.locator("input[placeholder='输入密码']").first
    return bool(phone_input.count() and password_input.count())


def _find_fastmoss_login_button(page: Any, login_modal: Any | None) -> Any | None:
    for scope in (login_modal, page):
        if scope is None:
            continue
        button = _find_fastmoss_text_button(scope, "登录/注册")
        if button is not None:
            return button
    return None


def _find_fastmoss_text_button(scope: Any, text: str) -> Any | None:
    button = scope.get_by_text(text, exact=True).first
    if button.count():
        return button
    return None


def _fastmoss_login_inputs(page: Any, login_modal: Any) -> tuple[Any, Any]:
    for scope in (login_modal, page):
        phone_input = scope.locator("input[placeholder='输入您的手机号码']").first
        password_input = scope.locator("input[placeholder='输入密码']").first
        if phone_input.count() and password_input.count():
            return phone_input, password_input
    return (
        login_modal.locator("input[placeholder='输入您的手机号码']").first,
        login_modal.locator("input[placeholder='输入密码']").first,
    )


def _resolve_fastmoss_login_state(
    page: Any,
    *,
    phone: str,
    password: str,
    step_delay_sec: float,
    login_settle_sec: float,
    verify_login: bool,
) -> str:
    if not verify_login:
        return "skipped_login_verification"
    return _ensure_fastmoss_logged_in(
        page,
        phone=phone,
        password=password,
        step_delay_sec=step_delay_sec,
        login_settle_sec=login_settle_sec,
    )


def _run_fastmoss_action_with_relogin(
    action: Callable[[], Any],
    *,
    page: Any,
    phone: str,
    password: str,
    step_delay_sec: float,
    login_settle_sec: float,
    restore_after_relogin: Callable[[], None] | None = None,
) -> tuple[Any, bool]:
    recovered = False
    if _fastmoss_session_relogin_required(page):
        _recover_fastmoss_session(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=restore_after_relogin,
        )
        recovered = True

    try:
        return action(), recovered
    except Exception:
        if not _fastmoss_session_relogin_required(page):
            raise
        _recover_fastmoss_session(
            page,
            phone=phone,
            password=password,
            step_delay_sec=step_delay_sec,
            login_settle_sec=login_settle_sec,
            restore_after_relogin=restore_after_relogin,
        )
        return action(), True


def _recover_fastmoss_session(
    page: Any,
    *,
    phone: str,
    password: str,
    step_delay_sec: float,
    login_settle_sec: float,
    restore_after_relogin: Callable[[], None] | None = None,
) -> None:
    if not phone or not password:
        raise FastMossStage2Error(
            "FastMoss session expired during data capture and no credentials were provided for re-login"
        )
    _ensure_fastmoss_logged_in(
        page,
        phone=phone,
        password=password,
        step_delay_sec=step_delay_sec,
        login_settle_sec=login_settle_sec,
    )
    if restore_after_relogin is not None:
        restore_after_relogin()


def _fastmoss_blocked_handling() -> BlockedHandlingConfig:
    return BlockedHandlingConfig(handler=_handle_fastmoss_blocked_context)


def _handle_fastmoss_blocked_context(
    _automation_page: Any, event: BlockedContext
) -> BlockedResolution:
    if _is_fastmoss_login_prompt_blocker(event):
        return BlockedResolution.force_continue("allowed FastMoss login prompt/modal interaction")
    return BlockedResolution.resume_default()


def _fastmoss_blocker_rules() -> BlockerRulesConfig:
    return BlockerRulesConfig(
        inherit_defaults=False,
        domain_rules=[
            BlockerRule(
                domains=["fastmoss.com"],
                detect_selectors=list(FASTMOSS_SECURITY_BLOCKER_DETECT_SELECTORS),
                detect_keywords=list(FASTMOSS_SECURITY_BLOCKER_KEYWORDS),
                classify_as="security_challenge",
            ),
            BlockerRule(
                domains=["fastmoss.com"],
                detect_selectors=list(FASTMOSS_BLOCKER_DETECT_SELECTORS),
                dismiss_selectors=list(FASTMOSS_BLOCKER_DISMISS_SELECTORS),
                dismiss_keywords=list(FASTMOSS_BLOCKER_DISMISS_KEYWORDS),
                classify_as="dom_modal",
            ),
        ],
    )


def _is_fastmoss_login_prompt_blocker(event: BlockedContext) -> bool:
    page_url = str(getattr(event, "page_url", "") or "").lower()
    if "fastmoss.com" not in page_url:
        return False

    blocker_type = str(getattr(event, "blocker_type", "") or "").strip().lower()
    if blocker_type == "security_challenge":
        return False

    candidate_texts = _collect_fastmoss_blocked_text_candidates(event)
    return any(
        marker in text for text in candidate_texts for marker in FASTMOSS_LOGIN_PROMPT_MARKERS
    )


def _collect_fastmoss_blocked_text_candidates(event: BlockedContext) -> tuple[str, ...]:
    candidates: list[str] = []
    normalized_summary = _normalize_fastmoss_browser_text(getattr(event, "summary", ""))
    if normalized_summary:
        candidates.append(normalized_summary)

    dom_summary = getattr(event, "dom_summary", None)
    if isinstance(dom_summary, dict):
        dialogs = dom_summary.get("dialogs")
        if isinstance(dialogs, list):
            for item in dialogs:
                if not isinstance(item, dict):
                    continue
                normalized_dialog_text = _normalize_fastmoss_browser_text(item.get("text", ""))
                if normalized_dialog_text:
                    candidates.append(normalized_dialog_text)
        normalized_body_text = _normalize_fastmoss_browser_text(
            dom_summary.get("body_text_excerpt", "")
        )
        if normalized_body_text:
            candidates.append(normalized_body_text)

    deduped: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return tuple(deduped)


def _fastmoss_session_relogin_required(page: Any) -> bool:
    normalized_body_text = _normalize_fastmoss_browser_text(_safe_fastmoss_body_text(page))
    if not normalized_body_text:
        return False
    return any(
        _normalize_fastmoss_browser_text(marker) in normalized_body_text
        for marker in FASTMOSS_LOGIN_PROMPT_MARKERS
    )


def _is_fastmoss_account_logged_in(page: Any) -> bool:
    body_text = page.locator("body").inner_text(timeout=5000)
    if "账号ID：" in body_text or "会员有效期至" in body_text:
        return True
    if (
        "游客身份" in body_text
        or "游客权限不足" in body_text
        or "权限不足，请登录/注册" in body_text
        or "登录/注册查看您的账户信息" in body_text
    ):
        return False
    return False


def _search_fastmoss_product_detail_url(
    page: Any,
    product_id: str,
    *,
    step_delay_sec: float,
) -> tuple[str, str]:
    _page_navigate(page, DEFAULT_FASTMOSS_SEARCH_URL)
    _sleep(step_delay_sec)

    search_input = page.locator("input[placeholder='商品搜索']").first
    if not search_input.count():
        raise FastMossStage2Error("FastMoss search input was not found")

    _page_type_text(page, search_input, product_id)
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
    _page_navigate(page, detail_url)
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
    _page_navigate(page, DEFAULT_FASTMOSS_SEARCH_URL)
    _sleep(step_delay_sec)

    search_input = page.locator("input[placeholder='商品搜索']").first
    if not search_input.count():
        raise FastMossStage2Error("FastMoss search input was not found")

    _page_type_text(page, search_input, search_keyword)
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
        _page_click(page, sort_header)
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


def _extract_fastmoss_search_page_candidates(
    rows: Any, *, search_keyword: str
) -> list[dict[str, Any]]:
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
    raw_product_id = (
        str(row.get_attribute("data-row-key") or "").strip()
        or str(
            row.locator("a[href*='/zh/e-commerce/detail/']").first.get_attribute("href") or ""
        ).strip()
    )
    try:
        product_id = _normalize_fastmoss_product_id(raw_product_id)
    except ValueError:
        return None

    detail_link = row.locator("a[href*='/zh/e-commerce/detail/']").first
    detail_href = str(detail_link.get_attribute("href") or "").strip()
    detail_url = (
        urljoin(DEFAULT_FASTMOSS_SEARCH_URL, detail_href)
        if detail_href
        else _build_fastmoss_detail_url(product_id)
    )
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

    _page_click(page, next_button)
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
    previous_overview_text = _safe_fastmoss_overview_text(overview)
    already_on_target_range = label_text in "".join(previous_overview_text.split())
    require_refresh = not _is_fastmoss_range_label_selected(range_label)
    selected_after_click = False
    if require_refresh:
        _page_click(page, range_label)
        _sleep(step_delay_sec)
        selected_after_click = _wait_for_fastmoss_range_label_selected(
            range_label,
            timeout_sec=max(step_delay_sec, 0.4) + 2.0,
        )
    return _wait_for_fastmoss_overview_sales_refresh(
        overview,
        previous_text=previous_overview_text,
        min_wait_sec=max(step_delay_sec, 0.4),
        require_change=require_refresh and not selected_after_click and not already_on_target_range,
    )


def _extract_fastmoss_yesterday_sales(
    page: Any,
    *,
    target_date: str,
    fallback_target_date: str | None = None,
    step_delay_sec: float,
) -> str:
    overview = _fastmoss_overview_locator(page)
    start_input = overview.locator("input[placeholder='开始日期']").first
    end_input = overview.locator("input[placeholder='结束日期']").first
    if not start_input.count() or not end_input.count():
        raise FastMossStage2Error("FastMoss overview date-range inputs were not found")
    previous_overview_text = _safe_fastmoss_overview_text(overview)

    candidate_dates = [target_date]
    normalized_fallback_date = str(fallback_target_date or "").strip()
    if normalized_fallback_date and normalized_fallback_date != target_date:
        candidate_dates.append(normalized_fallback_date)

    for candidate_date in candidate_dates:
        start_selected = _select_fastmoss_overview_date(
            page,
            overview,
            input_locator=start_input,
            target_date=candidate_date,
            step_delay_sec=step_delay_sec,
        )
        if not start_selected:
            continue

        end_selected = _select_fastmoss_overview_date(
            page,
            overview,
            input_locator=end_input,
            target_date=candidate_date,
            step_delay_sec=step_delay_sec,
        )
        if not end_selected:
            continue

        return _wait_for_fastmoss_overview_sales_refresh(
            overview,
            previous_text=previous_overview_text,
            min_wait_sec=max(step_delay_sec * 2, 0.8),
            require_change=True,
        )

    return "-1"


def _select_fastmoss_overview_date(
    page: Any,
    overview: Any,
    *,
    input_locator: Any,
    target_date: str,
    step_delay_sec: float,
) -> bool:
    _click_fastmoss_precise_target(page, input_locator)
    _sleep(step_delay_sec)

    picker = _visible_fastmoss_datepicker(page, overview=overview)
    if not _navigate_fastmoss_datepicker_to_month(
        page,
        picker,
        target_date=target_date,
        step_delay_sec=step_delay_sec,
    ):
        return False
    cell = _find_fastmoss_date_cell(picker, target_date=target_date)
    if cell is None:
        return False

    cell_inner = cell.locator(".ant-picker-cell-inner").first
    _click_fastmoss_precise_target(page, cell_inner if cell_inner.count() else cell)
    _sleep(step_delay_sec)
    _wait_for_fastmoss_date_value(input_locator, target_date)
    return True


def _find_fastmoss_date_cell(picker: Any, *, target_date: str) -> Any | None:
    candidate_groups = [
        picker.locator(f".ant-picker-cell.ant-picker-cell-in-view[title='{target_date}']"),
        picker.locator(f".ant-picker-cell[title='{target_date}']"),
    ]
    for group in candidate_groups:
        for index in range(group.count()):
            cell = group.nth(index)
            class_name = str(cell.get_attribute("class") or "")
            if "ant-picker-cell-disabled" in class_name:
                continue
            return cell
    return None


def _navigate_fastmoss_datepicker_to_month(
    page: Any,
    picker: Any,
    *,
    target_date: str,
    step_delay_sec: float,
    max_steps: int = 12,
) -> bool:
    target_month_key = _fastmoss_month_key_from_date(target_date)
    for _ in range(max_steps):
        visible_month_keys = _visible_fastmoss_datepicker_month_keys(picker)
        if not visible_month_keys:
            return False
        if target_month_key in visible_month_keys:
            return True

        if target_month_key > max(visible_month_keys):
            button = _fastmoss_datepicker_nav_button(picker, direction="next")
        else:
            button = _fastmoss_datepicker_nav_button(picker, direction="prev")
        if button is None:
            return False
        _click_fastmoss_precise_target(page, button)
        _sleep(step_delay_sec)
    return False


def _visible_fastmoss_datepicker_month_keys(picker: Any) -> list[int]:
    header_views = picker.locator(".ant-picker-header-view")
    month_keys: list[int] = []
    for index in range(header_views.count()):
        header_view = header_views.nth(index)
        header_text = str(header_view.inner_text(timeout=1000) or "").strip()
        month_key = _fastmoss_month_key_from_header_text(header_text)
        if month_key is not None:
            month_keys.append(month_key)
    return month_keys


def _fastmoss_month_key_from_date(date_value: str) -> int:
    normalized_value = str(date_value or "").strip()
    target = datetime.strptime(normalized_value, "%Y-%m-%d")
    return target.year * 12 + target.month


def _fastmoss_month_key_from_header_text(header_text: str) -> int | None:
    text = "".join(str(header_text or "").split())
    for pattern in (
        r"(?P<year>\d{4})年(?P<month>\d{1,2})月",
        r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        return int(match.group("year")) * 12 + int(match.group("month"))
    return None


def _fastmoss_datepicker_nav_button(picker: Any, *, direction: str) -> Any | None:
    selector = (
        ".ant-picker-header-next-btn" if direction == "next" else ".ant-picker-header-prev-btn"
    )
    locator = picker.locator(selector)
    if not locator.count():
        return None
    return locator.last if direction == "next" else locator.first


def _visible_fastmoss_datepicker(page: Any, *, overview: Any) -> Any:
    pickers = [
        page.locator(".ant-picker-dropdown:not(.ant-picker-dropdown-hidden)").last,
        overview.locator(".ant-picker-dropdown:not(.ant-picker-dropdown-hidden)").last,
        page.locator(".ant-picker-dropdown").last,
        overview.locator(".ant-picker-dropdown").last,
    ]
    for picker in pickers:
        if not picker.count():
            continue
        try:
            picker.wait_for(state="visible", timeout=3000)
        except Exception:
            continue
        return picker
    raise FastMossStage2Error("FastMoss date picker dropdown did not appear")


def _click_fastmoss_precise_target(page: Any, target: Any, *, timeout_ms: int = 5000) -> None:
    click = getattr(target, "click", None)
    if callable(click):
        try:
            click(timeout=timeout_ms)
            return
        except TypeError:
            try:
                click()
                return
            except Exception:
                pass
        except Exception:
            pass
    _page_click(page, target)


def _wait_for_fastmoss_date_value(input_locator: Any, expected_value: str) -> None:
    deadline = time.time() + 5.0
    last_value = ""
    while time.time() < deadline:
        try:
            last_value = str(input_locator.input_value(timeout=500) or "").strip()
        except Exception:
            last_value = ""
        if last_value == expected_value:
            return
        time.sleep(0.1)
    raise FastMossStage2Error(
        f"FastMoss date input did not update to '{expected_value}' (current='{last_value}')"
    )


def _wait_for_fastmoss_overview_sales_refresh(
    overview: Any,
    *,
    previous_text: str,
    min_wait_sec: float,
    require_change: bool,
    timeout_sec: float = 12.0,
) -> str:
    start_time = time.time()
    deadline = start_time + timeout_sec
    stable_value = ""
    stable_count = 0
    last_text = previous_text
    poll_count = 0
    loading_seen = False
    loading_clear_streak = 0
    loading_probe_polls = _fastmoss_wait_poll_count(
        max(min_wait_sec, 0.8 if require_change else min_wait_sec),
    )

    while time.time() < deadline:
        poll_count += 1
        current_text = _safe_fastmoss_overview_text(overview)
        is_loading = _fastmoss_overview_has_loading(overview)
        if is_loading:
            loading_seen = True
            loading_clear_streak = 0
            stable_value = ""
            stable_count = 0
            last_text = current_text
            time.sleep(FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC)
            continue

        if loading_seen:
            loading_clear_streak += 1

        try:
            current_value = _extract_sales_value_from_overview_text(current_text)
        except FastMossStage2Error:
            time.sleep(FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC)
            continue

        if current_value == stable_value:
            stable_count += 1
        else:
            stable_value = current_value
            stable_count = 1

        waited_long_enough = (time.time() - start_time) >= min_wait_sec
        text_changed = bool(current_text) and current_text != previous_text
        loading_probe_finished = loading_seen or poll_count >= loading_probe_polls
        loading_cleared = not loading_seen or loading_clear_streak >= 2
        if (
            waited_long_enough
            and loading_probe_finished
            and loading_cleared
            and stable_count >= 2
            and (text_changed or not require_change)
        ):
            return current_value

        last_text = current_text
        time.sleep(FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC)

    if not require_change and stable_value:
        return stable_value

    still_loading = _fastmoss_overview_has_loading(overview)
    raise FastMossStage2Error(
        "FastMoss overview sales metric did not refresh after the range/date switch "
        f"(text_changed={last_text != previous_text}, loading_seen={loading_seen}, still_loading={still_loading})"
    )


def _safe_fastmoss_overview_text(overview: Any) -> str:
    try:
        return str(overview.inner_text(timeout=5000) or "").strip()
    except Exception:
        return ""


def _safe_fastmoss_locator_text(locator: Any) -> str:
    try:
        return str(locator.inner_text(timeout=2000) or "").strip()
    except Exception:
        return ""


def _is_fastmoss_range_label_selected(range_label: Any) -> bool:
    aria_checked = str(range_label.get_attribute("aria-checked") or "").strip().lower()
    if aria_checked == "true":
        return True
    class_name = str(range_label.get_attribute("class") or "").strip().lower()
    return any(token in class_name for token in ("checked", "active", "selected"))


def _wait_for_fastmoss_range_label_selected(range_label: Any, *, timeout_sec: float = 4.0) -> bool:
    deadline = time.time() + max(timeout_sec, 0.2)
    while time.time() < deadline:
        if _is_fastmoss_range_label_selected(range_label):
            return True
        time.sleep(FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC)
    return _is_fastmoss_range_label_selected(range_label)


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


def _fastmoss_overview_has_loading(overview: Any) -> bool:
    for selector in FASTMOSS_OVERVIEW_LOADING_SELECTORS:
        try:
            candidates = overview.locator(selector)
        except Exception:
            continue
        count = candidates.count()
        for index in range(count):
            candidate = candidates.nth(index)
            try:
                if candidate.is_visible():
                    return True
            except Exception:
                class_name = str(candidate.get_attribute("class") or "").strip().lower()
                if class_name and "hidden" not in class_name:
                    return True
    return False


def _fastmoss_wait_poll_count(target_sec: float) -> int:
    normalized_target = max(target_sec, FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC * 2)
    return max(2, int((normalized_target / FASTMOSS_OVERVIEW_POLL_INTERVAL_SEC) + 0.999))


def _extract_fastmoss_product_title(page: Any) -> str:
    heading = page.locator("h1").first
    if heading.count():
        heading_text = str(heading.inner_text(timeout=3000) or "").strip()
        if heading_text:
            return heading_text
    title_parts = str(page.title() or "").split(" TikTok", 1)
    return title_parts[0].strip()


def _extract_fastmoss_price_amount(page: Any, *, timeout_sec: float = 6.0) -> str:
    deadline = time.time() + max(timeout_sec, 0.2)
    while time.time() < deadline:
        page_text = _safe_fastmoss_body_text(page)
        price_amount = _extract_fastmoss_price_amount_from_text(page_text)
        if price_amount:
            return price_amount
        time.sleep(0.2)

    raise FastMossStage2Error("FastMoss product price could not be parsed from detail page")


def _extract_fastmoss_price_amount_from_text(page_text: str) -> str:
    lines = [line.strip() for line in str(page_text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        inline_amount = _extract_fastmoss_price_amount_from_line(line)
        if inline_amount:
            return inline_amount
        if not _is_fastmoss_price_label(line):
            continue

        for candidate in lines[index + 1 : min(len(lines), index + 5)]:
            if _is_fastmoss_price_section_boundary(candidate):
                break
            price_amount = _normalize_fastmoss_price_amount(candidate)
            if price_amount:
                return price_amount
    return ""


def _extract_fastmoss_price_amount_from_line(line: str) -> str:
    match = re.search(r"(?:价格|price)\s*[:：]\s*([^\s]+)", line, flags=re.IGNORECASE)
    if not match:
        return ""
    return _normalize_fastmoss_price_amount(match.group(1))


def _is_fastmoss_price_label(line: str) -> bool:
    normalized = "".join(str(line or "").split()).lower()
    return normalized in {"价格", "价格：", "价格:", "price", "price:", "price："}


def _is_fastmoss_price_section_boundary(line: str) -> bool:
    normalized = "".join(str(line or "").split()).lower()
    if not normalized:
        return True
    return any(
        token in normalized
        for token in (
            "趋势",
            "运费",
            "佣金",
            "达人",
            "销量",
            "销售额",
            "利润",
            "利润率",
            "预估上架日期",
            "商品信息",
        )
    )


def _safe_fastmoss_body_text(page: Any) -> str:
    try:
        return str(page.locator("body").first.inner_text(timeout=5000) or "").strip()
    except Exception:
        return ""


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


def _normalize_fastmoss_price_amount(value: str) -> str:
    raw_value = str(value or "").strip().replace(",", "")
    if not raw_value:
        return ""

    match = re.search(r"(-?\d+(?:\.\d+)?)", raw_value)
    if not match:
        return ""
    return match.group(1)


def _yesterday_date_string(now: datetime | None = None) -> str:
    reference = now or datetime.now()
    return (reference.date() - timedelta(days=1)).isoformat()


def _day_before_yesterday_date_string(now: datetime | None = None) -> str:
    reference = now or datetime.now()
    return (reference.date() - timedelta(days=2)).isoformat()


def _preferred_fastmoss_yesterday_dates(now: datetime | None = None) -> tuple[str, str]:
    reference = now or datetime.now()
    return (
        _yesterday_date_string(reference),
        _day_before_yesterday_date_string(reference),
    )


def _sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(seconds)


def _normalize_fastmoss_browser_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _is_automation_page(page: Any) -> bool:
    return bool(getattr(page, "humanize", False)) and hasattr(page, "raw_page")


def _page_navigate(page: Any, url: str) -> None:
    if _is_automation_page(page):
        page.navigate(url)
        return
    page.goto(url, wait_until="domcontentloaded", timeout=60000)


def _page_click(page: Any, target: Any) -> None:
    if _is_automation_page(page):
        page.click(target)
        return
    target.click()


def _page_type_text(page: Any, target: Any, text: str) -> None:
    if _is_automation_page(page):
        page.type_text(target, text)
        return
    target.fill(text)
