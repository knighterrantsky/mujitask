from __future__ import annotations

import html
import json
import os
import re
import tempfile
import time
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    extract_product_id,
    failed_result,
    first_non_empty,
    json_fingerprint,
    now_timestamp,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    create_store_from_settings,
    sync_artifact_specs,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
    FastMossSessionConflictError,
)
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    attach_fastmoss_cookie_cache,
    refresh_fastmoss_session_cookies,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FASTMOSS_PRODUCT_SEARCH_ENDPOINT = "/api/goods/V2/search"
FASTMOSS_PRODUCT_DETAIL_URL_TEMPLATE = "https://www.fastmoss.com/zh/e-commerce/detail/{product_id}"
TIKTOK_PRODUCT_URL_TEMPLATE = "https://www.tiktok.com/view/product/{product_id}"
FASTMOSS_SECURITY_VERIFICATION_CODES = {"MSG_SAFE_0001"}

HANDLER_CODE = "fastmoss_product_search"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [coerce_str(item) for item in value if coerce_str(item)]
    if isinstance(value, tuple):
        return [coerce_str(item) for item in value if coerce_str(item)]
    text = coerce_str(value)
    return [text] if text else []


def fastmoss_product_search_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        query = _resolve_fastmoss_product_search_query(payload)
        raw_pages, runtime_pagination, session_snapshot = _resolve_fastmoss_product_search_pages(
            payload,
            query=query,
        )
        normalized = _build_fastmoss_product_search_result(
            context,
            payload,
            query=query,
            raw_pages=raw_pages,
            runtime_pagination=runtime_pagination,
            session_snapshot=session_snapshot,
        )
    except ValueError as exc:
        error = build_error(
            error_type="configuration_error",
            error_code="fastmoss_search_invalid_payload",
            message=str(exc),
            retryable=False,
            details={"handler_code": context.handler_code},
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})
    except FastMossAuthError as exc:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_auth_required",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})
    except FastMossHTTPError as exc:
        if isinstance(exc, FastMossSessionConflictError):
            error = build_error(
                error_type="auth_failure",
                error_code="fastmoss_session_conflict_or_external_login",
                message=str(exc),
                retryable=False,
                details=exc.to_dict(),
            )
            return failed_result(context, error=error, summary={"candidate_count": 0})
        if _is_fastmoss_security_verification_error(exc):
            error = build_error(
                error_type="security_verification",
                error_code="fastmoss_security_verification_required",
                message=str(exc),
                retryable=False,
                details=exc.to_dict(),
            )
            return failed_result(context, error=error, summary={"candidate_count": 0})
        error = build_error(
            error_type="transport_failure",
            error_code="fastmoss_http_failure",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})

    candidates = coerce_mapping_list(normalized.get("candidates"))
    auth_state = coerce_mapping(normalized.get("auth_state"))
    summary = {
        "search_mode": query["search_mode"],
        "keyword": query["keyword"],
        "region": query["region"],
        "candidate_count": len(candidates),
        "raw_candidate_count": int(
            coerce_mapping(normalized.get("condition_summary")).get("raw_candidate_count", 0) or 0
        ),
        "degraded_preview": bool(auth_state.get("degraded_preview")),
        "source_code": coerce_str(auth_state.get("source_code")),
        "stop_reason": coerce_str(coerce_mapping(normalized.get("pagination")).get("stop_reason")),
    }
    warnings = tuple(str(item) for item in normalized.pop("warnings", []) if str(item))

    if auth_state.get("degraded_preview") and not query["degraded_preview_allowed"]:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_search_degraded_preview",
            message="FastMoss product search returned degraded preview results instead of deliverable data.",
            retryable=False,
            details={"auth_state": auth_state, "query": normalized.get("query", {})},
        )
        return failed_result(context, error=error, summary=summary, result=normalized, warnings=warnings)

    return success_result(context, summary=summary, result=normalized, warnings=warnings)


