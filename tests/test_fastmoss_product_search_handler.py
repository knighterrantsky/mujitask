from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler import (
    fastmoss_product_search_handler,
)
from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPError, FastMossHTTPSession
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import build_fastmoss_cookie_cache_context
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _context(payload: dict[str, object]) -> HandlerContext:
    return HandlerContext(
        request_id="req-fastmoss-search",
        job_id="job-fastmoss-search",
        handler_code="fastmoss_product_search",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code="search_keyword_competitor_products",
        stage_code="search_product_candidates",
        job_code="fastmoss_product_search",
    )


def _raw_search_page(
    *,
    product_id: str,
    day7_sold_count: int,
    title: str = "Halloween decoration",
    price: str = "$14.50 - 18.97",
) -> dict[str, object]:
    return {
        "code": 200,
        "msg": "success!",
        "data": {
            "product_list": [
                {
                    "product_id": product_id,
                    "title": f"<span style='color:red'>{title.split()[0]}</span> {' '.join(title.split()[1:])}",
                    "img": "https://cdn.example.com/product.jpg",
                    "shop_name": "Spooky Shop",
                    "shop_info": {"seller_id": "seller-1", "currency": "USD"},
                    "price": price,
                    "ori_price": "$20.00",
                    "crate": "8%",
                    "crate_show": "8%",
                    "sold_count": "1.2K",
                    "sale_amount": "$1234.50",
                    "yday_sold_count": 30,
                    "day7_sold_count": day7_sold_count,
                    "day14_sold_count": 410,
                    "day28_sold_count": 900,
                    "relate_author_count": 12,
                    "relate_video_count": 20,
                    "relate_live_count": 2,
                    "product_rating": "4.7",
                    "detail_url": f"https://www.tiktok.com/view/product/{product_id}",
                    "trend": [
                        {
                            "dt": "2026-04-23",
                            "inc_sold_count": 9,
                            "inc_sale_amount": 88.5,
                            "region": "US",
                            "region_name": "United States",
                        }
                    ],
                }
            ],
            "total": 2,
        },
        "ext": {"is_login": 1},
    }


def test_bound_api_registry_includes_fastmoss_product_search(tmp_path: Path) -> None:
    registry = build_bound_api_handler_registry()
    entry = registry.get("fastmoss_product_search")
    assert entry.is_bound

    result = entry.invoke(
        _context(
            {
                "keyword": "Halloween decoration",
                "mock_fastmoss_search_response": _raw_search_page(
                    product_id="1731194997356205027",
                    day7_sold_count=260,
                ),
                "artifact_root": str(tmp_path),
            }
        )
    )

    assert result.status == "success"
    assert result.summary["candidate_count"] == 1
    assert result.result["candidates"][0]["product_id"] == "1731194997356205027"


