from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from automation_business_scaffold.acceptance import (
    build_runtime_acceptance_artifacts,
    compare_achieve_payload,
)
import automation_business_scaffold.business.feishu_common as feishu_common
from automation_business_scaffold.business.flows import runtime_orchestrator
import automation_business_scaffold.business.handlers.api.implementations as api_impl
from automation_business_scaffold.domains.competitor_intelligence.tasks.refresh_current_competitor_table import (
    RefreshCurrentCompetitorTableTask,
)
from automation_business_scaffold.domains.competitor_intelligence.tasks.search_keyword_competitor_products import (
    SearchKeywordCompetitorProductsTask,
)
from automation_business_scaffold.domains.competitor_intelligence.tasks.sync_tk_influencer_pool import (
    SyncTKInfluencerPoolTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "achieve_acceptance"

COMPETITOR_REF = "feishu://base/tbl_tk_competitor"
INFLUENCER_POOL_REF = "feishu://base/tbl_tk_influencer_pool"
COMPETITOR_TABLE = ("app-competitor", "tbl-competitor")
INFLUENCER_POOL_TABLE = ("app-influencer", "tbl-influencer")


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


def _load_acceptance_payload(workflow_code: str, scenario_id: str) -> tuple[dict[str, Any], Path]:
    base_dir = FIXTURE_ROOT / workflow_code / scenario_id
    return json.loads((base_dir / "payload.json").read_text(encoding="utf-8")), base_dir


def _load_json(base_dir: Path, relative_path: str) -> dict[str, Any]:
    return json.loads((base_dir / relative_path).read_text(encoding="utf-8"))


def _candidate_artifact_refs(workflow_code: str, scenario_id: str) -> dict[str, str]:
    prefix = f"artifact://runtime-acceptance/{workflow_code}/{scenario_id}"
    return {
        "runtime_trace_ref": f"{prefix}/runtime-trace.json",
        "fact_projection_ref": f"{prefix}/fact-projection.json",
        "feishu_projection_ref": f"{prefix}/feishu-projection.json",
        "outbox_ref": f"{prefix}/outbox.json",
    }


def _assert_matches_achieve(
    *,
    workflow_code: str,
    scenario_id: str,
    store: RuntimeStore,
    request_id: str,
    feishu_records: list[dict[str, Any]],
) -> dict[str, Any]:
    payload, base_dir = _load_acceptance_payload(workflow_code, scenario_id)
    payload = deepcopy(payload)
    refs = _candidate_artifact_refs(workflow_code, scenario_id)
    payload["candidate"].update(refs)
    baseline_trace = _load_json(base_dir, "baseline/trace.json")
    baseline_output = _load_json(base_dir, "baseline/output.json")
    artifacts = build_runtime_acceptance_artifacts(
        store=store,
        request_id=request_id,
        workflow_code=workflow_code,
        baseline_trace=baseline_trace,
        feishu_records=feishu_records,
        baseline_feishu_projection=baseline_output["feishu_projection"],
    )
    result = compare_achieve_payload(
        payload,
        base_dir=base_dir,
        artifact_values=artifacts.artifact_values(refs),
    )
    assert result["status"] == "pass", result
    assert result["summary"]["unexpected_difference_count"] == 0
    assert result["summary"]["missing_required_count"] == 0
    return result


def _run_until_terminal(runtime_db_url: str, task_code: str, request_id: str) -> dict[str, Any]:
    params = _runtime_params(runtime_db_url)
    terminal_statuses = {"success", "partial_success", "failed", "cancelled"}
    latest_status: dict[str, Any] = {}
    for _ in range(80):
        progressed = False
        for runner in (
            runtime_orchestrator.execute_executor_once,
            runtime_orchestrator.execute_api_worker_once,
            runtime_orchestrator.execute_browser_once,
        ):
            payload = runner(params)
            if payload.get("daemon_status") == "processed":
                progressed = True
        latest_status = runtime_orchestrator.get_task_request_status(
            task_code,
            _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
        )
        if latest_status.get("request_status") in terminal_statuses:
            return latest_status
        if not progressed:
            break
    pytest.fail(f"workflow did not reach terminal status: {latest_status}")


class _FakeFeishuState:
    def __init__(self) -> None:
        self.tables: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.refs: dict[tuple[str, str], str] = {
            COMPETITOR_TABLE: COMPETITOR_REF,
            INFLUENCER_POOL_TABLE: INFLUENCER_POOL_REF,
        }
        self.writes: list[dict[str, Any]] = []

    def table_refs(self) -> dict[str, dict[str, str]]:
        return {
            COMPETITOR_REF: {
                "app_token": COMPETITOR_TABLE[0],
                "table_id": COMPETITOR_TABLE[1],
                "view_id": "view-competitor",
                "access_token": "test-access-token",
            },
            INFLUENCER_POOL_REF: {
                "app_token": INFLUENCER_POOL_TABLE[0],
                "table_id": INFLUENCER_POOL_TABLE[1],
                "view_id": "view-influencer",
                "access_token": "test-access-token",
            },
        }

    def seed_table(self, table_key: tuple[str, str], rows: list[dict[str, Any]]) -> None:
        self.tables[table_key] = deepcopy(rows)

    def projection_records(self, *table_refs: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        wanted = set(table_refs)
        for table_key, rows in self.tables.items():
            table_ref = self.refs.get(table_key, "")
            if wanted and table_ref not in wanted:
                continue
            for row in rows:
                records.append(
                    {
                        "target_table_ref": table_ref,
                        "record_id": row.get("record_id", ""),
                        "fields": deepcopy(row.get("fields") or {}),
                    }
                )
        return records


def _bind_fake_feishu(monkeypatch: pytest.MonkeyPatch, state: _FakeFeishuState) -> None:
    class FakeFeishuBitableClient:
        def __init__(self, access_token: str, timeout: int = 30) -> None:
            self.access_token = access_token
            self.timeout = timeout

        def list_records(self, app_token, table_id, **kwargs):
            del kwargs
            return {"data": {"items": deepcopy(state.tables.get((app_token, table_id), [])), "has_more": False}}

        def list_all_records(self, app_token, table_id, page_size=100, view_id=None):
            del page_size, view_id
            return deepcopy(state.tables.get((app_token, table_id), []))

        def list_all_fields(self, app_token, table_id, page_size=100):
            del page_size
            field_names = {
                "SKU-ID",
                "商品链接",
                "产品链接",
                "商品名称",
                "标题",
                "商品状态",
                "昨日销量",
                "近7天销量",
                "近90天销量",
                "关联节日",
                "达人查找状态",
                "达人数量",
                "达人ID",
                "达人昵称",
                "粉丝数",
                "关联商品销量",
                "合作店铺",
                "记录日期",
                "记录时间",
                "备注",
            }
            for row in state.tables.get((app_token, table_id), []):
                field_names.update((row.get("fields") or {}).keys())
            return [{"field_name": name} for name in sorted(field_names)]

        def create_record(self, app_token, table_id, fields):
            table = state.tables.setdefault((app_token, table_id), [])
            record_id = _created_record_id(fields, len(table) + 1)
            table.append({"record_id": record_id, "fields": deepcopy(fields)})
            state.writes.append(
                {
                    "target_table_ref": state.refs.get((app_token, table_id), ""),
                    "record_id": record_id,
                    "fields": deepcopy(fields),
                }
            )
            return {"code": 0, "data": {"record_id": record_id, "record": {"record_id": record_id}}}

        def update_record(self, app_token, table_id, record_id, fields):
            table = state.tables.setdefault((app_token, table_id), [])
            for row in table:
                if row.get("record_id") == record_id:
                    row.setdefault("fields", {}).update(deepcopy(fields))
                    break
            else:
                table.append({"record_id": record_id, "fields": deepcopy(fields)})
            state.writes.append(
                {
                    "target_table_ref": state.refs.get((app_token, table_id), ""),
                    "record_id": record_id,
                    "fields": deepcopy(fields),
                }
            )
            return {"code": 0, "data": {"record_id": record_id, "record": {"record_id": record_id}}}

    monkeypatch.setattr(feishu_common, "FeishuBitableClient", FakeFeishuBitableClient)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", None, raising=False)


def _created_record_id(fields: dict[str, Any], index: int) -> str:
    if fields.get("达人ID"):
        return f"rec_{fields['达人ID']}"
    if fields.get("SKU-ID") == "SKU-HALLOWEEN-001":
        return "rec_keyword_seed_001"
    return f"rec-created-{index}"


def test_refresh_current_competitor_table_runtime_matches_achieve_acceptance(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _FakeFeishuState()
    state.seed_table(
        COMPETITOR_TABLE,
        [
            {
                "record_id": "rec_refresh_001",
                "fields": {
                    "SKU-ID": "SKU-1001",
                    "商品链接": "https://www.tiktok.com/shop/pdp/1001?source=feishu",
                    "商品状态": "待更新",
                    "昨日销量": "",
                    "近7天销量": "",
                    "记录日期": "",
                },
            }
        ],
    )
    _bind_fake_feishu(monkeypatch, state)

    submitted = RefreshCurrentCompetitorTableTask().run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            source_table_ref=COMPETITOR_REF,
            table_refs=state.table_refs(),
            field_names=["SKU-ID", "商品链接", "商品状态", "昨日销量", "近7天销量", "记录日期"],
            refresh_filter={"candidate_policy": "missing_auto_maintained_fields", "auto_fields": ["昨日销量", "近7天销量", "记录日期"]},
            raw_request_result={
                "product": {
                    "product_id": "SKU-1001",
                    "product_url": "https://www.tiktok.com/shop/pdp/1001",
                    "title": "Pumpkin light set",
                }
            },
            fastmoss_bundle=_product_fastmoss_bundle(
                product_id="SKU-1001",
                title="Pumpkin light set",
                day7=1200,
                yday=320,
            ),
            fact_db_url=runtime_db_url,
            artifact_root=str(tmp_path),
            reply_target="reply://acceptance/refresh",
            source_channel_code="console",
        )
    )
    request_id = str(submitted["request_id"])
    status = _run_until_terminal(runtime_db_url, "refresh_current_competitor_table", request_id)
    assert status["request_status"] == "success"

    _assert_matches_achieve(
        workflow_code="refresh_current_competitor_table",
        scenario_id="competitor_row_refresh_minimal",
        store=RuntimeStore(db_url=runtime_db_url),
        request_id=request_id,
        feishu_records=state.projection_records(COMPETITOR_REF),
    )


def test_search_keyword_competitor_products_runtime_matches_achieve_acceptance(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _FakeFeishuState()
    state.seed_table(COMPETITOR_TABLE, [])
    _bind_fake_feishu(monkeypatch, state)

    submitted = SearchKeywordCompetitorProductsTask().run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            search_query="Halloween decoration",
            filters={"min_day7_sold_count": 200},
            output_conditions={"dedupe_by": "sku_id"},
            max_candidates=1,
            seed_table_ref=COMPETITOR_REF,
            table_refs=state.table_refs(),
            mock_fastmoss_search_response={
                "code": 200,
                "data": {
                    "list": [
                        {
                            "product_id": "SKU-HALLOWEEN-001",
                            "product_url": "https://www.tiktok.com/shop/pdp/halloween-001",
                            "title": "Halloween pumpkin lights",
                            "day7_sold_count": 268,
                            "associated_holidays": ["Halloween", "Fall"],
                        }
                    ]
                },
            },
            raw_request_result={
                "product": {
                    "product_id": "SKU-HALLOWEEN-001",
                    "product_url": "https://www.tiktok.com/shop/pdp/halloween-001",
                    "title": "Halloween pumpkin lights",
                }
            },
            fastmoss_bundle=_product_fastmoss_bundle(
                product_id="SKU-HALLOWEEN-001",
                title="Halloween pumpkin lights",
                day7=268,
                yday=42,
            ),
            fact_db_url=runtime_db_url,
            artifact_root=str(tmp_path),
            reply_target="reply://acceptance/keyword",
            source_channel_code="console",
        )
    )
    request_id = str(submitted["request_id"])
    status = _run_until_terminal(runtime_db_url, "search_keyword_competitor_products", request_id)
    assert status["request_status"] == "success"

    _assert_matches_achieve(
        workflow_code="search_keyword_competitor_products",
        scenario_id="keyword_halloween_min_day7_sales",
        store=RuntimeStore(db_url=runtime_db_url),
        request_id=request_id,
        feishu_records=state.projection_records(COMPETITOR_REF),
    )


