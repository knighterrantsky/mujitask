from __future__ import annotations

import hashlib
import os
import base64
import re
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import requests
from PIL import Image

from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    attach_fastmoss_cookie_cache,
    build_fastmoss_cookie_cache_context,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

HANDLER_CODE = "fastmoss_security_browser_resolve"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]

FASTMOSS_PRODUCT_SEARCH_ENDPOINT = "/api/goods/V2/search"
FASTMOSS_SECURITY_VERIFICATION_CODES = {"MSG_SAFE_0001"}
DEFAULT_FASTMOSS_BROWSER_TIMEOUT_MS = 45_000
DEFAULT_FASTMOSS_SLIDER_CONFIRM_MS = 2_000
DEFAULT_FASTMOSS_SLIDER_APPEAR_TIMEOUT_MS = 8_000
DEFAULT_FASTMOSS_SLIDER_SETTLE_MS = 900
DEFAULT_FASTMOSS_SLIDER_ATTEMPTS = 3

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


def fastmoss_security_browser_resolve_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        search_request = _resolve_search_request(payload)
        fastmoss_settings = _resolve_fastmoss_settings(payload, search_request=search_request)
        inline_result = _inline_browser_result(payload)
        if inline_result:
            browser_result = _resolve_inline_browser_result(inline_result)
        else:
            browser_result = _resolve_fastmoss_security_with_browser(
                payload,
                search_request=search_request,
                fastmoss_settings=fastmoss_settings,
            )

        cookies = coerce_mapping_list(browser_result.get("cookies"))
        if not cookies:
            raise ValueError("FastMoss browser security resolve did not export cookies.")

        verification = coerce_mapping(browser_result.get("verification"))
        if verification.get("response_code") in FASTMOSS_SECURITY_VERIFICATION_CODES:
            raise FastMossHTTPError(
                "FastMoss search security verification is still required after browser fallback.",
                response_code=verification.get("response_code"),
                payload={"code": verification.get("response_code")},
                method="GET",
                path=FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                stage="browser_security.verify_search",
            )

        cache_status = _save_browser_cookies_to_cache(
            payload,
            fastmoss_settings=fastmoss_settings,
            cookies=cookies,
            verified_path=FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        )
    except FastMossHTTPError as exc:
        error_code = (
            "fastmoss_security_verification_required"
            if _is_fastmoss_security_error(exc)
            else "fastmoss_security_browser_resolve_http_failed"
        )
        error = build_error(
            error_type="security_verification" if _is_fastmoss_security_error(exc) else "browser_failure",
            error_code=error_code,
            message=str(exc),
            retryable=False,
            details=_redact_fastmoss_http_error(exc),
        )
        return failed_result(
            context,
            error=error,
            summary={"verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT, "resolved": False},
        )
    except Exception as exc:  # noqa: BLE001
        error = build_error(
            error_type="browser_failure",
            error_code="fastmoss_security_browser_resolve_failed",
            message=str(exc),
            retryable=False,
            details={"verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT},
        )
        return failed_result(
            context,
            error=error,
            summary={"verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT, "resolved": False},
        )

    slider_resolution = _redact_slider_resolution(coerce_mapping(browser_result.get("slider_resolution")))
    verification = coerce_mapping(browser_result.get("verification"))
    summary = {
        "resolved": True,
        "verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        "response_code": first_non_empty(verification.get("response_code"), "200"),
        "cookie_count": int(cache_status.get("cookie_count") or 0),
        "has_fd_tk": bool(cache_status.get("has_fd_tk")),
        "fd_tk_digest": str(cache_status.get("fd_tk_digest") or ""),
        "slider_attempted": bool(slider_resolution.get("attempted")),
        "slider_resolved": bool(slider_resolution.get("resolved")),
    }
    result = {
        "verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        "verification": compact_dict(
            {
                "verified": True,
                "verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                "response_code": first_non_empty(verification.get("response_code"), "200"),
                "ext_is_login": first_non_empty(verification.get("ext_is_login")),
                "total": verification.get("total"),
            }
        ),
        "cookie_cache": cache_status,
        "slider_resolution": slider_resolution,
        "login_cookie_bootstrap": coerce_mapping(browser_result.get("login_cookie_bootstrap")),
        "fallback_source_job_id": first_non_empty(payload.get("fallback_source_job_id")),
    }
    return success_result(context, summary=summary, result=result)


def _inline_browser_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("mock_fastmoss_security_browser_resolve", "browser_result", "mock_browser_result"):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate
    return {}


def _resolve_inline_browser_result(inline_result: Mapping[str, Any]) -> dict[str, Any]:
    response_code = first_non_empty(inline_result.get("response_code"), "200")
    return {
        "cookies": coerce_mapping_list(inline_result.get("cookies"))
        or coerce_mapping_list(inline_result.get("browser_cookies")),
        "verification": {
            "verified": response_code not in FASTMOSS_SECURITY_VERIFICATION_CODES,
            "verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
            "response_code": response_code,
            "ext_is_login": first_non_empty(inline_result.get("ext_is_login"), "1"),
            "total": inline_result.get("total"),
        },
        "slider_resolution": coerce_mapping(inline_result.get("slider_resolution"))
        or {
            "attempted": bool(inline_result.get("slider_attempted", False)),
            "resolved": True,
            "reason": first_non_empty(inline_result.get("slider_reason"), "mock_verified"),
            "attempts": [],
        },
    }


def _resolve_fastmoss_security_with_browser(
    payload: Mapping[str, Any],
    *,
    search_request: Mapping[str, Any],
    fastmoss_settings: Mapping[str, Any],
) -> dict[str, Any]:
    timeout_ms = _positive_int(
        first_non_empty(payload.get("browser_timeout_ms"), payload.get("fastmoss_browser_timeout_ms")),
        DEFAULT_FASTMOSS_BROWSER_TIMEOUT_MS,
    )
    profile_ref = first_non_empty(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        os.environ.get("FASTMOSS_BROWSER_PROFILE_REF"),
        os.environ.get("BROWSER_PROFILE_REF"),
    )
    provider_name = first_non_empty(
        payload.get("fastmoss_browser_provider_name"),
        payload.get("browser_provider_name"),
        os.environ.get("FASTMOSS_BROWSER_PROVIDER_NAME"),
        os.environ.get("BROWSER_PROVIDER_NAME"),
    )
    profile_id = first_non_empty(
        payload.get("fastmoss_browser_profile_id"),
        payload.get("browser_profile_id"),
        os.environ.get("FASTMOSS_BROWSER_PROFILE_ID"),
        os.environ.get("BROWSER_PROFILE_ID"),
    )
    if provider_name and profile_id:
        profile_ref = ""

    search_url = _build_fastmoss_search_page_url(search_request, fastmoss_settings=fastmoss_settings)
    login_cookie_bootstrap = _bootstrap_fastmoss_login_cookies(payload, fastmoss_settings=fastmoss_settings)
    with open_automation_page(
        profile_ref=profile_ref,
        workspace_id=_optional_int(
            first_non_empty(
                payload.get("fastmoss_browser_workspace_id"),
                payload.get("browser_workspace_id"),
                os.environ.get("FASTMOSS_BROWSER_WORKSPACE_ID"),
                os.environ.get("BROWSER_WORKSPACE_ID"),
            )
        ),
        profile_id=profile_id,
        provider_name=provider_name,
        headless=coerce_bool(payload.get("browser_headless"), default=False),
        force_open=coerce_bool(payload.get("browser_force_open"), default=False),
    ) as browser_session:
        imported_cookie_status = _import_fastmoss_browser_cookies(
            browser_session.raw_page,
            cookies=coerce_mapping_list(login_cookie_bootstrap.get("cookies")),
            base_url=str(fastmoss_settings["base_url"]),
        )
        _page_goto(browser_session.page, search_url, timeout_ms=timeout_ms)
        _safe_wait_for_timeout(browser_session.page, 1_000)
        slider_resolution = _try_resolve_fastmoss_slider_security_check(
            browser_session.page,
            raw_page=browser_session.raw_page,
            search_url=search_url,
            max_attempts=_positive_int(
                payload.get("fastmoss_slider_max_attempts"),
                DEFAULT_FASTMOSS_SLIDER_ATTEMPTS,
            ),
            appear_timeout_ms=_positive_int(
                payload.get("fastmoss_slider_appear_timeout_ms"),
                DEFAULT_FASTMOSS_SLIDER_APPEAR_TIMEOUT_MS,
            ),
            settle_ms=_positive_int(payload.get("fastmoss_slider_settle_ms"), DEFAULT_FASTMOSS_SLIDER_SETTLE_MS),
            confirm_ms=_positive_int(
                payload.get("fastmoss_slider_confirm_ms"),
                DEFAULT_FASTMOSS_SLIDER_CONFIRM_MS,
            ),
        )
        cookies = _export_fastmoss_browser_cookies(browser_session.raw_page, base_url=str(fastmoss_settings["base_url"]))

    verification = _verify_original_search_with_cookies(
        search_request,
        fastmoss_settings=fastmoss_settings,
        cookies=cookies,
    )
    return {
        "cookies": cookies,
        "verification": verification,
        "slider_resolution": slider_resolution,
        "login_cookie_bootstrap": {
            key: value
            for key, value in {
                **coerce_mapping(login_cookie_bootstrap.get("status")),
                "browser_imported_cookie_count": imported_cookie_status.get("imported_count"),
                "browser_cookie_import_status": imported_cookie_status.get("status"),
                "browser_cookie_import_reason": imported_cookie_status.get("reason"),
            }.items()
            if value not in ("", None, [], {})
        },
    }


def _bootstrap_fastmoss_login_cookies(
    payload: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
) -> dict[str, Any]:
    phone = first_non_empty(fastmoss_settings.get("phone"))
    password = first_non_empty(fastmoss_settings.get("password"))
    if not (phone and password):
        return {"cookies": [], "status": {"status": "missing_credentials"}}

    db_url = _runtime_db_url(payload, fastmoss_settings=fastmoss_settings)
    store = RuntimeStore(db_url=db_url) if db_url else None
    account_key = first_non_empty(
        fastmoss_settings.get("account_key"),
        fastmoss_settings.get("phone"),
        fastmoss_settings.get("phone_env"),
        "default",
    )
    region = first_non_empty(fastmoss_settings.get("region"), "US")
    ttl_seconds = _non_negative_float(
        fastmoss_settings.get("cookie_cache_ttl_seconds"),
        DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    )
    cache_status: dict[str, Any] = {"enabled": False, "reason": "missing_store"}
    session = FastMossHTTPSession(
        phone=phone,
        password=password,
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=region,
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(fastmoss_settings, provider="fastmoss"),
    )
    with session:
        if store is not None:
            cache_status = attach_fastmoss_cookie_cache(
                session,
                store=store,
                account_key=account_key,
                region=region,
                namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
                ttl_seconds=ttl_seconds,
            )
        cookies = session.export_cookies()
        if cookies:
            try:
                session.ensure_logged_in()
            except FastMossHTTPError:
                if store is not None:
                    cache_status = attach_fastmoss_cookie_cache(
                        session,
                        store=store,
                        account_key=account_key,
                        region=region,
                        namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
                        force_refresh=True,
                        ttl_seconds=ttl_seconds,
                    )
                else:
                    session.login()
        elif store is not None:
            cache_status = attach_fastmoss_cookie_cache(
                session,
                store=store,
                account_key=account_key,
                region=region,
                namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
                force_refresh=True,
                ttl_seconds=ttl_seconds,
            )
        else:
            session.login()

        cookies = session.export_cookies()
    return {
        "cookies": cookies,
        "status": {
            "status": first_non_empty(cache_status.get("status"), "login_refreshed"),
            "cache_enabled": bool(cache_status.get("enabled")),
            "cookie_count": len(cookies),
            "has_fd_tk": any(cookie.get("name") == "fd_tk" for cookie in cookies),
            "fd_tk_digest": _fd_tk_digest_from_cookies(cookies),
        },
    }


def _import_fastmoss_browser_cookies(
    raw_page: Any,
    *,
    cookies: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    if not cookies:
        return {"status": "skipped", "reason": "no_cookies", "imported_count": 0}
    context = getattr(raw_page, "context", None)
    add_cookies = getattr(context, "add_cookies", None)
    if not callable(add_cookies):
        return {"status": "skipped", "reason": "missing_add_cookies", "imported_count": 0}

    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        name = first_non_empty(cookie.get("name"))
        value = coerce_str(cookie.get("value"))
        domain = first_non_empty(cookie.get("domain"))
        if not (name and value):
            continue
        record: dict[str, Any] = {
            "name": name,
            "value": value,
            "path": first_non_empty(cookie.get("path"), "/"),
            "secure": bool(cookie.get("secure")),
        }
        if domain:
            record["domain"] = domain
        else:
            record["url"] = str(base_url).rstrip("/") or "https://www.fastmoss.com"
        expires = _optional_float(cookie.get("expires"))
        if expires and expires > 0:
            record["expires"] = expires
        normalized.append(record)

    if not normalized:
        return {"status": "skipped", "reason": "no_valid_cookies", "imported_count": 0}
    add_cookies(normalized)
    return {"status": "imported", "imported_count": len(normalized)}


def _verify_original_search_with_cookies(
    search_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
) -> dict[str, Any]:
    params = _resolve_original_search_params(search_request, fastmoss_settings=fastmoss_settings)
    session = FastMossHTTPSession(
        phone=first_non_empty(fastmoss_settings.get("phone")),
        password=first_non_empty(fastmoss_settings.get("password")),
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=params["region"],
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(fastmoss_settings, provider="fastmoss"),
    )
    with session:
        session.replace_browser_cookies(cookies)
        raw = session.search_products(
            params["keyword"],
            page=params["page"],
            pagesize=params["page_size"],
            region=params["region"],
            order=params["order"],
            extra_params=params["extra_params"],
            check_auth=False,
        )
    data = coerce_mapping(raw.get("data"))
    ext = coerce_mapping(raw.get("ext"))
    return compact_dict(
        {
            "verified": True,
            "verified_path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
            "response_code": first_non_empty(raw.get("code"), "200"),
            "ext_is_login": first_non_empty(ext.get("is_login")),
            "total": data.get("total") or data.get("total_cnt"),
        }
    )


def _save_browser_cookies_to_cache(
    payload: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
    verified_path: str,
) -> dict[str, Any]:
    db_url = _runtime_db_url(payload, fastmoss_settings=fastmoss_settings)
    if not db_url:
        raise ValueError("execution_control_db_url is required to persist FastMoss browser cookies.")

    account_key = first_non_empty(
        fastmoss_settings.get("account_key"),
        fastmoss_settings.get("phone"),
        fastmoss_settings.get("phone_env"),
        "default",
    )
    context = build_fastmoss_cookie_cache_context(
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        account_key=account_key,
        region=first_non_empty(fastmoss_settings.get("region"), "US"),
        namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
    )
    if not context.get("enabled"):
        raise ValueError(f"FastMoss cookie cache context is disabled: {context.get('reason')}")

    snapshot = _cookie_snapshot_from_browser_cookies(cookies)
    ttl_seconds = _non_negative_float(
        fastmoss_settings.get("cookie_cache_ttl_seconds"),
        DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    )
    store = RuntimeStore(db_url=db_url)
    saved = store.save_fastmoss_cookie_cache(
        cache_key=str(context["cache_key"]),
        namespace=str(context.get("namespace") or ""),
        account_key=str(context.get("account_key") or ""),
        base_url=str(context.get("base_url") or ""),
        region=str(context.get("region") or ""),
        cookies=cookies,
        cookie_count=int(snapshot["cookie_count"]),
        has_fd_tk=bool(snapshot["has_fd_tk"]),
        fd_tk_digest=str(snapshot["fd_tk_digest"]),
        expires_at=_resolve_browser_cookie_expires_at(cookies, ttl_seconds=ttl_seconds),
        last_login_at=time.time(),
    )
    return {
        "enabled": True,
        "cache_key": str(context["cache_key"]),
        "status": "saved",
        "verified_path": verified_path,
        **_redacted_cache_status(saved),
    }


def _resolve_search_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    search_request = coerce_mapping(payload.get("search_request"))
    if not search_request:
        search_request = {
            "search_query": first_non_empty(payload.get("search_query"), payload.get("keyword"), payload.get("words")),
            "keyword": first_non_empty(payload.get("keyword"), payload.get("search_query"), payload.get("words")),
            "region": first_non_empty(payload.get("region"), "US"),
            "pagination": coerce_mapping(payload.get("pagination")),
            "sort": coerce_mapping(payload.get("sort")),
            "extra_params": coerce_mapping(payload.get("extra_params")),
        }
    if not first_non_empty(search_request.get("search_query"), search_request.get("keyword"), search_request.get("words")):
        raise ValueError("FastMoss browser security resolve requires original search keyword.")
    return search_request


def _resolve_fastmoss_settings(payload: Mapping[str, Any], *, search_request: Mapping[str, Any]) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    settings = {
        **coerce_mapping(search_request.get("fastmoss")),
        **coerce_mapping(request_payload.get("fastmoss")),
        **coerce_mapping(payload.get("fastmoss")),
    }
    phone_env = first_non_empty(
        settings.get("phone_env"),
        settings.get("fastmoss_phone_env"),
        payload.get("fastmoss_phone_env"),
        request_payload.get("fastmoss_phone_env"),
    )
    password_env = first_non_empty(
        settings.get("password_env"),
        settings.get("fastmoss_password_env"),
        payload.get("fastmoss_password_env"),
        request_payload.get("fastmoss_password_env"),
    )
    return {
        **settings,
        "phone": first_non_empty(
            settings.get("phone"),
            payload.get("fastmoss_phone"),
            request_payload.get("fastmoss_phone"),
            _env_value(phone_env),
        ),
        "password": first_non_empty(
            settings.get("password"),
            payload.get("fastmoss_password"),
            request_payload.get("fastmoss_password"),
            _env_value(password_env),
        ),
        "phone_env": phone_env,
        "password_env": password_env,
        "base_url": first_non_empty(
            settings.get("base_url"),
            payload.get("fastmoss_base_url"),
            request_payload.get("fastmoss_base_url"),
            "https://www.fastmoss.com",
        ),
        "region": first_non_empty(search_request.get("region"), settings.get("region"), request_payload.get("region"), "US"),
        "timeout": settings.get("timeout", payload.get("fastmoss_timeout", request_payload.get("fastmoss_timeout", 30.0))),
        "execution_control_db_url": _runtime_db_url(payload, fastmoss_settings=settings),
        "cookie_cache_namespace": first_non_empty(settings.get("cookie_cache_namespace")),
        "cookie_cache_ttl_seconds": settings.get("cookie_cache_ttl_seconds"),
        "api_request_delay_min_seconds": first_non_empty(
            settings.get("api_request_delay_min_seconds"),
            payload.get("api_request_delay_min_seconds"),
            request_payload.get("api_request_delay_min_seconds"),
        ),
        "api_request_delay_max_seconds": first_non_empty(
            settings.get("api_request_delay_max_seconds"),
            payload.get("api_request_delay_max_seconds"),
            request_payload.get("api_request_delay_max_seconds"),
        ),
        "fastmoss_api_request_delay_min_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_min_seconds"),
            payload.get("fastmoss_api_request_delay_min_seconds"),
            request_payload.get("fastmoss_api_request_delay_min_seconds"),
        ),
        "fastmoss_api_request_delay_max_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_max_seconds"),
            payload.get("fastmoss_api_request_delay_max_seconds"),
            request_payload.get("fastmoss_api_request_delay_max_seconds"),
        ),
    }


