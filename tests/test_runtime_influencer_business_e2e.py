from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import automation_business_scaffold.capabilities.fact_sources.fastmoss.creator_fetch_handler as creator_fetch_impl
import automation_business_scaffold.capabilities.fact_sources.fastmoss.product_fetch_handler as product_fetch_impl
import automation_business_scaffold.capabilities.input_sources.feishu.table_common as feishu_common
import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool import (
    DISCOVER_CREATORS_STAGE_CODE,
    READ_STAGE_CODE,
    SYNC_INFLUENCER_POOL_STAGE_CODE,
    WRITEBACK_STAGE_CODE,
)
from automation_business_scaffold.domains.tiktok.tasks.sync_tk_influencer_pool import (
    SyncTKInfluencerPoolTask,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

TASK_CODE = "sync_tk_influencer_pool"
SOURCE_APP_TOKEN = "appSourceToken"
SOURCE_TABLE_ID = "tblSource"
POOL_APP_TOKEN = "appPoolToken"
POOL_TABLE_ID = "tblPool"
SOURCE_TABLE_URL = f"https://muji.feishu.cn/base/{SOURCE_APP_TOKEN}?table={SOURCE_TABLE_ID}&view=vewSource"
POOL_TABLE_URL = f"https://muji.feishu.cn/base/{POOL_APP_TOKEN}?table={POOL_TABLE_ID}"
SOURCE_RECORD_ID = "rec-source-1"
PRODUCT_ID = "1729384756012345678"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
CREATOR_ID = "roxy_creator"
CREATOR_UID = "7094679250578015274"


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
        "execution_child_runner_mode": "inline",
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _submit_request(runtime_db_url: str) -> dict[str, object]:
    task = SyncTKInfluencerPoolTask()
    return task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_url=SOURCE_TABLE_URL,
            target_table_url=POOL_TABLE_URL,
            access_token="test-access-token",
            fact_db_url=runtime_db_url,
            source_record_ids=[SOURCE_RECORD_ID],
            source_channel_code="console",
            reply_target="reply://influencer-business-e2e",
            fastmoss={
                "live_fetch": True,
                "ensure_logged_in": False,
                "window_days": 28,
                "region": "US",
            },
            relation_policy={
                "creator_sold_count_min": 10,
                "creator_follower_count_min": 10000,
            },
        )
    )


def _status(runtime_db_url: str, request_id: str) -> dict[str, object]:
    return runtime_orchestrator.get_task_request_status(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )


def _jobs_for_stage(payload: dict[str, object], stage_code: str, job_code: str = "") -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    for job in payload.get("api_worker_jobs", []):
        if not isinstance(job, dict):
            continue
        job_payload = dict(job.get("payload") or {})
        if str(job_payload.get("stage_code") or "") != stage_code:
            continue
        if job_code and str(job.get("job_code") or "") != job_code:
            continue
        jobs.append(job)
    return jobs


