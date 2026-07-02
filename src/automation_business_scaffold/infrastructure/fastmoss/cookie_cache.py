"""Postgres-backed FastMoss cookie cache helpers."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Mapping

from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossHTTPError,
    FastMossHTTPSession,
    FastMossSessionConflictError,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS = 12 * 60 * 60


def build_fastmoss_cookie_cache_context(
    *,
    base_url: str,
    account_key: str,
    region: str,
    namespace: str = "",
) -> dict[str, Any]:
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    normalized_account = str(account_key or "").strip()
    normalized_region = str(region or "").strip() or "US"
    normalized_namespace = str(namespace or "").strip()
    if not normalized_base_url or not normalized_account:
        return {"enabled": False, "reason": "missing_base_url_or_account"}
    source = (
        "fastmoss:v1:"
        f"{normalized_namespace}:"
        f"{normalized_base_url}:"
        f"{normalized_region}:"
        f"{normalized_account}"
    )
    return {
        "enabled": True,
        "cache_key": "fm_" + hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "namespace": normalized_namespace,
        "account_key": normalized_account,
        "base_url": normalized_base_url,
        "region": normalized_region,
    }


def attach_fastmoss_cookie_cache(
    fastmoss: FastMossHTTPSession,
    *,
    store: RuntimeStore | None,
    account_key: str,
    region: str,
    namespace: str = "",
    enabled: bool = True,
    force_refresh: bool = False,
    ttl_seconds: float = DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False, "reason": "disabled"}
    if store is None:
        return {"enabled": False, "reason": "missing_store"}

    context = build_fastmoss_cookie_cache_context(
        base_url=str(getattr(fastmoss, "base_url", "https://www.fastmoss.com") or "https://www.fastmoss.com"),
        account_key=account_key,
        region=region,
        namespace=namespace,
    )
    if not context.get("enabled"):
        return context

    cache_key = str(context["cache_key"])

    def _refresh_session(session: FastMossHTTPSession, event: dict[str, Any]) -> None:
        current_digest = str(session.cookie_snapshot().get("fd_tk_digest") or "")
        with store.fastmoss_cookie_cache_lock(cache_key=cache_key):
            latest = store.load_fastmoss_cookie_cache(cache_key=cache_key)
            if _record_can_be_reused(latest):
                latest_digest = str((latest or {}).get("fd_tk_digest") or "")
                if latest_digest and latest_digest != current_digest:
                    session.replace_browser_cookies((latest or {}).get("cookies", []))
                    try:
                        _ensure_logged_in_without_refresh(session)
                        return
                    except FastMossHTTPError:
                        store.mark_fastmoss_cookie_cache_auth_failed(cache_key=cache_key)
            store.mark_fastmoss_cookie_cache_auth_failed(cache_key=cache_key)
            try:
                _login_refresh_session(session)
                save_fastmoss_cookie_cache_from_session(
                    session,
                    store=store,
                    context=context,
                    ttl_seconds=ttl_seconds,
                    last_login_at=time.time(),
                )
            except FastMossHTTPError as exc:
                raise _session_conflict_error(exc, event=event) from exc

    fastmoss.set_auth_refresh_callback(_refresh_session)

    if force_refresh:
        with store.fastmoss_cookie_cache_lock(cache_key=cache_key):
            _login_refresh_session(fastmoss)
            saved = save_fastmoss_cookie_cache_from_session(
                fastmoss,
                store=store,
                context=context,
                ttl_seconds=ttl_seconds,
                last_login_at=time.time(),
            )
        return {
            "enabled": True,
            "cache_key": cache_key,
            "status": "refreshed",
            **_redacted_cache_status(saved),
        }

    cached = store.load_fastmoss_cookie_cache(cache_key=cache_key)
    if _record_can_be_reused(cached):
        fastmoss.replace_browser_cookies((cached or {}).get("cookies", []))
        return {
            "enabled": True,
            "cache_key": cache_key,
            "status": "loaded",
            **_redacted_cache_status(cached),
        }

    return {
        "enabled": True,
        "cache_key": cache_key,
        "status": "missing" if cached is None else "expired",
        **_redacted_cache_status(cached),
    }


def refresh_fastmoss_session_cookies(
    fastmoss: FastMossHTTPSession,
    *,
    store: RuntimeStore | None,
    account_key: str,
    region: str,
    namespace: str = "",
    enabled: bool = True,
    cookies: list[Mapping[str, Any]] | None = None,
    prefer_login: bool = True,
    ttl_seconds: float = DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    reason: str = "manual_refresh",
) -> dict[str, Any]:
    """Refresh FastMoss session material and save the resulting cookie cache.

    This is the provider-level recovery hook used when a request needs fresh
    session material outside the normal auth callback path, for example after
    FastMoss returns a security verification response.
    """

    if not enabled:
        return {"enabled": False, "reason": "disabled"}

    context = build_fastmoss_cookie_cache_context(
        base_url=str(getattr(fastmoss, "base_url", "https://www.fastmoss.com") or "https://www.fastmoss.com"),
        account_key=account_key,
        region=region,
        namespace=namespace,
    )
    if not context.get("enabled"):
        return context

    cache_key = str(context["cache_key"])
    if store is None:
        _refresh_session_without_cache(fastmoss, cookies=cookies or [], prefer_login=prefer_login)
        return {
            "enabled": False,
            "cache_key": cache_key,
            "status": "refreshed_without_cache",
            "reason": reason,
            **fastmoss.cookie_snapshot(),
        }

    with store.fastmoss_cookie_cache_lock(cache_key=cache_key):
        store.mark_fastmoss_cookie_cache_auth_failed(cache_key=cache_key)
        try:
            _refresh_session_without_cache(fastmoss, cookies=cookies or [], prefer_login=prefer_login)
        except FastMossHTTPError as exc:
            raise _session_conflict_error(exc, event={"stage": reason}) from exc
        saved = save_fastmoss_cookie_cache_from_session(
            fastmoss,
            store=store,
            context=context,
            ttl_seconds=ttl_seconds,
            last_login_at=time.time() if prefer_login else None,
        )
    return {
        "enabled": True,
        "cache_key": cache_key,
        "status": "refreshed",
        "reason": reason,
        **_redacted_cache_status(saved),
    }


def save_fastmoss_cookie_cache_from_session(
    fastmoss: FastMossHTTPSession,
    *,
    store: RuntimeStore,
    context: Mapping[str, Any],
    ttl_seconds: float = DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS,
    last_login_at: float | None = None,
) -> dict[str, Any]:
    if not context.get("enabled"):
        return {"enabled": False, "reason": str(context.get("reason") or "disabled")}

    cookies = fastmoss.export_cookies()
    snapshot = fastmoss.cookie_snapshot()
    if not cookies:
        return {
            "enabled": True,
            "cache_key": str(context.get("cache_key") or ""),
            "status": "skipped_empty_cookie_jar",
            **snapshot,
        }
    if not bool(snapshot.get("has_fd_tk")):
        return {
            "enabled": True,
            "cache_key": str(context.get("cache_key") or ""),
            "status": "skipped_missing_fd_tk",
            **snapshot,
        }

    saved = store.save_fastmoss_cookie_cache(
        cache_key=str(context["cache_key"]),
        namespace=str(context.get("namespace") or ""),
        account_key=str(context.get("account_key") or ""),
        base_url=str(context.get("base_url") or ""),
        region=str(context.get("region") or ""),
        cookies=cookies,
        cookie_count=int(snapshot.get("cookie_count") or len(cookies)),
        has_fd_tk=bool(snapshot.get("has_fd_tk")),
        fd_tk_digest=str(snapshot.get("fd_tk_digest") or ""),
        expires_at=_resolve_expires_at(cookies, ttl_seconds=ttl_seconds),
        last_login_at=last_login_at,
    )
    return {
        "enabled": True,
        "cache_key": str(context["cache_key"]),
        "status": "saved",
        **_redacted_cache_status(saved),
    }


def _record_can_be_reused(record: Mapping[str, Any] | None) -> bool:
    if not record:
        return False
    cookies = record.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return False
    if not bool(record.get("has_fd_tk")):
        return False
    if float(record.get("last_auth_failed_at") or 0.0) > 0:
        return False
    expires_at = float(record.get("expires_at") or 0.0)
    return expires_at <= 0 or expires_at > time.time()


def _refresh_session_without_cache(
    session: FastMossHTTPSession,
    *,
    cookies: list[Mapping[str, Any]],
    prefer_login: bool,
) -> None:
    if prefer_login and _session_has_credentials(session):
        _login_refresh_session(session)
        return
    if cookies:
        session.replace_browser_cookies(cookies)
        _ensure_logged_in_without_refresh(session)
        return
    _login_refresh_session(session)


def _login_refresh_session(session: FastMossHTTPSession) -> None:
    session.clear_cookies_for_domain("fastmoss.com")
    session.login()
    _ensure_logged_in_without_refresh(session)


def _ensure_logged_in_without_refresh(session: FastMossHTTPSession) -> dict[str, Any]:
    try:
        return session.ensure_logged_in(relogin_on_auth_fail=False)
    except TypeError:
        return session.ensure_logged_in()


def _session_has_credentials(session: FastMossHTTPSession) -> bool:
    has_credentials = getattr(session, "has_credentials", None)
    if callable(has_credentials):
        return bool(has_credentials())
    return True


def _session_conflict_error(exc: FastMossHTTPError, *, event: Mapping[str, Any]) -> FastMossSessionConflictError:
    if isinstance(exc, FastMossSessionConflictError):
        return exc
    return FastMossSessionConflictError(
        "FastMoss session refresh did not restore authentication; the account may have been logged in elsewhere.",
        status_code=exc.status_code,
        response_code=exc.response_code,
        payload=exc.payload,
        stage=str(event.get("stage") or exc.stage or "auth.refresh"),
        method=str(event.get("method") or exc.method or ""),
        path=str(event.get("path") or exc.path or ""),
        params=exc.params,
        referer=exc.referer,
        region=exc.region,
    )


def _resolve_expires_at(cookies: list[dict[str, Any]], *, ttl_seconds: float) -> float:
    now = time.time()
    expires_values: list[float] = []
    for cookie in cookies:
        raw_expires = cookie.get("expires")
        if raw_expires in (None, ""):
            continue
        try:
            expires = float(raw_expires)
        except (TypeError, ValueError):
            continue
        if expires > now:
            expires_values.append(expires)
    if expires_values:
        return min(expires_values)
    return now + max(float(ttl_seconds or DEFAULT_FASTMOSS_COOKIE_CACHE_TTL_SECONDS), 60.0)


def _redacted_cache_status(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {
            "cookie_count": 0,
            "has_fd_tk": False,
            "fd_tk_digest": "",
            "expires_at": 0.0,
            "updated_at": 0.0,
        }
    return {
        "cookie_count": int(record.get("cookie_count") or 0),
        "has_fd_tk": bool(record.get("has_fd_tk")),
        "fd_tk_digest": str(record.get("fd_tk_digest") or ""),
        "expires_at": float(record.get("expires_at") or 0.0),
        "updated_at": float(record.get("updated_at") or 0.0),
    }
