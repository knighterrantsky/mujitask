from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerNextAction, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    compact_dict,
    fallback_required_result,
    first_non_empty,
    failed_result,
)
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    attach_fastmoss_cookie_cache,
    refresh_fastmoss_session_cookies,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
    FastMossSessionConflictError,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

FASTMOSS_SECURITY_VERIFICATION_CODES = {"MSG_SAFE_0001"}


def is_fastmoss_security_verification_error(exc: FastMossHTTPError) -> bool:
    return str(exc.response_code or "").strip() in FASTMOSS_SECURITY_VERIFICATION_CODES


def is_fastmoss_session_conflict_error(exc: FastMossHTTPError) -> bool:
    return isinstance(exc, FastMossSessionConflictError)


def fastmoss_session_conflict_failed_result(
    context: HandlerContext,
    *,
    exc: FastMossHTTPError,
    operation: str,
    summary: Mapping[str, Any] | None = None,
) -> HandlerResult:
    error = build_error(
        error_type="auth_failure",
        error_code="fastmoss_session_conflict_or_external_login",
        message=str(exc),
        retryable=False,
        details=redact_fastmoss_http_error(exc),
    )
    return failed_result(
        context,
        error=error,
        summary=compact_dict(
            {
                **dict(summary or {}),
                "operation": operation,
                "response_code": error.details.get("response_code"),
                "path": error.details.get("path"),
                "stage": error.details.get("stage"),
            }
        ),
    )


def fastmoss_security_fallback_required_result(
    context: HandlerContext,
    *,
    exc: FastMossHTTPError,
    handler_payload: Mapping[str, Any],
    fastmoss_settings: Mapping[str, Any],
    operation: str,
    entity_identity: Mapping[str, Any] | None = None,
) -> HandlerResult:
    details = redact_fastmoss_http_error(exc)
    fallback_payload = fastmoss_security_fallback_payload(
        context,
        exc=exc,
        handler_payload=handler_payload,
        fastmoss_settings=fastmoss_settings,
        operation=operation,
        entity_identity=entity_identity or {},
    )
    error = build_error(
        error_type="security_verification",
        error_code="fastmoss_security_verification_required",
        message=str(exc) or "FastMoss security verification is required.",
        retryable=False,
        fallback_allowed=True,
        fallback_reason="fastmoss_api_security_verification",
        details=details,
    )
    return fallback_required_result(
        context,
        error=error,
        summary={
            "fallback_required": True,
            "fallback_reason": "fastmoss_api_security_verification",
            "operation": operation,
            "response_code": details.get("response_code"),
            "path": details.get("path"),
            "stage": details.get("stage"),
        },
        result=fallback_payload,
        next_action=HandlerNextAction(type="browser_fallback", payload=fallback_payload),
    )


def fastmoss_security_fallback_payload(
    context: HandlerContext,
    *,
    exc: FastMossHTTPError,
    handler_payload: Mapping[str, Any],
    fastmoss_settings: Mapping[str, Any],
    operation: str,
    entity_identity: Mapping[str, Any],
) -> dict[str, Any]:
    details = redact_fastmoss_http_error(exc)
    verification_request = _verification_request_from_error(exc, fastmoss_settings=fastmoss_settings)
    return compact_dict(
        {
            "fallback_required": True,
            "fallback_reason": "fastmoss_api_security_verification",
            "source_handler_code": context.handler_code,
            "retry_handler_code": context.handler_code,
            "operation": operation,
            "entity_identity": dict(entity_identity),
            "security_context": details,
            "verification_request": verification_request,
            "request_payload": coerce_mapping(handler_payload.get("request_payload")),
            "fastmoss": redact_fastmoss_settings(fastmoss_settings),
        }
    )


def redact_fastmoss_http_error(exc: FastMossHTTPError) -> dict[str, Any]:
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
            "params": _redact_request_params(exc.params or {}),
            "referer": exc.referer,
            "region": exc.region,
            "data_id": data.get("id"),
            "ext_is_login": ext.get("is_login"),
        }
    )


def redact_fastmoss_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(settings)
    for key in ("password", "browser_cookies"):
        redacted.pop(key, None)
    return {key: value for key, value in redacted.items() if value not in ("", None, [], {})}


def attach_fastmoss_cookie_cache_if_configured(
    session: FastMossHTTPSession,
    *,
    settings: Mapping[str, Any],
    default_region: str = "US",
    account_required: bool = False,
) -> dict[str, Any]:
    db_url = _runtime_db_url(settings)
    if not db_url:
        return {"enabled": False, "reason": "missing_db_url"}
    resolved_account_key = first_non_empty(
        settings.get("account_key"),
        settings.get("phone"),
        settings.get("phone_env"),
    )
    if account_required and not resolved_account_key:
        return {}
    enabled = coerce_bool(settings.get("cookie_cache_enabled"), default=True)
    if not enabled:
        return {"enabled": False, "reason": "disabled"}
    account_key = first_non_empty(resolved_account_key, "default")
    ttl_seconds = _non_negative_float(
        settings.get("cookie_cache_ttl_seconds"),
        DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    )
    return attach_fastmoss_cookie_cache(
        session,
        store=RuntimeStore(db_url=db_url),
        account_key=account_key,
        region=first_non_empty(settings.get("region"), default_region),
        namespace=first_non_empty(settings.get("cookie_cache_namespace")),
        enabled=enabled,
        ttl_seconds=ttl_seconds,
    )


