from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.capabilities.browser.fastmoss_security.cookie_bridge import (
    fd_tk_digest_from_cookies,
)
from automation_business_scaffold.contracts.handler.shared import coerce_bool, first_non_empty
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    attach_fastmoss_cookie_cache,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def bootstrap_fastmoss_login_cookies(
    *,
    db_url: str,
    fastmoss_settings: Mapping[str, Any],
) -> dict[str, Any]:
    phone = first_non_empty(fastmoss_settings.get("phone"))
    password = first_non_empty(fastmoss_settings.get("password"))
    if not (phone and password):
        return {"cookies": [], "status": {"status": "missing_credentials"}}

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
            "fd_tk_digest": fd_tk_digest_from_cookies(cookies),
        },
    }


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
