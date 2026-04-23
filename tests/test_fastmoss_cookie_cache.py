from __future__ import annotations

import time

from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    attach_fastmoss_cookie_cache,
    build_fastmoss_cookie_cache_context,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


class _CookieCacheFakeSession:
    base_url = "https://www.fastmoss.com"

    def __init__(self) -> None:
        self.token = ""
        self.login_count = 0
        self._auth_refresh_callback = None

    def set_auth_refresh_callback(self, callback):
        self._auth_refresh_callback = callback

    def replace_browser_cookies(self, cookies, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        values = [str(cookie.get("value") or "") for cookie in cookies if cookie.get("name") == "fd_tk"]
        self.token = values[0] if values else ""
        return len(cookies)

    def export_cookies(self, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        return [
            {
                "name": "fd_tk",
                "value": self.token,
                "domain": ".fastmoss.com",
                "path": "/",
                "secure": True,
            }
        ]

    def cookie_snapshot(self, *, domain_keyword="fastmoss.com"):
        del domain_keyword
        digest = {
            "old-token": "old-digest",
            "new-token": "new-digest",
            "other-token": "other-digest",
        }.get(self.token, "")
        return {
            "cookie_count": 1 if self.token else 0,
            "cookie_names": ["fd_tk"] if self.token else [],
            "has_fd_tk": bool(self.token),
            "fd_tk_digest": digest,
        }

    def login(self):
        self.login_count += 1
        self.token = "new-token"
        return {"code": 200, "ext": {"is_login": 1}}


def test_fastmoss_cookie_cache_refreshes_db_cookie_after_auth_issue(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    context = build_fastmoss_cookie_cache_context(
        base_url="https://www.fastmoss.com",
        account_key="18000000000",
        region="US",
    )
    store.save_fastmoss_cookie_cache(
        cache_key=context["cache_key"],
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[{"name": "fd_tk", "value": "old-token", "domain": ".fastmoss.com", "path": "/"}],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="old-digest",
        expires_at=time.time() + 3600,
    )
    session = _CookieCacheFakeSession()
    attach_fastmoss_cookie_cache(
        session,
        store=store,
        account_key="18000000000",
        region="US",
    )

    session._auth_refresh_callback(session, {"response_code": "MAG_AUTH_3001"})
    loaded = store.load_fastmoss_cookie_cache(cache_key=context["cache_key"])

    assert session.login_count == 1
    assert session.token == "new-token"
    assert loaded is not None
    assert loaded["cookies"][0]["value"] == "new-token"
    assert loaded["fd_tk_digest"] == "new-digest"
    assert loaded["last_auth_failed_at"] == 0


def test_fastmoss_cookie_cache_reuses_newer_db_cookie_without_login(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    context = build_fastmoss_cookie_cache_context(
        base_url="https://www.fastmoss.com",
        account_key="18000000000",
        region="US",
    )
    store.save_fastmoss_cookie_cache(
        cache_key=context["cache_key"],
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[{"name": "fd_tk", "value": "old-token", "domain": ".fastmoss.com", "path": "/"}],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="old-digest",
        expires_at=time.time() + 3600,
    )
    session = _CookieCacheFakeSession()
    attach_fastmoss_cookie_cache(
        session,
        store=store,
        account_key="18000000000",
        region="US",
    )
    store.save_fastmoss_cookie_cache(
        cache_key=context["cache_key"],
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[{"name": "fd_tk", "value": "other-token", "domain": ".fastmoss.com", "path": "/"}],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="other-digest",
        expires_at=time.time() + 3600,
    )

    session._auth_refresh_callback(session, {"response_code": "MAG_AUTH_3001"})

    assert session.login_count == 0
    assert session.token == "other-token"
