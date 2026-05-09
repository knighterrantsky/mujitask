from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlencode

from automation_business_scaffold.capabilities.browser.fastmoss_security.cookie_bridge import (
    cookie_snapshot_from_browser_cookies as _cookie_snapshot_from_browser_cookies,
    export_fastmoss_browser_cookies as _export_fastmoss_browser_cookies,
    import_fastmoss_browser_cookies as _import_fastmoss_browser_cookies,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.cookie_cache_persistence import (
    save_browser_cookies_to_cache as _save_browser_cookies_to_cache,
)
from automation_business_scaffold.capabilities.browser.page_primitives import (
    page_goto as _page_goto,
    safe_wait_for_timeout as _safe_wait_for_timeout,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.diagnostics import (
    _capture_fastmoss_browser_diagnostic_artifacts,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.element_state import (
    DEFAULT_FASTMOSS_BROWSER_TIMEOUT_MS,
    DEFAULT_FASTMOSS_SLIDER_APPEAR_TIMEOUT_MS,
    DEFAULT_FASTMOSS_SLIDER_CONFIRM_MS,
    DEFAULT_FASTMOSS_SLIDER_SETTLE_MS,
    _read_fastmoss_slider_state,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.slider_challenge import (
    DEFAULT_FASTMOSS_SLIDER_ATTEMPTS,
    DEFAULT_FASTMOSS_SLIDER_AUDIT_DIR,
    _try_resolve_fastmoss_slider_security_check,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.request_verification import (
    FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
    FASTMOSS_SECURITY_VERIFICATION_CODES,
    is_fastmoss_security_error as _is_fastmoss_security_error,
    redact_fastmoss_http_error as _redact_fastmoss_http_error,
    redact_replay_params as _redact_replay_params,
    verify_original_request_with_cookies_result as _verify_original_request_with_cookies_result,
)
from automation_business_scaffold.capabilities.browser.fastmoss_security.session_bootstrap import (
    bootstrap_fastmoss_login_cookies as _bootstrap_fastmoss_login_cookies,
)
from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    compact_dict,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.infrastructure.browser.browser_bridge import open_automation_page
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
)

HANDLER_CODE = "fastmoss_security_browser_resolve"
CONTRACT = BROWSER_HANDLER_CONTRACTS[HANDLER_CODE]


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
            db_url=_runtime_db_url(payload, fastmoss_settings=fastmoss_settings),
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
    login_cookie_bootstrap = _bootstrap_fastmoss_login_cookies(
        db_url=_runtime_db_url(payload, fastmoss_settings=fastmoss_settings),
        fastmoss_settings=fastmoss_settings,
    )
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
        default_referer=security_page_url,
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


def _runtime_db_url(payload: Mapping[str, Any], *, fastmoss_settings: Mapping[str, Any]) -> str:
    request_payload = coerce_mapping(payload.get("request_payload"))
    return first_non_empty(
        payload.get("execution_control_db_url"),
        payload.get("db_url"),
        request_payload.get("execution_control_db_url"),
        request_payload.get("db_url"),
        fastmoss_settings.get("execution_control_db_url"),
        fastmoss_settings.get("db_url"),
        os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_URL"),
        os.environ.get("EXECUTION_CONTROL_DB_URL"),
    )


def _env_value(name: str) -> str:
    return os.environ.get(name, "") if name else ""


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_security_browser_resolve_handler"]