def _resolve_original_search_params(
    search_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
) -> dict[str, Any]:
    pagination = coerce_mapping(search_request.get("pagination"))
    sort = coerce_mapping(search_request.get("sort"))
    filters = coerce_mapping(search_request.get("filters"))
    return {
        "keyword": first_non_empty(search_request.get("keyword"), search_request.get("search_query"), search_request.get("words")),
        "region": first_non_empty(search_request.get("region"), filters.get("region"), filters.get("country_code"), fastmoss_settings.get("region"), "US"),
        "page": _positive_int(first_non_empty(pagination.get("page"), search_request.get("page")), 1),
        "page_size": _positive_int(
            first_non_empty(pagination.get("page_size"), pagination.get("pagesize"), search_request.get("page_size")),
            10,
        ),
        "order": first_non_empty(sort.get("source_order"), search_request.get("source_order"), search_request.get("order"), "2,2"),
        "extra_params": coerce_mapping(search_request.get("extra_params")),
    }


def _build_fastmoss_search_page_url(
    search_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
) -> str:
    params = _resolve_original_search_params(search_request, fastmoss_settings=fastmoss_settings)
    query = urlencode({"region": params["region"], "page": params["page"], "words": params["keyword"]})
    return f"{str(fastmoss_settings['base_url']).rstrip('/')}/zh/e-commerce/search?{query}"


