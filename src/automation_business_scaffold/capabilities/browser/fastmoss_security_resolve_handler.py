from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlencode, urljoin

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


def fastmoss_security_browser_resolve_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    browser_result: dict[str, Any] = {}
    verified_path = FASTMOSS_PRODUCT_SEARCH_ENDPOINT
    try:
        search_request = _resolve_search_request(payload)
        verification_request = _resolve_verification_request(payload, search_request=search_request)
        verified_path = first_non_empty(verification_request.get("path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT)
        fastmoss_settings = _resolve_fastmoss_settings(payload, search_request=search_request)
        inline_result = _inline_browser_result(payload)
        if inline_result:
            browser_result = _resolve_inline_browser_result(inline_result, verified_path=verified_path)
        else:
            browser_result = _resolve_fastmoss_security_with_browser(
                payload,
                search_request=search_request,
                verification_request=verification_request,
                fastmoss_settings=fastmoss_settings,
            )

        cookies = coerce_mapping_list(browser_result.get("cookies"))
        if not cookies:
            raise ValueError("FastMoss browser security resolve did not export cookies.")

        verification = coerce_mapping(browser_result.get("verification"))
        if verification.get("response_code") in FASTMOSS_SECURITY_VERIFICATION_CODES:
            exc = FastMossHTTPError(
                "FastMoss search security verification is still required after browser fallback.",
                response_code=verification.get("response_code"),
                payload={
                    "code": verification.get("response_code"),
                    "data": {"id": verification.get("data_id")},
                    "ext": {"is_login": verification.get("ext_is_login")},
                },
                method="GET",
                path=verified_path,
                stage="browser_security.verify_original_request",
            )
            error = build_error(
                error_type="security_verification",
                error_code="fastmoss_security_verification_required",
                message=str(exc),
                retryable=False,
                details=_redact_fastmoss_http_error(exc),
            )
            return failed_result(
                context,
                error=error,
                summary=_browser_resolve_failure_summary(browser_result, error_code="fastmoss_security_verification_required"),
                result=_browser_resolve_result_payload(payload, browser_result, resolved=False, error_details=error.details),
            )

        cache_status = _save_browser_cookies_to_cache(
            payload,
            fastmoss_settings=fastmoss_settings,
            cookies=cookies,
            verified_path=verified_path,
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
            summary=_browser_resolve_failure_summary(browser_result, error_code=error_code),
            result=_browser_resolve_result_payload(payload, browser_result, resolved=False, error_details=error.details),
        )
    except Exception as exc:  # noqa: BLE001
        error = build_error(
            error_type="browser_failure",
            error_code="fastmoss_security_browser_resolve_failed",
            message=str(exc),
            retryable=False,
            details={"verified_path": verified_path},
        )
        return failed_result(
            context,
            error=error,
            summary=_browser_resolve_failure_summary(browser_result, error_code="fastmoss_security_browser_resolve_failed"),
            result=_browser_resolve_result_payload(payload, browser_result, resolved=False, error_details=error.details),
        )

    slider_resolution = _redact_slider_resolution(coerce_mapping(browser_result.get("slider_resolution")))
    verification = coerce_mapping(browser_result.get("verification"))
    summary = {
        "resolved": True,
        "verified_path": verified_path,
        "response_code": first_non_empty(verification.get("response_code"), "200"),
        "cookie_count": int(cache_status.get("cookie_count") or 0),
        "has_fd_tk": bool(cache_status.get("has_fd_tk")),
        "fd_tk_digest": str(cache_status.get("fd_tk_digest") or ""),
        "slider_attempted": bool(slider_resolution.get("attempted")),
        "slider_resolved": bool(slider_resolution.get("resolved")),
    }
    result = {
        "verified_path": verified_path,
        "verification": compact_dict(
            {
                "verified": True,
                "verified_path": verified_path,
                "response_code": first_non_empty(verification.get("response_code"), "200"),
                "ext_is_login": first_non_empty(verification.get("ext_is_login")),
                "total": verification.get("total"),
            }
        ),
        "cookie_cache": cache_status,
        "slider_resolution": slider_resolution,
        "slider_captcha_audit_artifact_refs": coerce_mapping_list(browser_result.get("slider_captcha_audit_artifact_refs")),
        "browser_diagnostic_artifact_refs": coerce_mapping_list(browser_result.get("browser_diagnostic_artifact_refs")),
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


def _resolve_inline_browser_result(inline_result: Mapping[str, Any], *, verified_path: str) -> dict[str, Any]:
    response_code = first_non_empty(inline_result.get("response_code"), "200")
    return {
        "cookies": coerce_mapping_list(inline_result.get("cookies"))
        or coerce_mapping_list(inline_result.get("browser_cookies")),
        "verification": {
            "verified": response_code not in FASTMOSS_SECURITY_VERIFICATION_CODES,
            "verified_path": first_non_empty(inline_result.get("verified_path"), verified_path),
            "response_code": response_code,
            "ext_is_login": first_non_empty(inline_result.get("ext_is_login"), "1"),
            "data_id": first_non_empty(inline_result.get("data_id")),
            "total": inline_result.get("total"),
        },
        "slider_resolution": coerce_mapping(inline_result.get("slider_resolution"))
        or {
            "attempted": bool(inline_result.get("slider_attempted", False)),
            "resolved": True,
            "reason": first_non_empty(inline_result.get("slider_reason"), "mock_verified"),
            "attempts": [],
        },
        "slider_captcha_audit_artifact_refs": coerce_mapping_list(inline_result.get("slider_captcha_audit_artifact_refs")),
        "browser_diagnostic_artifact_refs": coerce_mapping_list(inline_result.get("browser_diagnostic_artifact_refs")),
        "login_cookie_bootstrap": coerce_mapping(inline_result.get("login_cookie_bootstrap")),
    }


def _browser_resolve_failure_summary(browser_result: Mapping[str, Any], *, error_code: str) -> dict[str, Any]:
    verification = coerce_mapping(browser_result.get("verification"))
    slider_resolution = _redact_slider_resolution(coerce_mapping(browser_result.get("slider_resolution")))
    return compact_dict(
        {
            "resolved": False,
            "verified_path": first_non_empty(verification.get("verified_path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT),
            "response_code": first_non_empty(verification.get("response_code")),
            "data_id": first_non_empty(verification.get("data_id")),
            "ext_is_login": first_non_empty(verification.get("ext_is_login")),
            "error_code": error_code,
            "slider_attempted": bool(slider_resolution.get("attempted")),
            "slider_resolved": bool(slider_resolution.get("resolved")),
            "slider_reason": first_non_empty(slider_resolution.get("reason")),
            "slider_artifact_count": len(coerce_mapping_list(browser_result.get("slider_captcha_audit_artifact_refs"))),
            "browser_diagnostic_artifact_count": len(
                coerce_mapping_list(browser_result.get("browser_diagnostic_artifact_refs"))
            ),
        }
    )


def _browser_resolve_result_payload(
    payload: Mapping[str, Any],
    browser_result: Mapping[str, Any],
    *,
    resolved: bool,
    error_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    verification = coerce_mapping(browser_result.get("verification"))
    details = coerce_mapping(error_details)
    cookies = coerce_mapping_list(browser_result.get("cookies"))
    response_code = first_non_empty(verification.get("response_code"), details.get("response_code"))
    data_id = first_non_empty(verification.get("data_id"), details.get("data_id"))
    ext_is_login = first_non_empty(verification.get("ext_is_login"), details.get("ext_is_login"))
    return compact_dict(
        {
            "verified_path": first_non_empty(verification.get("verified_path"), details.get("path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT),
            "resolved": bool(resolved),
            "verification": compact_dict(
                {
                    "verified": bool(resolved),
                    "verified_path": first_non_empty(
                        verification.get("verified_path"),
                        FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                    ),
                    "response_code": response_code,
                    "data_id": data_id,
                    "ext_is_login": ext_is_login,
                    "total": verification.get("total"),
                    "error_code": first_non_empty(verification.get("error_code"), details.get("error_code")),
                }
            ),
            "error_details": details,
            "browser_cookie_export": _cookie_snapshot_from_browser_cookies(cookies),
            "slider_resolution": _redact_slider_resolution(coerce_mapping(browser_result.get("slider_resolution"))),
            "slider_captcha_audit_artifact_refs": coerce_mapping_list(
                browser_result.get("slider_captcha_audit_artifact_refs")
            ),
            "browser_diagnostic_artifact_refs": coerce_mapping_list(
                browser_result.get("browser_diagnostic_artifact_refs")
            ),
            "login_cookie_bootstrap": coerce_mapping(browser_result.get("login_cookie_bootstrap")),
            "fallback_source_job_id": first_non_empty(payload.get("fallback_source_job_id")),
        }
    )


def _resolve_fastmoss_security_with_browser(
    payload: Mapping[str, Any],
    *,
    search_request: Mapping[str, Any],
    verification_request: Mapping[str, Any],
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

    security_page_url = _build_fastmoss_security_page_url(
        search_request,
        verification_request=verification_request,
        fastmoss_settings=fastmoss_settings,
    )
    login_cookie_bootstrap = _bootstrap_fastmoss_login_cookies(payload, fastmoss_settings=fastmoss_settings)
    audit_dir = first_non_empty(
        payload.get("fastmoss_slider_captcha_audit_dir"),
        payload.get("slider_captcha_audit_dir"),
        DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR,
    )
    diagnostic_artifact_refs: list[dict[str, Any]] = []
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
        _page_goto(browser_session.page, security_page_url, timeout_ms=timeout_ms)
        _safe_wait_for_timeout(browser_session.page, 1_000)
        initial_slider_state = _read_fastmoss_slider_state(browser_session.page)
        diagnostic_artifact_refs.extend(
            _capture_fastmoss_browser_diagnostic_artifacts(
                browser_session.page,
                raw_page=browser_session.raw_page,
                audit_dir=audit_dir,
                search_url=security_page_url,
                label="after_security_page_goto",
                state={"slider_state": initial_slider_state, "security_page_url": security_page_url},
            )
        )
        slider_resolution = _try_resolve_fastmoss_slider_security_check(
            browser_session.page,
            automation_page=browser_session,
            raw_page=browser_session.raw_page,
            search_url=security_page_url,
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
            audit_dir=audit_dir,
            provider_config=(
                coerce_mapping(payload.get("fastmoss_slider_captcha_provider_config"))
                or coerce_mapping(payload.get("slider_captcha_provider_config"))
            ),
            resolver_config=(
                coerce_mapping(payload.get("fastmoss_slider_captcha_resolver_config"))
                or coerce_mapping(payload.get("slider_captcha_resolver_config"))
            ),
            selectors=(
                coerce_mapping(payload.get("fastmoss_slider_captcha_selectors"))
                or coerce_mapping(payload.get("slider_captcha_selectors"))
            ),
        )
        diagnostic_artifact_refs.extend(
            _capture_fastmoss_browser_diagnostic_artifacts(
                browser_session.page,
                raw_page=browser_session.raw_page,
                audit_dir=audit_dir,
                search_url=security_page_url,
                label="after_slider_resolution",
                state={"slider_resolution": slider_resolution, "security_page_url": security_page_url},
            )
        )
        cookies = _export_fastmoss_browser_cookies(browser_session.raw_page, base_url=str(fastmoss_settings["base_url"]))

    verification = _verify_original_request_with_cookies_result(
        verification_request,
        fastmoss_settings=fastmoss_settings,
        cookies=cookies,
    )
    return {
        "cookies": cookies,
        "verification": verification,
        "slider_resolution": slider_resolution,
        "slider_captcha_audit_artifact_refs": coerce_mapping_list(slider_resolution.get("artifact_refs")),
        "browser_diagnostic_artifact_refs": diagnostic_artifact_refs,
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
        trust_env=coerce_bool(fastmoss_settings.get("trust_env"), default=False),
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


def _verify_original_request_with_cookies(
    verification_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
) -> dict[str, Any]:
    path = first_non_empty(verification_request.get("path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT)
    params = coerce_mapping(verification_request.get("params"))
    region = first_non_empty(verification_request.get("region"), fastmoss_settings.get("region"), "US")
    session = FastMossHTTPSession(
        phone=first_non_empty(fastmoss_settings.get("phone")),
        password=first_non_empty(fastmoss_settings.get("password")),
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=region,
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(fastmoss_settings, provider="fastmoss"),
        trust_env=coerce_bool(fastmoss_settings.get("trust_env"), default=False),
    )
    with session:
        session.replace_browser_cookies(cookies)
        raw = session.request_json(
            first_non_empty(verification_request.get("method"), "GET"),
            path,
            params=params,
            referer=first_non_empty(verification_request.get("referer"), _default_referer_for_request(verification_request, fastmoss_settings=fastmoss_settings)),
            region=region,
            stage=first_non_empty(verification_request.get("stage"), "browser_security.verify_original_request"),
            check_auth=False,
        )
    data = coerce_mapping(raw.get("data"))
    ext = coerce_mapping(raw.get("ext"))
    return compact_dict(
        {
            "verified": True,
            "verified_path": path,
            "response_code": first_non_empty(raw.get("code"), "200"),
            "ext_is_login": first_non_empty(ext.get("is_login")),
            "total": data.get("total") or data.get("total_cnt"),
        }
    )


def _verify_original_request_with_cookies_result(
    verification_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return _verify_original_request_with_cookies(
            verification_request,
            fastmoss_settings=fastmoss_settings,
            cookies=cookies,
        )
    except FastMossHTTPError as exc:
        if not _is_fastmoss_security_error(exc):
            raise
        details = _redact_fastmoss_http_error(exc)
        return compact_dict(
            {
                "verified": False,
                "verified_path": first_non_empty(details.get("path"), verification_request.get("path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT),
                "response_code": first_non_empty(details.get("response_code"), exc.response_code),
                "error_code": "fastmoss_security_verification_required",
                "error_type": "security_verification",
                "data_id": first_non_empty(details.get("data_id")),
                "ext_is_login": first_non_empty(details.get("ext_is_login")),
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
        if not _resolve_verification_request(payload, search_request=search_request).get("path"):
            raise ValueError("FastMoss browser security resolve requires original search keyword or verification_request.path.")
    return search_request


def _resolve_verification_request(payload: Mapping[str, Any], *, search_request: Mapping[str, Any]) -> dict[str, Any]:
    verification_request = (
        coerce_mapping(payload.get("verification_request"))
        or coerce_mapping(payload.get("fastmoss_original_request"))
        or coerce_mapping(coerce_mapping(payload.get("security_context")).get("verification_request"))
    )
    if verification_request:
        params = coerce_mapping(verification_request.get("params"))
        return compact_dict(
            {
                "method": first_non_empty(verification_request.get("method"), "GET").upper(),
                "path": first_non_empty(verification_request.get("path"), FASTMOSS_PRODUCT_SEARCH_ENDPOINT),
                "params": _redact_replay_params(params),
                "referer": first_non_empty(verification_request.get("referer")),
                "region": first_non_empty(verification_request.get("region"), search_request.get("region"), "US"),
                "stage": first_non_empty(verification_request.get("stage"), "browser_security.verify_original_request"),
            }
        )
    params = _resolve_original_search_params(search_request, fastmoss_settings=coerce_mapping(payload.get("fastmoss")))
    return {
        "method": "GET",
        "path": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        "params": {
            "page": params["page"],
            "pagesize": params["page_size"],
            "order": params["order"],
            "region": params["region"],
            **({"words": params["keyword"]} if params["keyword"] else {}),
            **params["extra_params"],
        },
        "region": params["region"],
        "stage": "product.search",
    }


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
        "trust_env": coerce_bool(
            first_non_empty(
                settings.get("trust_env"),
                settings.get("use_system_proxy"),
                settings.get("fastmoss_trust_env"),
                settings.get("fastmoss_use_system_proxy"),
                payload.get("fastmoss_trust_env"),
                payload.get("fastmoss_use_system_proxy"),
                request_payload.get("fastmoss_trust_env"),
                request_payload.get("fastmoss_use_system_proxy"),
            ),
            default=False,
        ),
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


def _build_fastmoss_security_page_url(
    search_request: Mapping[str, Any],
    *,
    verification_request: Mapping[str, Any],
    fastmoss_settings: Mapping[str, Any],
) -> str:
    referer = first_non_empty(verification_request.get("referer"))
    if referer:
        return referer
    path = first_non_empty(verification_request.get("path"))
    params = coerce_mapping(verification_request.get("params"))
    base_url = str(fastmoss_settings["base_url"]).rstrip("/")
    product_id = first_non_empty(params.get("product_id"), params.get("goods_id"))
    if product_id or "/api/goods/" in path:
        return f"{base_url}/zh/e-commerce/detail/{product_id}" if product_id else f"{base_url}/zh/e-commerce/search"
    uid = first_non_empty(params.get("uid"), params.get("author_uid"), params.get("unique_id"))
    if uid or "/api/author/" in path:
        return f"{base_url}/zh/influencer/detail/{uid}" if uid else f"{base_url}/zh/influencer/search"
    seller_id = first_non_empty(params.get("seller_id"), params.get("shop_id"))
    if seller_id or "/api/shop/" in path:
        return f"{base_url}/zh/shop-marketing/detail/{seller_id}" if seller_id else f"{base_url}/zh/shop-marketing"
    video_id = first_non_empty(params.get("video_id"))
    if video_id or "/api/video/" in path:
        return f"{base_url}/zh/media-source/video/{video_id}" if video_id else f"{base_url}/zh/media-source"
    return _build_fastmoss_search_page_url(search_request, fastmoss_settings=fastmoss_settings)


def _default_referer_for_request(
    verification_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
) -> str:
    return _build_fastmoss_security_page_url({}, verification_request=verification_request, fastmoss_settings=fastmoss_settings)


def _redact_replay_params(params: Mapping[str, Any]) -> dict[str, Any]:
    replay = dict(params)
    for key in ("fm-sign", "cnonce", "_time"):
        replay.pop(key, None)
    return replay


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
        piece_image = _locator_screenshot_bytes(piece_locator)
        background_box = _locator_bounding_box(background_locator)
        piece_box = _locator_bounding_box(piece_locator)
        handle_box = _locator_bounding_box(handle_locator)
        if not (background_image and piece_image and background_box and piece_box and handle_box):
            raise RuntimeError("FastMoss slider artifacts are incomplete.")
        background_width, background_height = _image_size(background_image)
        piece_width, piece_height = _image_size(piece_image)
        extra[background_key] = background_image
        extra[piece_key] = piece_image
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
            "source": "locator_screenshot",
            "image_width": piece_width,
            "image_height": piece_height,
            "rendered_box": piece_box,
            "artifact_key": piece_key,
            "sha256": hashlib.sha256(piece_image).hexdigest(),
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


def _wait_locator_visible(locator: Any, *, timeout_ms: int) -> None:
    wait_for = getattr(locator, "wait_for", None)
    if callable(wait_for):
        wait_for(state="visible", timeout=timeout_ms)


def _fastmoss_background_css_image_bytes(locator: Any, *, page: Any, selector: str) -> bytes:
    resource = _fastmoss_background_css_resource(locator)
    if not resource and page is not None and selector:
        resource = _fastmoss_page_background_css_resource(page, selector=selector)
    return _load_fastmoss_browser_image_resource(resource, page=page)


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


def _build_fastmoss_mixed_slider_mapping(
    page: Any,
    *,
    slider_result: Any,
    background_box: Mapping[str, float],
    background_image_size: tuple[int, int],
    piece_box: Mapping[str, float],
    handle_box: Mapping[str, float],
    drag_scale: float,
    drag_offset_x: float,
) -> dict[str, Any]:
    image_width = float(background_image_size[0] or 1)
    rendered_width = float(background_box.get("width") or 0.0)
    raw_target_x = float(getattr(slider_result, "target_x", 0.0) or 0.0)
    raw_target_y = float(getattr(slider_result, "target_y", 0.0) or 0.0)
    raw_payload = coerce_mapping(getattr(slider_result, "raw", None))
    shape_anchor = coerce_mapping(raw_payload.get("fastmoss_shape_anchor"))
    piece_anchor_ratio_x = _optional_float(shape_anchor.get("piece_anchor_ratio_x"))
    css_target_x = (raw_target_x / max(image_width, 1.0)) * rendered_width
    current_piece_center_x = (
        float(piece_box.get("x") or 0.0)
        + (float(piece_box.get("width") or 0.0) / 2)
        - float(background_box.get("x") or 0.0)
    )
    current_piece_anchor_x = current_piece_center_x
    if piece_anchor_ratio_x is not None:
        current_piece_anchor_x = (
            float(piece_box.get("x") or 0.0)
            + (float(piece_box.get("width") or 0.0) * min(max(piece_anchor_ratio_x, 0.0), 1.0))
            - float(background_box.get("x") or 0.0)
        )
    unscaled_drag_distance = css_target_x - current_piece_anchor_x
    drag_distance = (unscaled_drag_distance * drag_scale) + drag_offset_x
    handle_start_x = float(handle_box.get("x") or 0.0) + (float(handle_box.get("width") or 0.0) / 2)
    handle_start_y = float(handle_box.get("y") or 0.0) + (float(handle_box.get("height") or 0.0) / 2)
    return {
        "raw_target_x": raw_target_x,
        "raw_target_y": raw_target_y,
        "raw_target_box": _raw_target_box(first_non_empty(raw_payload.get("target_box"), raw_payload.get("target"))),
        "target_interpretation": first_non_empty(
            shape_anchor.get("target_interpretation"),
            "target_center_minus_piece_center",
        ),
        "background_image_width": background_image_size[0],
        "background_image_height": background_image_size[1],
        "background_box": dict(background_box),
        "piece_box": dict(piece_box),
        "handle_box": dict(handle_box),
        "css_target_x": css_target_x,
        "current_piece_center_x": current_piece_center_x,
        "current_piece_anchor_x": current_piece_anchor_x,
        "piece_anchor_ratio_x": piece_anchor_ratio_x,
        "fastmoss_shape_anchor": shape_anchor,
        "unscaled_drag_distance": unscaled_drag_distance,
        "drag_scale": drag_scale,
        "drag_offset_x": drag_offset_x,
        "drag_distance": drag_distance,
        "handle_start_x": handle_start_x,
        "handle_start_y": handle_start_y,
        "handle_end_x": handle_start_x + drag_distance,
        "handle_end_y": handle_start_y,
        "device_pixel_ratio": _device_pixel_ratio(page),
    }


def _select_fastmoss_shape_anchor_slider_result(
    slider_result: Any,
    *,
    background_image: bytes,
    piece_image: bytes,
    background_box: Mapping[str, float],
    piece_box: Mapping[str, float],
) -> Any:
    """Use FastMoss puzzle outline geometry to correct low-confidence OCR points."""
    background_width, background_height = _image_size(background_image)
    if background_width <= 0 or background_height <= 0:
        return slider_result

    rendered_width = float(background_box.get("width") or 0.0)
    if rendered_width <= 0:
        return slider_result

    raw_target_x = float(getattr(slider_result, "target_x", 0.0) or 0.0)
    raw_target_y = float(getattr(slider_result, "target_y", 0.0) or 0.0)
    current_piece_center_x = (
        float(piece_box.get("x") or 0.0)
        + (float(piece_box.get("width") or 0.0) / 2)
        - float(background_box.get("x") or 0.0)
    )
    current_piece_center_raw_x = current_piece_center_x * background_width / rendered_width
    piece_outline_box = _fastmoss_piece_outline_box(piece_image)
    piece_anchor_ratio_x = _fastmoss_box_center_ratio(piece_outline_box, image_width=_image_size(piece_image)[0])
    candidates = _fastmoss_background_puzzle_outline_candidates(
        background_image,
        current_piece_center_raw_x=current_piece_center_raw_x,
        piece_outline_box=piece_outline_box,
    )
    if not candidates:
        return slider_result

    selected = _select_fastmoss_outline_candidate(
        candidates,
        raw_target_x=raw_target_x,
        raw_target_y=raw_target_y,
        current_piece_center_raw_x=current_piece_center_raw_x,
    )
    if not selected:
        return slider_result

    target_x = int(round(float(selected["anchor_x"])))
    target_y = int(round(float(selected["anchor_y"])))
    raw_payload = dict(coerce_mapping(getattr(slider_result, "raw", None)))
    shape_anchor = {
        "enabled": True,
        "source_target_x": raw_target_x,
        "source_target_y": raw_target_y,
        "source_confidence": getattr(slider_result, "confidence", None),
        "selected_box": _compact_box(selected),
        "candidate_boxes": [_compact_box(candidate) for candidate in candidates[:8]],
        "piece_outline_box": _compact_box(piece_outline_box),
        "piece_anchor_ratio_x": piece_anchor_ratio_x,
        "current_piece_center_raw_x": current_piece_center_raw_x,
        "target_interpretation": "fastmoss_outline_bbox_center_minus_piece_outline_anchor",
        "selection_reason": first_non_empty(selected.get("selection_reason"), "nearest_outline_bbox_to_ocr_target"),
    }
    return SimpleNamespace(
        target_x=target_x,
        target_y=target_y,
        confidence=getattr(slider_result, "confidence", None),
        raw={
            **raw_payload,
            "target_box": [
                int(round(float(selected["x"]))),
                int(round(float(selected["y"]))),
                int(round(float(selected["x"]) + float(selected["width"]))),
                int(round(float(selected["y"]) + float(selected["height"]))),
            ],
            "fastmoss_shape_anchor": shape_anchor,
        },
    )


def _fastmoss_piece_outline_box(piece_image: bytes) -> dict[str, float]:
    image = _open_rgb_image(piece_image)
    if image is None:
        return {}
    candidates = _light_outline_components(image, min_area=12)
    if not candidates:
        return {"x": 0.0, "y": 0.0, "width": float(image.width), "height": float(image.height)}
    merged = _merge_outline_boxes(candidates, max_gap=8.0)
    plausible = [
        box
        for box in merged
        if box["width"] >= image.width * 0.25 and box["height"] >= image.height * 0.25
    ]
    if not plausible:
        plausible = merged
    return max(plausible, key=lambda box: float(box.get("area", 0.0) or 0.0))


def _fastmoss_background_puzzle_outline_candidates(
    background_image: bytes,
    *,
    current_piece_center_raw_x: float,
    piece_outline_box: Mapping[str, Any],
) -> list[dict[str, float | str]]:
    image = _open_rgb_image(background_image)
    if image is None:
        return []
    piece_width = float(piece_outline_box.get("width") or 0.0)
    piece_height = float(piece_outline_box.get("height") or 0.0)
    min_width = max(24.0, piece_width * 0.45)
    max_width = max(150.0, piece_width * 2.8)
    min_height = max(22.0, piece_height * 0.35)
    max_height = max(130.0, piece_height * 2.6)
    raw_components = [
        component
        for component in _light_outline_components(image, min_area=35)
        if float(component.get("width") or 0.0) <= max_width
        and float(component.get("height") or 0.0) <= max_height
    ]
    merged = _merge_outline_boxes(raw_components, max_gap=34.0)
    candidates: list[dict[str, float | str]] = []
    for box in merged:
        width = float(box.get("width") or 0.0)
        height = float(box.get("height") or 0.0)
        if not (min_width <= width <= max_width and min_height <= height <= max_height):
            continue
        if (
            float(box.get("x") or 0.0) <= 2.0
            or float(box.get("y") or 0.0) <= 2.0
            or float(box.get("x") or 0.0) + width >= float(image.width) - 2.0
            or float(box.get("y") or 0.0) + height >= float(image.height) - 2.0
        ):
            continue
        center_x = float(box["x"]) + width / 2.0
        center_y = float(box["y"]) + height / 2.0
        is_current = abs(center_x - current_piece_center_raw_x) <= max(width * 0.8, piece_width * 0.7, 28.0)
        score = float(box.get("area", 0.0) or 0.0) / max(width * height, 1.0)
        candidates.append(
            {
                **box,
                "anchor_x": center_x,
                "anchor_y": center_y,
                "center_x": center_x,
                "center_y": center_y,
                "score": score,
                "is_current_piece": bool(is_current),
            }
        )
    candidates.sort(key=lambda box: (bool(box.get("is_current_piece")), -float(box.get("score") or 0.0)))
    return candidates


def _select_fastmoss_outline_candidate(
    candidates: list[dict[str, Any]],
    *,
    raw_target_x: float,
    raw_target_y: float,
    current_piece_center_raw_x: float,
) -> dict[str, Any]:
    usable = [candidate for candidate in candidates if not candidate.get("is_current_piece")]
    if not usable:
        usable = candidates
    if not usable:
        return {}

    def distance(candidate: Mapping[str, Any]) -> float:
        center_x = float(candidate.get("center_x") or candidate.get("anchor_x") or 0.0)
        center_y = float(candidate.get("center_y") or candidate.get("anchor_y") or 0.0)
        return ((center_x - raw_target_x) ** 2 + ((center_y - raw_target_y) * 0.8) ** 2) ** 0.5

    nearest = min(usable, key=distance)
    nearest = dict(nearest)
    nearest_distance = distance(nearest)
    if nearest_distance <= max(float(nearest.get("width") or 0.0) * 1.2, 72.0):
        nearest["selection_reason"] = "nearest_outline_bbox_to_ocr_target"
        return nearest

    source_looks_like_current_piece = abs(raw_target_x - current_piece_center_raw_x) <= 96.0
    right_side = [
        candidate
        for candidate in usable
        if float(candidate.get("center_x") or 0.0) > current_piece_center_raw_x + max(float(candidate.get("width") or 0.0) * 0.4, 28.0)
    ]
    if source_looks_like_current_piece and right_side:
        selected = max(right_side, key=lambda candidate: float(candidate.get("score") or 0.0))
        selected = dict(selected)
        selected["selection_reason"] = "best_non_current_outline_bbox"
        return selected

    return {}


def _light_outline_components(image: Image.Image, *, min_area: int) -> list[dict[str, float]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    visited = bytearray(width * height)
    components: list[dict[str, float]] = []

    def is_outline_pixel(x: int, y: int) -> bool:
        red, green, blue = pixels[x, y]
        luminance = (int(red) + int(green) + int(blue)) / 3.0
        saturation = max(red, green, blue) - min(red, green, blue)
        return luminance >= 165.0 and saturation <= 95

    for y in range(height):
        for x in range(width):
            index = (y * width) + x
            if visited[index] or not is_outline_pixel(x, y):
                continue
            stack = [(x, y)]
            visited[index] = 1
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                current_x, current_y = stack.pop()
                area += 1
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)
                for next_x, next_y in (
                    (current_x + 1, current_y),
                    (current_x - 1, current_y),
                    (current_x, current_y + 1),
                    (current_x, current_y - 1),
                ):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    next_index = (next_y * width) + next_x
                    if visited[next_index] or not is_outline_pixel(next_x, next_y):
                        continue
                    visited[next_index] = 1
                    stack.append((next_x, next_y))
            box_width = max_x - min_x + 1
            box_height = max_y - min_y + 1
            if area >= min_area and box_width >= 6 and box_height >= 6:
                components.append(
                    {
                        "x": float(min_x),
                        "y": float(min_y),
                        "width": float(box_width),
                        "height": float(box_height),
                        "area": float(area),
                    }
                )
    return components


def _merge_outline_boxes(boxes: list[dict[str, float]], *, max_gap: float) -> list[dict[str, float]]:
    merged = [dict(box) for box in boxes]
    changed = True
    while changed:
        changed = False
        next_boxes: list[dict[str, float]] = []
        used = [False] * len(merged)
        for index, box in enumerate(merged):
            if used[index]:
                continue
            current = dict(box)
            used[index] = True
            for other_index in range(index + 1, len(merged)):
                if used[other_index]:
                    continue
                other = merged[other_index]
                if not _outline_boxes_should_merge(current, other, max_gap=max_gap):
                    continue
                current = _union_outline_boxes(current, other)
                used[other_index] = True
                changed = True
            next_boxes.append(current)
        merged = next_boxes
    return merged


def _outline_boxes_should_merge(
    first: Mapping[str, float],
    second: Mapping[str, float],
    *,
    max_gap: float,
) -> bool:
    first_x1 = float(first["x"])
    first_y1 = float(first["y"])
    first_x2 = first_x1 + float(first["width"])
    first_y2 = first_y1 + float(first["height"])
    second_x1 = float(second["x"])
    second_y1 = float(second["y"])
    second_x2 = second_x1 + float(second["width"])
    second_y2 = second_y1 + float(second["height"])
    horizontal_overlap = max(0.0, min(first_x2, second_x2) - max(first_x1, second_x1))
    vertical_overlap = max(0.0, min(first_y2, second_y2) - max(first_y1, second_y1))
    min_width = max(min(float(first["width"]), float(second["width"])), 1.0)
    min_height = max(min(float(first["height"]), float(second["height"])), 1.0)
    horizontal_gap = max(0.0, max(first_x1, second_x1) - min(first_x2, second_x2))
    vertical_gap = max(0.0, max(first_y1, second_y1) - min(first_y2, second_y2))
    return (
        (horizontal_overlap / min_width >= 0.35 and vertical_gap <= max_gap)
        or (vertical_overlap / min_height >= 0.35 and horizontal_gap <= max_gap)
    )


def _union_outline_boxes(first: Mapping[str, float], second: Mapping[str, float]) -> dict[str, float]:
    x1 = min(float(first["x"]), float(second["x"]))
    y1 = min(float(first["y"]), float(second["y"]))
    x2 = max(float(first["x"]) + float(first["width"]), float(second["x"]) + float(second["width"]))
    y2 = max(float(first["y"]) + float(first["height"]), float(second["y"]) + float(second["height"]))
    return {
        "x": x1,
        "y": y1,
        "width": x2 - x1,
        "height": y2 - y1,
        "area": float(first.get("area", 0.0) or 0.0) + float(second.get("area", 0.0) or 0.0),
    }


def _compact_box(box: Mapping[str, Any]) -> dict[str, Any]:
    if not box:
        return {}
    keys = ("x", "y", "width", "height", "anchor_x", "anchor_y", "score", "is_current_piece")
    return compact_dict({key: box.get(key) for key in keys})


def _fastmoss_box_center_ratio(box: Mapping[str, Any], *, image_width: int) -> float | None:
    if not box or image_width <= 0:
        return None
    return (float(box.get("x") or 0.0) + (float(box.get("width") or 0.0) / 2.0)) / float(image_width)


def _open_rgb_image(image_bytes: bytes) -> Image.Image | None:
    if not image_bytes:
        return None
    try:
        from io import BytesIO

        return Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None


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


def _raw_target_box(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        return [int(value[0]), int(value[1]), int(value[2]), int(value[3])]
    except (TypeError, ValueError):
        return None


def _device_pixel_ratio(page: Any) -> float | None:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        return float(evaluate("() => window.devicePixelRatio"))
    except Exception:
        return None


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
            "ready_state": attempt.get("ready_state") if isinstance(attempt.get("ready_state"), Mapping) else {},
            "post_drag_verify_wait_ms": post_drag_verify_wait_ms,
            "popup_still_visible": attempt.get("popup_still_visible"),
            "selector_success": attempt.get("selector_success"),
            "artifact_keys": {
                "background": background.get("artifact_key"),
                "piece": piece.get("artifact_key"),
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


def _capture_fastmoss_browser_diagnostic_artifacts(
    page: Any,
    *,
    raw_page: Any | None,
    audit_dir: str,
    search_url: str,
    label: str,
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    root = Path(audit_dir or DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR)
    run_key = hashlib.sha256(search_url.encode("utf-8")).hexdigest()[:16]
    target_dir = root / run_key / "browser_diagnostics"
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label)).strip("_") or "browser_diagnostic"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        state_payload = {
            "label": safe_label,
            "search_url": search_url,
            "page_url": first_non_empty(getattr(page, "url", ""), getattr(raw_page, "url", "")),
            "page_title": _page_title(page) or _page_title(raw_page),
            "state": _json_safe_value(dict(state)),
        }
        refs = [
            _write_fastmoss_slider_json_file(
                target_dir / f"{safe_label}_state.json",
                state_payload,
                artifact_key=f"{safe_label}_state",
            )
        ]
        screenshot = _capture_page_screenshot_bytes(page) or _capture_page_screenshot_bytes(raw_page)
        if screenshot:
            refs.append(
                _write_fastmoss_slider_binary_file(
                    target_dir / f"{safe_label}_screenshot",
                    screenshot,
                    artifact_key=f"{safe_label}_screenshot",
                )
            )
        else:
            refs.append(
                _write_fastmoss_slider_json_file(
                    target_dir / f"{safe_label}_screenshot_unavailable.json",
                    {"reason": "page_screenshot_unavailable"},
                    artifact_key=f"{safe_label}_screenshot_unavailable",
                )
            )
        return refs
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "artifact_key": f"{safe_label}_diagnostic_capture_failed",
                "error": str(exc),
                "mime_type": "application/json",
            }
        ]


def _capture_page_screenshot_bytes(page: Any | None) -> bytes:
    if page is None:
        return b""
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return b""
    for kwargs in ({"full_page": True, "timeout": 3_000}, {"full_page": True}, {}):
        try:
            payload = screenshot(**kwargs)
        except TypeError:
            continue
        except Exception:
            return b""
        return payload if isinstance(payload, bytes) else b""
    return b""


def _page_title(page: Any | None) -> str:
    title = getattr(page, "title", None)
    if not callable(title):
        return ""
    try:
        return coerce_str(title())
    except Exception:
        return ""


def _json_safe_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


def _persist_fastmoss_slider_artifacts_payload(
    artifacts_payload: Mapping[str, Any],
    *,
    audit_dir: str,
    search_url: str,
) -> list[dict[str, Any]]:
    root = Path(audit_dir or DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR)
    run_key = hashlib.sha256(search_url.encode("utf-8")).hexdigest()[:16]
    target_dir = root / run_key
    target_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []
    state_dump = artifacts_payload.get("state_dump")
    if state_dump:
        refs.append(_write_fastmoss_slider_json_file(target_dir / "slider_captcha_audit.json", state_dump))

    extra = artifacts_payload.get("extra") if isinstance(artifacts_payload.get("extra"), Mapping) else {}
    for key, value in extra.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(key)).strip("_") or "artifact"
        if isinstance(value, bytes):
            refs.append(_write_fastmoss_slider_binary_file(target_dir / f"{safe_key}.bin", value, artifact_key=str(key)))
        elif key == "slider_captcha_audit":
            continue
        elif isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            refs.append(_write_fastmoss_slider_json_file(target_dir / f"{safe_key}.json", value, artifact_key=str(key)))
    return refs


def _write_fastmoss_slider_json_file(
    path: Path,
    value: Any,
    *,
    artifact_key: str = "slider_captcha_audit",
) -> dict[str, Any]:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "artifact_key": artifact_key,
        "local_path": str(path),
        "file_name": path.name,
        "mime_type": "application/json",
    }


def _write_fastmoss_slider_binary_file(path: Path, value: bytes, *, artifact_key: str) -> dict[str, Any]:
    suffix = ".png" if value.startswith(b"\x89PNG") else ".jpg" if value.startswith(b"\xff\xd8") else ".bin"
    final_path = path.with_suffix(suffix)
    final_path.write_bytes(value)
    return {
        "artifact_key": artifact_key,
        "local_path": str(final_path),
        "file_name": final_path.name,
        "mime_type": "image/png" if suffix == ".png" else "image/jpeg" if suffix == ".jpg" else "application/octet-stream",
    }


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


def _build_slider_captcha_provider(provider_config: Mapping[str, Any] | None = None) -> Any:
    from automation_framework.captcha import DdddOcrCaptchaProvider

    return DdddOcrCaptchaProvider(**dict(provider_config or {}))


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
                        "match_method": record.get("match_method"),
                        "mode": record.get("mode"),
                        "simple_target": record.get("simple_target"),
                        "target_x": record.get("target_x"),
                        "target_y": record.get("target_y"),
                        "raw_result": record.get("raw_result"),
                        "coordinate_mapping": record.get("coordinate_mapping"),
                        "drag_distance": record.get("drag_distance"),
                        "confidence": record.get("confidence"),
                        "artifact_keys": record.get("artifact_keys"),
                        "post_drag_verify_wait_ms": record.get("post_drag_verify_wait_ms"),
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
            "framework_resolver": first_non_empty(slider_resolution.get("framework_resolver")),
            "post_drag_verify_wait_ms": slider_resolution.get("post_drag_verify_wait_ms"),
            "confirmation_wait_ms": slider_resolution.get("confirmation_wait_ms"),
            "drag_profile": coerce_mapping(slider_resolution.get("drag_profile")),
            "artifact_refs": coerce_mapping_list(slider_resolution.get("artifact_refs")),
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
            "params": _redact_replay_params(exc.params or {}),
            "referer": exc.referer,
            "region": exc.region,
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


def _float_value(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


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
