from __future__ import annotations

import time
from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.fastmoss_security.cookie_bridge import (
    cookie_snapshot_from_browser_cookies,
)
from automation_business_scaffold.contracts.handler.shared import first_non_empty
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    build_fastmoss_cookie_cache_context,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def save_browser_cookies_to_cache(
    *,
    db_url: str,
    fastmoss_settings: Mapping[str, Any],
    cookies: list[dict[str, Any]],
    verified_path: str,
) -> dict[str, Any]:
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

    snapshot = cookie_snapshot_from_browser_cookies(cookies)
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
        expires_at=resolve_browser_cookie_expires_at(cookies, ttl_seconds=ttl_seconds),
        last_login_at=time.time(),
    )
    return {
        "enabled": True,
        "cache_key": str(context["cache_key"]),
        "status": "saved",
        "verified_path": verified_path,
        **redacted_cache_status(saved),
    }


def resolve_browser_cookie_expires_at(cookies: list[dict[str, Any]], *, ttl_seconds: float) -> float:
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


def redacted_cache_status(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cookie_count": int(record.get("cookie_count") or 0),
        "has_fd_tk": bool(record.get("has_fd_tk")),
        "fd_tk_digest": str(record.get("fd_tk_digest") or ""),
        "expires_at": float(record.get("expires_at") or 0.0),
        "updated_at": float(record.get("updated_at") or 0.0),
    }


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