def _resolve_fastmoss_product_search_query(payload: dict[str, Any]) -> dict[str, Any]:
    filters = coerce_mapping(payload.get("filters"))
    fastmoss_settings = _resolve_fastmoss_search_settings(payload)
    search_mode = first_non_empty(payload.get("search_mode"), "keyword")
    if search_mode != "keyword":
        raise ValueError(f"Unsupported FastMoss product search_mode: {search_mode}")

    keyword = first_non_empty(
        payload.get("keyword"),
        payload.get("search_query"),
        payload.get("search_keyword"),
        payload.get("words"),
    )
    if not keyword:
        raise ValueError("FastMoss product keyword/search_query is required.")

    output_conditions = coerce_mapping(payload.get("output_conditions"))
    legacy_condition_context = coerce_mapping(payload.get("condition_context"))
    if legacy_condition_context:
        output_conditions = {**legacy_condition_context, **output_conditions}
    limit_default = _non_negative_int(payload.get("limit"), 20)
    raw_max_candidates = output_conditions.get("max_candidates")
    max_candidates = (
        limit_default
        if raw_max_candidates in (None, "")
        else _non_negative_int(raw_max_candidates, limit_default)
    )
    output_conditions["max_candidates"] = max_candidates
    sales_7d_threshold = _positive_int(payload.get("sales_7d_threshold"), 0)
    if sales_7d_threshold > 0:
        business_conditions = coerce_mapping(output_conditions.get("business_conditions"))
        business_conditions.setdefault("min_day7_sold_count", sales_7d_threshold)
        output_conditions["business_conditions"] = business_conditions

    sort = coerce_mapping(payload.get("sort"))
    pagination = coerce_mapping(payload.get("pagination"))
    session_policy = coerce_mapping(payload.get("session_policy"))
    raw_capture_policy = coerce_mapping(payload.get("raw_capture_policy"))
    page = _positive_int(pagination.get("page"), _positive_int(payload.get("page"), 1)) or 1
    page_size = _positive_int(
        first_non_empty(pagination.get("page_size"), pagination.get("pagesize")),
        _positive_int(payload.get("page_size"), 10),
    ) or 10
    max_pages = _positive_int(pagination.get("max_pages"), _positive_int(payload.get("max_pages"), 50)) or 50
    require_login = coerce_bool(session_policy.get("require_login"), default=True)
    degraded_preview_allowed = coerce_bool(
        session_policy.get("degraded_preview_allowed"),
        default=False,
    )

    extra_params, filter_warnings = _fastmoss_search_extra_params(filters)
    region = first_non_empty(
        payload.get("region"),
        filters.get("region"),
        filters.get("country_code"),
        fastmoss_settings.get("region"),
        "US",
    )
    source_order = first_non_empty(
        sort.get("source_order"),
        payload.get("source_order"),
        payload.get("order"),
        _source_order_from_sort(sort),
        "2,2",
    )
    raw_capture_policy.setdefault("store_raw_response", True)

    return {
        "search_mode": search_mode,
        "keyword": keyword,
        "region": region,
        "filters": filters,
        "sort": sort,
        "source_order": source_order,
        "page": page,
        "page_size": page_size,
        "max_pages": max_pages,
        "stop_when_no_new_product": coerce_bool(
            pagination.get("stop_when_no_new_product"),
            default=True,
        ),
        "max_candidates": max_candidates,
        "page_request_delay_seconds": _non_negative_float(payload.get("page_request_delay_seconds"), 0.0),
        "output_conditions": output_conditions,
        "session_policy": session_policy,
        "raw_capture_policy": raw_capture_policy,
        "require_login": require_login,
        "degraded_preview_allowed": degraded_preview_allowed,
        "fastmoss_settings": fastmoss_settings,
        "extra_params": extra_params,
        "warnings": filter_warnings,
    }


def _resolve_fastmoss_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = coerce_mapping(payload.get("fastmoss"))
    phone_env = first_non_empty(
        settings.get("phone_env"),
        settings.get("fastmoss_phone_env"),
        payload.get("fastmoss_phone_env"),
    )
    password_env = first_non_empty(
        settings.get("password_env"),
        settings.get("fastmoss_password_env"),
        payload.get("fastmoss_password_env"),
    )
    browser_cookies = settings.get("browser_cookies", payload.get("browser_cookies"))
    return {
        "phone": first_non_empty(
            settings.get("phone"),
            payload.get("fastmoss_phone"),
            _env_value(phone_env),
        ),
        "password": first_non_empty(
            settings.get("password"),
            payload.get("fastmoss_password"),
            _env_value(password_env),
        ),
        "phone_env": phone_env,
        "password_env": password_env,
        "base_url": first_non_empty(settings.get("base_url"), payload.get("fastmoss_base_url"), "https://www.fastmoss.com"),
        "region": first_non_empty(settings.get("region"), payload.get("region"), "US"),
        "timeout": settings.get("timeout", payload.get("fastmoss_timeout", 30.0)),
        "browser_cookies": browser_cookies if isinstance(browser_cookies, list) else [],
        "live_fetch": settings.get("live_fetch", payload.get("fastmoss_live_fetch", True)),
        "ensure_logged_in": settings.get("ensure_logged_in", payload.get("ensure_fastmoss_logged_in", None)),
        "execution_control_db_url": first_non_empty(
            settings.get("execution_control_db_url"),
            settings.get("db_url"),
            payload.get("execution_control_db_url"),
            payload.get("db_url"),
        ),
        "cookie_cache_namespace": first_non_empty(
            settings.get("cookie_cache_namespace"),
            payload.get("fastmoss_cookie_cache_namespace"),
        ),
        "cookie_cache_enabled": first_non_empty(
            settings.get("cookie_cache_enabled"),
            payload.get("fastmoss_cookie_cache_enabled"),
        ),
        "cookie_cache_ttl_seconds": first_non_empty(
            settings.get("cookie_cache_ttl_seconds"),
            payload.get("fastmoss_cookie_cache_ttl_seconds"),
        ),
        "trust_env": coerce_bool(
            first_non_empty(
                settings.get("trust_env"),
                settings.get("use_system_proxy"),
                settings.get("fastmoss_trust_env"),
                settings.get("fastmoss_use_system_proxy"),
                payload.get("fastmoss_trust_env"),
                payload.get("fastmoss_use_system_proxy"),
            ),
            default=False,
        ),
        "api_request_delay_min_seconds": first_non_empty(
            settings.get("api_request_delay_min_seconds"),
            settings.get("request_delay_min_seconds"),
            payload.get("api_request_delay_min_seconds"),
        ),
        "api_request_delay_max_seconds": first_non_empty(
            settings.get("api_request_delay_max_seconds"),
            settings.get("request_delay_max_seconds"),
            payload.get("api_request_delay_max_seconds"),
        ),
        "fastmoss_api_request_delay_min_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_min_seconds"),
            settings.get("fastmoss_request_delay_min_seconds"),
            payload.get("fastmoss_api_request_delay_min_seconds"),
        ),
        "fastmoss_api_request_delay_max_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_max_seconds"),
            settings.get("fastmoss_request_delay_max_seconds"),
            payload.get("fastmoss_api_request_delay_max_seconds"),
        ),
    }