def test_fastmoss_product_search_normalizes_candidates_and_raw_capture(tmp_path: Path) -> None:
    result = fastmoss_product_search_handler(
        _context(
            {
                "search_mode": "keyword",
                "keyword": "Halloween decoration",
                "region": "US",
                "mock_fastmoss_search_pages": [
                    {
                        "page": 1,
                        "response": _raw_search_page(
                            product_id="1731194997356205027",
                            day7_sold_count=260,
                        ),
                    },
                    {
                        "page": 2,
                        "response": _raw_search_page(
                            product_id="1731194997356205027",
                            day7_sold_count=260,
                        ),
                    },
                    {
                        "page": 3,
                        "response": _raw_search_page(
                            product_id="1730000000000000000",
                            day7_sold_count=10,
                            title="Low volume product",
                        ),
                    },
                ],
                "output_conditions": {
                    "max_candidates": 20,
                    "required_fields": ["product_id", "normalized_product_url", "title"],
                    "dedupe_by": ["product_id", "normalized_product_url"],
                    "business_conditions": {"min_day7_sold_count": 200},
                },
                "artifact_root": str(tmp_path),
            }
        )
    )

    assert result.status == "success"
    assert result.contract_revision == "phase2"
    assert result.result["query"]["source_endpoint"] == "/api/goods/V2/search"
    assert result.result["pagination"]["stop_reason"] == "inline_response"
    assert result.result["condition_summary"]["raw_candidate_count"] == 3
    assert result.result["condition_summary"]["accepted_count"] == 1
    assert result.result["condition_summary"]["rejected_count"] == 1
    assert result.result["condition_summary"]["deduped_count"] == 1

    candidate = result.result["candidates"][0]
    assert candidate["title"] == "Halloween decoration"
    assert candidate["price"]["amount"] == 14.5
    assert candidate["price"]["min_amount"] == 14.5
    assert candidate["price"]["max_amount"] == 18.97
    assert candidate["commission"]["rate"] == 0.08
    assert candidate["metrics"]["sold_count"] == 1200
    assert candidate["metrics"]["day7_sold_count"] == 260
    assert candidate["matched_conditions"] == {"min_day7_sold_count": True}
    assert candidate["quality_score"] == 1.0
    assert candidate["raw_item_ref"].startswith(result.result["raw_response_ref"])

    raw_ref = result.result["raw_response_ref"]
    raw_path = Path(unquote(urlparse(raw_ref).path))
    assert raw_path.exists()
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw_payload["query"]["keyword"] == "Halloween decoration"
    assert len(raw_payload["pages"]) == 3


def test_fastmoss_product_search_filters_by_price_range_max_amount(tmp_path: Path) -> None:
    result = fastmoss_product_search_handler(
        _context(
            {
                "search_mode": "keyword",
                "keyword": "Halloween decoration",
                "mock_fastmoss_search_pages": [
                    {
                        "page": 1,
                        "response": _raw_search_page(
                            product_id="1731194997356205027",
                            day7_sold_count=260,
                            price="$8.99 - $12.50",
                        ),
                    },
                    {
                        "page": 2,
                        "response": _raw_search_page(
                            product_id="1730000000000000001",
                            day7_sold_count=260,
                            price="$8.99 - $10.50",
                        ),
                    },
                    {
                        "page": 3,
                        "response": _raw_search_page(
                            product_id="1730000000000000002",
                            day7_sold_count=260,
                            price="$12.00",
                        ),
                    },
                    {
                        "page": 4,
                        "response": _raw_search_page(
                            product_id="1730000000000000003",
                            day7_sold_count=260,
                            price="$9.99",
                        ),
                    },
                ],
                "output_conditions": {
                    "max_candidates": 20,
                    "business_conditions": {"min_price_range_max_amount": 10.99},
                },
                "artifact_root": str(tmp_path),
            }
        )
    )

    assert result.status == "success"
    assert result.result["condition_summary"]["accepted_count"] == 2
    assert result.result["condition_summary"]["rejected_count"] == 2
    assert [candidate["product_id"] for candidate in result.result["candidates"]] == [
        "1731194997356205027",
        "1730000000000000002",
    ]
    assert result.result["candidates"][0]["matched_conditions"] == {"min_price_range_max_amount": True}


def test_fastmoss_product_search_keeps_existing_max_price_amount_semantics(tmp_path: Path) -> None:
    result = fastmoss_product_search_handler(
        _context(
            {
                "search_mode": "keyword",
                "keyword": "Halloween decoration",
                "mock_fastmoss_search_pages": [
                    {
                        "page": 1,
                        "response": _raw_search_page(
                            product_id="1731194997356205027",
                            day7_sold_count=260,
                            price="$14.50 - 18.97",
                        ),
                    },
                    {
                        "page": 2,
                        "response": _raw_search_page(
                            product_id="1730000000000000001",
                            day7_sold_count=260,
                            price="$20.00",
                        ),
                    },
                ],
                "output_conditions": {
                    "max_candidates": 20,
                    "business_conditions": {"max_price_amount": 15},
                },
                "artifact_root": str(tmp_path),
            }
        )
    )

    assert result.status == "success"
    assert [candidate["product_id"] for candidate in result.result["candidates"]] == ["1731194997356205027"]


