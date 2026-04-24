#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure HTTP FastMoss client helpers.

This module keeps FastMoss access fully inside a requests.Session and avoids
browser automation entirely. It is intentionally self-contained so it can be
introduced without touching workflow/task wiring.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import string
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

FASTMOSS_BASE_URL = "https://www.fastmoss.com"
FASTMOSS_ACCOUNT_CENTER_REFERER = "https://www.fastmoss.com/zh/account/center"
FASTMOSS_AUTHOR_SEARCH_REFERER_TEMPLATE = (
    "https://www.fastmoss.com/zh/influencer/search?"
    "shop_window={shop_window}&page={page}&words={words}&words_search_type={words_search_type}"
)
FASTMOSS_GOODS_SEARCH_REFERER_TEMPLATE = (
    "https://www.fastmoss.com/zh/e-commerce/search?region={region}&page={page}&words={words}"
)
FASTMOSS_GOODS_DETAIL_REFERER_TEMPLATE = "https://www.fastmoss.com/zh/e-commerce/detail/{product_id}"
FASTMOSS_AUTHOR_DETAIL_REFERER_TEMPLATE = "https://www.fastmoss.com/zh/influencer/detail/{uid}"
FASTMOSS_VIDEO_DETAIL_REFERER_TEMPLATE = "https://www.fastmoss.com/zh/media-source/video/{video_id}"
FASTMOSS_SHOP_DETAIL_REFERER_TEMPLATE = "https://www.fastmoss.com/zh/shop-marketing/detail/{shop_id}"
FASTMOSS_FM_SIGN_SALT = "LAA6edGHBkcc3eTiOIRfg89bu9ODA6PB"
FASTMOSS_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
FASTMOSS_DEFAULT_SEARCH_REGION = "US"
FASTMOSS_DEFAULT_LOGIN_REGION = "Global"
FASTMOSS_AUTHOR_SEARCH_DF = "ZnNfaHR0cHM6Ly93d3cuZmFzdG1vc3MuY29tX3Bz"

_FASTMOSS_AUTH_CODES = {
    "MAG_AUTH_3001",
    "MAG_AUTH_3002",
    "MAG_AUTH_3017",
    "MSG_30001",
}


@dataclass
class FastMossHTTPError(RuntimeError):
    """Raised when a FastMoss HTTP request fails."""

    message: str
    status_code: int | None = None
    response_code: Any | None = None
    payload: dict[str, Any] | None = None
    stage: str = ""
    method: str = ""
    path: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "status_code": self.status_code,
            "response_code": self.response_code,
            "stage": self.stage,
            "method": self.method,
            "path": self.path,
            "payload": self.payload or {},
        }


class FastMossAuthError(FastMossHTTPError):
    """Raised when FastMoss requires a fresh login."""


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_fm_sign(params: Mapping[str, Any], body_text: str = "") -> str:
    """Build the FastMoss fm-sign exactly as observed in the frontend bundle."""

    source = ""
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        source += f"{key}{_coerce_str(value)}{FASTMOSS_FM_SIGN_SALT}"

    md5_hex = hashlib.md5((source + body_text).encode("utf-8")).hexdigest()
    left = 0
    right = len(md5_hex) - 1
    prefix_chars: list[str] = []
    while left < right:
        prefix_chars.append(format(int(md5_hex[left], 16) ^ int(md5_hex[right], 16), "x"))
        left += 1
        right -= 1
    return "".join(prefix_chars) + md5_hex[left:]


def _default_nonce_factory() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(8))


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    if not params:
        return cleaned
    for key, value in params.items():
        if value is None:
            continue
        cleaned[str(key)] = value
    return cleaned


def _cookie_domain_matches(domain: str, domain_keyword: str) -> bool:
    normalized_domain = str(domain or "").strip().lstrip(".").lower()
    normalized_keyword = str(domain_keyword or "").strip().lstrip(".").lower()
    if not normalized_domain or not normalized_keyword:
        return False
    return normalized_domain == normalized_keyword or normalized_domain.endswith(f".{normalized_keyword}")


