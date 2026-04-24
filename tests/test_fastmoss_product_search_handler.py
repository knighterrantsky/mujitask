from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from automation_business_scaffold.business.handlers import HandlerContext
from automation_business_scaffold.business.handlers.api import build_bound_api_handler_registry
from automation_business_scaffold.business.handlers.api.implementations import (
    fastmoss_product_search_handler,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPSession


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


def _raw_search_page(*, product_id: str, day7_sold_count: int, title: str = "Halloween decoration") -> dict[str, object]:
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
                    "price": "$14.50 - 18.97",
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
        "automation_business_scaffold.business.handlers.api.implementations.FastMossHTTPSession",
        FakeFastMossSession,
    )

    result = fastmoss_product_search_handler(
        _context(
            {
                "keyword": "desk lamp",
                "fastmoss": {"phone": "phone", "password": "password", "live_fetch": True},
                "pagination": {"page": 1, "page_size": 2, "max_pages": 3, "stop_when_no_new_product": True},
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
