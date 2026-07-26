from __future__ import annotations

import importlib

from automation_business_scaffold.capabilities.input_sources.feishu.row_updates import (
    fields_for_update,
    merge_update_fields,
)
from automation_business_scaffold.capabilities.input_sources.feishu.write_payloads import (
    normalize_write_record,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.product_discovery import (
    HANDLER_CODE as PRODUCT_DISCOVERY_HANDLER_CODE,
    product_video_creator_discovery_handler,
)
from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.creator_sync import (
    HANDLER_CODE as CREATOR_SYNC_HANDLER_CODE,
    influencer_monitor_sync_handler,
)
from automation_business_scaffold.domains.tiktok.mappers.influencer_monitor_source_adapter import (
    influencer_monitor_source_adapter,
)
from automation_business_scaffold.domains.tiktok.policies.influencer_monitor_candidate_policy import (
    aggregate_creator_candidates,
    select_product_video_creator_candidates,
)
from automation_business_scaffold.domains.tiktok.projections.feishu_influencer_monitor_projection import (
    influencer_monitor_projection_mapper,
)

product_discovery_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.product_discovery"
)
creator_sync_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.creator_sync"
)


def _product_discovery_context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-monitor",
        job_id="job-product",
        handler_code=PRODUCT_DISCOVERY_HANDLER_CODE,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code="monitor_tk_influencers",
        stage_code="discover_product_video_creators",
        job_code="discover_product_video_creators",
        business_key="sku-a",
        dedupe_key="monitor:sku-a",
    )


def _successful_fact_result(context: HandlerContext) -> HandlerResult:
    return HandlerResult.success(
        context,
        result={
            "persistence_mode": "database",
            "persisted_counts": {
                "video_product_window_performance": 5,
                "creator_product_window_performance": 3,
            },
            "observation_refs": ["observation"],
        },
    )


def _creator_sync_context(payload: dict) -> HandlerContext:
    return HandlerContext(
        request_id="req-monitor",
        job_id="job-creator",
        handler_code=CREATOR_SYNC_HANDLER_CODE,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code="monitor_tk_influencers",
        stage_code="sync_monitored_influencers",
        job_code="sync_monitored_influencers",
        business_key="creator-a",
        dedupe_key="monitor:creator-a",
    )


def test_source_adapter_includes_all_product_statuses_and_merges_duplicate_skus() -> None:
    result = influencer_monitor_source_adapter(
        [
            {
                "record_id": "rec-normal",
                "fields": {
                    "SKU-ID": "1729679758111249333",
                    "图片": [{"file_token": "img-a", "name": "a.jpg"}],
                    "节日": ["万圣节"],
                    "商品状态": "正常",
                    "达人查找状态": "已完成",
                },
            },
            {
                "record_id": "rec-delisted",
                "fields": {
                    "SKU-ID": "1729679758111249333",
                    "图片": [
                        {"file_token": "img-a", "name": "a.jpg"},
                        {"file_token": "img-b", "name": "b.jpg"},
                    ],
                    "节日": ["圣诞节"],
                    "商品状态": "已下架",
                    "达人查找状态": "失败重试",
                },
            },
            {
                "record_id": "rec-region",
                "fields": {
                    "SKU-ID": "1732183562851553564",
                    "商品状态": "区域不可售",
                    "达人查找状态": "处理中",
                },
            },
        ],
        {"source_table_ref": "feishu://mujitask/tk_competitor"},
    )

    assert [row["product_id"] for row in result["source_rows"]] == [
        "1729679758111249333",
        "1732183562851553564",
    ]
    merged = result["source_rows"][0]
    assert merged["source_record_ids"] == ["rec-normal", "rec-delisted"]
    assert [item["file_token"] for item in merged["source_product_images"]] == [
        "img-a",
        "img-b",
    ]
    assert merged["holidays"] == ["万圣节", "圣诞节"]
    assert set(merged["observed_product_statuses"]) == {"正常", "已下架"}
    assert result["adapter_summary"]["input_row_count"] == 3
    assert result["adapter_summary"]["deduped_product_count"] == 2
    assert result["adapter_summary"]["status_filtered_count"] == 0


