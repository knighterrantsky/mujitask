from __future__ import annotations

from automation_business_scaffold.capabilities.fact_sources.fastmoss.creator_fetch_handler import (
    fastmoss_creator_fetch_handler,
)
from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry
from automation_business_scaffold.contracts.handler.contract import HandlerContext


def _context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-creator-1",
        job_id="job-creator-1",
        handler_code="fastmoss_creator_fetch",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
    )


def _creator_payload() -> dict:
    return {
        "creator_identity": {
            "uid": "7094679250578015274",
            "unique_id": "roxy_creator",
            "profile_url": "https://www.fastmoss.com/zh/influencer/detail/7094679250578015274",
        },
        "detail_level": "profile_metrics_contact_goods_video",
        "observed_at": "2026-04-24T00:00:00Z",
        "fetch_plan": {"date_type": 28},
        "source_context": {
            "source_record_id": "rec-1",
            "source_table_ref": "feishu://mujitask/TK竞品收集",
            "product_id": "1732183068040729370",
            "holiday": "Graduation",
            "matched_product_sold_count": 72,
        },
        "relation_policy": {
            "include_source_product_relation": True,
            "min_source_product_sold_count": 50,
        },
        "fastmoss_creator_bundle": {
            "uid": "7094679250578015274",
            "unique_id": "roxy_creator",
            "base_info": {
                "uid": "7094679250578015274",
                "unique_id": "roxy_creator",
                "nickname": "Roxy",
                "avatar": "https://example.com/avatar.png",
                "region": "US",
                "follower_count": 128000,
            },
            "author_index": {
                "aweme_28d_count": 16,
                "interaction_rate": 0.12,
            },
            "stat_info": {
                "video_sale_amount": 32000,
                "goods_sale_amount": 41000,
            },
            "cargo_summary": {
                "goods_count": 24,
                "shop_count": 3,
                "total_sold_count": 900,
                "video_sale_amount": 32000,
            },
            "author_contact": {"email": "hello@example.com"},
            "shop_list": {
                "list": [
                    {
                        "seller_id": "7496166867916327706",
                        "shop_name": "Roxy Shop",
                        "shop_avatar": "https://example.com/shop.png",
                    }
                ]
            },
            "goods_list": {
                "list": [
                    {
                        "product_id": "1732183068040729370",
                        "title": "Graduation party decoration set",
                        "cover": "https://example.com/product.png",
                        "seller_id": "7496166867916327706",
                        "shop_title": "Roxy Shop",
                        "sold_count": 72,
                        "sale_amount": 1299,
                        "commission_rate": 0.18,
                    }
                ]
            },
            "video_list": {
                "list": [
                    {
                        "video_id": "7623147954093690143",
                        "video_desc": "Gift haul",
                        "cover": "https://example.com/video.png",
                        "sold_count": 30,
                        "sale_amount": 500,
                        "product_info": [
                            {
                                "product_id": "1732183068040729370",
                                "title": "Graduation party decoration set",
                            }
                        ],
                    }
                ]
            },
        },
    }


def test_default_api_registry_binds_fastmoss_creator_fetch_and_maps_contract() -> None:
    registry = build_bound_api_handler_registry()

    result = registry.dispatch("fastmoss_creator_fetch", _context(_creator_payload()))

    assert result.status == "success"
    assert result.summary["entity_count"] >= 4
    assert result.summary["observation_count"] >= 4
    assert result.result["creator_fact_bundle"]["creator_id"] == "roxy_creator"
    assert result.result["creator_fact_bundle"]["display_name"] == "Roxy"
    assert result.result["creator_fact_bundle"]["metrics"]["follower_count"] == 128000
    assert result.result["quality"]["contact_available"] is True

    entities = result.result["entities"]
    assert entities["creators"][0]["entity_key"] == "fastmoss_creator:roxy_creator"
    assert entities["shops"][0]["entity_key"] == "fastmoss_shop:7496166867916327706"
    assert entities["videos"][0]["entity_key"] == "fastmoss_video:7623147954093690143"

    creator_product_relation = next(
        relation
        for relation in result.result["relations"]
        if relation["relation_type"] == "creator_promotes_product"
    )
    assert creator_product_relation["relation_key"] == (
        "creator_product:roxy_creator:1732183068040729370"
    )
    assert creator_product_relation["metrics"]["sold_count"] == 72
    assert creator_product_relation["metrics"]["sale_amount"] == 1299
    assert creator_product_relation["source_context"]["source_record_id"] == "rec-1"
    assert result.result["product_relations"] == [creator_product_relation]

    observations = result.result["observations"]
    assert {
        (observation["metric_name"], observation["metric_value"])
        for observation in observations
    } >= {
        ("follower_count", 128000),
        ("video_sale_amount", 32000),
    }
    assert any(ref["media_type"] == "creator_avatar" for ref in result.result["media_refs"])
    assert any(ref.endswith("/author.goods_list") for ref in result.result["raw_response_refs"])
    assert result.result["fact_bundle"]["relations"]["creator_videos"][0]["video_id"] == (
        "7623147954093690143"
    )


def test_fastmoss_creator_fetch_skips_without_payload_or_live_config() -> None:
    result = fastmoss_creator_fetch_handler(
        _context({"creator_identity": {"unique_id": "roxy_creator"}})
    )

    assert result.status == "skipped"
    assert result.result["creator_fact_bundle"] == {}
    assert result.result["fact_bundle"]["creators"] == []