def build_fastmoss_session(
    settings: Mapping[str, Any],
    *,
    default_region: str = "US",
    session_factory: Callable[..., FastMossHTTPSession] = FastMossHTTPSession,
) -> FastMossHTTPSession:
    return session_factory(
        phone=first_non_empty(settings.get("phone")),
        password=first_non_empty(settings.get("password")),
        base_url=first_non_empty(settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(settings.get("region"), default_region),
        timeout=float(settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(settings, provider="fastmoss"),
        trust_env=coerce_bool(
            first_non_empty(
                settings.get("trust_env"),
                settings.get("use_system_proxy"),
                settings.get("fastmoss_trust_env"),
                settings.get("fastmoss_use_system_proxy"),
            ),
            default=False,
        ),
    )


def prepare_fastmoss_session(
    session: FastMossHTTPSession,
    *,
    settings: Mapping[str, Any],
    default_region: str = "US",
    require_login: bool | None = None,
    account_required: bool = False,
    cookie_cache_errors: str = "raise",
) -> dict[str, Any]:
    try:
        cookie_cache_status = attach_fastmoss_cookie_cache_if_configured(
            session,
            settings=settings,
            default_region=first_non_empty(settings.get("region"), default_region),
            account_required=account_required,
        )
    except Exception as exc:  # noqa: BLE001
        if cookie_cache_errors != "return":
            raise
        cookie_cache_status = {
            "enabled": coerce_bool(settings.get("cookie_cache_enabled"), default=True),
            "status": "unavailable",
            "reason": str(exc),
        }

    cookies = settings.get("browser_cookies")
    if isinstance(cookies, list) and cookies:
        session.replace_browser_cookies(cookies)
    login_required = (
        bool(require_login)
        if require_login is not None
        else coerce_bool(settings.get("ensure_logged_in"), default=bool(cookies or settings.get("phone")))
    )
    if login_required:
        session.ensure_logged_in()
    return cookie_cache_status


def refresh_fastmoss_session_after_security_check(
    session: FastMossHTTPSession,
    *,
    settings: Mapping[str, Any],
    default_region: str = "US",
    cookies: list[Mapping[str, Any]] | None = None,
    has_credentials: bool | None = None,
    reason: str = "fastmoss_security_check_login_refresh",
) -> bool:
    cookie_rows = cookies if cookies is not None else settings.get("browser_cookies")
    cookie_rows = cookie_rows if isinstance(cookie_rows, list) else []
    credentials_available = (
        bool(has_credentials)
        if has_credentials is not None
        else bool(first_non_empty(settings.get("phone")) and first_non_empty(settings.get("password")))
    )
    if not credentials_available and not cookie_rows:
        return False

    db_url = _runtime_db_url(settings)
    account_key = first_non_empty(
        settings.get("account_key"),
        settings.get("phone"),
        settings.get("phone_env"),
    )
    store = RuntimeStore(db_url=db_url) if db_url and account_key else None
    refresh_fastmoss_session_cookies(
        session,
        store=store,
        account_key=first_non_empty(account_key, "default"),
        region=first_non_empty(settings.get("region"), default_region),
        namespace=first_non_empty(settings.get("cookie_cache_namespace")),
        enabled=coerce_bool(settings.get("cookie_cache_enabled"), default=True),
        cookies=cookie_rows,
        prefer_login=credentials_available,
        ttl_seconds=_non_negative_float(
            settings.get("cookie_cache_ttl_seconds"),
            DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
        ),
        reason=reason,
    )
    return True


def fastmoss_settings_from_payload(payload: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
    settings = {
        **dict(defaults or {}),
        **coerce_mapping(request_payload.get("fastmoss")),
        **coerce_mapping(payload.get("fastmoss")),
    }
    explicit_live_fetch = first_non_empty(
        settings.get("live_fetch"),
        payload.get("fastmoss_live_fetch"),
        request_payload.get("fastmoss_live_fetch"),
    )
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
            os.environ.get(phone_env, ""),
        ),
        "password": first_non_empty(
            settings.get("password"),
            payload.get("fastmoss_password"),
            request_payload.get("fastmoss_password"),
            os.environ.get(password_env, ""),
        ),
        "phone_env": phone_env,
        "password_env": password_env,
        "base_url": first_non_empty(
            settings.get("base_url"),
            payload.get("fastmoss_base_url"),
            request_payload.get("fastmoss_base_url"),
            "https://www.fastmoss.com",
        ),
        "region": first_non_empty(settings.get("region"), payload.get("region"), request_payload.get("region"), "US"),
        "timeout": settings.get("timeout", payload.get("fastmoss_timeout", request_payload.get("fastmoss_timeout", 30.0))),
        "browser_cookies": settings.get("browser_cookies", []),
        "live_fetch": explicit_live_fetch,
        "_has_live_config": _has_fastmoss_live_config(settings, payload, request_payload),
        "ensure_logged_in": settings.get(
            "ensure_logged_in",
            payload.get("ensure_fastmoss_logged_in", request_payload.get("ensure_fastmoss_logged_in", None)),
        ),
        "execution_control_db_url": first_non_empty(
            settings.get("execution_control_db_url"),
            settings.get("db_url"),
            _runtime_db_url({}),
        ),
        "db_url": first_non_empty(
            settings.get("db_url"),
            _runtime_db_url({}),
        ),
        "cookie_cache_namespace": first_non_empty(
            settings.get("cookie_cache_namespace"),
            payload.get("fastmoss_cookie_cache_namespace"),
            request_payload.get("fastmoss_cookie_cache_namespace"),
        ),
        "cookie_cache_enabled": first_non_empty(
            settings.get("cookie_cache_enabled"),
            payload.get("fastmoss_cookie_cache_enabled"),
            request_payload.get("fastmoss_cookie_cache_enabled"),
            True,
        ),
        "cookie_cache_ttl_seconds": first_non_empty(
            settings.get("cookie_cache_ttl_seconds"),
            payload.get("fastmoss_cookie_cache_ttl_seconds"),
            request_payload.get("fastmoss_cookie_cache_ttl_seconds"),
        ),
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
            settings.get("request_delay_min_seconds"),
            payload.get("api_request_delay_min_seconds"),
            request_payload.get("api_request_delay_min_seconds"),
        ),
        "api_request_delay_max_seconds": first_non_empty(
            settings.get("api_request_delay_max_seconds"),
            settings.get("request_delay_max_seconds"),
            payload.get("api_request_delay_max_seconds"),
            request_payload.get("api_request_delay_max_seconds"),
        ),
        "fastmoss_api_request_delay_min_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_min_seconds"),
            settings.get("fastmoss_request_delay_min_seconds"),
            payload.get("fastmoss_api_request_delay_min_seconds"),
            request_payload.get("fastmoss_api_request_delay_min_seconds"),
        ),
        "fastmoss_api_request_delay_max_seconds": first_non_empty(
            settings.get("fastmoss_api_request_delay_max_seconds"),
            settings.get("fastmoss_request_delay_max_seconds"),
            payload.get("fastmoss_api_request_delay_max_seconds"),
            request_payload.get("fastmoss_api_request_delay_max_seconds"),
        ),
    }