def test_candidate_policy_uses_strict_threshold_creator_uid_and_per_product_max() -> None:
    selected = select_product_video_creator_candidates(
        [
            {
                "video_id": "video-50",
                "uid": "creator-a",
                "author": {"uid": "creator-a", "unique_id": "alice"},
                "sold_count": 50,
            },
            {
                "video_id": "video-60",
                "uid": "creator-a",
                "author": {"uid": "creator-a", "unique_id": "alice"},
                "sold_count": 60,
            },
            {
                "video_id": "video-90",
                "uid": "creator-a",
                "author": {"uid": "creator-a", "unique_id": "alice"},
                "sold_count": 90,
            },
            {
                "video_id": "video-51",
                "uid": "creator-b",
                "author": {"uid": "creator-b", "unique_id": "bob"},
                "sold_count": 51,
            },
            {
                "video_id": "video-invalid-identity",
                "uid": "creator-c",
                "author": {"uid": "creator-other"},
                "sold_count": 100,
            },
            {
                "video_id": "video-invalid-sales",
                "uid": "creator-d",
                "author": {"uid": "creator-d"},
                "sold_count": "",
            },
        ],
        product_id="sku-a",
        min_video_sales_28d=50,
    )

    assert selected["qualified_video_count"] == 3
    assert selected["invalid_identity_count"] == 1
    assert selected["invalid_sales_count"] == 1
    assert selected["candidates"] == [
        {
            "creator_id": "creator-a",
            "uid": "creator-a",
            "unique_id": "alice",
            "product_id": "sku-a",
            "video_product_sales_28d": 90,
            "winning_video_id": "video-90",
            "qualified_video_count": 2,
        },
        {
            "creator_id": "creator-b",
            "uid": "creator-b",
            "unique_id": "bob",
            "product_id": "sku-a",
            "video_product_sales_28d": 51,
            "winning_video_id": "video-51",
            "qualified_video_count": 1,
        },
    ]


def test_cross_product_creator_aggregation_uses_max_and_merges_all_qualified_hits() -> None:
    creators = aggregate_creator_candidates(
        [
            {
                "creator_id": "creator-a",
                "uid": "creator-a",
                "unique_id": "alice",
                "product_id": "sku-a",
                "video_product_sales_28d": 90,
                "winning_video_id": "video-a",
                "source_product_images": [{"file_token": "img-a"}],
                "holidays": ["万圣节"],
            },
            {
                "creator_id": "creator-a",
                "uid": "creator-a",
                "unique_id": "alice",
                "product_id": "sku-b",
                "video_product_sales_28d": 120,
                "winning_video_id": "video-b",
                "source_product_images": [{"file_token": "img-b"}],
                "holidays": ["圣诞节"],
            },
        ]
    )

    assert len(creators) == 1
    assert creators[0]["creator_run_max_sales_28d"] == 120
    assert [hit["product_id"] for hit in creators[0]["product_hits"]] == ["sku-a", "sku-b"]


def test_monitor_projection_declares_max_merge_and_noop_date_contract() -> None:
    record = influencer_monitor_projection_mapper(
        {
            "creator_id": "creator-a",
            "creator_fact_bundle": {
                "creator_id": "creator-a",
                "metrics": {
                    "follower_count": 155_500,
                    "aweme_28d_count": 12,
                    "video_sale_amount": 2_442_300,
                    "live_sale_amount": 9_999,
                },
                "contact": {"normalized_text": "creator@example.com"},
            },
            "creator_run_max_sales_28d": 120,
            "source_product_images": [
                {
                    "bucket": "business-media",
                    "object_key": "source/product.jpg",
                    "content_digest": "a" * 64,
                }
            ],
            "holidays": ["万圣节"],
            "cooperation_shop_names": ["Happy Shop"],
        },
        {"write_mode": "upsert"},
    )

    assert record["upsert_key"] == {"field": "达人ID", "value": "creator-a"}
    assert record["fields"]["关联商品销量"] == "120"
    assert record["fields"]["粉丝数"] == "16W"
    assert record["fields"]["带货视频 GMV"] == "244W"
    assert record["fields"]["带货直播 GMV"] == "小于1W"
    assert record["update_merge_strategies"] == {"关联商品销量": "max_numeric"}
    assert record["skip_unchanged_update_fields"] is True
    assert record["conditional_update_fields"] == ["更新日期"]
    assert record["update_excluded_fields"] == ["记录日期"]