def test_fastmoss_product_search_limit_zero_keeps_all_matching_candidates(tmp_path: Path) -> None:
    result = fastmoss_product_search_handler(
        _context(
            {
                "search_mode": "keyword",
                "keyword": "Halloween decoration",
                "mock_fastmoss_search_pages": [
                    {
                        "page": 1,
                        "response": _raw_search_page(
                            product_id="1731194997356205027",
                            day7_sold_count=260,
                        ),
                    },
                    {
                        "page": 2,
                        "response": _raw_search_page(
                            product_id="1730000000000000001",
                            day7_sold_count=240,
                        ),
                    },
                    {
                        "page": 3,
                        "response": _raw_search_page(
                            product_id="1730000000000000002",
                            day7_sold_count=220,
                        ),
                    },
                ],
                "limit": 0,
                "output_conditions": {"business_conditions": {"min_day7_sold_count": 200}},
                "artifact_root": str(tmp_path),
            }
        )
    )

    assert result.status == "success"
    assert result.result["condition_context"]["max_candidates"] == 0
    assert result.result["condition_summary"]["accepted_count"] == 3
    assert [candidate["product_id"] for candidate in result.result["candidates"]] == [
        "1731194997356205027",
        "1730000000000000001",
        "1730000000000000002",
    ]


def test_fastmoss_product_search_stops_when_day7_sorted_page_is_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    pages = {
        1: {
            "code": 200,
            "msg": "success!",
            "data": {
                "product_list": [
                    {"product_id": "p1", "title": "Graduation gift one", "day7_sold_count": 320},
                    {"product_id": "p2", "title": "Graduation gift two", "day7_sold_count": 260},
                ],
                "total": 100,
            },
            "ext": {"is_login": 1},
        },
        2: {
            "code": 200,
            "msg": "success!",
            "data": {
                "product_list": [
                    {"product_id": "p3", "title": "Graduation gift low one", "day7_sold_count": 199},
                    {"product_id": "p4", "title": "Graduation gift low two", "day7_sold_count": 120},
                ],
                "total": 100,
            },
            "ext": {"is_login": 1},
        },
    }

    class FakeFastMossSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("init", dict(kwargs)))

        def __enter__(self) -> "FakeFastMossSession":
            calls.append(("enter", {}))
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("exit", {}))

        def ensure_logged_in(self) -> dict[str, object]:
            calls.append(("ensure_logged_in", {}))
            return {"code": 200}

        def search_products(self, words: str, **kwargs: object) -> dict[str, object]:
            calls.append(("search_products", {"words": words, **kwargs}))
            page = int(kwargs["page"])
            assert page in pages
            return pages[page]

        def cookie_snapshot(self) -> dict[str, object]:
            return {"has_fd_tk": True, "cookie_count": 1}

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "2026 graduation",
                "fastmoss": {
                    "phone": "phone",
                    "password": "password",
                    "live_fetch": True,
                    "fastmoss_api_request_delay_min_seconds": 0,
                    "fastmoss_api_request_delay_max_seconds": 0,
                },
                "pagination": {"page": 1, "page_size": 2, "max_pages": 5, "stop_when_no_new_product": True},
                "limit": 0,
                "output_conditions": {"business_conditions": {"min_day7_sold_count": 200}},
                "page_request_delay_seconds": 0,
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )

    search_calls = [payload for name, payload in calls if name == "search_products"]
    assert result.status == "success"
    assert [call["page"] for call in search_calls] == [1, 2]
    assert all(call["order"] == "2,2" for call in search_calls)
    assert result.result["pagination"]["stop_reason"] == "below_min_day7_sold_count"
    assert result.result["pagination"]["has_more"] is False
    assert result.result["pagination"]["fetched_pages"] == 2
    assert result.result["condition_summary"]["raw_candidate_count"] == 4
    assert result.result["condition_summary"]["accepted_count"] == 2
    assert result.result["condition_summary"]["rejected_count"] == 2
    assert [candidate["product_id"] for candidate in result.result["candidates"]] == ["p1", "p2"]


