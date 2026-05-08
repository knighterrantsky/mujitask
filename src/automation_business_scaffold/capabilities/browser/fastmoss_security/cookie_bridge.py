from __future__ import annotations

import hashlib
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_mapping, coerce_str, first_non_empty


def import_fastmoss_browser_cookies(
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


def export_fastmoss_browser_cookies(raw_page: Any, *, base_url: str) -> list[dict[str, Any]]:
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


def cookie_snapshot_from_browser_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    fd_tk_digest = ""
    for cookie in cookies:
        if cookie.get("name") == "fd_tk" and not fd_tk_digest:
            fd_tk_digest = cookie_value_digest(str(cookie.get("value") or ""))
    return {
        "cookie_count": len(cookies),
        "has_fd_tk": bool(fd_tk_digest),
        "fd_tk_digest": fd_tk_digest,
    }


def fd_tk_digest_from_cookies(cookies: list[dict[str, Any]]) -> str:
    for cookie in cookies:
        if cookie.get("name") == "fd_tk":
            return cookie_value_digest(str(cookie.get("value") or ""))
    return ""


def cookie_value_digest(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