def test_explicit_max_merge_does_not_change_existing_default_sales_sum() -> None:
    default_merged = merge_update_fields(
        {"关联商品销量": "80"},
        existing_fields={"关联商品销量": "120"},
        field_schema={},
    )
    max_merged = merge_update_fields(
        {"关联商品销量": "80"},
        existing_fields={"关联商品销量": "120"},
        field_schema={},
        merge_strategies={"关联商品销量": "max_numeric"},
    )

    assert default_merged["关联商品销量"] == "200"
    assert max_merged["关联商品销量"] == "120"


def test_write_record_normalization_preserves_monitor_merge_contract() -> None:
    normalized = normalize_write_record(
        {
            "op": "upsert",
            "fields": {"达人ID": "creator-a", "关联商品销量": "90"},
            "update_merge_strategies": {"关联商品销量": "max_numeric"},
            "skip_unchanged_update_fields": True,
            "conditional_update_fields": ["更新日期"],
        },
        {},
    )

    assert normalized["update_merge_strategies"] == {"关联商品销量": "max_numeric"}
    assert normalized["skip_unchanged_update_fields"] is True
    assert normalized["conditional_update_fields"] == ["更新日期"]


def test_lower_sales_and_identical_fields_produce_no_update_or_date_change() -> None:
    update_fields = fields_for_update(
        {
            "update_excluded_fields": ["记录日期"],
            "update_merge_strategies": {"关联商品销量": "max_numeric"},
            "skip_unchanged_update_fields": True,
            "conditional_update_fields": ["更新日期"],
        },
        {
            "关联商品销量": "80",
            "关联节日": ["万圣节"],
            "记录日期": "2026-07-24",
            "更新日期": "2026-07-25",
        },
        existing_fields={
            "关联商品销量": "120",
            "关联节日": ["万圣节"],
            "记录日期": "2026-07-01",
            "更新日期": "2026-07-24",
        },
        field_schema={"关联节日": {"type": 4}},
    )

    assert update_fields == {}


