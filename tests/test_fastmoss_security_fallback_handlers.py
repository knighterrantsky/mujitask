from __future__ import annotations

from typing import Any

import pytest

from automation_business_scaffold.capabilities.fact_sources.fastmoss import creator_fetch_handler as creator_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import product_fetch_handler as product_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import shop_fetch_handler as shop_module
from automation_business_scaffold.capabilities.fact_sources.fastmoss import video_fetch_handler as video_module
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPError, FastMossSessionConflictError


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