def _resolve_fastmoss_product_search_pages(
    payload: dict[str, Any],
    *,
    query: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    raw_pages = _inline_fastmoss_search_pages(payload, first_page=int(query["page"]))
    if raw_pages:
        return raw_pages, _pagination_runtime_from_raw_pages(raw_pages, query=query), {}

    fastmoss_settings = coerce_mapping(query.get("fastmoss_settings"))
    live_fetch = coerce_bool(fastmoss_settings.get("live_fetch"), default=True)
    if not live_fetch:
        raise ValueError("FastMoss live_fetch is disabled and no raw search response was provided.")

    cookies = fastmoss_settings.get("browser_cookies") if isinstance(fastmoss_settings.get("browser_cookies"), list) else []
    phone = first_non_empty(fastmoss_settings.get("phone"))
    password = first_non_empty(fastmoss_settings.get("password"))
    if query["require_login"] and not cookies and not (phone and password):
        raise ValueError("FastMoss product search requires credentials or browser_cookies.")

    session = FastMossHTTPSession(
        phone=phone,
        password=password,
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(query.get("region"), fastmoss_settings.get("region"), "US"),
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(fastmoss_settings, provider="fastmoss"),
        trust_env=coerce_bool(fastmoss_settings.get("trust_env"), default=False),
    )
    with session:
        cookie_cache_status = _attach_fastmoss_cookie_cache_if_configured(
            session,
            fastmoss_settings=fastmoss_settings,
            query=query,
        )
        if cookies:
            session.replace_browser_cookies(cookies)
        ensure_logged_in = coerce_bool(
            fastmoss_settings.get("ensure_logged_in"),
            default=bool(query["require_login"] or cookies or phone),
        )
        if ensure_logged_in:
            session.ensure_logged_in()

        try:
            raw_pages = _fetch_fastmoss_search_pages(session, query=query)
        except FastMossHTTPError as exc:
            if not _is_fastmoss_security_verification_error(exc):
                raise
            if not _refresh_fastmoss_session_after_security_check(
                session,
                cookies=cookies,
                has_credentials=bool(phone and password),
                fastmoss_settings=fastmoss_settings,
                query=query,
            ):
                raise
            raw_pages = _fetch_fastmoss_search_pages(session, query=query)

        session_snapshot = session.cookie_snapshot()
        if cookie_cache_status:
            session_snapshot["cookie_cache"] = cookie_cache_status
        return raw_pages, {}, session_snapshot


def _is_fastmoss_security_verification_error(exc: FastMossHTTPError) -> bool:
    return coerce_str(exc.response_code) in FASTMOSS_SECURITY_VERIFICATION_CODES


def _refresh_fastmoss_session_after_security_check(
    session: FastMossHTTPSession,
    *,
    cookies: list[Mapping[str, Any]],
    has_credentials: bool,
    fastmoss_settings: Mapping[str, Any],
    query: Mapping[str, Any],
) -> bool:
    """Refresh FastMoss auth material once after a safety challenge response."""

    if not has_credentials and not cookies:
        return False
    db_url = first_non_empty(fastmoss_settings.get("execution_control_db_url"), fastmoss_settings.get("db_url"))
    account_key = first_non_empty(
        fastmoss_settings.get("account_key"),
        fastmoss_settings.get("phone"),
        fastmoss_settings.get("phone_env"),
    )
    store = RuntimeStore(db_url=db_url) if db_url and account_key else None
    refresh_fastmoss_session_cookies(
        session,
        store=store,
        account_key=first_non_empty(account_key, "default"),
        region=first_non_empty(query.get("region"), fastmoss_settings.get("region"), "US"),
        namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
        enabled=coerce_bool(fastmoss_settings.get("cookie_cache_enabled"), default=True),
        cookies=cookies,
        prefer_login=has_credentials,
        ttl_seconds=_non_negative_float(
            fastmoss_settings.get("cookie_cache_ttl_seconds"),
            12 * 60 * 60,
        ),
        reason="fastmoss_security_check_login_refresh",
    )
    return True


def _attach_fastmoss_cookie_cache_if_configured(
    session: FastMossHTTPSession,
    *,
    fastmoss_settings: Mapping[str, Any],
    query: Mapping[str, Any],
) -> dict[str, Any]:
    db_url = first_non_empty(fastmoss_settings.get("execution_control_db_url"), fastmoss_settings.get("db_url"))
    account_key = first_non_empty(
        fastmoss_settings.get("account_key"),
        fastmoss_settings.get("phone"),
        fastmoss_settings.get("phone_env"),
    )
    if not db_url or not account_key:
        return {}
    enabled = coerce_bool(fastmoss_settings.get("cookie_cache_enabled"), default=True)
    try:
        store = RuntimeStore(db_url=db_url)
        return attach_fastmoss_cookie_cache(
            session,
            store=store,
            account_key=account_key,
            region=first_non_empty(query.get("region"), fastmoss_settings.get("region"), "US"),
            namespace=first_non_empty(fastmoss_settings.get("cookie_cache_namespace")),
            enabled=enabled,
            ttl_seconds=_non_negative_float(
                fastmoss_settings.get("cookie_cache_ttl_seconds"),
                12 * 60 * 60,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return {"enabled": enabled, "status": "unavailable", "reason": str(exc)}


def _fetch_fastmoss_search_pages(
    session: FastMossHTTPSession,
    *,
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_pages: list[dict[str, Any]] = []
    seen_product_keys: set[str] = set()
    page = int(query["page"])
    stop_reason = "max_pages"
    page_request_delay_seconds = _non_negative_float(query.get("page_request_delay_seconds"), 0.0)
    min_day7_sold_count = _fastmoss_min_day7_sold_count(query)
    stop_on_day7_threshold = _fastmoss_query_uses_day7_desc(query) and min_day7_sold_count is not None
    for _ in range(max(int(query["max_pages"]), 1)):
        if raw_pages and page_request_delay_seconds > 0:
            time.sleep(page_request_delay_seconds)
        raw = session.search_products(
            query["keyword"],
            page=page,
            pagesize=int(query["page_size"]),
            region=query["region"],
            order=query["source_order"],
            extra_params=coerce_mapping(query.get("extra_params")),
            check_auth=False,
        )
        raw_pages.append({"page": page, "response": raw})
        rows = _fastmoss_search_rows(raw)
        if not rows:
            stop_reason = "empty_page"
            break

        page_keys = {
            _fastmoss_product_row_key(row)
            for row in rows
            if _fastmoss_product_row_key(row)
        }
        new_keys = {key for key in page_keys if key not in seen_product_keys}
        seen_product_keys.update(new_keys)
        auth_state = _fastmoss_auth_state_from_payloads([raw], session_snapshot={})
        if auth_state.get("degraded_preview"):
            stop_reason = "degraded_preview"
            break
        if query["stop_when_no_new_product"] and not new_keys:
            stop_reason = "no_new_product"
            break
        if stop_on_day7_threshold and _fastmoss_page_below_min_day7_sold_count(rows, min_day7_sold_count):
            stop_reason = "below_min_day7_sold_count"
            break
        if int(query["max_candidates"]) > 0 and len(seen_product_keys) >= int(query["max_candidates"]):
            stop_reason = "max_candidates"
            break

        total = _positive_int(
            first_non_empty(
                coerce_mapping(raw.get("data")).get("total"),
                coerce_mapping(raw.get("data")).get("total_cnt"),
            ),
            0,
        )
        if total > 0 and page * int(query["page_size"]) >= total:
            stop_reason = "total_reached"
            break
        page += 1

    if raw_pages:
        raw_pages[-1]["stop_reason"] = stop_reason
    return raw_pages


def _fastmoss_min_day7_sold_count(query: Mapping[str, Any]) -> int | float | None:
    output_conditions = coerce_mapping(query.get("output_conditions"))
    business_conditions = coerce_mapping(output_conditions.get("business_conditions"))
    threshold = _parse_number(business_conditions.get("min_day7_sold_count"))
    if threshold is None or threshold <= 0:
        return None
    return threshold


def _fastmoss_query_uses_day7_desc(query: Mapping[str, Any]) -> bool:
    return coerce_str(query.get("source_order")) == "2,2"


def _fastmoss_page_below_min_day7_sold_count(
    rows: list[dict[str, Any]],
    threshold: int | float | None,
) -> bool:
    if threshold is None:
        return False
    day7_values = [
        value
        for value in (_parse_number(row.get("day7_sold_count")) for row in rows)
        if value is not None
    ]
    return bool(day7_values) and max(day7_values) < threshold


def _build_fastmoss_product_search_result(
    context: HandlerContext,
    payload: dict[str, Any],
    *,
    query: dict[str, Any],
    raw_pages: list[dict[str, Any]],
    runtime_pagination: dict[str, Any],
    session_snapshot: dict[str, Any],
) -> dict[str, Any]:
    raw_response_ref, artifact_refs, capture_warnings = _capture_fastmoss_search_raw_response(
        context,
        payload,
        raw_pages=raw_pages,
        query=query,
    )
    output_conditions = coerce_mapping(query.get("output_conditions"))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected_count = 0
    deduped_count = 0
    raw_candidate_count = 0
    deferred_conditions: dict[str, Any] = {}
    max_candidates = int(query["max_candidates"])

    for page_record in raw_pages:
        page_number = _positive_int(page_record.get("page"), int(query["page"])) or int(query["page"])
        for row_index, row in enumerate(_fastmoss_search_rows(coerce_mapping(page_record.get("response"))), start=1):
            raw_candidate_count += 1
            candidate = _normalize_fastmoss_search_candidate(
                row,
                query=query,
                page_number=page_number,
                raw_index=raw_candidate_count,
                raw_response_ref=raw_response_ref,
            )
            matched, deferred, condition_allowed = _evaluate_fastmoss_output_conditions(
                candidate,
                output_conditions,
            )
            candidate["matched_conditions"] = matched
            candidate["deferred_conditions"] = deferred
            deferred_conditions.update(deferred)
            candidate["quality_score"] = _fastmoss_candidate_quality_score(candidate, output_conditions)
            if not condition_allowed or not _fastmoss_candidate_allowed(candidate, output_conditions):
                rejected_count += 1
                continue
            dedupe_key = _fastmoss_candidate_dedupe_key(candidate, output_conditions)
            if dedupe_key in seen:
                deduped_count += 1
                continue
            seen.add(dedupe_key)
            candidate["rank"] = len(candidates) + 1
            candidate["search_rank"] = candidate["rank"]
            candidates.append(candidate)
            if max_candidates > 0 and len(candidates) >= max_candidates:
                break
        if max_candidates > 0 and len(candidates) >= max_candidates:
            break

    auth_state = _fastmoss_auth_state_from_payloads(
        [coerce_mapping(page.get("response")) for page in raw_pages],
        session_snapshot=session_snapshot,
    )
    pagination = _build_fastmoss_search_pagination(
        raw_pages,
        query=query,
        runtime_pagination=runtime_pagination,
        accepted_count=len(candidates),
    )
    condition_summary = {
        "applied": compact_dict(
            {
                "business_conditions": coerce_mapping(output_conditions.get("business_conditions")),
                "required_fields": output_conditions.get("required_fields"),
                "min_quality_score": output_conditions.get("min_quality_score"),
                "dedupe_by": output_conditions.get("dedupe_by"),
            }
        ),
        "deferred": deferred_conditions,
        "raw_candidate_count": raw_candidate_count,
        "accepted_count": len(candidates),
        "rejected_count": rejected_count,
        "deduped_count": deduped_count,
    }
    condition_context = dict(coerce_mapping(payload.get("condition_context")) or output_conditions)
    condition_context["condition_summary"] = condition_summary
    result = {
        "query": {
            "search_mode": query["search_mode"],
            "keyword": query["keyword"],
            "region": query["region"],
            "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
            "source_order": query["source_order"],
            "page": query["page"],
            "page_size": query["page_size"],
        },
        "candidates": candidates,
        "condition_summary": condition_summary,
        "condition_context": condition_context,
        "pagination": pagination,
        "auth_state": auth_state,
        "raw_response_ref": raw_response_ref,
        "artifact_refs": artifact_refs,
        "warnings": [*query.get("warnings", []), *capture_warnings],
    }
    return result


def _inline_fastmoss_search_pages(payload: dict[str, Any], *, first_page: int) -> list[dict[str, Any]]:
    for key in (
        "fastmoss_search_response",
        "product_search_response",
        "search_response",
        "mock_fastmoss_search_response",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return [{"page": first_page, "response": candidate}]

    for key in (
        "fastmoss_search_pages",
        "product_search_pages",
        "search_pages",
        "mock_fastmoss_search_pages",
    ):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        pages: list[dict[str, Any]] = []
        for index, item in enumerate(value, start=0):
            record = coerce_mapping(item)
            if not record:
                continue
            response = coerce_mapping(record.get("response")) or coerce_mapping(record.get("payload")) or record
            pages.append(
                {
                    "page": _positive_int(record.get("page"), first_page + index) or first_page + index,
                    "response": response,
                    "stop_reason": coerce_str(record.get("stop_reason")),
                }
            )
        if pages:
            return pages
    return []


def _capture_fastmoss_search_raw_response(
    context: HandlerContext,
    payload: dict[str, Any],
    *,
    raw_pages: list[dict[str, Any]],
    query: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    raw_capture_policy = coerce_mapping(query.get("raw_capture_policy"))
    if not coerce_bool(raw_capture_policy.get("store_raw_response"), default=True):
        return "", [], []

    artifact_settings = _resolve_artifact_settings(payload)
    artifact_store = create_store_from_settings(artifact_settings)
    artifact_root = Path(
        first_non_empty(
            payload.get("artifact_root"),
            payload.get("execution_control_artifact_root"),
            tempfile.gettempdir(),
        )
    )
    artifact_bucket = first_non_empty(
        payload.get("artifact_bucket"),
        artifact_settings.get("artifact_bucket"),
        "runtime-artifacts",
    )
    artifact_object_prefix = first_non_empty(
        payload.get("artifact_object_prefix"),
        artifact_settings.get("artifact_object_prefix"),
    )
    run_id = first_non_empty(payload.get("run_id"), context.metadata.get("run_id"), context.job_id)
    relative_name = "artifacts/fastmoss_product_search/raw_response.json"
    raw_path = artifact_root / "runs" / run_id / relative_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {
                "query": {
                    "keyword": query["keyword"],
                    "region": query["region"],
                    "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                    "source_order": query["source_order"],
                },
                "pages": raw_pages,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    records, _artifact_uri_prefix = sync_artifact_specs(
        run_id=run_id,
        request_id=context.request_id,
        execution_id=context.job_id,
        artifact_root=artifact_root,
        artifact_bucket=artifact_bucket,
        artifact_object_prefix=artifact_object_prefix,
        specs=[
            ArtifactFileSpec(
                kind="fastmoss_product_search_raw_json",
                step_id=context.handler_code,
                relative_name=relative_name,
                path=raw_path,
                content_type="application/json",
                metadata={
                    "source_platform": "fastmoss",
                    "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                },
            )
        ],
        artifact_store=artifact_store,
        created_at=now_timestamp(),
    )
    if not records:
        return raw_path.resolve().as_uri(), [], []
    record = records[0]
    raw_response_ref = first_non_empty(
        record.metadata.get("remote_uri"),
        record.metadata.get("local_uri"),
        Path(record.source_path).resolve().as_uri(),
    )
    return raw_response_ref, [record.to_dict() for record in records], []


def _normalize_fastmoss_search_candidate(
    row: dict[str, Any],
    *,
    query: dict[str, Any],
    page_number: int,
    raw_index: int,
    raw_response_ref: str,
) -> dict[str, Any]:
    product_id = first_non_empty(
        row.get("product_id"),
        row.get("id"),
        extract_product_id(row.get("detail_url"), row.get("product_url")),
    )
    normalized_product_url = first_non_empty(
        row.get("normalized_product_url"),
        row.get("product_url"),
        row.get("tiktok_product_url"),
        _tiktok_product_url(product_id),
    )
    title_raw = first_non_empty(row.get("title"), row.get("product_title"), row.get("name"))
    title = _strip_html(title_raw)
    shop_info = coerce_mapping(row.get("shop_info"))
    currency = first_non_empty(
        row.get("currency"),
        shop_info.get("currency"),
        coerce_mapping(coerce_mapping(query.get("filters")).get("price_range")).get("currency"),
        "USD",
    )
    price_display = first_non_empty(row.get("price"), row.get("price_show"))
    price_amounts = _parse_price_amounts(price_display)
    candidate = {
        "source": "fastmoss",
        "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        "product_id": product_id,
        "normalized_product_url": normalized_product_url,
        "product_url": normalized_product_url,
        "fastmoss_product_url": _fastmoss_product_detail_url(product_id),
        "detail_url": first_non_empty(row.get("detail_url")),
        "title": title,
        "title_raw": title_raw,
        "image_url": first_non_empty(row.get("img"), row.get("image_url"), row.get("cover")),
        "shop": {
            "seller_id": first_non_empty(
                shop_info.get("seller_id"),
                shop_info.get("shop_id"),
                row.get("seller_id"),
            ),
            "shop_name": first_non_empty(row.get("shop_name"), shop_info.get("shop_name"), shop_info.get("name")),
            "raw": shop_info,
        },
        "price": {
            "amount": price_amounts.get("amount"),
            "min_amount": price_amounts.get("min_amount"),
            "max_amount": price_amounts.get("max_amount"),
            "currency": currency,
            "display": price_display,
        },
        "original_price": {
            "amount": _parse_number(row.get("ori_price")),
            "currency": currency,
            "display": first_non_empty(row.get("ori_price"), row.get("original_price_show")),
        },
        "commission": {
            "rate": _parse_rate(first_non_empty(row.get("crate"), row.get("commission_rate"))),
            "display": first_non_empty(row.get("crate_show"), row.get("commission_rate_show")),
        },
        "metrics": _fastmoss_product_search_metrics(row),
        "trend": _fastmoss_product_search_trend(row),
        "associated_holidays": (
            _list_text(row.get("associated_holidays"))
            or _list_text(row.get("holiday_tags"))
            or _list_text(row.get("holidays"))
            or _list_text(row.get("关联节日"))
        ),
        "dedupe_keys": {
            "product_id": product_id,
            "normalized_product_url": normalized_product_url,
        },
        "matched_conditions": {},
        "deferred_conditions": {},
        "quality_score": 1.0,
        "raw_item_ref": f"{raw_response_ref}#page-{page_number}/product_list/{raw_index}" if raw_response_ref else "",
        "page": page_number,
        "raw_index": raw_index,
    }
    return candidate


def _fastmoss_product_search_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sold_count": _parse_number(row.get("sold_count")),
        "sale_amount": _parse_number(row.get("sale_amount")),
        "yday_sold_count": _parse_number(row.get("yday_sold_count")),
        "day7_sold_count": _parse_number(row.get("day7_sold_count")),
        "day14_sold_count": _parse_number(row.get("day14_sold_count")),
        "day28_sold_count": _parse_number(row.get("day28_sold_count")),
        "relate_author_count": _parse_number(row.get("relate_author_count")),
        "relate_video_count": _parse_number(row.get("relate_video_count")),
        "relate_live_count": _parse_number(row.get("relate_live_count")),
        "product_rating": _parse_number(row.get("product_rating")),
    }


def _fastmoss_product_search_trend(row: dict[str, Any]) -> list[dict[str, Any]]:
    trend: list[dict[str, Any]] = []
    for item in coerce_mapping_list(row.get("trend")):
        trend.append(
            compact_dict(
                {
                    "date": first_non_empty(item.get("date"), item.get("dt")),
                    "inc_sold_count": _parse_number(item.get("inc_sold_count")),
                    "inc_sale_amount": _parse_number(item.get("inc_sale_amount")),
                    "region": item.get("region"),
                    "region_name": item.get("region_name"),
                }
            )
        )
    return trend


def _evaluate_fastmoss_output_conditions(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, Any], bool]:
    business_conditions = coerce_mapping(output_conditions.get("business_conditions"))
    matched: dict[str, bool] = {}
    deferred: dict[str, Any] = {}
    for key, expected in business_conditions.items():
        threshold = _parse_number(expected)
        if key == "min_day7_sold_count":
            matched[key] = _number_at_least(candidate["metrics"].get("day7_sold_count"), threshold)
        elif key == "min_sold_count":
            matched[key] = _number_at_least(candidate["metrics"].get("sold_count"), threshold)
        elif key == "min_sale_amount":
            matched[key] = _number_at_least(candidate["metrics"].get("sale_amount"), threshold)
        elif key == "min_product_rating":
            matched[key] = _number_at_least(candidate["metrics"].get("product_rating"), threshold)
        elif key == "min_relate_author_count":
            matched[key] = _number_at_least(candidate["metrics"].get("relate_author_count"), threshold)
        elif key == "max_price_amount":
            matched[key] = _number_at_most(candidate["price"].get("amount"), threshold)
        elif key in {"min_price_range_max_amount", "min_price_max_amount", "min_product_price_amount"}:
            matched[key] = _number_at_least(candidate["price"].get("max_amount"), threshold)
        elif key == "min_commission_rate":
            matched[key] = _number_at_least(candidate["commission"].get("rate"), threshold)
        else:
            deferred[key] = expected
    return matched, deferred, all(matched.values())


def _fastmoss_candidate_allowed(candidate: dict[str, Any], output_conditions: dict[str, Any]) -> bool:
    allowed_ids = {coerce_str(item) for item in output_conditions.get("allowed_product_ids") or [] if coerce_str(item)}
    excluded_ids = {coerce_str(item) for item in output_conditions.get("exclude_product_ids") or [] if coerce_str(item)}
    product_id = coerce_str(candidate.get("product_id"))
    if allowed_ids and product_id not in allowed_ids:
        return False
    if excluded_ids and product_id in excluded_ids:
        return False
    if coerce_bool(output_conditions.get("require_product_url"), default=False) and not coerce_str(
        candidate.get("normalized_product_url")
    ):
        return False
    min_quality_score = _parse_number(output_conditions.get("min_quality_score"))
    if min_quality_score is not None and float(candidate.get("quality_score") or 0.0) < float(min_quality_score):
        return False
    return True


def _fastmoss_candidate_quality_score(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> float:
    required_fields = [coerce_str(item) for item in output_conditions.get("required_fields") or [] if coerce_str(item)]
    if not required_fields:
        return 1.0
    present_count = sum(1 for field_name in required_fields if _candidate_field_value(candidate, field_name) not in ("", None))
    return round(present_count / len(required_fields), 4)


def _fastmoss_candidate_dedupe_key(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> str:
    dedupe_fields = [coerce_str(item) for item in output_conditions.get("dedupe_by") or [] if coerce_str(item)]
    if not dedupe_fields:
        dedupe_fields = ["product_id", "normalized_product_url"]
    parts = []
    for field_name in dedupe_fields:
        value = _candidate_field_value(candidate, field_name)
        if value not in ("", None):
            parts.append(f"{field_name}:{value}")
    return "|".join(parts) or json_fingerprint(candidate)


def _candidate_field_value(candidate: Mapping[str, Any], field_name: str) -> Any:
    current: Any = candidate
    for part in coerce_str(field_name).split("."):
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part)
    return current


def _fastmoss_search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = coerce_mapping(payload.get("data"))
    return (
        coerce_mapping_list(data.get("product_list"))
        or coerce_mapping_list(data.get("list"))
        or coerce_mapping_list(data.get("goods_list"))
    )


def _fastmoss_product_row_key(row: dict[str, Any]) -> str:
    return first_non_empty(row.get("product_id"), row.get("id"), row.get("detail_url"), row.get("title"))


def _fastmoss_auth_state_from_payloads(
    payloads: list[dict[str, Any]],
    *,
    session_snapshot: dict[str, Any],
) -> dict[str, Any]:
    source_code = ""
    source_msg = ""
    is_login = bool(session_snapshot.get("has_fd_tk"))
    degraded_preview = False
    for payload in payloads:
        source_code = first_non_empty(payload.get("code"), source_code)
        source_msg = first_non_empty(payload.get("msg"), source_msg)
        ext = coerce_mapping(payload.get("ext"))
        if ext.get("is_login") in {1, "1", True}:
            is_login = True
        if ext.get("is_login") in {0, "0", False}:
            degraded_preview = True
        if coerce_str(payload.get("code")) == "MAG_AUTH_3001":
            degraded_preview = True
    return compact_dict(
        {
            "is_login": is_login,
            "degraded_preview": degraded_preview,
            "source_code": source_code,
            "source_msg": source_msg,
            "session_snapshot": session_snapshot,
        }
    )


def _build_fastmoss_search_pagination(
    raw_pages: list[dict[str, Any]],
    *,
    query: dict[str, Any],
    runtime_pagination: dict[str, Any],
    accepted_count: int,
) -> dict[str, Any]:
    last_record = raw_pages[-1] if raw_pages else {}
    last_payload = coerce_mapping(last_record.get("response"))
    data = coerce_mapping(last_payload.get("data"))
    total = _positive_int(
        first_non_empty(data.get("total"), data.get("total_cnt"), data.get("result_cnt")),
        0,
    )
    last_page = _positive_int(last_record.get("page"), int(query["page"])) or int(query["page"])
    stop_reason = first_non_empty(
        runtime_pagination.get("stop_reason"),
        last_record.get("stop_reason"),
        "completed",
    )
    has_more = False
    if total > 0:
        has_more = last_page * int(query["page_size"]) < total
    if stop_reason in {
        "empty_page",
        "no_new_product",
        "degraded_preview",
        "max_candidates",
        "below_min_day7_sold_count",
    }:
        has_more = False
    return {
        "page": int(query["page"]),
        "page_size": int(query["page_size"]),
        "total": total,
        "has_more": has_more,
        "next_page": last_page + 1 if has_more else None,
        "stop_reason": stop_reason,
        "accepted_count": accepted_count,
        "fetched_pages": len(raw_pages),
    }


def _pagination_runtime_from_raw_pages(
    raw_pages: list[dict[str, Any]],
    *,
    query: dict[str, Any],
) -> dict[str, Any]:
    if not raw_pages:
        return {"stop_reason": "empty_page"}
    stop_reason = first_non_empty(raw_pages[-1].get("stop_reason"))
    if stop_reason:
        return {"stop_reason": stop_reason}
    last_rows = _fastmoss_search_rows(coerce_mapping(raw_pages[-1].get("response")))
    if not last_rows:
        return {"stop_reason": "empty_page"}
    if len(raw_pages) >= int(query["max_pages"]):
        return {"stop_reason": "max_pages"}
    return {"stop_reason": "inline_response"}


def _fastmoss_search_extra_params(filters: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    extra_params: dict[str, Any] = {}
    warnings: list[str] = []
    for source in (coerce_mapping(filters.get("extra")), coerce_mapping(filters.get("source_params"))):
        for key, value in source.items():
            normalized_key = coerce_str(key)
            if not normalized_key or normalized_key in {
                "page",
                "pagesize",
                "page_size",
                "order",
                "region",
                "words",
                "_time",
                "cnonce",
                "fm-sign",
            }:
                continue
            if isinstance(value, (str, int, float, bool)):
                extra_params[normalized_key] = value
    ignored_filter_keys = sorted(
        key
        for key in filters
        if key not in {"country_code", "region", "extra", "source_params"}
        and filters.get(key) not in (None, "", [], {})
    )
    if ignored_filter_keys:
        warnings.append(f"FastMoss search ignored unsupported input filters: {', '.join(ignored_filter_keys)}.")
    return extra_params, warnings


def _source_order_from_sort(sort: dict[str, Any]) -> str:
    field = coerce_str(sort.get("field"))
    direction = coerce_str(sort.get("direction")).lower()
    if field == "day7_sold_count" and direction in {"", "desc", "descending"}:
        return "2,2"
    return ""


def _strip_html(value: Any) -> str:
    text = html.unescape(coerce_str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = coerce_str(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([kKmMwW万亿]?)", text)
    if match is None:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1_000
    elif suffix in {"m", "w", "万"}:
        number *= 1_000_000 if suffix == "m" else 10_000
    elif suffix == "亿":
        number *= 100_000_000
    return int(number) if number.is_integer() else number


def _parse_price_amounts(value: Any) -> dict[str, int | float | None]:
    if value is None or isinstance(value, bool):
        return {"amount": None, "min_amount": None, "max_amount": None}
    if isinstance(value, (int, float)):
        number = _parse_number(value)
        return {"amount": number, "min_amount": number, "max_amount": number}
    text = coerce_str(value).replace(",", "")
    if not text:
        return {"amount": None, "min_amount": None, "max_amount": None}
    numbers: list[int | float] = []
    for match in re.finditer(r"([-+]?\d+(?:\.\d+)?)\s*([kKmMwW万亿]?)", text):
        number = _parse_number(match.group(0))
        if number is not None:
            numbers.append(number)
    if not numbers:
        return {"amount": None, "min_amount": None, "max_amount": None}
    return {"amount": numbers[0], "min_amount": min(numbers), "max_amount": max(numbers)}


def _parse_rate(value: Any) -> float | None:
    text = coerce_str(value)
    number = _parse_number(text)
    if number is None:
        return None
    rate = float(number)
    if "%" in text or rate > 1:
        rate /= 100
    return round(rate, 6)


def _number_at_least(value: Any, threshold: Any) -> bool:
    value_number = _parse_number(value)
    threshold_number = _parse_number(threshold)
    if value_number is None or threshold_number is None:
        return False
    return float(value_number) >= float(threshold_number)


def _number_at_most(value: Any, threshold: Any) -> bool:
    value_number = _parse_number(value)
    threshold_number = _parse_number(threshold)
    if value_number is None or threshold_number is None:
        return False
    return float(value_number) <= float(threshold_number)


def _positive_int(value: Any, default: int) -> int:
    number = _parse_number(value)
    if number is None:
        return default
    try:
        integer = int(number)
    except (TypeError, ValueError):
        return default
    return integer if integer > 0 else default


def _non_negative_int(value: Any, default: int) -> int:
    number = _parse_number(value)
    if number is None:
        return default
    try:
        integer = int(number)
    except (TypeError, ValueError):
        return default
    return integer if integer >= 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _env_value(env_name: str) -> str:
    name = coerce_str(env_name)
    if not name:
        return ""
    return coerce_str(os.environ.get(name))


def _tiktok_product_url(product_id: str) -> str:
    return TIKTOK_PRODUCT_URL_TEMPLATE.format(product_id=product_id) if product_id else ""


def _fastmoss_product_detail_url(product_id: str) -> str:
    return FASTMOSS_PRODUCT_DETAIL_URL_TEMPLATE.format(product_id=product_id) if product_id else ""


def _resolve_artifact_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = coerce_mapping(payload.get("artifact_store"))
    if settings:
        return settings
    return compact_dict(
        {
            "artifact_store_provider": payload.get("artifact_store_provider"),
            "artifact_bucket": payload.get("artifact_bucket"),
            "artifact_object_prefix": payload.get("artifact_object_prefix"),
            "minio_endpoint": payload.get("minio_endpoint"),
            "minio_access_key": payload.get("minio_access_key"),
            "minio_secret_key": payload.get("minio_secret_key"),
            "minio_secure": payload.get("minio_secure"),
            "minio_region": payload.get("minio_region"),
            "minio_create_bucket": payload.get("minio_create_bucket"),
        }
    )


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_product_search_handler"]
