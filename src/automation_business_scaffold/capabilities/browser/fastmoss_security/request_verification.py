from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import (
    coerce_bool,
    coerce_mapping,
    coerce_str,
    compact_dict,
    first_non_empty,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range

FASTMOSS_PRODUCT_SEARCH_ENDPOINT = "/api/goods/V2/search"
FASTMOSS_SECURITY_VERIFICATION_CODES = {"MSG_SAFE_0001"}


def verify_original_request_with_cookies(
    verification_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
    default_referer: str = "",
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
            referer=first_non_empty(verification_request.get("referer"), default_referer),
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


def verify_original_request_with_cookies_result(
    verification_request: Mapping[str, Any],
    *,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
    default_referer: str = "",
) -> dict[str, Any]:
    try:
        return verify_original_request_with_cookies(
            verification_request,
            fastmoss_settings=fastmoss_settings,
            cookies=cookies,
            default_referer=default_referer,
        )
    except FastMossHTTPError as exc:
        if not is_fastmoss_security_error(exc):
            raise
        details = redact_fastmoss_http_error(exc)
        return compact_dict(
            {
                "verified": False,
                "verified_path": first_non_empty(
                    details.get("path"),
                    verification_request.get("path"),
                    FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                ),
                "response_code": first_non_empty(details.get("response_code"), exc.response_code),
                "error_code": "fastmoss_security_verification_required",
                "error_type": "security_verification",
                "data_id": first_non_empty(details.get("data_id")),
                "ext_is_login": first_non_empty(details.get("ext_is_login")),
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
            "params": redact_replay_params(exc.params or {}),
            "referer": exc.referer,
            "region": exc.region,
            "data_id": data.get("id"),
            "ext_is_login": ext.get("is_login"),
        }
    )


def is_fastmoss_security_error(exc: FastMossHTTPError) -> bool:
    return coerce_str(exc.response_code) in FASTMOSS_SECURITY_VERIFICATION_CODES


def redact_replay_params(params: Mapping[str, Any]) -> dict[str, Any]:
    replay = dict(params)
    for key in ("fm-sign", "cnonce", "_time"):
        replay.pop(key, None)
    return replay