def _try_resolve_fastmoss_slider_security_check(
    page: Any,
    *,
    raw_page: Any | None = None,
    search_url: str,
    max_attempts: int,
    appear_timeout_ms: int,
    settle_ms: int,
    confirm_ms: int,
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


def _wait_for_fastmoss_slider_elements(page: Any, *, timeout_ms: int) -> tuple[Any | None, str, Any | None, str, Any | None, str]:
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    last: tuple[Any | None, str, Any | None, str, Any | None, str] = (None, "", None, "", None, "")
    while True:
        background_locator, background_selector = _first_visible_locator(page, FASTMOSS_SLIDER_BACKGROUND_SELECTORS)
        target_locator, target_selector = _first_visible_locator(page, FASTMOSS_SLIDER_TARGET_SELECTORS)
        handle_locator, handle_selector = _first_visible_locator(page, FASTMOSS_SLIDER_HANDLE_SELECTORS)
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


def _first_visible_locator(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 500) -> tuple[Any | None, str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            target = getattr(locator, "first", locator)
            if _locator_is_visible(target, timeout_ms=timeout_ms):
                return target, selector
        except Exception:
            continue
    return None, ""


def _locator_is_visible(locator: Any, *, timeout_ms: int = 500) -> bool:
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


def _click_first_visible_locator(page: Any, selectors: tuple[str, ...]) -> bool:
    locator, _selector = _first_visible_locator(page, selectors, timeout_ms=250)
    click = getattr(locator, "click", None)
    if not callable(click):
        return False
    try:
        click(timeout=1_000)
    except TypeError:
        click()
    return True


def _locator_image_bytes(locator: Any, *, page: Any | None = None, selector: str = "") -> bytes:
    resource = _locator_image_resource(locator, page=page, selector=selector)
    if resource:
        payload = _load_image_resource_bytes(resource)
        if payload:
            return payload
    return _locator_screenshot_bytes(locator)


def _locator_image_resource(locator: Any, *, page: Any | None = None, selector: str = "") -> str:
    payload = _locator_image_resource_payload(locator)
    if not payload and page is not None and selector:
        payload = _page_image_resource_payload(page, selector=selector)
    if not isinstance(payload, Mapping):
        return ""
    background_image = first_non_empty(payload.get("backgroundImage"))
    background_url = _extract_css_url(background_image)
    if background_url:
        return background_url
    return first_non_empty(payload.get("src"))


def _locator_image_resource_payload(locator: Any) -> dict[str, Any]:
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


def _page_image_resource_payload(page: Any, *, selector: str) -> dict[str, Any]:
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


def _extract_css_url(value: str) -> str:
    text = str(value or "").strip()
    if not text or text == "none":
        return ""
    matched = re.search(r"url\((['\"]?)(.*?)\1\)", text)
    return matched.group(2) if matched else ""


def _load_image_resource_bytes(resource: str) -> bytes:
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


def _locator_screenshot_bytes(locator: Any) -> bytes:
    screenshot = getattr(locator, "screenshot", None)
    if not callable(screenshot):
        return b""
    try:
        payload = screenshot(timeout=3_000)
    except TypeError:
        payload = screenshot()
    return payload if isinstance(payload, bytes) else b""


def _locator_bounding_box(locator: Any) -> dict[str, float]:
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


def _calculate_slider_drag_distance(
    *,
    slider_match: Any,
    background_box: Mapping[str, float],
    background_image_size: tuple[int, int],
    target_box: Mapping[str, float],
    handle_box: Mapping[str, float],
) -> float:
    target_left = float(getattr(slider_match, "target_x", 0))
    image_width = int(background_image_size[0] or 0)
    rendered_width = float(background_box.get("width") or 0)
    if image_width > 0 and rendered_width > 0:
        target_left = target_left * rendered_width / float(image_width)
    if target_box:
        current_left = float(target_box.get("x") or 0) - float(background_box.get("x") or 0)
    else:
        current_left = float(handle_box.get("x") or 0) - float(background_box.get("x") or 0)
    drag_distance = target_left - current_left
    return target_left if abs(drag_distance) < 1 else drag_distance


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    if not image_bytes:
        return (0, 0)
    try:
        import io

        with Image.open(io.BytesIO(image_bytes)) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return (0, 0)


def _drag_slider_handle(page: Any, *, handle_box: Mapping[str, float], drag_distance: float) -> None:
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        raise RuntimeError("FastMoss slider captcha requires page mouse support")
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


def _build_slider_captcha_provider() -> Any:
    from automation_framework.captcha import DdddOcrCaptchaProvider

    return DdddOcrCaptchaProvider()


def _page_goto(page: Any, url: str, *, timeout_ms: int) -> None:
    goto = getattr(page, "goto", None)
    if not callable(goto):
        return
    try:
        goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except TypeError:
        goto(url)


def _safe_wait_for_timeout(page: Any, timeout_ms: int) -> None:
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        wait(max(int(timeout_ms), 1))
        return
    time.sleep(max(float(timeout_ms), 1.0) / 1000.0)


def _export_fastmoss_browser_cookies(raw_page: Any, *, base_url: str) -> list[dict[str, Any]]:
    context = getattr(raw_page, "context", None)
    cookies_func = getattr(context, "cookies", None)
    if not callable(cookies_func):
        return []
    try:
        raw_cookies = cookies_func(base_url)
    except TypeError:
        raw_cookies = cookies_func()
    cookies: list[dict[str, Any]] = []
    for cookie in raw_cookies or []:
        record = coerce_mapping(cookie)
        domain = first_non_empty(record.get("domain"))
        if "fastmoss.com" not in domain.lstrip(".").lower():
            continue
        cookies.append(
            {
                "name": first_non_empty(record.get("name")),
                "value": coerce_str(record.get("value")),
                "domain": domain,
                "path": first_non_empty(record.get("path"), "/"),
                "expires": record.get("expires"),
                "secure": bool(record.get("secure")),
            }
        )
    return [cookie for cookie in cookies if cookie["name"]]


def _cookie_snapshot_from_browser_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    fd_tk_digest = ""
    for cookie in cookies:
        if cookie.get("name") == "fd_tk" and not fd_tk_digest:
            fd_tk_digest = _cookie_value_digest(str(cookie.get("value") or ""))
    return {
        "cookie_count": len(cookies),
        "has_fd_tk": bool(fd_tk_digest),
        "fd_tk_digest": fd_tk_digest,
    }


def _resolve_browser_cookie_expires_at(cookies: list[dict[str, Any]], *, ttl_seconds: float) -> float:
    now = time.time()
    candidates: list[float] = []
    for cookie in cookies:
        try:
            expires = float(cookie.get("expires"))
        except (TypeError, ValueError):
            continue
        if expires > now:
            candidates.append(expires)
    if candidates:
        return min(candidates)
    return now + max(float(ttl_seconds or DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS), 60.0)


def _redacted_cache_status(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cookie_count": int(record.get("cookie_count") or 0),
        "has_fd_tk": bool(record.get("has_fd_tk")),
        "fd_tk_digest": str(record.get("fd_tk_digest") or ""),
        "expires_at": float(record.get("expires_at") or 0.0),
        "updated_at": float(record.get("updated_at") or 0.0),
    }


def _redact_slider_resolution(slider_resolution: Mapping[str, Any]) -> dict[str, Any]:
    attempts = slider_resolution.get("attempts")
    safe_attempts: list[dict[str, Any]] = []
    if isinstance(attempts, list):
        for attempt in attempts:
            record = coerce_mapping(attempt)
            safe_attempts.append(
                compact_dict(
                    {
                        "attempt": record.get("attempt"),
                        "reason": record.get("reason"),
                        "drag_distance": record.get("drag_distance"),
                        "confidence": record.get("confidence"),
                        "confirmation_wait_ms": record.get("confirmation_wait_ms"),
                        "confirmation_popup_still_visible": record.get("confirmation_popup_still_visible"),
                    }
                )
            )
    return compact_dict(
        {
            "attempted": bool(slider_resolution.get("attempted")),
            "resolved": bool(slider_resolution.get("resolved")),
            "reason": first_non_empty(slider_resolution.get("reason")),
            "attempts": safe_attempts,
        }
    )


def _redact_fastmoss_http_error(exc: FastMossHTTPError) -> dict[str, Any]:
    payload = coerce_mapping(exc.payload)
    data = coerce_mapping(payload.get("data"))
    ext = coerce_mapping(payload.get("ext"))
    return compact_dict(
        {
            "message": exc.message,
            "status_code": exc.status_code,
            "response_code": exc.response_code,
            "stage": exc.stage,
            "method": exc.method,
            "path": exc.path,
            "data_id": data.get("id"),
            "ext_is_login": ext.get("is_login"),
        }
    )


def _is_fastmoss_security_error(exc: FastMossHTTPError) -> bool:
    return coerce_str(exc.response_code) in FASTMOSS_SECURITY_VERIFICATION_CODES


def _runtime_db_url(payload: Mapping[str, Any], *, fastmoss_settings: Mapping[str, Any]) -> str:
    request_payload = coerce_mapping(payload.get("request_payload"))
    return first_non_empty(
        payload.get("execution_control_db_url"),
        payload.get("db_url"),
        request_payload.get("execution_control_db_url"),
        request_payload.get("db_url"),
        fastmoss_settings.get("execution_control_db_url"),
        fastmoss_settings.get("db_url"),
    )


def _cookie_value_digest(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _fd_tk_digest_from_cookies(cookies: list[dict[str, Any]]) -> str:
    for cookie in cookies:
        if cookie.get("name") == "fd_tk":
            return _cookie_value_digest(str(cookie.get("value") or ""))
    return ""


def _env_value(name: str) -> str:
    return os.environ.get(name, "") if name else ""


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


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_security_browser_resolve_handler"]
