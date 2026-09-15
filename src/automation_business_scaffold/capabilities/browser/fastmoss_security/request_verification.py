from __future__ import annotations

from contextlib import contextmanager
import secrets
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

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
    build_fm_sign,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range

FASTMOSS_PRODUCT_SEARCH_ENDPOINT = "/api/goods/V2/search"
FASTMOSS_SECURITY_VERIFICATION_CODES = {"MSG_SAFE_0001"}
FASTMOSS_AUTH_VERIFICATION_CODES = {"MAG_AUTH_3001", "MAG_AUTH_3002", "MAG_AUTH_3017", "MSG_30001"}


@contextmanager
def observe_browser_verification(page: Any, verification_request: Mapping[str, Any], *, base_url: str):
    """Observe server confirmation on this page without persisting response bodies."""
    evidence = {
        "captcha_required": False, "captcha_confirmed": False,
        "browser_verified": False, "http_verified": False, "http_replayed": False,
        "captcha_response_code": "", "browser_response_code": "", "observation_errors": 0,
    }
    origin = urlsplit(base_url)
    identity = {
        key: str(value) for key, value in coerce_mapping(verification_request.get("params")).items()
        if key in {"uid", "author_uid", "unique_id", "product_id", "goods_id", "seller_id", "shop_id", "video_id", "id"}
    }

    def completed(request):
        url = urlsplit(request.url)
        if (url.scheme, url.netloc) != (origin.scheme, origin.netloc):
            return
        is_captcha = url.path in {"/api/captcha/config", "/api/captcha/verify"} and request.method == "POST"
        is_business = (
            url.path == verification_request.get("path")
            and request.method == verification_request.get("method", "GET").upper()
            and all(parse_qs(url.query).get(key) == [value] for key, value in identity.items())
        )
        if not (is_captcha or is_business):
            return
        try:
            response = request.response()
            body = coerce_mapping(response.json())
            code = str(body.get("code", ""))
            # Do not persist arbitrary server messages, tokens, headers, or IDs.
            if len(code) > 64 or not code.replace("_", "").isalnum():
                code = "unknown"
            success = 200 <= response.status < 300 and code == "200"
            success = success and coerce_mapping(body.get("ext")).get("is_login") not in (0, "0", False)
            if is_captcha:
                evidence["captcha_required"] = True
                evidence["browser_verified"] = False
                accepted = coerce_mapping(body.get("data")).get("is_ok") in (True, 1, "1")
                evidence["captcha_confirmed"] = success and accepted if url.path == "/api/captcha/verify" else False
                evidence["captcha_response_code"] = code if url.path == "/api/captcha/verify" else ""
            else:
                evidence["browser_response_code"] = code
                evidence["browser_verified"] = success
                if code in FASTMOSS_SECURITY_VERIFICATION_CODES:
                    evidence["captcha_required"] = True
        except Exception:
            evidence["observation_errors"] += 1

    supported = callable(getattr(page, "on", None)) and callable(getattr(page, "remove_listener", None))
    if supported:
        page.on("requestfinished", completed)
    try:
        yield evidence
    finally:
        if supported:
            page.remove_listener("requestfinished", completed)


def browser_verification_failure_stage(evidence: Mapping[str, Any]) -> str:
    if evidence.get("captcha_required") and not evidence.get("captcha_confirmed"):
        return "captcha_confirmation"
    if not evidence.get("browser_verified"):
        return "browser_business"
    return ""


def request_original_api_in_browser(
    page: Any, verification_request: Mapping[str, Any], *, base_url: str, timeout_ms: int,
) -> None:
    """Load an original read API that the detail page does not request itself."""
    url = urljoin(base_url.rstrip("/") + "/", str(verification_request.get("path", "")))
    origin, target = urlsplit(base_url), urlsplit(url)
    if (
        verification_request.get("method", "GET").upper() != "GET"
        or (origin.scheme, origin.netloc) != (target.scheme, target.netloc)
        or not target.path.startswith("/api/")
    ):
        raise ValueError("Browser verification probe requires a same-origin GET API.")
    params = {key: value for key, value in coerce_mapping(verification_request.get("params")).items()
              if value is not None and value != ""}
    params.update(_time=int(time.time()), cnonce=str(10000000 + secrets.randbelow(90000000)))
    query = urlencode(sorted(params.items())).replace("~", "%7E")
    signed_path = target.path + "?" + query
    page.evaluate("""async ({url, path, headers, timeout_ms}) => {
        headers["fm-sig"] = globalThis.__SIG__.gen(path, {source: "pc"});
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout_ms);
        try {
            const response = await fetch(url, {method: "GET", headers, credentials: "include",
                cache: "no-store", signal: controller.signal});
            await response.arrayBuffer();
        } finally { clearTimeout(timer); }
    }""", {
        "url": f"{origin.scheme}://{origin.netloc}{signed_path}", "path": signed_path,
        "timeout_ms": timeout_ms,
        "headers": {"source": "pc", "region": str(verification_request.get("region") or "US"),
                    "lang": "ZH_CN", "fm-sign": build_fm_sign(params)},
    })


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
            retries=1,
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
        if not (is_fastmoss_security_error(exc) or is_fastmoss_auth_error(exc)):
            raise
        details = redact_fastmoss_http_error(exc)
        auth_error = is_fastmoss_auth_error(exc)
        return compact_dict(
            {
                "verified": False,
                "verified_path": first_non_empty(
                    details.get("path"),
                    verification_request.get("path"),
                    FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                ),
                "response_code": first_non_empty(details.get("response_code"), exc.response_code),
                "error_code": (
                    "fastmoss_auth_session_recovery_required"
                    if auth_error
                    else "fastmoss_security_verification_required"
                ),
                "error_type": "auth_failure" if auth_error else "security_verification",
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


def is_fastmoss_auth_error(exc: FastMossHTTPError) -> bool:
    code = coerce_str(exc.response_code)
    if code in FASTMOSS_AUTH_VERIFICATION_CODES or code.startswith("MAG_AUTH_"):
        return True
    payload = coerce_mapping(exc.payload)
    ext = coerce_mapping(payload.get("ext"))
    return ext.get("is_login") in {0, "0", False}


def redact_replay_params(params: Mapping[str, Any]) -> dict[str, Any]:
    replay = dict(params)
    for key in ("fm-sign", "cnonce", "_time"):
        replay.pop(key, None)
    return replay