def test_product_discovery_uses_exact_query_contract_and_stops_at_sorted_threshold(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_fact_upsert(context: HandlerContext) -> HandlerResult:
        captured["payload"] = context.payload
        return _successful_fact_result(context)

    monkeypatch.setattr(
        product_discovery_module,
        "fact_bundle_upsert_handler",
        fake_fact_upsert,
    )
    result = product_video_creator_discovery_handler(
        _product_discovery_context(
            {
                "product_id": "sku-a",
                "min_video_sales_28d": 50,
                "source_record_ids": ["rec-a"],
                "source_product_images": [{"file_token": "img-a"}],
                "holidays": ["万圣节"],
                "mock_fastmoss_product_video_pages": [
                    {
                        "data": {
                            "total": 10,
                            "list": [
                                {
                                    "video_id": "video-100",
                                    "uid": "creator-a",
                                    "sold_count": 100,
                                },
                                {
                                    "video_id": "video-90",
                                    "uid": "creator-a",
                                    "sold_count": 90,
                                },
                                {
                                    "video_id": "video-51",
                                    "uid": "creator-b",
                                    "sold_count": 51,
                                },
                                {
                                    "video_id": "video-50",
                                    "uid": "creator-c",
                                    "sold_count": 50,
                                },
                                {
                                    "video_id": "video-49",
                                    "uid": "creator-d",
                                    "sold_count": 49,
                                },
                            ],
                        }
                    },
                    {
                        "data": {
                            "total": 10,
                            "list": [
                                {
                                    "video_id": "video-not-fetched",
                                    "uid": "creator-e",
                                    "sold_count": 200,
                                }
                            ],
                        }
                    },
                ],
            }
        )
    )

    assert result.status == "success"
    assert result.result["query"] == {
        "pagesize": 5,
        "order": "1,2",
        "d_type": 0,
        "date_type": 28,
        "is_promoted": -1,
    }
    assert result.result["pagination"] == {
        "page_size": 5,
        "pages_fetched": 1,
        "early_stopped": True,
        "monotonic_sales_prefix": True,
        "stop_reason": "sorted_sales_threshold_reached",
    }
    assert result.result["qualified_video_count"] == 3
    assert result.result["qualified_creator_count"] == 2
    assert result.result["candidates"][0]["video_product_sales_28d"] == 100
    assert result.result["candidates"][0]["source_product_images"] == [
        {"file_token": "img-a"}
    ]
    fact_payload = captured["payload"]
    assert isinstance(fact_payload, dict)
    assert fact_payload["require_database_persistence"] is True
    fact_bundle = fact_payload["fact_bundle"]
    assert fact_bundle["media_assets"] == []
    assert len(fact_bundle["video_product_window_performance"]) == 5
    assert len(fact_bundle["creator_product_window_performance"]) == 2
    assert "fact_bundle" not in result.result


def test_live_product_discovery_stops_requesting_after_first_sorted_boundary(
    monkeypatch,
) -> None:
    class FakeFastMossSession:
        calls: list[dict] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def list_product_videos(self, product_id, **params):
            self.calls.append({"product_id": product_id, **params})
            if len(self.calls) > 1:
                raise AssertionError(
                    "sorted threshold boundary must stop the next page request"
                )
            return {
                "data": {
                    "total": 10,
                    "list": [
                        {
                            "video_id": "video-100",
                            "uid": "creator-a",
                            "sold_count": 100,
                        },
                        {
                            "video_id": "video-80",
                            "uid": "creator-b",
                            "sold_count": 80,
                        },
                        {
                            "video_id": "video-60",
                            "uid": "creator-c",
                            "sold_count": 60,
                        },
                        {
                            "video_id": "video-50",
                            "uid": "creator-d",
                            "sold_count": 50,
                        },
                        {
                            "video_id": "video-40",
                            "uid": "creator-e",
                            "sold_count": 40,
                        },
                    ],
                }
            }

    session = FakeFastMossSession()
    monkeypatch.setattr(
        product_discovery_module,
        "build_fastmoss_session",
        lambda settings, session_factory: session,
    )
    monkeypatch.setattr(
        product_discovery_module,
        "prepare_fastmoss_session",
        lambda session, settings: None,
    )
    monkeypatch.setattr(
        product_discovery_module,
        "fact_bundle_upsert_handler",
        _successful_fact_result,
    )

    result = product_video_creator_discovery_handler(
        _product_discovery_context(
            {
                "product_id": "sku-a",
                "min_video_sales_28d": 50,
                "fastmoss": {"live_fetch": True},
            }
        )
    )

    assert result.status == "success"
    assert session.calls == [
        {
            "product_id": "sku-a",
            "page": 1,
            "pagesize": 5,
            "order": "1,2",
            "d_type": 0,
            "date_type": 28,
            "is_promoted": -1,
        }
    ]
    assert result.result["pagination"]["early_stopped"] is True


def test_product_discovery_does_not_early_stop_when_sales_order_is_invalid(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        product_discovery_module,
        "fact_bundle_upsert_handler",
        _successful_fact_result,
    )
    result = product_video_creator_discovery_handler(
        _product_discovery_context(
            {
                "product_id": "sku-a",
                "min_video_sales_28d": 50,
                "mock_fastmoss_product_video_pages": [
                    {
                        "data": {
                            "total": 6,
                            "list": [
                                {
                                    "video_id": "video-100",
                                    "uid": "creator-a",
                                    "sold_count": 100,
                                },
                                {
                                    "video_id": "video-40",
                                    "uid": "creator-b",
                                    "sold_count": 40,
                                },
                                {
                                    "video_id": "video-80",
                                    "uid": "creator-c",
                                    "sold_count": 80,
                                },
                                {
                                    "video_id": "video-invalid",
                                    "uid": "creator-d",
                                    "sold_count": "unknown",
                                },
                                {
                                    "video_id": "video-70",
                                    "uid": "creator-e",
                                    "sold_count": 70,
                                },
                            ],
                        }
                    },
                    {
                        "data": {
                            "total": 6,
                            "list": [
                                {
                                    "video_id": "video-30",
                                    "uid": "creator-f",
                                    "sold_count": 30,
                                }
                            ],
                        }
                    },
                ],
            }
        )
    )

    assert result.status == "success"
    assert result.result["pagination"]["pages_fetched"] == 2
    assert result.result["pagination"]["early_stopped"] is False
    assert result.result["pagination"]["monotonic_sales_prefix"] is False
    assert (
        result.result["pagination"]["stop_reason"]
        == "source_exhausted_non_monotonic"
    )
    assert result.result["qualified_video_count"] == 3
    assert result.result["invalid_sales_count"] == 1


def test_product_discovery_fails_when_fact_persistence_fails(monkeypatch) -> None:
    def failed_fact_upsert(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="persistence_failure",
                error_code="fact_bundle_upsert_failed",
                message="database unavailable",
                retryable=True,
            ),
        )

    monkeypatch.setattr(
        product_discovery_module,
        "fact_bundle_upsert_handler",
        failed_fact_upsert,
    )
    result = product_video_creator_discovery_handler(
        _product_discovery_context(
            {
                "product_id": "sku-a",
                "mock_fastmoss_product_videos": [
                    {
                        "video_id": "video-100",
                        "uid": "creator-a",
                        "sold_count": 100,
                    }
                ],
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "fact_bundle_upsert_failed"
    assert result.result == {
        "product_id": "sku-a",
        "fetch_status": "failed",
        "fetched_video_count": 1,
    }


def test_creator_sync_uses_stable_uid_persists_avatar_and_writes_new_projection(
    monkeypatch,
) -> None:
    captured: dict[str, dict] = {}

    def fake_creator_fetch(context: HandlerContext) -> HandlerResult:
        captured["creator_fetch"] = context.payload
        return HandlerResult.success(
            context,
            result={
                "creator_fact_bundle": {
                    "creator_id": "creator-a",
                    "uid": "creator-a",
                    "metrics": {
                        "follower_count": 125_000,
                        "aweme_28d_count": 15,
                    },
                    "contact": {"normalized_text": "creator@example.com"},
                    "cooperation_shops": [{"shop_name": "Shop A"}],
                },
                "fact_bundle": {
                    "creators": [
                        {
                            "creator_id": "creator-a",
                            "uid": "creator-a",
                        }
                    ],
                    "media_assets": [
                        {
                            "entity_type": "creator",
                            "entity_key": "creator:creator-a",
                            "media_role": "creator_avatar",
                            "source_url": "https://cdn.example.com/avatar.webp",
                        },
                        {
                            "entity_type": "product",
                            "entity_key": "product:sku-a",
                            "media_role": "product_main",
                            "source_url": "https://cdn.example.com/product.webp",
                        },
                    ],
                },
                "media_refs": [
                    {
                        "entity_type": "creator",
                        "entity_key": "creator:creator-a",
                        "media_role": "creator_avatar",
                        "source_url": "https://cdn.example.com/avatar.webp",
                    },
                    {
                        "entity_type": "product",
                        "entity_key": "product:sku-a",
                        "media_role": "product_main",
                        "source_url": "https://cdn.example.com/product.webp",
                    },
                ],
            },
        )

    def fake_media_sync(context: HandlerContext) -> HandlerResult:
        captured["media_sync"] = context.payload
        avatar = {
            "entity_type": "creator",
            "entity_key": "creator:creator-a",
            "media_role": "creator_avatar",
            "bucket": "business-media",
            "object_key": "creator/avatar.webp",
            "content_digest": "a" * 64,
            "size_bytes": 100,
        }
        return HandlerResult.success(
            context,
            result={
                "synced_assets": [avatar],
                "media_fact_bundle": {"media_assets": [avatar]},
            },
        )

    def fake_fact_upsert(context: HandlerContext) -> HandlerResult:
        captured["fact_upsert"] = context.payload
        return _successful_fact_result(context)

    def fake_feishu_write(context: HandlerContext) -> HandlerResult:
        captured["feishu_write"] = context.payload
        return HandlerResult.success(
            context,
            result={
                "written_count": 1,
                "failed_count": 0,
                "records": [{"status": "success", "op": "update"}],
            },
        )

    monkeypatch.setattr(
        creator_sync_module,
        "fastmoss_creator_fetch_handler",
        fake_creator_fetch,
    )
    monkeypatch.setattr(
        creator_sync_module,
        "media_asset_sync_handler",
        fake_media_sync,
    )
    monkeypatch.setattr(
        creator_sync_module,
        "fact_bundle_upsert_handler",
        fake_fact_upsert,
    )
    monkeypatch.setattr(
        creator_sync_module,
        "feishu_table_write_handler",
        fake_feishu_write,
    )

    result = influencer_monitor_sync_handler(
        _creator_sync_context(
            {
                "creator_identity": {
                    "creator_id": "creator-a",
                    "uid": "creator-a",
                    "unique_id": "alice",
                },
                "creator_run_max_sales_28d": 120,
                "product_hits": [
                    {
                        "product_id": "sku-a",
                        "video_product_sales_28d": 120,
                    }
                ],
                "source_product_images": [{"file_token": "img-a"}],
                "holidays": ["万圣节"],
                "target_table_ref": "feishu://mujitask/tk_influencer_monitoring",
            }
        )
    )

    assert result.status == "success"
    assert result.result["write_status"] == "success"
    assert captured["creator_fetch"]["fetch_plan"] == {
        "date_type": 28,
        "endpoints": [
            "base_info",
            "author_index",
            "stat_info",
            "contact",
            "cargo_summary",
            "shop_list",
        ],
    }
    assert captured["media_sync"]["asset_refs"] == [
        {
            "entity_type": "creator",
            "entity_key": "creator:creator-a",
            "media_role": "creator_avatar",
            "source_url": "https://cdn.example.com/avatar.webp",
        }
    ]
    fact_bundle = captured["fact_upsert"]["fact_bundle"]
    assert captured["fact_upsert"]["require_database_persistence"] is True
    assert len(fact_bundle["media_assets"]) == 1
    assert fact_bundle["media_assets"][0]["bucket"] == "business-media"
    assert captured["feishu_write"]["mapper_code"] == (
        "influencer_monitor_projection_mapper"
    )
    assert captured["feishu_write"]["write_mode"] == "upsert"
    assert captured["feishu_write"]["records"][0][
        "creator_run_max_sales_28d"
    ] == 120
    assert captured["feishu_write"]["records"][0]["source_product_images"] == [
        {"file_token": "img-a"}
    ]
    assert captured["feishu_write"]["records"][0]["holidays"] == ["万圣节"]


def test_creator_sync_rejects_noncanonical_creator_identity(monkeypatch) -> None:
    def should_not_fetch(context: HandlerContext) -> HandlerResult:
        raise AssertionError("invalid identity must fail before FastMoss fetch")

    monkeypatch.setattr(
        creator_sync_module,
        "fastmoss_creator_fetch_handler",
        should_not_fetch,
    )
    result = influencer_monitor_sync_handler(
        _creator_sync_context(
            {
                "creator_identity": {
                    "creator_id": "creator-a",
                    "uid": "creator-b",
                }
            }
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_code == "monitor_creator_identity_invalid"