def _cookie_value_digest(value: str) -> str:
    normalized = str(value or "")
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _coerce_cookie_expires(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        expires = int(float(value))
    except (TypeError, ValueError):
        return None
    return expires if expires > 0 else None


def _coerce_cookie_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _response_code_text(payload: Mapping[str, Any]) -> str:
    return _coerce_str(payload.get("code")).strip()


def _is_success_code(payload: Mapping[str, Any]) -> bool:
    code_value = payload.get("code")
    if code_value in {0, 200, "0", "200"}:
        return True
    return _response_code_text(payload) in {"0", "200"}


def _is_auth_issue(payload: Mapping[str, Any]) -> bool:
    ext = payload.get("ext")
    if isinstance(ext, Mapping):
        is_login = ext.get("is_login")
        if is_login in {0, "0", False}:
            return True

    code_text = _response_code_text(payload)
    if code_text in _FASTMOSS_AUTH_CODES:
        return True
    if code_text.startswith("MAG_AUTH_"):
        return True

    msg_text = _coerce_str(payload.get("msg")).lower()
    if "请登录" in msg_text or "login" in msg_text or "游客" in msg_text:
        return True
    return False


def _extract_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _extract_list(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = _extract_data(payload)
    items = data.get("list")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


class FastMossHTTPSession:
    """Pure HTTP FastMoss client with in-memory login state only."""

    def __init__(
        self,
        phone: str | None = None,
        password: str | None = None,
        *,
        base_url: str = FASTMOSS_BASE_URL,
        timeout: float = 30.0,
        user_agent: str = FASTMOSS_DEFAULT_USER_AGENT,
        default_region: str = FASTMOSS_DEFAULT_SEARCH_REGION,
        request_delay_range: tuple[float, float] = (0.0, 0.0),
        time_factory: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
        sleep_factory: Callable[[float], None] = time.sleep,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        auth_refresh_callback: Callable[["FastMossHTTPSession", dict[str, Any]], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.default_region = default_region
        self.request_delay_range = request_delay_range
        self._phone = phone
        self._password = password
        self._time_factory = time_factory
        self._nonce_factory = nonce_factory or _default_nonce_factory
        self._sleep_factory = sleep_factory
        self._last_request_finished_at: float | None = None
        self._event_callback = event_callback
        self._auth_refresh_callback = auth_refresh_callback
        self._debug_context: dict[str, Any] = {}
        self.session = requests.Session()

    def __enter__(self) -> "FastMossHTTPSession":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    def set_debug_context(self, **context: Any) -> None:
        self._debug_context = {
            str(key): value
            for key, value in context.items()
            if str(key).strip() and value is not None and value != ""
        }

    def clear_debug_context(self) -> None:
        self._debug_context = {}

    def set_auth_refresh_callback(
        self,
        callback: Callable[["FastMossHTTPSession", dict[str, Any]], None] | None,
    ) -> None:
        self._auth_refresh_callback = callback

    def cookie_snapshot(self, *, domain_keyword: str = "fastmoss.com") -> dict[str, Any]:
        cookie_names: list[str] = []
        fd_tk_digest = ""
        for cookie in self.session.cookies:
            if not _cookie_domain_matches(cookie.domain or "", domain_keyword):
                continue
            cookie_names.append(str(cookie.name))
            if cookie.name == "fd_tk" and not fd_tk_digest:
                fd_tk_digest = _cookie_value_digest(cookie.value)
        return {
            "cookie_count": len(cookie_names),
            "cookie_names": sorted(cookie_names),
            "has_fd_tk": bool(fd_tk_digest),
            "fd_tk_digest": fd_tk_digest,
        }

    def export_cookies(self, *, domain_keyword: str = "fastmoss.com") -> list[dict[str, Any]]:
        """Return serializable cookies for one domain family.

        The returned values include sensitive cookie values and should only be
        written to the configured session cache, never to logs or task results.
        """

        exported: list[dict[str, Any]] = []
        for cookie in self.session.cookies:
            if not _cookie_domain_matches(cookie.domain or "", domain_keyword):
                continue
            exported.append(
                {
                    "name": str(cookie.name or ""),
                    "value": str(cookie.value or ""),
                    "domain": str(cookie.domain or ""),
                    "path": str(cookie.path or "/") or "/",
                    "expires": cookie.expires,
                    "secure": bool(cookie.secure),
                }
            )
        return exported

    def replace_browser_cookies(
        self,
        cookies: list[Mapping[str, Any]],
        *,
        domain_keyword: str = "fastmoss.com",
    ) -> int:
        """Replace session cookies for one domain family with browser-exported cookies."""

        removed = self.clear_cookies_for_domain(domain_keyword)
        inserted = 0
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").strip()
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            path = str(cookie.get("path") or "/").strip() or "/"
            if not name or not _cookie_domain_matches(domain, domain_keyword):
                continue
            self.session.cookies.set(
                name,
                value,
                domain=domain or None,
                path=path,
                expires=_coerce_cookie_expires(cookie.get("expires")),
                secure=_coerce_cookie_bool(cookie.get("secure")),
            )
            inserted += 1
        replaced_count = inserted if inserted > 0 else removed
        self._emit_event(
            "cookie_sync_from_browser",
            synced_cookie_count=replaced_count,
            browser_cookie_count=len(cookies),
            **self.cookie_snapshot(domain_keyword=domain_keyword),
        )
        return replaced_count

    def clear_cookies_for_domain(self, domain_keyword: str) -> int:
        """Clear cookies from the session jar that belong to the target domain family."""

        jar = self.session.cookies
        to_remove: list[tuple[str | None, str | None, str]] = []
        for cookie in jar:
            if not _cookie_domain_matches(cookie.domain or "", domain_keyword):
                continue
            to_remove.append((cookie.domain, cookie.path, cookie.name))
        for domain, path, name in to_remove:
            jar.clear(domain=domain, path=path, name=name)
        return len(to_remove)

    def has_cookie(self, name: str, *, domain_keyword: str = "fastmoss.com") -> bool:
        """Return True when the current session jar contains the named cookie."""

        for cookie in self.session.cookies:
            if cookie.name != name:
                continue
            if _cookie_domain_matches(cookie.domain or "", domain_keyword):
                return True
        return False

    def login(self, phone: str | None = None, password: str | None = None) -> dict[str, Any]:
        """Login with phone/password and keep fd_tk inside the session cookie jar."""

        self._update_credentials(phone=phone, password=password)
        if not self._phone or not self._password:
            raise FastMossAuthError("FastMoss login requires phone/password credentials")

        payload = self.request_json(
            "POST",
            "/api/user/login",
            json_body={
                "phone": self._phone,
                "password": self._password,
                "account": self._phone,
                "area_code": "86",
                "action": 0,
                "source": "1",
                "type": "1",
            },
            referer=FASTMOSS_ACCOUNT_CENTER_REFERER,
            region=FASTMOSS_DEFAULT_LOGIN_REGION,
            retries=3,
            relogin_on_auth_fail=False,
            check_auth=False,
            stage="auth.login",
        )

        if not _is_success_code(payload):
            raise FastMossHTTPError(
                _coerce_str(payload.get("msg")) or "FastMoss login failed",
                status_code=None,
                response_code=payload.get("code"),
                payload=payload,
            )
        return payload

    def ensure_logged_in(self, phone: str | None = None, password: str | None = None) -> dict[str, Any]:
        """Verify login state, re-login once if FastMoss reports an expired session."""

        self._update_credentials(phone=phone, password=password)
        payload = self.request_json(
            "GET",
            "/api/user/index/userInfo",
            referer=FASTMOSS_ACCOUNT_CENTER_REFERER,
            region=FASTMOSS_DEFAULT_LOGIN_REGION,
            retries=3,
            relogin_on_auth_fail=True,
            check_auth=True,
            stage="auth.user_info",
        )
        ext = payload.get("ext")
        if not isinstance(ext, Mapping) or ext.get("is_login") not in {1, "1", True}:
            raise FastMossAuthError(
                "FastMoss login check did not confirm an authenticated session",
                response_code=payload.get("code"),
                payload=payload,
            )
        return payload

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
        region: str | None = None,
        retries: int = 3,
        relogin_on_auth_fail: bool = True,
        check_auth: bool = True,
        timeout: float | None = None,
        stage: str = "",
    ) -> dict[str, Any]:
        """Send a signed FastMoss request and return the parsed JSON payload."""

        method = method.upper().strip()
        if method not in {"GET", "POST"}:
            raise ValueError(f"Unsupported FastMoss HTTP method: {method}")

        signed_params = _clean_params(params)
        timeout_value = self.timeout if timeout is None else timeout
        login_retried = False

        for attempt in range(max(retries, 1)):
            self._apply_inter_request_delay()
            body_text = "" if method == "GET" or json_body is None else _json_dumps(json_body)
            signed_params_with_nonce = dict(signed_params)
            signed_params_with_nonce["_time"] = int(self._time_factory())
            signed_params_with_nonce["cnonce"] = self._nonce_factory()
            signed_params_with_nonce["fm-sign"] = build_fm_sign(signed_params_with_nonce, body_text)
            self._emit_event(
                "http_request_start",
                stage=stage,
                method=method,
                path=path,
                attempt=attempt + 1,
                request_param_keys=sorted(signed_params.keys()),
                request_has_body=bool(body_text),
                **self.cookie_snapshot(),
            )

            request_headers = self._build_headers(
                method=method,
                referer=referer,
                region=region,
                has_json_body=bool(body_text),
                extra_headers=headers,
            )
            url = self._build_url(path)

            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=signed_params_with_nonce,
                    data=body_text.encode("utf-8") if body_text else None,
                    headers=request_headers,
                    timeout=timeout_value,
                )
                self._last_request_finished_at = time.monotonic()
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                self._last_request_finished_at = time.monotonic()
                self._emit_event(
                    "http_request_transport_error",
                    stage=stage,
                    method=method,
                    path=path,
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    **self.cookie_snapshot(),
                )
                if attempt >= retries - 1:
                    raise FastMossHTTPError(
                        f"FastMoss transport error: {exc}",
                        stage=stage,
                        method=method,
                        path=path,
                    ) from exc
                time.sleep(2**attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= retries - 1:
                    raise FastMossHTTPError(
                        f"FastMoss HTTP status {response.status_code} after retries",
                        status_code=response.status_code,
                        stage=stage,
                        method=method,
                        path=path,
                    )
                time.sleep(2**attempt)
                continue

            payload = self._decode_json(response)
            response_code = payload.get("code")
            response_ext = payload.get("ext")
            ext_is_login = ""
            if isinstance(response_ext, Mapping):
                ext_is_login = _coerce_str(response_ext.get("is_login")).strip()
            set_cookie_header = str(response.headers.get("Set-Cookie") or "")
            self._emit_event(
                "http_response",
                stage=stage,
                method=method,
                path=path,
                attempt=attempt + 1,
                status_code=response.status_code,
                response_code=_coerce_str(response_code).strip(),
                message=_coerce_str(payload.get("msg")).strip(),
                ext_is_login=ext_is_login,
                response_has_set_cookie=bool(set_cookie_header),
                response_has_set_cookie_fd_tk="fd_tk=" in set_cookie_header,
                **self.cookie_snapshot(),
            )

            if check_auth and _is_auth_issue(payload):
                if relogin_on_auth_fail and not login_retried and self._has_credentials():
                    login_retried = True
                    refresh_context = {
                        "stage": stage,
                        "method": method,
                        "path": path,
                        "response_code": _coerce_str(response_code).strip(),
                        "message": _coerce_str(payload.get("msg")).strip(),
                    }
                    self._emit_event(
                        "http_auth_retry_login",
                        stage=stage,
                        method=method,
                        path=path,
                        response_code=refresh_context["response_code"],
                        **self.cookie_snapshot(),
                    )
                    if self._auth_refresh_callback is not None:
                        self._auth_refresh_callback(self, refresh_context)
                    else:
                        self.login()
                    self._emit_event(
                        "http_auth_retry_ready",
                        stage=stage,
                        method=method,
                        path=path,
                        response_code=refresh_context["response_code"],
                        **self.cookie_snapshot(),
                    )
                    continue
                raise FastMossAuthError(
                    _coerce_str(payload.get("msg")) or "FastMoss login required",
                    status_code=response.status_code,
                    response_code=payload.get("code"),
                    payload=payload,
                    stage=stage,
                    method=method,
                    path=path,
                )

            if not _is_success_code(payload):
                raise FastMossHTTPError(
                    _coerce_str(payload.get("msg")) or "FastMoss request failed",
                    status_code=response.status_code,
                    response_code=payload.get("code"),
                    payload=payload,
                    stage=stage,
                    method=method,
                    path=path,
                )

            return payload

        raise FastMossHTTPError(
            "FastMoss request failed after retries",
            stage=stage,
            method=method,
            path=path,
        )

    def get_product_base(self, product_id: str) -> dict[str, Any]:
        """Return the `data` object from /api/goods/v3/base."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/v3/base"
        params = {"product_id": normalized_product_id}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product.base",
        )
        return _extract_data(payload)

    def search_products(
        self,
        words: str,
        *,
        page: int = 1,
        pagesize: int = 10,
        region: str | None = None,
        order: str = "2,2",
        extra_params: Mapping[str, Any] | None = None,
        check_auth: bool = True,
    ) -> dict[str, Any]:
        """Search FastMoss products through /api/goods/V2/search.

        The raw FastMoss response envelope is returned so callers can inspect
        auth/degraded state and normalize pagination themselves.
        """

        region_value = region or self.default_region
        path = "/api/goods/V2/search"
        params: dict[str, Any] = {
            "page": page,
            "pagesize": pagesize,
            "order": order,
            "region": region_value,
        }
        normalized_words = _coerce_str(words).strip()
        if normalized_words:
            params["words"] = normalized_words
        params.update(_clean_params(extra_params))
        return self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_search_referer(
                words=normalized_words,
                page=page,
                region=region_value,
            ),
            region=region_value,
            stage="product.search",
            check_auth=check_auth,
        )

    def get_product_overview(
        self,
        product_id: str,
        *,
        d_type: int | str = 28,
        start_date: str = "",
        end_date: str = "",
    ) -> dict[str, Any]:
        """Return the `data` object from /api/goods/v3/overview."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/v3/overview"
        params: dict[str, Any] = {"product_id": normalized_product_id}
        if start_date and end_date:
            params["start_date"] = start_date
            params["end_date"] = end_date
        else:
            params["d_type"] = d_type
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product.overview",
        )
        return _extract_data(payload)

    def get_product_skus(
        self,
        product_id: str,
        *,
        d_type: int | str = 28,
    ) -> dict[str, Any]:
        """Return the `data` object from /api/goods/v3/productSku."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/v3/productSku"
        params = {"product_id": normalized_product_id, "d_type": d_type}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product.skus",
        )
        return _extract_data(payload)

    def get_product_sku_distribution(
        self,
        product_id: str,
        *,
        d_type: int | str = 28,
    ) -> dict[str, Any]:
        """Return the `data` object from /api/goods/productSku."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/productSku"
        params = {"product_id": normalized_product_id, "d_type": d_type}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product.sku_distribution",
        )
        return _extract_data(payload)

    def list_product_authors(
        self,
        product_id: str,
        *,
        page: int = 1,
        pagesize: int = 10,
        order: str = "2,2",
        ecommerce_type: str = "all",
    ) -> dict[str, Any]:
        """Fetch one page of product-to-author relations."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/v3/author"
        params = {
            "product_id": normalized_product_id,
            "page": page,
            "pagesize": pagesize,
            "order": order,
            "ecommerce_type": ecommerce_type,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product_authors.list",
        )
        return payload

    def list_product_videos(
        self,
        product_id: str,
        *,
        page: int = 1,
        pagesize: int = 10,
        order: str = "1,2",
        d_type: int | str = 0,
        date_type: int | str = 28,
        is_promoted: int | str = -1,
    ) -> dict[str, Any]:
        """Fetch one page of product-to-video relations."""

        normalized_product_id = self._normalize_product_id(product_id)
        path = "/api/goods/v3/video"
        params = {
            "page": page,
            "product_id": normalized_product_id,
            "order": order,
            "d_type": d_type,
            "pagesize": pagesize,
            "is_promoted": is_promoted,
            "date_type": date_type,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_goods_detail_referer(normalized_product_id),
            region=self.default_region,
            stage="product_videos.list",
        )
        return payload

    def iter_product_authors(
        self,
        product_id: str,
        *,
        pagesize: int = 10,
        order: str = "2,2",
        ecommerce_type: str = "all",
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield all author rows for a product by paging through /api/goods/v3/author."""

        page = 1
        seen_rows = 0
        while True:
            payload = self.list_product_authors(
                product_id,
                page=page,
                pagesize=pagesize,
                order=order,
                ecommerce_type=ecommerce_type,
            )
            data = _extract_data(payload)
            rows = [row for row in _extract_list(payload)]
            if not rows:
                break
            for row in rows:
                yield row
            seen_rows += len(rows)
            total = data.get("total")
            if isinstance(total, int) and total > 0 and seen_rows >= total:
                break
            if len(rows) < pagesize:
                break
            page += 1
            if max_pages is not None and page > max_pages:
                break

    def search_author(
        self,
        unique_id: str,
        *,
        page: int = 1,
        pagesize: int = 10,
        region: str | None = None,
        order: str = "12,2",
        shop_window: int = 1,
        words_search_type: int = 1,
        df: str = FASTMOSS_AUTHOR_SEARCH_DF,
    ) -> dict[str, Any]:
        """Search an author by unique_id and return the raw JSON payload."""

        region_value = region or self.default_region
        path = "/api/author/search"
        params = {
            "page": page,
            "pagesize": pagesize,
            "df": df,
            "region": region_value,
            "order": order,
            "shop_window": shop_window,
            "words": unique_id,
            "words_search_type": words_search_type,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_search_referer(
                words=unique_id,
                page=page,
                shop_window=shop_window,
                words_search_type=words_search_type,
            ),
            region=region_value,
            stage="author.search",
        )
        return payload

    def get_author_base_info(self, uid: str) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/baseInfo."""

        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/baseInfo"
        params = {"uid": normalized_uid}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=self.default_region,
            stage="author.base_info",
        )
        return _extract_data(payload)

    def get_author_index(self, uid: str) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/authorIndex."""

        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/authorIndex"
        params = {"uid": normalized_uid}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=self.default_region,
            stage="author.index",
        )
        return _extract_data(payload)

    def get_author_cargo_summary(self, uid: str) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/cargoSummary."""

        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/cargoSummary"
        params = {"uid": normalized_uid}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=self.default_region,
            stage="author.cargo_summary",
        )
        return _extract_data(payload)

    def get_author_shop_list(
        self,
        uid: str,
        *,
        page: int = 1,
        page_size: int = 5,
        region: str | None = None,
        order: str = "sold_count,2",
    ) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/shopList.

        The defaults intentionally mirror the FastMoss influencer detail page's
        "TOP 5 合作店铺" table.
        """

        region_value = region or self.default_region
        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/shopList"
        params = {
            "page": page,
            "uid": normalized_uid,
            "region": region_value,
            "order": order,
            "pagesize": page_size,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=region_value,
            stage="author.shop_list",
        )
        return _extract_data(payload)

    def get_author_contact(self, uid: str) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/authorContact."""

        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/authorContact"
        params = {"uid": normalized_uid}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=self.default_region,
            stage="author.contact",
        )
        return _extract_data(payload)

    def get_author_video_list(
        self,
        uid: str,
        *,
        page: int = 1,
        page_size: int = 5,
        region: str | None = None,
        order: str = "sold_count,2",
        date_type: int | str = 28,
    ) -> dict[str, Any]:
        """Return the `data` object from /api/author/v3/detail/videoList."""

        region_value = region or self.default_region
        normalized_uid = self._normalize_uid(uid)
        path = "/api/author/v3/detail/videoList"
        params = {
            "region": region_value,
            "order": order,
            "uid": normalized_uid,
            "date_type": date_type,
            "pagesize": page_size,
            "page": page,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_author_detail_referer(normalized_uid),
            region=region_value,
            stage="author.video_list",
        )
        return _extract_data(payload)

    def get_video_overview(self, video_id: str) -> dict[str, Any]:
        """Return the `data` object from /api/video/overview."""

        normalized_video_id = self._normalize_video_id(video_id)
        path = "/api/video/overview"
        params = {"id": normalized_video_id}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_video_detail_referer(normalized_video_id),
            region=self.default_region,
            stage="video.overview",
        )
        return _extract_data(payload)

    def list_video_goods(
        self,
        video_id: str,
        *,
        order: str = "1,2",
    ) -> dict[str, Any]:
        """Return the `data` object from /api/video/v2/goods."""

        normalized_video_id = self._normalize_video_id(video_id)
        path = "/api/video/v2/goods"
        params = {"id": normalized_video_id, "order": order}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_video_detail_referer(normalized_video_id),
            region=self.default_region,
            stage="video.goods",
        )
        return _extract_data(payload)

    def get_shop_base(self, seller_id: str) -> dict[str, Any]:
        """Return the `data` object from /api/shop/v3/base."""

        normalized_shop_id = self._normalize_shop_id(seller_id)
        path = "/api/shop/v3/base"
        params = {"id": normalized_shop_id}
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_shop_detail_referer(normalized_shop_id),
            region=self.default_region,
            stage="shop.base",
        )
        return _extract_data(payload)

    def list_shop_goods(
        self,
        seller_id: str,
        *,
        page: int = 1,
        page_size: int = 10,
        d_type: int | str = 28,
        order: str = "sold_count,2",
    ) -> dict[str, Any]:
        """Return the `data` object from /api/shop/v3/goods."""

        normalized_shop_id = self._normalize_shop_id(seller_id)
        path = "/api/shop/v3/goods"
        params = {
            "id": normalized_shop_id,
            "page": page,
            "pagesize": page_size,
            "d_type": d_type,
            "order": order,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_shop_detail_referer(normalized_shop_id),
            region=self.default_region,
            stage="shop.goods",
        )
        return _extract_data(payload)

    def list_shop_authors(
        self,
        seller_id: str,
        *,
        page: int = 1,
        page_size: int = 10,
        d_type: int | str = 28,
        author_product_type: int | str = 3,
        order: str = "sold_count,2",
    ) -> dict[str, Any]:
        """Return the `data` object from /api/shop/v3/author."""

        normalized_shop_id = self._normalize_shop_id(seller_id)
        path = "/api/shop/v3/author"
        params = {
            "id": normalized_shop_id,
            "page": page,
            "pagesize": page_size,
            "d_type": d_type,
            "author_product_type": author_product_type,
            "order": order,
        }
        payload = self.request_json(
            "GET",
            path,
            params=params,
            referer=self._build_shop_detail_referer(normalized_shop_id),
            region=self.default_region,
            stage="shop.authors",
        )
        return _extract_data(payload)

    def fetch_author_bundle(
        self,
        *,
        uid: str | None = None,
        unique_id: str | None = None,
        include_shop_list: bool = True,
        include_contact: bool = True,
        shop_page: int = 1,
        shop_page_size: int = 5,
    ) -> dict[str, Any]:
        """Fetch the core detail bundle for a creator.

        If only `unique_id` is provided, the method falls back to `search_author`
        to resolve the FastMoss internal `uid`.
        """

        resolved_uid = self._resolve_author_uid(uid=uid, unique_id=unique_id)
        base_info = self.get_author_base_info(resolved_uid)
        author_index = self.get_author_index(resolved_uid)
        cargo_summary = self.get_author_cargo_summary(resolved_uid)
        bundle: dict[str, Any] = {
            "uid": resolved_uid,
            "unique_id": base_info.get("unique_id") or unique_id or "",
            "base_info": base_info,
            "author_index": author_index,
            "cargo_summary": cargo_summary,
        }
        if include_shop_list:
            bundle["shop_list"] = self.get_author_shop_list(
                resolved_uid,
                page=shop_page,
                page_size=shop_page_size,
            )
        if include_contact:
            bundle["author_contact"] = self.get_author_contact(resolved_uid)
        return bundle

    def resolve_author_uid(self, *, uid: str | None = None, unique_id: str | None = None) -> str:
        """Public wrapper around FastMoss uid resolution."""

        return self._resolve_author_uid(uid=uid, unique_id=unique_id)

    def _resolve_author_uid(self, *, uid: str | None, unique_id: str | None) -> str:
        normalized_uid = self._normalize_uid(uid) if uid else ""
        if normalized_uid:
            return normalized_uid
        normalized_unique_id = self._normalize_unique_id(unique_id) if unique_id else ""
        if not normalized_unique_id:
            raise ValueError("Either uid or unique_id must be provided")

        payload = self.search_author(normalized_unique_id)
        for item in _extract_list(payload):
            if self._normalize_unique_id(item.get("unique_id")) == normalized_unique_id:
                resolved = self._normalize_uid(item.get("uid"))
                if resolved:
                    return resolved
        rows = _extract_list(payload)
        if rows:
            resolved = self._normalize_uid(rows[0].get("uid"))
            if resolved:
                return resolved
        raise FastMossHTTPError(
            f"Unable to resolve FastMoss uid for unique_id={normalized_unique_id}",
            stage="author.uid_resolve",
            method="GET",
            path="/api/author/search",
        )

    def _update_credentials(self, *, phone: str | None = None, password: str | None = None) -> None:
        if phone is not None and phone.strip():
            self._phone = phone.strip()
        if password is not None and password.strip():
            self._password = password.strip()

    def _has_credentials(self) -> bool:
        return bool(self._phone and self._password)

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _build_headers(
        self,
        *,
        method: str,
        referer: str | None,
        region: str | None,
        has_json_body: bool,
        extra_headers: Mapping[str, str] | None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "lang": "ZH_CN",
            "source": "pc",
            "region": region or self.default_region,
            "User-Agent": self.user_agent,
        }
        if referer:
            headers["Referer"] = referer
        if method == "POST" or has_json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Origin"] = FASTMOSS_BASE_URL
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        return headers

    def _decode_json(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FastMossHTTPError(
                "FastMoss returned a non-JSON payload",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise FastMossHTTPError(
                "FastMoss returned a non-object JSON payload",
                status_code=response.status_code,
            )
        return payload

    def _normalize_product_id(self, product_id: str | int) -> str:
        value = _coerce_str(product_id).strip()
        if not value:
            raise ValueError("product_id is required")
        return value

    def _normalize_uid(self, uid: str | int | None) -> str:
        value = _coerce_str(uid).strip()
        if not value:
            raise ValueError("uid is required")
        return value

    def _normalize_unique_id(self, unique_id: str | None) -> str:
        value = _coerce_str(unique_id).strip()
        if not value:
            raise ValueError("unique_id is required")
        return value

    def _normalize_video_id(self, video_id: str | int) -> str:
        value = _coerce_str(video_id).strip()
        if not value:
            raise ValueError("video_id is required")
        return value

    def _normalize_shop_id(self, shop_id: str | int) -> str:
        value = _coerce_str(shop_id).strip()
        if not value:
            raise ValueError("shop_id is required")
        return value

    def _build_goods_detail_referer(self, product_id: str | int) -> str:
        return FASTMOSS_GOODS_DETAIL_REFERER_TEMPLATE.format(product_id=self._normalize_product_id(product_id))

    def _build_goods_search_referer(
        self,
        *,
        words: str,
        page: int,
        region: str,
    ) -> str:
        return FASTMOSS_GOODS_SEARCH_REFERER_TEMPLATE.format(
            region=region,
            page=page,
            words=words,
        )

    def _build_author_detail_referer(self, uid: str | int) -> str:
        return FASTMOSS_AUTHOR_DETAIL_REFERER_TEMPLATE.format(uid=self._normalize_uid(uid))

    def _build_video_detail_referer(self, video_id: str | int) -> str:
        return FASTMOSS_VIDEO_DETAIL_REFERER_TEMPLATE.format(video_id=self._normalize_video_id(video_id))

    def _build_shop_detail_referer(self, shop_id: str | int) -> str:
        return FASTMOSS_SHOP_DETAIL_REFERER_TEMPLATE.format(shop_id=self._normalize_shop_id(shop_id))

    def _build_author_search_referer(
        self,
        *,
        words: str,
        page: int,
        shop_window: int,
        words_search_type: int,
    ) -> str:
        return FASTMOSS_AUTHOR_SEARCH_REFERER_TEMPLATE.format(
            shop_window=shop_window,
            page=page,
            words=words,
            words_search_type=words_search_type,
        )

    def _apply_inter_request_delay(self) -> None:
        if self._last_request_finished_at is None:
            return
        min_delay, max_delay = self.request_delay_range
        if max_delay <= 0:
            return
        if min_delay < 0:
            min_delay = 0.0
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay
        delay_seconds = random.uniform(min_delay, max_delay)
        if delay_seconds > 0:
            self._sleep_factory(delay_seconds)

    def _emit_event(self, kind: str, **payload: Any) -> None:
        if self._event_callback is None:
            return
        event = {"kind": str(kind), "ts_ms": int(time.time() * 1000)}
        if self._debug_context:
            event.update(self._debug_context)
        for key, value in payload.items():
            if value is None:
                continue
            event[str(key)] = value
        try:
            self._event_callback(event)
        except Exception:
            return