def _verification_request_from_error(
    exc: FastMossHTTPError,
    *,
    fastmoss_settings: Mapping[str, Any],
) -> dict[str, Any]:
    return compact_dict(
        {
            "method": first_non_empty(exc.method, "GET").upper(),
            "path": first_non_empty(exc.path),
            "params": _redact_request_params(exc.params or {}),
            "referer": first_non_empty(exc.referer),
            "region": first_non_empty(exc.region, fastmoss_settings.get("region"), "US"),
            "stage": first_non_empty(exc.stage),
        }
    )


def _redact_request_params(params: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(params)
    for key in ("fm-sign", "cnonce", "_time", "password", "pwd"):
        redacted.pop(key, None)
    return redacted


def _runtime_db_url(settings: Mapping[str, Any]) -> str:
    defaults = get_execution_control_defaults()
    return first_non_empty(
        settings.get("execution_control_db_url"),
        settings.get("db_url"),
        os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_URL"),
        os.environ.get("EXECUTION_CONTROL_DB_URL"),
        defaults.db_url,
    )


def _non_negative_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(number, 0.0)


def _has_fastmoss_live_config(
    settings: Mapping[str, Any],
    payload: Mapping[str, Any],
    request_payload: Mapping[str, Any],
) -> bool:
    markers = (
        "phone",
        "password",
        "phone_env",
        "password_env",
        "fastmoss_phone_env",
        "fastmoss_password_env",
        "browser_cookies",
    )
    return any(settings.get(key) not in ("", None, [], {}) for key in markers) or any(
        payload.get(key) not in ("", None, [], {}) or request_payload.get(key) not in ("", None, [], {})
        for key in (
            "fastmoss_phone",
            "fastmoss_password",
            "fastmoss_phone_env",
            "fastmoss_password_env",
            "browser_cookies",
        )
    )