def test_sync_tk_influencer_pool_runtime_matches_achieve_acceptance(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _FakeFeishuState()
    state.seed_table(
        COMPETITOR_TABLE,
        [
            {
                "record_id": "rec_competitor_009",
                "fields": {
                    "SKU-ID": "SKU-CREATOR-009",
                    "商品链接": "https://www.tiktok.com/shop/pdp/creator-009",
                    "达人查找状态": "待查找",
                },
            }
        ],
    )
    state.seed_table(INFLUENCER_POOL_TABLE, [])
    _bind_fake_feishu(monkeypatch, state)
    monkeypatch.setattr(api_impl, "FastMossHTTPSession", _FakeFastMossHTTPSession)

    submitted = SyncTKInfluencerPoolTask().run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            source_table_ref=COMPETITOR_REF,
            influencer_pool_table_ref=INFLUENCER_POOL_REF,
            competitor_status_table_ref=COMPETITOR_REF,
            table_refs=state.table_refs(),
            access_token="test-access-token",
            source_record_ids=["rec_competitor_009"],
            creator_filters={"min_follower_count": 10000, "max_creator_count": 1},
            relation_policy={"creator_sold_count_min": 1, "creator_follower_count_min": 10000},
            upsert_key="达人ID",
            fastmoss={"live_fetch": True, "ensure_logged_in": False, "window_days": 28, "region": "US"},
            fact_db_url=runtime_db_url,
            reply_target="reply://acceptance/influencer",
            source_channel_code="console",
        )
    )
    request_id = str(submitted["request_id"])
    status = _run_until_terminal(runtime_db_url, "sync_tk_influencer_pool", request_id)
    assert status["request_status"] == "success"

    _assert_matches_achieve(
        workflow_code="sync_tk_influencer_pool",
        scenario_id="influencer_pool_basic_upsert",
        store=RuntimeStore(db_url=runtime_db_url),
        request_id=request_id,
        feishu_records=state.projection_records(INFLUENCER_POOL_REF, COMPETITOR_REF),
    )