def test_fastmoss_product_search_fetches_live_pages_with_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    pages = {
        1: {
            "code": 200,
            "msg": "success!",
            "data": {
                "product_list": [
                    {"product_id": "p1", "title": "Desk lamp one", "day7_sold_count": 300},
                    {"product_id": "p2", "title": "Desk lamp two", "day7_sold_count": 250},
                ],
                "total": 20,
            },
            "ext": {"is_login": 1},
        },
        2: {
            "code": 200,
            "msg": "success!",
            "data": {
                "product_list": [
                    {"product_id": "p1", "title": "Desk lamp one", "day7_sold_count": 300},
                ],
                "total": 20,
            },
            "ext": {"is_login": 1},
        },
    }

    class FakeFastMossSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("init", dict(kwargs)))

        def __enter__(self) -> "FakeFastMossSession":
            calls.append(("enter", {}))
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("exit", {}))

        def replace_browser_cookies(self, cookies: list[dict[str, object]]) -> int:
            calls.append(("replace_browser_cookies", {"count": len(cookies)}))
            return len(cookies)

        def ensure_logged_in(self) -> dict[str, object]:
            calls.append(("ensure_logged_in", {}))
            return {"code": 200}

        def search_products(self, words: str, **kwargs: object) -> dict[str, object]:
            calls.append(("search_products", {"words": words, **kwargs}))
            return pages[int(kwargs["page"])]

        def cookie_snapshot(self) -> dict[str, object]:
            return {"has_fd_tk": True, "cookie_count": 1}

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "desk lamp",
                "fastmoss": {
                    "phone": "phone",
                    "password": "password",
                    "live_fetch": True,
                    "fastmoss_api_request_delay_min_seconds": 0,
                    "fastmoss_api_request_delay_max_seconds": 0,
                },
                "pagination": {"page": 1, "page_size": 2, "max_pages": 3, "stop_when_no_new_product": True},
                "page_request_delay_seconds": 0,
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )

    search_calls = [payload for name, payload in calls if name == "search_products"]
    assert result.status == "success"
    assert [call["page"] for call in search_calls] == [1, 2]
    assert all(call["check_auth"] is False for call in search_calls)
    assert result.result["pagination"]["stop_reason"] == "no_new_product"
    assert [candidate["product_id"] for candidate in result.result["candidates"]] == ["p1", "p2"]
    assert ("ensure_logged_in", {}) in calls
    init_payload = next(payload for name, payload in calls if name == "init")
    assert init_payload["request_delay_range"] == (0.0, 0.0)


def test_fastmoss_product_search_refreshes_session_once_after_security_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeFastMossSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.search_count = 0
            calls.append(("init", dict(kwargs)))

        def __enter__(self) -> "FakeFastMossSession":
            calls.append(("enter", {}))
            return self

        def __exit__(self, *args: object) -> None:
            calls.append(("exit", {}))

        def replace_browser_cookies(self, cookies: list[dict[str, object]]) -> int:
            calls.append(("replace_browser_cookies", {"count": len(cookies)}))
            return len(cookies)

        def clear_cookies_for_domain(self, domain_keyword: str) -> int:
            calls.append(("clear_cookies_for_domain", {"domain_keyword": domain_keyword}))
            return 1

        def login(self) -> dict[str, object]:
            calls.append(("login", {}))
            return {"code": 200}

        def ensure_logged_in(self) -> dict[str, object]:
            calls.append(("ensure_logged_in", {}))
            return {"code": 200}

        def search_products(self, words: str, **kwargs: object) -> dict[str, object]:
            self.search_count += 1
            calls.append(("search_products", {"words": words, **kwargs}))
            if self.search_count == 1:
                raise FastMossHTTPError(
                    "FastMoss request failed",
                    status_code=200,
                    response_code="MSG_SAFE_0001",
                    payload={"code": "MSG_SAFE_0001", "data": {"id": 290777}, "ext": {"is_login": 1}},
                    stage="product.search",
                    method="GET",
                    path="/api/goods/V2/search",
                )
            return {
                "code": 200,
                "msg": "success!",
                "data": {
                    "product_list": [
                        {"product_id": "p1", "title": "Desk lamp one", "day7_sold_count": 300},
                    ],
                    "total": 1,
                },
                "ext": {"is_login": 1},
            }

        def cookie_snapshot(self) -> dict[str, object]:
            return {"has_fd_tk": True, "cookie_count": 1}

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "desk lamp",
                "fastmoss": {"phone": "phone", "password": "password", "live_fetch": True},
                "pagination": {"page": 1, "page_size": 2, "max_pages": 1},
                "page_request_delay_seconds": 0,
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )

    assert result.status == "success"
    assert [name for name, _payload in calls].count("search_products") == 2
    assert ("clear_cookies_for_domain", {"domain_keyword": "fastmoss.com"}) in calls
    assert ("login", {}) in calls
    assert result.result["candidates"][0]["product_id"] == "p1"


