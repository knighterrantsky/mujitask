from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from automation_business_scaffold.capabilities.fact_sources.fastmoss import creator_fetch_handler as creator_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import product_fetch_handler as product_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import shop_fetch_handler as shop_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import video_fetch_handler as video_module
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPError, FastMossSessionConflictError


@pytest.mark.parametrize("failure", ["", "navigation", "callback"])
def test_browser_recovery_reports_real_operations_across_rounds(monkeypatch, failure):
    from automation_business_scaffold.capabilities.browser import fastmoss_security_resolve_handler as module

    events = []
    opened = []
    verified = []

    def progress(operation, **kwargs):
        if failure == "callback":
            raise RuntimeError("observer unavailable")
        events.append((operation, kwargs["details"]))

    @contextmanager
    def open_page(**kwargs):
        assert kwargs["progress_callback"] is progress
        opened.append(True)
        yield SimpleNamespace(page=object(), raw_page=object())

    def goto(*args, **kwargs):
        if failure != "callback":
            assert events[-1][0] == "fastmoss_page_goto"
            assert events[-1][1]["state"] == "started"
        if failure == "navigation":
            raise RuntimeError("secret-page-url")

    def verify(*args, **kwargs):
        verified.append(True)
        return {"verified": len(verified) > 1, "response_code": "200" if len(verified) > 1 else "MSG_SAFE_0001"}

    def slider(*args, **kwargs):
        assert kwargs["progress_callback"] is progress
        return {"attempted": False, "resolved": True}

    cookies = [{"name": "fd_tk", "value": "secret-cookie", "domain": ".fastmoss.com"}]
    monkeypatch.setattr(module, "open_automation_page", open_page)
    monkeypatch.setattr(module, "_page_goto", goto)
    monkeypatch.setattr(module, "_safe_wait_for_timeout", lambda *a, **k: None)
    monkeypatch.setattr(module, "_read_fastmoss_slider_state", lambda *a, **k: {})
    monkeypatch.setattr(module, "_capture_fastmoss_browser_diagnostic_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(module, "_export_fastmoss_browser_cookies", lambda *a, **k: cookies)
    monkeypatch.setattr(module, "_reset_fastmoss_browser_session", lambda *a, **k: {})
    monkeypatch.setattr(module, "_bootstrap_fastmoss_login_cookies", lambda *a, **k: {"cookies": cookies})
    monkeypatch.setattr(module, "_import_fastmoss_browser_cookies", lambda *a, **k: {})
    monkeypatch.setattr(module, "_try_resolve_fastmoss_slider_security_check", slider)
    monkeypatch.setattr(module, "_verify_original_request_with_cookies_result", verify)
    monkeypatch.setattr(module, "_save_browser_cookies_to_cache", lambda **k: {})
    context = HandlerContext(
        request_id="req-progress", job_id="job-progress", handler_code=module.HANDLER_CODE,
        worker_type="browser_worker", runtime_table="task_execution",
        payload={"search_request": {"keyword": "test"}, "fastmoss_browser_max_attempts": 2},
        metadata={"progress_callback": progress},
    )
    result = module.fastmoss_security_browser_resolve_handler(context)
    assert result.status == ("failed" if failure == "navigation" else "success")
    assert len(opened) == (1 if failure == "navigation" else 2)
    if failure == "callback":
        return
    starts = {details["operation_id"] for _, details in events if details["state"] == "started"}
    terminals = {details["operation_id"] for _, details in events if details["state"] in {"completed", "failed"}}
    assert starts == terminals
    assert "secret-cookie" not in str(events)
    assert "secret-page-url" not in str(events)
    if failure == "navigation":
        assert events[-1][0] == "fastmoss_page_goto"
        assert events[-1][1]["state"] == "failed"
    else:
        assert sum(op == "fastmoss_page_goto" and d["state"] == "completed" for op, d in events) == 2
        assert {op for op, _ in events} >= {"fastmoss_session_reset", "fastmoss_request_verification", "fastmoss_slider_resolution"}


def _context(handler_code: str, payload: dict[str, Any]) -> HandlerContext:
    return HandlerContext(
        request_id="req-fastmoss-security",
        job_id=f"job-{handler_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        job_code=handler_code,
    )


class _SecuritySession:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.default_region = str(kwargs.get("default_region") or "US")
        self.base_url = str(kwargs.get("base_url") or "https://www.fastmoss.com")

    def __enter__(self) -> "_SecuritySession":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def set_auth_refresh_callback(self, callback: Any) -> None:
        del callback

    def replace_browser_cookies(self, cookies: list[dict[str, Any]], *, domain_keyword: str = "fastmoss.com") -> int:
        del domain_keyword
        return len(cookies)

    def ensure_logged_in(self) -> dict[str, Any]:
        return {"code": 200, "ext": {"is_login": 1}}

    def cookie_snapshot(self) -> dict[str, Any]:
        return {"cookie_count": 1, "has_fd_tk": True, "fd_tk_digest": "digest"}

    def resolve_author_uid(self, *, uid: str = "", unique_id: str = "") -> str:
        return uid or unique_id or "creator-uid"

    def get_product_base(self, product_id: str) -> dict[str, Any]:
        raise _security_error("product.base", "/api/goods/v3/base", {"product_id": product_id})

    def get_author_base_info(self, uid: str) -> dict[str, Any]:
        raise _security_error("author.base_info", "/api/author/v3/detail/baseInfo", {"uid": uid})

    def get_shop_base(self, seller_id: str) -> dict[str, Any]:
        raise _security_error("shop.base", "/api/shop/v3/base", {"id": seller_id})

    def get_video_overview(self, video_id: str) -> dict[str, Any]:
        raise _security_error("video.overview", "/api/video/overview", {"id": video_id})


class _SessionConflictSession(_SecuritySession):
    def get_product_base(self, product_id: str) -> dict[str, Any]:
        raise FastMossSessionConflictError(
            "FastMoss session refresh did not restore authentication; the account may have been logged in elsewhere.",
            status_code=200,
            response_code="MAG_AUTH_3001",
            payload={"code": "MAG_AUTH_3001", "ext": {"is_login": 0}},
            stage="product.base",
            method="GET",
            path="/api/goods/v3/base",
            params={"product_id": product_id},
            region="US",
        )


def _security_error(stage: str, path: str, params: dict[str, Any]) -> FastMossHTTPError:
    return FastMossHTTPError(
        "FastMoss request failed",
        status_code=200,
        response_code="MSG_SAFE_0001",
        payload={"code": "MSG_SAFE_0001", "data": {"id": 300856}, "ext": {"is_login": 1}},
        stage=stage,
        method="GET",
        path=path,
        params=params,
        referer="https://www.fastmoss.com/zh/e-commerce/detail/1732183420263764252",
        region="US",
    )


@pytest.mark.parametrize(
    ("module", "handler_name", "handler_code", "payload", "expected_path", "expected_param"),
    [
        (
            product_module,
            "fastmoss_product_fetch_handler",
            "fastmoss_product_fetch",
            {"product_identity": {"product_id": "1732183420263764252"}},
            "/api/goods/v3/base",
            ("product_id", "1732183420263764252"),
        ),
        (
            creator_module,
            "fastmoss_creator_fetch_handler",
            "fastmoss_creator_fetch",
            {"creator_identity": {"uid": "7491111111111111111"}, "fetch_plan": {"endpoints": ["base_info"]}},
            "/api/author/v3/detail/baseInfo",
            ("uid", "7491111111111111111"),
        ),
        (
            shop_module,
            "fastmoss_shop_fetch_handler",
            "fastmoss_shop_fetch",
            {"shop_identity": {"seller_id": "7492222222222222222"}, "fetch_plan": {"endpoints": ["base"]}},
            "/api/shop/v3/base",
            ("id", "7492222222222222222"),
        ),
        (
            video_module,
            "fastmoss_video_fetch_handler",
            "fastmoss_video_fetch",
            {"video_identity": {"video_id": "7433333333333333333"}, "fetch_plan": {"endpoints": ["overview"]}},
            "/api/video/overview",
            ("id", "7433333333333333333"),
        ),
    ],
)
def test_fastmoss_handlers_return_browser_fallback_for_security_verification(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    handler_name: str,
    handler_code: str,
    payload: dict[str, Any],
    expected_path: str,
    expected_param: tuple[str, str],
) -> None:
    monkeypatch.setattr(module, "FastMossHTTPSession", _SecuritySession)
    handler = getattr(module, handler_name)

    result = handler(
        _context(
            handler_code,
            {
                **payload,
                "fastmoss": {"phone": "18000000000", "password": "secret", "live_fetch": True},
            },
        )
    )

    assert result.status == "fallback_required"
    assert result.error is not None
    assert result.error.error_code == "fastmoss_security_verification_required"
    assert result.error.details["response_code"] == "MSG_SAFE_0001"
    assert result.result["fallback_reason"] == "fastmoss_api_security_verification"
    assert result.result["verification_request"]["path"] == expected_path
    assert result.result["verification_request"]["params"][expected_param[0]] == expected_param[1]
    assert result.next_action is not None
    assert result.next_action.type == "browser_fallback"


def test_fastmoss_handler_returns_browser_fallback_for_session_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(product_module, "FastMossHTTPSession", _SessionConflictSession)

    result = product_module.fastmoss_product_fetch_handler(
        _context(
            "fastmoss_product_fetch",
            {
                "product_identity": {"product_id": "1732183420263764252"},
                "fastmoss": {"phone": "18000000000", "password": "secret", "live_fetch": True},
            },
        )
    )

    assert result.status == "fallback_required"
    assert result.error is not None
    assert result.error.error_code == "fastmoss_auth_session_recovery_required"
    assert result.error.retryable is False
    assert result.result["fallback_reason"] == "fastmoss_auth_session_recovery"
    assert result.result["verification_request"]["path"] == "/api/goods/v3/base"
    assert result.next_action.type == "browser_fallback"