def _product_fastmoss_bundle(*, product_id: str, title: str, day7: int, yday: int) -> dict[str, Any]:
    return {
        "base": {
            "data": {
                "product": {"product_id": product_id, "title": title, "real_price": "$9.99"},
                "shop": {"seller_id": "shop-pumpkin", "name": "Pumpkin Home", "region": "US"},
            }
        },
        "overview": {
            "data": {
                "product_id": product_id,
                "d_type": 28,
                "overview": {
                    "real_price": "$9.99",
                    "yday_sold_count": yday,
                    "day7_sold_count": day7,
                },
                "chart_list": [{"dt": "2026-04-23", "inc_sold_count": yday, "inc_sale_amount": 99}],
            }
        },
        "skus": {"data": {"product_id": product_id, "d_type": 28, "sku_list": []}},
    }


class _FakeFastMossHTTPSession:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.default_region = str(kwargs.get("default_region") or "US")

    def __enter__(self) -> "_FakeFastMossHTTPSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def replace_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        del cookies

    def ensure_logged_in(self) -> None:
        return None

    def cookie_snapshot(self) -> dict[str, Any]:
        return {"source": "pytest", "cookie_count": 0}

    def get_product_base(self, product_id: str) -> dict[str, Any]:
        return {
            "product": {"product_id": product_id, "title": "Halloween pumpkin lights", "region": "US"},
            "shop": {"seller_id": "shop-pumpkin", "name": "Pumpkin Home", "region": "US"},
        }

    def get_product_overview(self, product_id: str, d_type: int = 28) -> dict[str, Any]:
        return {
            "product_id": product_id,
            "d_type": d_type,
            "overview": {"product_id": product_id, "day7_sold_count": 268, "sale_amount": 129900},
            "chart_list": [{"dt": "2026-04-23", "inc_sold_count": 38, "inc_sale_amount": 399}],
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
            "data": {
                "list": [
                    {
                        "product_id": product_id,
                        "uid": "uid_creator_9009",
                        "unique_id": "creator_9009",
                        "nickname": "Pumpkin Maker",
                        "sold_count": 268,
                        "follower_count": 15000,
                    }
                ]
            }
        }

    def resolve_author_uid(self, uid: str = "", unique_id: str = "") -> str:
        del unique_id
        return uid or "uid_creator_9009"

    def get_author_base_info(self, uid: str) -> dict[str, Any]:
        return {
            "uid": uid,
            "unique_id": "creator_9009",
            "nickname": "Pumpkin Maker",
            "avatar": "https://example.com/creator.png",
            "region": "US",
            "follower_count": 15000,
        }

    def get_author_index(self, uid: str) -> dict[str, Any]:
        del uid
        return {"aweme_28d_count": 8, "follower_count": 15000}

    def get_author_stat_info(self, uid: str) -> dict[str, Any]:
        del uid
        return {"video_sale_amount": 12000, "live_sale_amount": 0}

    def get_author_cargo_summary(self, uid: str) -> dict[str, Any]:
        del uid
        return {"goods_count": 12, "shop_count": 2}

    def get_author_contact(self, uid: str) -> dict[str, Any]:
        del uid
        return {"email": "pumpkin@example.com"}

    def get_author_shop_list(
        self,
        uid: str,
        page: int = 1,
        page_size: int = 5,
        region: str = "US",
        order: str = "sold_count,2",
    ) -> dict[str, Any]:
        del uid, page, page_size, region, order
        return {"list": [{"seller_id": "shop-pumpkin", "shop_name": "Pumpkin Home"}, {"seller_id": "shop-fall", "shop_name": "Fall Finds"}]}

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
                    "product_id": "SKU-CREATOR-009",
                    "title": "Halloween pumpkin lights",
                    "seller_id": "shop-pumpkin",
                    "shop_title": "Pumpkin Home",
                    "sold_count": 268,
                    "sale_amount": 1299,
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
        return {"list": []}