def test_fastmoss_product_search_persists_cookie_after_security_login_refresh(
    monkeypatch: pytest.MonkeyPatch,
    runtime_db_url: str,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeFastMossSession:
        base_url = "https://www.fastmoss.com"

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.search_count = 0
            self.token = "old-token"
            calls.append(("init", dict(kwargs)))

        def __enter__(self) -> "FakeFastMossSession":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def has_credentials(self) -> bool:
            return True

        def clear_cookies_for_domain(self, domain_keyword: str) -> int:
            calls.append(("clear_cookies_for_domain", {"domain_keyword": domain_keyword}))
            self.token = ""
            return 1

        def login(self) -> dict[str, object]:
            calls.append(("login", {}))
            self.token = "new-token"
            return {"code": 200, "ext": {"is_login": 1}}

        def ensure_logged_in(self, *, relogin_on_auth_fail: bool = True) -> dict[str, object]:
            calls.append(("ensure_logged_in", {"relogin_on_auth_fail": relogin_on_auth_fail}))
            return {"code": 200, "ext": {"is_login": 1}}

        def search_products(self, words: str, **kwargs: object) -> dict[str, object]:
            self.search_count += 1
            calls.append(("search_products", {"words": words, **kwargs}))
            if self.search_count == 1:
                raise FastMossHTTPError(
                    "FastMoss request failed",
                    status_code=200,
                    response_code="MSG_SAFE_0001",
                    payload={"code": "MSG_SAFE_0001", "data": {"id": 290777}, "ext": {"is_login": 1}},
                    stage="product.search",
                    method="GET",
                    path="/api/goods/V2/search",
                )
            return {
                "code": 200,
                "msg": "success!",
                "data": {"product_list": [{"product_id": "p1", "title": "Desk lamp one", "day7_sold_count": 300}], "total": 1},
                "ext": {"is_login": 1},
            }

        def export_cookies(self, *, domain_keyword: str = "fastmoss.com") -> list[dict[str, object]]:
            del domain_keyword
            return [{"name": "fd_tk", "value": self.token, "domain": ".fastmoss.com", "path": "/", "secure": True}]

        def replace_browser_cookies(self, cookies: list[dict[str, object]]) -> int:
            values = [str(cookie.get("value") or "") for cookie in cookies if cookie.get("name") == "fd_tk"]
            self.token = values[0] if values else ""
            return len(cookies)

        def cookie_snapshot(self) -> dict[str, object]:
            digest = {"old-token": "old-digest", "new-token": "new-digest"}.get(self.token, "")
            return {"has_fd_tk": bool(self.token), "cookie_count": 1 if self.token else 0, "fd_tk_digest": digest}

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "desk lamp",
                "fastmoss": {
                    "phone": "18000000000",
                    "password": "password",
                    "live_fetch": True,
                    "execution_control_db_url": runtime_db_url,
                },
                "pagination": {"page": 1, "page_size": 2, "max_pages": 1},
                "page_request_delay_seconds": 0,
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )
    cache_context = build_fastmoss_cookie_cache_context(
        base_url="https://www.fastmoss.com",
        account_key="18000000000",
        region="US",
    )
    loaded = RuntimeStore(db_url=runtime_db_url).load_fastmoss_cookie_cache(cache_key=str(cache_context["cache_key"]))

    assert result.status == "success"
    assert loaded is not None
    assert loaded["cookies"][0]["value"] == "new-token"
    assert loaded["fd_tk_digest"] == "new-digest"
    assert loaded["last_auth_failed_at"] == 0


