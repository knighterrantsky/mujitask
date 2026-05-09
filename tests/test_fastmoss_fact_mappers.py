from __future__ import annotations

import importlib

from automation_business_scaffold.capabilities.fact_sources.fastmoss.mappers.fact_bundle_mapper import (
    map_fastmoss_author_video_list,
    map_fastmoss_goods_author,
    map_fastmoss_goods_base,
    map_fastmoss_goods_overview,
    map_fastmoss_goods_product_sku,
    map_fastmoss_shop_author,
    map_fastmoss_shop_goods,
    map_fastmoss_video_goods,
    map_fastmoss_video_overview,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.product_fetch_handler import (
    _resolve_fastmoss_product_settings,
)
from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPSession
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

product_fetch_module = importlib.import_module(
    "automation_business_scaffold.capabilities.fact_sources.fastmoss.product_fetch_handler"
)


def _handler_context(handler_code: str, payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-fastmoss-handler",
        job_id=f"job-{handler_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        job_code=handler_code,
    )


def test_map_fastmoss_goods_base_extracts_product_shop_relation_and_media():
    mapped = map_fastmoss_goods_base(
        {
            "code": 200,
            "data": {
                "product": {
                    "product_id": "1732183068040729370",
                    "title": "Valentine Gift",
                    "real_price": "$12.99",
                    "img": "https://example.com/product.png",
                },
                "shop": {
                    "seller_id": "7496166867916327706",
                    "name": "Roxy Shop",
                    "region": "US",
                },
            },
        }
    )

    assert mapped["products"][0]["product_id"] == "1732183068040729370"
    assert mapped["products"][0]["shop_id"] == "7496166867916327706"
    assert mapped["shops"][0]["shop_name"] == "Roxy Shop"
    assert mapped["relations"]["product_shops"][0]["shop_id"] == "7496166867916327706"
    assert mapped["media_assets"][0]["entity_external_id"] == "1732183068040729370"
    assert mapped["products"][0]["facts"] == {}


def test_fastmoss_product_fetch_unwraps_overview_for_metrics_and_observations():
    result = build_bound_api_handler_registry().dispatch(
        "fastmoss_product_fetch",
        _handler_context(
            "fastmoss_product_fetch",
            {
                "product_identity": {"product_id": "1732183068040729370"},
                "fastmoss_bundle": {
                    "base": {
                        "data": {
                            "product": {
                                "product_id": "1732183068040729370",
                                "title": "Valentine Gift",
                                "real_price": "$12.99",
                            }
                        }
                    },
                    "overview": {
                        "data": {
                            "product_id": "1732183068040729370",
                            "d_type": 28,
                            "overview": {"day7_sold_count": 412},
                            "chart_list": [{"dt": "2026-04-23", "inc_sold_count": 38}],
                        }
                    },
                },
            },
        ),
    )

    assert result.status == "success"
    assert result.result["metrics_snapshot"]["overview"]["day7_sold_count"] == 412
    assert result.result["product_fact_bundle"]["product_metric_snapshots"]
    assert result.result["product_fact_bundle"]["product_daily_metrics"][0]["sold_count"] == 38


def test_fastmoss_product_fetch_maps_window_overview_sales_to_sales_90d():
    result = build_bound_api_handler_registry().dispatch(
        "fastmoss_product_fetch",
        _handler_context(
            "fastmoss_product_fetch",
            {
                "product_identity": {"product_id": "1732183618577011482"},
                "fastmoss_bundle": {
                    "overview": {
                        "data": {
                            "product_id": "1732183618577011482",
                            "d_type": 90,
                            "overview": {
                                "sold_count": 1690,
                                "real_sold_count": 1659,
                                "sale_amount": 32642.85,
                            },
                            "chart_list": [
                                {"dt": "2026-04-24", "inc_sold_count": 119},
                                {"dt": "2026-04-25", "inc_sold_count": 168},
                            ],
                        }
                    }
                },
            },
        ),
    )

    assert result.status == "success"
    overview = result.result["metrics_snapshot"]["overview"]
    assert result.result["metrics_snapshot"]["window_days"] == 90
    assert overview["sales_90d"] == "1690"
    assert overview["day90_sold_count"] == "1690"
    assert overview["sold_count_90d"] == "1690"
    assert overview["real_sales_90d"] == "1659"
    metric_snapshot = result.result["product_fact_bundle"]["product_metric_snapshots"][0]
    assert metric_snapshot["window_days"] == 90
    assert metric_snapshot["payload"]["overview"]["sales_90d"] == "1690"
    raw_overview = result.result["product_fact_bundle"]["raw_api_responses"][0]
    assert raw_overview["source_endpoint"] == "goods.overview"
    assert raw_overview["request_params"] == {
        "product_id": "1732183618577011482",
        "d_type": 90,
    }


def test_fastmoss_product_fetch_live_fetch_collects_7_28_90_overview_windows(monkeypatch):
    class FakeSession:
        overview_calls: list[int] = []
        sku_calls: list[int] = []

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def replace_browser_cookies(self, _cookies):
            return None

        def get_product_base(self, product_id):
            return {"data": {"product": {"product_id": product_id, "title": "Windowed Product"}}}

        def get_product_overview(self, product_id, *, d_type):
            self.overview_calls.append(int(d_type))
            return {
                "data": {
                    "product_id": product_id,
                    "d_type": int(d_type),
                    "overview": {
                        "real_sold_count": int(d_type) * 10,
                        "sold_count": int(d_type) * 11,
                        "price": "$9.99",
                    },
                    "chart_list": [{"dt": "2026-04-25", "inc_sold_count": int(d_type)}],
                }
            }

        def get_product_skus(self, product_id, *, d_type):
            self.sku_calls.append(int(d_type))
            return {"data": {"product_id": product_id, "d_type": int(d_type), "sku_list": []}}

        def get_product_sku_distribution(self, product_id, *, d_type):
            return {"data": {"product_id": product_id, "d_type": int(d_type), "sku_list": []}}

        def cookie_snapshot(self):
            return {"cookie_count": 1}

    monkeypatch.setattr(product_fetch_module, "FastMossHTTPSession", FakeSession)

    result = product_fetch_module.fastmoss_product_fetch_handler(
        _handler_context(
            "fastmoss_product_fetch",
            {
                "product_identity": {"product_id": "1732183618577011482"},
                "fastmoss": {"live_fetch": True},
                "fastmoss_overview_window_days": [7, 28, 90],
            },
        )
    )

    assert result.status == "success"
    assert FakeSession.overview_calls == [7, 28, 90]
    assert FakeSession.sku_calls == [28]
    overview = result.result["metrics_snapshot"]["overview"]
    assert result.result["metrics_snapshot"]["window_days"] == [7, 28, 90]
    assert overview["sales_7d"] == "77"
    assert overview["sales_28d"] == "308"
    assert overview["sales_90d"] == "990"
    raw_overview_params = [
        item["request_params"]
        for item in result.result["product_fact_bundle"]["raw_api_responses"]
        if item["source_endpoint"] == "goods.overview"
    ]
    assert raw_overview_params == [
        {"product_id": "1732183618577011482", "d_type": 7},
        {"product_id": "1732183618577011482", "d_type": 28},
        {"product_id": "1732183618577011482", "d_type": 90},
    ]
    assert [
        item["window_days"]
        for item in result.result["product_fact_bundle"]["product_metric_snapshots"]
    ] == [7, 28, 90]


def test_fastmoss_product_fetch_resolves_credentials_from_env_markers(monkeypatch):
    monkeypatch.setenv("PYTEST_FASTMOSS_PHONE", "18000000000")
    monkeypatch.setenv("PYTEST_FASTMOSS_PASSWORD", "secret")

    settings = _resolve_fastmoss_product_settings(
        {
            "fastmoss": {
                "phone_env": "PYTEST_FASTMOSS_PHONE",
                "password_env": "PYTEST_FASTMOSS_PASSWORD",
                "window_days": 90,
            }
        }
    )

    assert settings["phone"] == "18000000000"
    assert settings["password"] == "secret"
    assert settings["window_days"] == 90
    assert settings["overview_window_days"] == [90]


def test_fastmoss_product_fetch_settings_accepts_comma_separated_overview_windows():
    settings = _resolve_fastmoss_product_settings(
        {
            "fastmoss_overview_window_days": "7,28,90",
        }
    )

    assert settings["overview_window_days"] == [7, 28, 90]
    assert settings["window_days"] == 90
    assert settings["sku_window_days"] == 28


def test_fastmoss_product_fetch_direct_window_days_overrides_default_28():
    settings = _resolve_fastmoss_product_settings(
        {
            "fastmoss_window_days": 90,
        }
    )

    assert settings["overview_window_days"] == [90]
    assert settings["window_days"] == 90


def test_product_overview_mapper_keeps_metrics_out_of_product_main_facts():
    mapped = map_fastmoss_goods_overview(
        {
            "data": {
                "product_id": "1732183068040729370",
                "overview": {
                    "d_type": 28,
                    "sales_7d": 88,
                    "sale_amount": 1200.5,
                },
            }
        }
    )

    assert mapped["products"] == [
        {
            "product_id": "1732183068040729370",
            "source_platform": "fastmoss",
            "facts": {},
        }
    ]


def test_product_sku_mapper_keeps_price_and_stock_out_of_sku_main():
    mapped = map_fastmoss_goods_product_sku(
        {
            "data": {
                "product_id": "1732183068040729370",
                "sku_list": [
                    {
                        "sku_id": "sku-pink",
                        "sku_name": "Pink",
                        "real_price": "$12.99",
                        "stock": 7,
                        "sold_count": 31,
                    }
                ],
            }
        }
    )

    sku = mapped["product_skus"][0]
    assert sku == {
        "product_id": "1732183068040729370",
        "sku_id": "sku-pink",
        "sku_name": "Pink",
        "spec_name": "",
        "facts": {},
    }


def test_map_fastmoss_goods_author_extracts_creator_product_and_representative_video():
    mapped = map_fastmoss_goods_author(
        {
            "code": 200,
            "data": {
                "list": [
                    {
                        "uid": "7094679250578015274",
                        "unique_id": "roxy_creator",
                        "nickname": "Roxy",
                        "avatar": "https://example.com/avatar.png",
                        "sold_count": 321,
                        "videos": [
                            {
                                "video_id": "7623147954093690143",
                                "video_desc": "Gift haul",
                                "cover": "https://example.com/video.png",
                            }
                        ],
                    }
                ]
            },
        },
        product_id="1732183068040729370",
    )

    assert mapped["creators"][0]["creator_id"] == "roxy_creator"
    assert mapped["relations"]["creator_products"][0]["product_id"] == "1732183068040729370"
    assert mapped["relations"]["creator_products"][0]["sold_count"] == 321
    assert mapped["videos"][0]["video_id"] == "7623147954093690143"
    assert mapped["videos"][0]["product_id"] == "1732183068040729370"


def test_video_and_author_video_mappers_extract_video_product_relations():
    overview = map_fastmoss_video_overview(
        {
            "data": {
                "video_id": "7623147954093690143",
                "uid": "7094679250578015274",
                "unique_id": "roxy_creator",
                "nickname": "Roxy",
                "video_desc": "Gift haul",
                "cover": "https://example.com/video.png",
            }
        }
    )
    goods = map_fastmoss_video_goods(
        {
            "data": {
                "list": [
                    {
                        "product_id": "1732183068040729370",
                        "title": "Valentine Gift",
                        "seller_id": "7496166867916327706",
                        "shop_name": "Roxy Shop",
                    }
                ]
            }
        },
        video_id="7623147954093690143",
    )
    author_videos = map_fastmoss_author_video_list(
        {
            "data": {
                "list": [
                    {
                        "video_id": "7623147954093690143",
                        "video_desc": "Gift haul",
                        "product_info": [{"product_id": "1732183068040729370", "title": "Valentine Gift"}],
                    }
                ]
            }
        },
        uid="7094679250578015274",
        unique_id="roxy_creator",
    )

    assert overview["creators"][0]["creator_id"] == "roxy_creator"
    assert goods["relations"]["video_products"][0]["product_id"] == "1732183068040729370"
    assert goods["relations"]["product_shops"][0]["shop_id"] == "7496166867916327706"
    assert author_videos["relations"]["video_products"][0]["video_id"] == "7623147954093690143"
    assert author_videos["relations"]["creator_products"][0]["creator_id"] == "roxy_creator"


def test_shop_mappers_extract_products_creators_and_shop_relations():
    goods = map_fastmoss_shop_goods(
        {
            "data": {
                "seller_id": "7496166867916327706",
                "shop_name": "Roxy Shop",
                "product_list": [
                    {
                        "product_id": "1732183068040729370",
                        "title": "Valentine Gift",
                        "img": "https://example.com/product.png",
                    }
                ],
            }
        }
    )
    authors = map_fastmoss_shop_author(
        {
            "data": {
                "seller_id": "7496166867916327706",
                "shop_name": "Roxy Shop",
                "list": [{"uid": "7094679250578015274", "unique_id": "roxy_creator"}],
            }
        }
    )

    assert goods["products"][0]["product_id"] == "1732183068040729370"
    assert goods["relations"]["product_shops"][0]["shop_id"] == "7496166867916327706"
    assert authors["creators"][0]["creator_id"] == "roxy_creator"
    assert authors["relations"]["shop_creators"][0]["shop_key"] == "shop_id:7496166867916327706"


def test_fastmoss_session_only_returns_payload_for_business_layer():
    payload = {
        "code": 200,
        "data": {
            "list": [
                {
                    "uid": "7094679250578015274",
                    "unique_id": "roxy_creator",
                    "nickname": "Roxy",
                    "sold_count": 321,
                }
            ]
        },
    }
    session = FastMossHTTPSession()
    session.request_json = lambda *args, **kwargs: payload  # type: ignore[method-assign]

    returned = session.list_product_authors("1732183068040729370", page=2, pagesize=5)

    assert returned is payload


def test_business_layer_maps_accepted_rows_then_explicitly_ingests(runtime_db_url):
    raw_payload = {
        "code": 200,
        "data": {
            "list": [
                {
                    "uid": "7094679250578015274",
                    "unique_id": "accepted_creator",
                    "nickname": "Accepted",
                    "sold_count": 321,
                },
                {
                    "uid": "111",
                    "unique_id": "rejected_creator",
                    "nickname": "Rejected",
                    "sold_count": 3,
                },
            ]
        },
    }
    accepted_rows = [
        row
        for row in raw_payload["data"]["list"]
        if int(row.get("sold_count") or 0) > 50
    ]
    accepted_payload = {"code": 200, "data": {"list": accepted_rows}}
    mapped = map_fastmoss_goods_author(accepted_payload, product_id="1732183068040729370")
    store = RuntimeStore(db_url=runtime_db_url)

    persisted = TKFactIngestionService(runtime_store=store).ingest_api_response(
        source_platform="fastmoss",
        source_endpoint="goods.v3.author",
        request_params={"product_id": "1732183068040729370"},
        response_payload=accepted_payload,
        products=mapped["products"],
        product_skus=mapped["product_skus"],
        shops=mapped["shops"],
        creators=mapped["creators"],
        videos=mapped["videos"],
        media_assets=mapped["media_assets"],
        relations=mapped["relations"],
        raw_entity_links=mapped["raw_entity_links"],
    )
    fact_store = TKFactStore(runtime_store=store)

    assert any(entity.get("creator_id") == "accepted_creator" for entity in persisted["fact_entities"])
    assert not any(entity.get("creator_id") == "rejected_creator" for entity in persisted["fact_entities"])
    assert fact_store.creator_has_product(
        creator_id="accepted_creator",
        product_id="1732183068040729370",
    )
    assert not fact_store.creator_has_product(
        creator_id="rejected_creator",
        product_id="1732183068040729370",
    )