def _bind_fake_business_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "tables": {
            (SOURCE_APP_TOKEN, SOURCE_TABLE_ID): [
                {
                    "record_id": SOURCE_RECORD_ID,
                    "fields": {
                        "SKU-ID": PRODUCT_ID,
                        "产品链接": {"text": PRODUCT_URL, "link": PRODUCT_URL},
                        "节日": "毕业季",
                        "商品状态": "在售",
                        "达人查找状态": "待查找",
                        "商品图片": [{"file_token": "tok-product-image", "name": "product.png"}],
                    },
                }
            ],
            (POOL_APP_TOKEN, POOL_TABLE_ID): [],
        },
        "updates": [],
        "creates": [],
    }

    class FakeFeishuBitableClient:
        def __init__(self, access_token: str, timeout: int = 30) -> None:
            self.access_token = access_token
            self.timeout = timeout

        def list_records(
            self,
            app_token: str,
            table_id: str,
            page_size: int = 20,
            filter_expr: str | None = None,
            page_token: str | None = None,
            view_id: str | None = None,
        ) -> dict[str, Any]:
            del page_size, filter_expr, page_token, view_id
            return {
                "code": 0,
                "data": {
                    "items": deepcopy(state["tables"].get((app_token, table_id), [])),
                    "has_more": False,
                    "page_token": "",
                },
            }

        def list_all_records(
            self,
            app_token: str,
            table_id: str,
            page_size: int = 100,
            filter_expr: str | None = None,
            view_id: str | None = None,
        ) -> list[dict[str, Any]]:
            del page_size, filter_expr, view_id
            return deepcopy(state["tables"].get((app_token, table_id), []))

        def list_all_fields(self, app_token: str, table_id: str, page_size: int = 100) -> list[dict[str, Any]]:
            del page_size
            field_names: set[str] = set()
            for record in state["tables"].get((app_token, table_id), []):
                field_names.update(dict(record.get("fields") or {}).keys())
            return [{"field_name": name} for name in sorted(field_names)]

        def create_record(self, app_token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
            table = state["tables"].setdefault((app_token, table_id), [])
            record_id = f"rec-created-{len(table) + 1}"
            table.append({"record_id": record_id, "fields": deepcopy(fields)})
            state["creates"].append(
                {"app_token": app_token, "table_id": table_id, "record_id": record_id, "fields": deepcopy(fields)}
            )
            return {"code": 0, "data": {"record_id": record_id, "record": {"record_id": record_id}}}

        def update_record(
            self,
            app_token: str,
            table_id: str,
            record_id: str,
            fields: dict[str, Any],
        ) -> dict[str, Any]:
            table = state["tables"].setdefault((app_token, table_id), [])
            for record in table:
                if str(record.get("record_id") or "") == record_id:
                    record.setdefault("fields", {}).update(deepcopy(fields))
                    break
            else:
                table.append({"record_id": record_id, "fields": deepcopy(fields)})
            state["updates"].append(
                {"app_token": app_token, "table_id": table_id, "record_id": record_id, "fields": deepcopy(fields)}
            )
            return {"code": 0, "data": {"record_id": record_id, "record": {"record_id": record_id}}}

    class FakeFastMossHTTPSession:
        default_region = "US"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.default_region = str(kwargs.get("default_region") or "US")

        def __enter__(self) -> "FakeFastMossHTTPSession":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def replace_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
            del cookies

        def set_auth_refresh_callback(self, callback: object) -> None:
            del callback

        def ensure_logged_in(self) -> None:
            return None

        def cookie_snapshot(self) -> dict[str, Any]:
            return {"source": "pytest", "cookie_count": 0}

        def get_product_base(self, product_id: str) -> dict[str, Any]:
            return {
                "product": {
                    "product_id": product_id,
                    "title": "Graduation party decoration set",
                    "img": "https://example.com/product.png",
                    "region": "US",
                },
                "shop": {
                    "seller_id": "shop-1",
                    "name": "Graduation Shop",
                    "region": "US",
                },
            }

        def get_product_overview(self, product_id: str, d_type: int = 28) -> dict[str, Any]:
            return {
                "product_id": product_id,
                "d_type": d_type,
                "overview": {"product_id": product_id, "sales_7d": 88, "sale_amount": 129900},
                "chart_list": [{"dt": "2026-04-23", "inc_sold_count": 12, "inc_sale_amount": 399}],
            }

        def get_product_skus(self, product_id: str, d_type: int = 28) -> dict[str, Any]:
            return {"product_id": product_id, "d_type": d_type, "sku_list": []}

        def get_product_sku_distribution(self, product_id: str, d_type: int = 28) -> dict[str, Any]:
            return {"product_id": product_id, "d_type": d_type, "sku_list": []}

        def list_product_authors(
            self,
            product_id: str,
            page: int = 1,
            pagesize: int = 10,
            order: str = "2,2",
            ecommerce_type: str = "all",
        ) -> dict[str, Any]:
            del page, pagesize, order, ecommerce_type
            return {
                "code": 200,
                "data": {
                    "list": [
                        {
                            "product_id": product_id,
                            "uid": CREATOR_UID,
                            "unique_id": CREATOR_ID,
                            "nickname": "Roxy",
                            "avatar": "https://example.com/avatar.png",
                            "sold_count": 72,
                            "follower_count": 128000,
                        }
                    ]
                },
            }

        def resolve_author_uid(self, uid: str = "", unique_id: str = "") -> str:
            del unique_id
            return uid or CREATOR_UID

        def get_author_base_info(self, uid: str) -> dict[str, Any]:
            return {
                "uid": uid,
                "unique_id": CREATOR_ID,
                "nickname": "Roxy",
                "avatar": "https://example.com/avatar.png",
                "region": "US",
                "follower_count": 128000,
            }

        def get_author_index(self, uid: str) -> dict[str, Any]:
            del uid
            return {"aweme_28d_count": 16, "follower_count": 128000}

        def get_author_stat_info(self, uid: str) -> dict[str, Any]:
            del uid
            return {"video_sale_amount": 32000, "live_sale_amount": 0}

        def get_author_cargo_summary(self, uid: str) -> dict[str, Any]:
            del uid
            return {"goods_count": 24, "shop_count": 1}

        def get_author_contact(self, uid: str) -> dict[str, Any]:
            del uid
            return {"email": "hello@example.com"}

        def get_author_shop_list(
            self,
            uid: str,
            page: int = 1,
            page_size: int = 5,
            region: str = "US",
            order: str = "sold_count,2",
        ) -> dict[str, Any]:
            del uid, page, page_size, region, order
            return {"list": [{"seller_id": "shop-1", "shop_name": "Graduation Shop"}]}

        def list_author_goods(
            self,
            uid: str,
            page: int = 1,
            page_size: int = 5,
            region: str = "US",
            order: str = "sold_count,2",
            date_type: int | str = 28,
        ) -> dict[str, Any]:
            del uid, page, page_size, region, order, date_type
            return {
                "list": [
                    {
                        "product_id": PRODUCT_ID,
                        "title": "Graduation party decoration set",
                        "cover": "https://example.com/product.png",
                        "seller_id": "shop-1",
                        "shop_title": "Graduation Shop",
                        "sold_count": 72,
                        "sale_amount": 1299,
                        "commission_rate": 0.18,
                    }
                ]
            }

        def get_author_video_list(
            self,
            uid: str,
            page: int = 1,
            page_size: int = 5,
            region: str = "US",
            order: str = "sold_count,2",
            date_type: int | str = 28,
        ) -> dict[str, Any]:
            del uid, page, page_size, region, order, date_type
            return {
                "list": [
                    {
                        "video_id": "7620000000000000000",
                        "video_desc": "Gift haul",
                        "cover": "https://example.com/video.png",
                        "sold_count": 30,
                        "sale_amount": 500,
                        "product_info": [
                            {
                                "product_id": PRODUCT_ID,
                                "title": "Graduation party decoration set",
                            }
                        ],
                    }
                ]
            }

    monkeypatch.setattr(feishu_common, "FeishuBitableClient", FakeFeishuBitableClient)
    monkeypatch.setattr(product_fetch_impl, "FastMossHTTPSession", FakeFastMossHTTPSession)
    monkeypatch.setattr(creator_fetch_impl, "FastMossHTTPSession", FakeFastMossHTTPSession)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", None, raising=False)
    return state


def test_sync_tk_influencer_pool_real_business_e2e_persists_facts_and_writes_feishu(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feishu_state = _bind_fake_business_clients(monkeypatch)
    runtime_params = _runtime_params(runtime_db_url)
    submitted = _submit_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    read_dispatch = runtime_orchestrator.execute_executor_once(runtime_params)
    assert read_dispatch["request_id"] == request_id
    assert read_dispatch["current_stage"] == READ_STAGE_CODE
    assert {job["job_code"] for job in read_dispatch["api_worker_jobs"]} == {"feishu_table_read"}

    read_worker = runtime_orchestrator.execute_api_worker_once(runtime_params)
    assert read_worker["api_worker_job"]["job_code"] == "feishu_table_read"
    assert read_worker["api_worker_job"]["status"] == "success"
    assert read_worker["api_worker_job"]["result"]["source_rows"][0]["business_fields"]["holiday"] == "毕业季"

    product_dispatch = runtime_orchestrator.execute_executor_once(runtime_params)
    assert product_dispatch["current_stage"] == DISCOVER_CREATORS_STAGE_CODE
    product_jobs = _jobs_for_stage(product_dispatch, DISCOVER_CREATORS_STAGE_CODE, "product_creator_discovery")
    assert len(product_jobs) == 1
    assert product_jobs[0]["payload"]["fastmoss"]["live_fetch"] is True
    assert product_jobs[0]["payload"]["relation_policy"]["creator_sold_count_min"] == 10

    product_worker = runtime_orchestrator.execute_api_worker_once(runtime_params)
    assert product_worker["api_worker_job"]["job_code"] == "product_creator_discovery"
    assert product_worker["api_worker_job"]["status"] == "success"
    assert product_worker["api_worker_job"]["result"]["related_creators"][0]["creator_id"] == CREATOR_ID

    creator_dispatch = runtime_orchestrator.execute_executor_once(runtime_params)
    assert creator_dispatch["current_stage"] == SYNC_INFLUENCER_POOL_STAGE_CODE
    creator_jobs = _jobs_for_stage(creator_dispatch, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")
    assert len(creator_jobs) == 1
    assert creator_jobs[0]["payload"]["creator_identity"]["uid"] == CREATOR_UID
    assert creator_jobs[0]["payload"]["product_hits"][0]["holiday"] == "毕业季"
    assert "shop_list" in creator_jobs[0]["payload"]["fetch_plan"]["endpoints"]

    creator_worker = runtime_orchestrator.execute_api_worker_once(runtime_params)
    assert creator_worker["api_worker_job"]["job_code"] == "influencer_creator_sync"
    assert creator_worker["api_worker_job"]["status"] == "success"
    assert creator_worker["api_worker_job"]["result"]["creator_fact_bundle"]["creator_id"] == CREATOR_ID
    assert creator_worker["api_worker_job"]["result"]["influencer_pool_write"]["status"] == "success"

    fact_store = TKFactStore(db_url=runtime_db_url)
    assert fact_store.get_product(product_id=PRODUCT_ID)["title"] == "Graduation party decoration set"
    assert fact_store.creator_has_product(
        creator_id=CREATOR_ID,
        uid=CREATOR_UID,
        unique_id=CREATOR_ID,
        product_id=PRODUCT_ID,
    )

    pool_records = feishu_state["tables"][(POOL_APP_TOKEN, POOL_TABLE_ID)]
    assert len(pool_records) == 1
    pool_fields = pool_records[0]["fields"]
    assert pool_fields["达人ID"] == CREATOR_ID
    assert pool_fields["关联节日"] == ["毕业季"]
    assert pool_fields["关联商品销量"] == "72"
    assert pool_fields["粉丝数"] == "13W"
    assert pool_fields["28天视频数"] == "16"
    assert pool_fields["带货视频 GMV"] == "3W"
    assert pool_fields["带货直播 GMV"] == "小于1W"
    assert pool_fields["合作店铺"] == ["Graduation Shop"]
    assert pool_fields["达人联系方式"] == "hello@example.com"
    assert pool_fields["带货商品图"] == [{"file_token": "tok-product-image"}]
    assert pool_fields["达人头像"] == [{"url": "https://example.com/avatar.png"}]

    writeback_dispatch = runtime_orchestrator.execute_executor_once(runtime_params)
    assert writeback_dispatch["current_stage"] == WRITEBACK_STAGE_CODE
    writeback_jobs = _jobs_for_stage(writeback_dispatch, WRITEBACK_STAGE_CODE, "feishu_table_write")
    assert len(writeback_jobs) == 1
    assert writeback_jobs[0]["payload"]["records"][0]["influencer_sync_status"] == "success"

    writeback_worker = runtime_orchestrator.execute_api_worker_once(runtime_params)
    assert writeback_worker["api_worker_job"]["job_code"] == "feishu_table_write"
    assert writeback_worker["api_worker_job"]["status"] == "success"

    source_fields = feishu_state["tables"][(SOURCE_APP_TOKEN, SOURCE_TABLE_ID)][0]["fields"]
    assert source_fields["达人查找状态"] == "已完成"
    assert any(
        update["record_id"] == SOURCE_RECORD_ID and update["fields"]["达人查找状态"] == "已完成"
        for update in feishu_state["updates"]
    )

    finalized = runtime_orchestrator.execute_executor_once(runtime_params)
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "completed"
    assert finalized["summary"]["product_group_status_counts"] == {"success": 1}
    assert finalized["summary"]["product_groups"][0]["fact_persist_success_count"] == 1

    status_payload = _status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["current_stage"] == "completed"
    assert len(_jobs_for_stage(status_payload, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")) == 1