def test_fastmoss_product_search_reports_security_verification_after_refresh_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeFastMossSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("init", dict(kwargs)))

        def __enter__(self) -> "FakeFastMossSession":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def clear_cookies_for_domain(self, domain_keyword: str) -> int:
            calls.append(("clear_cookies_for_domain", {"domain_keyword": domain_keyword}))
            return 1

        def login(self) -> dict[str, object]:
            calls.append(("login", {}))
            return {"code": 200}

        def ensure_logged_in(self) -> dict[str, object]:
            calls.append(("ensure_logged_in", {}))
            return {"code": 200}

        def search_products(self, words: str, **kwargs: object) -> dict[str, object]:
            calls.append(("search_products", {"words": words, **kwargs}))
            raise FastMossHTTPError(
                "FastMoss request failed",
                status_code=200,
                response_code="MSG_SAFE_0001",
                payload={"code": "MSG_SAFE_0001", "data": {"id": 290777}, "ext": {"is_login": 1}},
                stage="product.search",
                method="GET",
                path="/api/goods/V2/search",
            )

        def cookie_snapshot(self) -> dict[str, object]:
            return {"has_fd_tk": True, "cookie_count": 1}

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_search_handler.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "desk lamp",
                "fastmoss": {"phone": "phone", "password": "password", "live_fetch": True},
                "pagination": {"page": 1, "page_size": 2, "max_pages": 1},
                "page_request_delay_seconds": 0,
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "security_verification"
    assert result.error.error_code == "fastmoss_security_verification_required"
    assert result.error.retryable is False
    assert result.error.details["response_code"] == "MSG_SAFE_0001"
    assert [name for name, _payload in calls].count("search_products") == 2
    assert ("login", {}) in calls


def test_fastmoss_product_search_rejects_degraded_preview() -> None:
    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "Halloween decoration",
                "mock_fastmoss_search_response": {
                    "code": "MAG_AUTH_3001",
                    "msg": "Sorry, insufficient search times.",
                    "data": {
                        "product_list": [
                            {
                                "product_id": "1729398461940339414",
                                "title": "Preview product",
                            }
                        ],
                        "total": 5,
                    },
                    "ext": {"is_login": 0},
                },
                "session_policy": {"require_login": True, "degraded_preview_allowed": False},
                "raw_capture_policy": {"store_raw_response": False},
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "fastmoss_search_degraded_preview"
    assert result.result["auth_state"]["degraded_preview"] is True


def test_fastmoss_http_session_search_products_uses_goods_v2_search(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FastMossHTTPSession()
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, **kwargs: object) -> dict[str, object]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"code": 200, "msg": "success!", "data": {"product_list": []}, "ext": {"is_login": 1}}

    monkeypatch.setattr(session, "request_json", fake_request_json)

    payload = session.search_products(
        "desk lamp",
        page=2,
        pagesize=20,
        region="US",
        order="2,2",
        extra_params={"category": "home"},
        check_auth=False,
    )

    assert payload["code"] == 200
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/goods/V2/search"
    assert captured["params"] == {
        "page": 2,
        "pagesize": 20,
        "order": "2,2",
        "region": "US",
        "words": "desk lamp",
        "category": "home",
    }
    assert captured["region"] == "US"
    assert captured["stage"] == "product.search"
    assert captured["check_auth"] is False


def test_fastmoss_http_session_does_not_inherit_environment_proxy_by_default() -> None:
    session = FastMossHTTPSession()
    try:
        assert session.trust_env is False
        assert session.session.trust_env is False
    finally:
        session.close()


def test_fastmoss_http_session_can_opt_into_environment_proxy() -> None:
    session = FastMossHTTPSession(trust_env=True)
    try:
        assert session.trust_env is True
        assert session.session.trust_env is True
    finally:
        session.close()
