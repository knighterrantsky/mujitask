import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from automation_framework.agent.server import create_app
from automation_framework.runtime import RunRegistry

from automation_business_scaffold.cli import list_registered_tasks, run_registered_task
from automation_business_scaffold.flows.tk_fact_store import TKFactStore
from automation_business_scaffold.registry import build_task_registry
from automation_business_scaffold.tasks import (
    FeishuClearRowByUrlTask,
    FeishuSingleRowUpdateTask,
    RefreshCurrentCompetitorTableTask,
    SearchKeywordCompetitorProductsTask,
    SourceToTargetPublishDemoTask,
)


def test_demo_task_runs_and_records_steps_signals_and_artifacts(tmp_path):
    registry = build_task_registry()
    run_registry = RunRegistry(str(tmp_path / "runs"))
    app = create_app(registry, run_registry=run_registry)
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "task_name": "source_to_target_publish_demo",
            "params": {
                "title": "Demo Vintage Chair",
                "price": 128,
                "run_mode": "draft",
            },
            "wait": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"

    steps_payload = client.get(f"/runs/{payload['run_id']}/steps").json()
    assert [item["step_id"] for item in steps_payload] == [
        "extract_source_item",
        "map_publish_payload",
        "fill_target_form",
        "save_target_draft",
    ]
    assert all(item["status"] == "success" for item in steps_payload)
    assert Path(steps_payload[0]["artifacts"]["state_dump"]).exists()
    assert Path(steps_payload[2]["artifacts"]["html_snapshot"]).exists()

    signals_payload = client.get(f"/runs/{payload['run_id']}/signals").json()
    assert [item["signal_type"] for item in signals_payload] == [
        "step.completed",
        "step.completed",
        "step.completed",
        "step.completed",
    ]

    artifacts_payload = client.get(f"/runs/{payload['run_id']}/artifacts").json()
    assert [item["step_id"] for item in artifacts_payload] == [
        "extract_source_item",
        "map_publish_payload",
        "fill_target_form",
        "save_target_draft",
    ]


def test_demo_task_submit_effect_is_blocked_in_draft_mode(tmp_path):
    registry = build_task_registry()
    run_registry = RunRegistry(str(tmp_path / "runs"))
    app = create_app(registry, run_registry=run_registry)
    client = TestClient(app)

    response = client.post(
        "/runs",
        json={
            "task_name": "source_to_target_publish_demo",
            "params": {
                "title": "Demo Vintage Chair",
                "price": 128,
                "run_mode": "draft",
                "include_submit": True,
            },
            "wait": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"

    steps_payload = client.get(f"/runs/{payload['run_id']}/steps").json()
    assert [item["step_id"] for item in steps_payload] == [
        "extract_source_item",
        "map_publish_payload",
        "fill_target_form",
        "submit_target_publish",
    ]
    assert steps_payload[-1]["status"] == "failed"
    assert steps_payload[-1]["validation"]["code"] == "RUN_MODE_BLOCKED"

    signals_payload = client.get(f"/runs/{payload['run_id']}/signals").json()
    assert [item["signal_type"] for item in signals_payload] == [
        "step.completed",
        "step.completed",
        "step.completed",
        "run_mode.blocked",
    ]


def test_demo_task_workflow_builder_uses_expected_workflow_id():
    task = SourceToTargetPublishDemoTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "source_to_target_publish_demo_v1"
    assert workflow.run_mode == "draft"


def test_feishu_clear_row_by_url_workflow_builder_uses_expected_workflow_id():
    task = FeishuClearRowByUrlTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "feishu_clear_row_by_url_v1"
    assert workflow.run_mode == "draft"


def test_search_keyword_competitor_products_workflow_builder_uses_expected_workflow_id():
    task = SearchKeywordCompetitorProductsTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "search_keyword_competitor_products_v1"
    assert workflow.run_mode == "draft"


def test_cli_runner_executes_registered_workflow_task_and_records_outputs(tmp_path):
    payload = run_registered_task(
        "source_to_target_publish_demo",
        params={
            "title": "CLI Demo Chair",
            "price": 128,
            "run_mode": "draft",
        },
        run_dir=tmp_path / "cli_runs",
    )

    assert payload["status"] == "success"
    assert Path(payload["run_file"]).exists()
    assert Path(payload["steps_file"]).exists()
    assert Path(payload["signals_file"]).exists()
    assert Path(payload["artifacts_dir"]).exists()
    assert payload["result"]["data"]["workflow_id"] == "source_to_target_publish_demo_v1"


def test_cli_runner_lists_registered_tasks():
    payload = list_registered_tasks()

    assert payload == [
        {
            "name": "fastmoss_keyword_candidate_discovery",
            "description": (
                "Search FastMoss by keyword, keep products whose 7-day sales exceed the threshold, "
                "and skip existing Feishu items."
            ),
        },
        {
            "name": "fastmoss_login_check",
            "description": "Validate the FastMoss account login once at the beginning of an orchestrated flow.",
        },
        {
            "name": "fastmoss_product_sales_snapshot",
            "description": (
                "Open a FastMoss detail page directly, log in only if the detail page requires it, "
                "and collect price plus yesterday/7d/28d/90d sales metrics."
            ),
        },
        {
            "name": "feishu_clear_row_by_url",
            "description": (
                "Find one Feishu competitor row by 产品链接 and clear every other field for testing reset flows."
            ),
        },
        {
            "name": "feishu_pending_rows_scan",
            "description": "Scan the Feishu table and return rows whose auto-maintained fields are still incomplete.",
        },
        {
            "name": "feishu_seed_row_insert",
            "description": (
                "Insert one new Feishu seed row for a discovered SKU and mark its source keyword in the remark field."
            ),
        },
        {
            "name": "feishu_single_row_update",
            "description": (
                "Update one Feishu competitor row by fetching TikTok fields plus FastMoss screenshot and sales metrics."
            ),
        },
        {
            "name": "refresh_current_competitor_table",
            "description": (
                "Refresh the current Feishu competitor table by running cleanup, scanning pending rows, "
                "queueing browser updates, and emitting one final summary notification."
            ),
        },
        {
            "name": "search_keyword_competitor_products",
            "description": (
                "Search FastMoss by keyword, insert new Feishu seed rows, queue browser detail updates, "
                "and emit one final summary notification."
            ),
        },
        {
            "name": "source_to_target_publish_demo",
            "description": "Demo workflow showing extract -> map -> fill -> draft/submit on top of automation-framework.",
        },
        {
            "name": "sync_tk_influencer_pool",
            "description": "Synchronize pending competitor products into the TK influencer pool via FastMoss HTTP APIs.",
        },
        {
            "name": "tiktok_feishu_single_sync",
            "description": (
                "Fetch one TikTok Shop product URL and insert one Feishu Bitable row; "
                "skip if the URL or SKU already exists."
            ),
        },
        {
            "name": "tiktok_product_link_cleanup",
            "description": (
                "Normalize TikTok product links from Feishu, write the normalized URL back to 产品链接, "
                "and delete duplicate rows."
            ),
        },
        {
            "name": "tiktok_product_to_feishu",
            "description": "Fetch a TikTok Shop product page and prepare Feishu Bitable fields for the item.",
        },
    ]


def test_cli_runner_supports_controlled_submit_without_workflow_validation_error(tmp_path):
    payload = run_registered_task(
        "feishu_single_row_update",
        params={
            "control_action": "submit",
            "record_id": "rec-cli-submit",
            "profile_ref": "main",
            "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
            "access_token": "token-demo",
            "execution_control_db_path": str(tmp_path / "control.sqlite3"),
        },
        run_dir=tmp_path / "cli_runs",
    )

    assert payload["status"] == "success"
    step_output = payload["result"]["data"]["step_outputs"]["update_single_row"]
    assert step_output["control_action"] == "submit"
    assert step_output["summary"]["total"] == 1
    assert step_output["summary"]["counts"] == {"queued": 1}
    assert step_output["request_status"] == "queued"


def _controlled_context(params: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        step=SimpleNamespace(step_id="update_single_row"),
        params=params,
    )


def _refresh_context(params: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        step=SimpleNamespace(step_id="orchestrate_refresh_current_competitor_table"),
        params=params,
    )


def _keyword_search_context(params: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        step=SimpleNamespace(step_id="orchestrate_search_keyword_competitor_products"),
        params=params,
    )


def test_controlled_task_submit_and_status_round_trip(tmp_path):
    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control.sqlite3"

    submit_result = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-001",
                "profile_ref": "main",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert submit_result.data["request_status"] == "queued"
    assert submit_result.data["execution_status"] == "queued"
    assert submit_result.data["resource_code"] == "browser.tiktok.main"

    status_result = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "status",
                "request_id": submit_result.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert status_result.data["request_id"] == submit_result.data["request_id"]
    assert status_result.data["queue_position"] == 1
    assert status_result.data["request"]["payload"]["record_id"] == "rec-001"


def test_cli_runner_supports_phase1_top_level_submit_without_workflow_validation_error(tmp_path):
    payload = run_registered_task(
        "refresh_current_competitor_table",
        params={
            "control_action": "submit",
            "profile_ref": "main",
            "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
            "access_token": "token-demo",
            "execution_control_db_path": str(tmp_path / "phase1.sqlite3"),
        },
        run_dir=tmp_path / "cli_runs",
    )

    assert payload["status"] == "success"
    step_output = payload["result"]["data"]["step_outputs"]["orchestrate_refresh_current_competitor_table"]
    assert step_output["control_action"] == "submit"
    assert step_output["request_status"] == "pending"
    assert step_output["summary"]["total"] == 1
    assert step_output["summary"]["counts"] == {"queued": 1}


def test_phase1_refresh_task_submit_status_executor_browser_outbox_round_trip(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_tiktok_product_link_cleanup",
            "run_feishu_pending_rows_scan",
            "run_feishu_single_row_update",
            "Phase1RuntimeStore",
        ],
    )
    task = RefreshCurrentCompetitorTableTask()
    db_path = tmp_path / "phase1.sqlite3"

    def fake_cleanup(params):
        return {
            "summary": {"total": 3, "counts": {"keep": 3}},
            "items": [{"record_id": "rec-a"}, {"record_id": "rec-b"}, {"record_id": "rec-c"}],
        }

    def fake_scan(params):
        return {
            "summary": {"total": 3, "counts": {"pending": 2, "skipped_completed": 1}},
            "items": [
                {"record_id": "rec-a", "status": "pending"},
                {"record_id": "rec-b", "status": "pending"},
                {"record_id": "rec-c", "status": "skipped_completed"},
            ],
            "target_rows": [
                {"record_id": "rec-a", "source_url": "https://example.com/a"},
                {"record_id": "rec-b", "source_url": "https://example.com/b"},
            ],
        }

    def fake_single_row_update(params):
        record_id = str(params["record_id"])
        if record_id == "rec-b":
            item = {"record_id": record_id, "status": "skipped_unavailable"}
        else:
            item = {
                "record_id": record_id,
                "status": "updated",
                "source_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                "product_id": "1731098351299629802",
                "fields": {"Fastmoss价格": "10.19", "记录日期": 1776009600000},
                "logical_fields": {
                    "product_id": "1731098351299629802",
                    "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "title": "Product A",
                    "price_amount": "9.99",
                },
                "fastmoss_snapshot": {
                    "product_id": "1731098351299629802",
                    "fastmoss_price_amount": "10.19",
                    "sales_7d": "499",
                },
            }
        return {
            "summary": {"total": 1, "counts": {item["status"]: 1}},
            "item": item,
            "items": [item],
        }

    monkeypatch.setattr(module, "run_tiktok_product_link_cleanup", fake_cleanup)
    monkeypatch.setattr(module, "run_feishu_pending_rows_scan", fake_scan)
    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_single_row_update)

    submitted = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "submit",
                "profile_ref": "main",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "noop",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert submitted.data["request_status"] == "pending"
    assert submitted.data["summary"]["counts"] == {"queued": 1}

    status_payload = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "status",
                "request_id": submitted.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert status_payload.data["request_status"] == "pending"

    executor_once = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert executor_once.data["request_status"] == "waiting_children"
    assert executor_once.data["current_stage"] == "waiting_children"
    assert executor_once.data["child_total_count"] == 2
    assert len(executor_once.data["executions"]) == 2

    browser_loop = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "browser_loop",
                "execution_control_db_path": str(db_path),
                "execution_control_stop_when_idle": True,
                "execution_control_max_idle_cycles": 1,
            }
        )
    )
    assert browser_loop.data["processed_count"] == 2

    ready_status = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "status",
                "request_id": submitted.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert ready_status.data["request_status"] == "ready_for_summary"

    executor_summary = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert executor_summary.data["request_status"] == "success"
    assert executor_summary.data["current_stage"] == "completed"
    assert executor_summary.data["summary"]["total"] == 2
    assert executor_summary.data["summary"]["counts"] == {"success": 1, "skipped": 1}
    assert len(executor_summary.data["outbox"]) == 1
    assert executor_summary.data["outbox"][0]["status"] == "pending"

    store = module.Phase1RuntimeStore(db_path=str(db_path))
    execution_records = store.list_task_executions(request_id=submitted.data["request_id"])
    assert len(execution_records) == 2
    assert all(len(store.list_artifacts(run_id=record.run_id)) == 5 for record in execution_records)
    fact_store = TKFactStore(runtime_store=store)
    product = fact_store.get_product(product_id="1731098351299629802")
    assert product["product_id"] == "1731098351299629802"
    assert product["title"] == "Product A"
    assert "entity_snapshot" not in fact_store.table_names()

    outbox_once = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "outbox_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert outbox_once.data["dispatcher_status"] == "processed"
    assert outbox_once.data["summary"]["counts"] == {"sent": 1}

    final_result = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "result",
                "request_id": submitted.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert final_result.data["request_status"] == "success"
    assert final_result.data["outbox"][0]["status"] == "sent"
    assert {item["record_id"] for item in final_result.data["items"]} == {"rec-a", "rec-b"}
    assert len(final_result.data["result"]["fact_entities"]) >= 1
    assert final_result.data["result"]["fact_entities"][0]["product_id"] == "1731098351299629802"
    assert len(final_result.data["result"]["raw_api_responses"]) >= 1
    assert "entity_snapshots" not in final_result.data["result"]


def test_phase1_keyword_search_task_round_trip(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_fastmoss_keyword_candidate_discovery",
            "run_feishu_seed_row_insert",
            "run_feishu_single_row_update",
            "Phase1RuntimeStore",
        ],
    )
    task = SearchKeywordCompetitorProductsTask()
    db_path = tmp_path / "phase1_keyword.sqlite3"

    def fake_discovery(params):
        keyword = str(params["search_keyword"])
        return {
            "summary": {"total": 3, "counts": {"candidate_new": 2, "skipped_existing": 1}},
            "items": [
                {
                    "product_id": "sku-1",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "status": "candidate_new",
                },
                {
                    "product_id": "sku-2",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
                    "status": "candidate_new",
                },
                {
                    "product_id": "sku-existing",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731000000000000000",
                    "status": "skipped_existing",
                    "existing_record_id": "rec-existing",
                },
            ],
            "target_items": [
                {
                    "product_id": "sku-1",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "search_keyword": keyword,
                },
                {
                    "product_id": "sku-2",
                    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
                    "search_keyword": keyword,
                },
            ],
            "settings": {"search_keyword": keyword, "sales_7d_threshold": 200.0},
        }

    def fake_seed_insert(params):
        sku_id = str(params["sku_id"])
        record_id = "rec-a" if sku_id == "sku-1" else "rec-b"
        return {
            "summary": {"total": 1, "counts": {"inserted": 1}},
            "item": {
                "record_id": record_id,
                "product_id": sku_id,
                "normalized_url": str(params.get("product_url", "") or ""),
                "status": "inserted",
            },
            "items": [
                {
                    "record_id": record_id,
                    "product_id": sku_id,
                    "normalized_url": str(params.get("product_url", "") or ""),
                    "status": "inserted",
                }
            ],
        }

    def fake_single_row_update(params):
        record_id = str(params["record_id"])
        if record_id == "rec-b":
            item = {
                "record_id": record_id,
                "status": "skipped_unavailable",
                "source_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
                "normalized_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
                "product_id": "sku-2",
            }
        else:
            item = {
                "record_id": record_id,
                "status": "updated",
                "source_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                "product_id": "1731098351299629802",
                "fields": {"Fastmoss价格": "10.19", "记录日期": 1776009600000},
                "logical_fields": {
                    "product_id": "1731098351299629802",
                    "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "title": "Keyword Product A",
                    "price_amount": "9.99",
                },
                "fastmoss_snapshot": {
                    "product_id": "1731098351299629802",
                    "fastmoss_price_amount": "10.19",
                    "sales_7d": "499",
                },
            }
        return {
            "summary": {"total": 1, "counts": {item["status"]: 1}},
            "item": item,
            "items": [item],
        }

    monkeypatch.setattr(module, "run_fastmoss_keyword_candidate_discovery", fake_discovery)
    monkeypatch.setattr(module, "run_feishu_seed_row_insert", fake_seed_insert)
    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_single_row_update)

    submitted = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "submit",
                "profile_ref": "main",
                "search_keyword": "Easter Basket Stuffers",
                "sales_7d_threshold": "200",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "noop",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert submitted.data["request_status"] == "pending"
    assert submitted.data["summary"]["counts"] == {"queued": 1}

    executor_once = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert executor_once.data["request_status"] == "waiting_children"
    assert len(executor_once.data["executions"]) == 1
    assert executor_once.data["executions"][0]["item_code"] == "fastmoss_keyword_candidate_discovery"

    browser_once = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "browser_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert browser_once.data["processed_count"] == 1
    assert browser_once.data["execution_status"] == "success"

    executor_resume = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert executor_resume.data["request_status"] == "waiting_children"
    assert executor_resume.data["result"]["seed_insert"]["summary"]["counts"] == {"inserted": 2}

    browser_loop = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "browser_loop",
                "execution_control_db_path": str(db_path),
                "execution_control_stop_when_idle": True,
                "execution_control_max_idle_cycles": 1,
            }
        )
    )
    assert browser_loop.data["processed_count"] == 2

    executor_summary = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert executor_summary.data["request_status"] == "success"
    assert executor_summary.data["summary"]["total"] == 3
    assert executor_summary.data["summary"]["counts"] == {
        "candidate_new": 2,
        "inserted": 2,
        "skipped": 1,
        "skipped_existing": 1,
        "success": 1,
    }

    outbox_once = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "outbox_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert outbox_once.data["dispatcher_status"] == "processed"

    final_result = task.execute_workflow_step(
        _keyword_search_context(
            {
                "control_action": "result",
                "request_id": submitted.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert final_result.data["request_status"] == "success"
    assert final_result.data["outbox"][0]["status"] == "sent"
    assert final_result.data["result"]["discovery"]["summary"]["counts"] == {
        "candidate_new": 2,
        "skipped_existing": 1,
    }
    assert final_result.data["result"]["seed_insert"]["summary"]["counts"] == {"inserted": 2}
    assert {item["record_id"] for item in final_result.data["items"]} == {"rec-a", "rec-b"}
    assert len(final_result.data["result"]["fact_entities"]) >= 1
    assert final_result.data["result"]["fact_entities"][0]["product_id"] == "1731098351299629802"
    assert "entity_bindings" not in final_result.data["result"]

    store = module.Phase1RuntimeStore(db_path=str(db_path))
    executions = store.list_task_executions(request_id=submitted.data["request_id"])
    assert len(executions) == 3
    assert sum(1 for execution in executions if execution.item_code == "fastmoss_keyword_candidate_discovery") == 1
    assert sum(1 for execution in executions if execution.item_code == "feishu_single_row_update") == 2


def test_keyword_outbox_text_reports_zero_results():
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=["_build_outbox_text"],
    )

    request = SimpleNamespace(
        request_id="req-keyword-zero",
        task_code="search_keyword_competitor_products",
        payload={"search_keyword": "Halloween decoration"},
    )
    summary = {"total": 0, "counts": {}}
    result = {
        "discovery": {"summary": {"total": 0, "counts": {}}},
        "discovery_execution": {"result": {"pages_scanned": 1, "rows_scanned": 0}},
        "seed_insert": {"summary": {"total": 0, "counts": {}}},
    }

    message_text = module._build_outbox_text(request, summary, result)

    assert message_text == (
        "关键词 Halloween decoration 搜索完成；扫描 1 页；命中 0 条候选；未写入新记录"
    )


def test_keyword_failure_outbox_text_includes_keyword():
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=["_build_failure_outbox_text"],
    )

    request = SimpleNamespace(
        request_id="req-keyword-failed",
        task_code="search_keyword_competitor_products",
        payload={"search_keyword": "Easter Basket Stuffers"},
    )

    message_text = module._build_failure_outbox_text(request, "FastMoss login failed")

    assert message_text == "关键词 Easter Basket Stuffers 搜索失败：FastMoss login failed"


def test_phase1_refresh_task_syncs_browser_artifacts_to_minio_store(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_tiktok_product_link_cleanup",
            "run_feishu_pending_rows_scan",
            "run_feishu_single_row_update",
            "Phase1RuntimeStore",
        ],
    )
    from automation_business_scaffold.flows.artifact_store import StoredArtifact

    task = RefreshCurrentCompetitorTableTask()
    db_path = tmp_path / "phase1_minio.sqlite3"
    image_path = tmp_path / "main-image.png"
    product_screenshot_path = tmp_path / "product-page.png"
    fastmoss_screenshot_path = tmp_path / "fastmoss-page.png"
    image_path.write_bytes(b"png-image")
    product_screenshot_path.write_bytes(b"png-product")
    fastmoss_screenshot_path.write_bytes(b"png-fastmoss")
    uploaded_keys: list[str] = []

    class FakeArtifactStore:
        provider_code = "minio"

        def build_uri(self, *, bucket: str, object_key: str) -> str:
            return f"s3://{bucket}/{object_key}"

        def upload_file(self, *, bucket: str, object_key: str, local_path: Path, content_type: str, metadata=None):
            uploaded_keys.append(object_key)
            return StoredArtifact(
                bucket=bucket,
                object_key=object_key,
                etag=f"etag-{local_path.name}",
                size=local_path.stat().st_size,
                content_type=content_type,
                uri=self.build_uri(bucket=bucket, object_key=object_key),
                metadata={"storage_backend": "minio", "endpoint": "127.0.0.1:9000"},
            )

    monkeypatch.setattr(module, "create_store_from_settings", lambda _settings: FakeArtifactStore())
    monkeypatch.setattr(
        module,
        "run_tiktok_product_link_cleanup",
        lambda params: {"summary": {"total": 1, "counts": {"keep": 1}}, "items": [{"record_id": "rec-a"}]},
    )
    monkeypatch.setattr(
        module,
        "run_feishu_pending_rows_scan",
        lambda params: {
            "summary": {"total": 1, "counts": {"pending": 1}},
            "items": [{"record_id": "rec-a", "status": "pending"}],
            "target_rows": [{"record_id": "rec-a", "source_url": "https://example.com/a"}],
        },
    )
    monkeypatch.setattr(
        module,
        "run_feishu_single_row_update",
        lambda params: {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {
                "record_id": str(params["record_id"]),
                "status": "updated",
                "fields": {
                    "图片": {
                        "type": "local_file",
                        "path": str(image_path),
                        "file_name": "main-image.png",
                        "mime_type": "image/png",
                    },
                    "前台截图": {
                        "type": "local_file",
                        "path": str(product_screenshot_path),
                        "file_name": "product-page.png",
                        "mime_type": "image/png",
                    },
                },
                "logical_fields": {
                    "main_image_local_path": str(image_path),
                    "main_image_file_name": "main-image.png",
                    "main_image_mime_type": "image/png",
                    "product_page_screenshot_local_path": str(product_screenshot_path),
                    "product_page_screenshot_file_name": "product-page.png",
                    "product_page_screenshot_mime_type": "image/png",
                },
                "fastmoss_snapshot": {
                    "detail_page_screenshot_local_path": str(fastmoss_screenshot_path),
                    "detail_page_screenshot_file_name": "fastmoss-page.png",
                    "detail_page_screenshot_mime_type": "image/png",
                },
            },
            "items": [
                {
                    "record_id": str(params["record_id"]),
                    "status": "updated",
                }
            ],
        },
    )

    submitted = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "submit",
                "profile_ref": "main",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "noop",
                "execution_control_db_path": str(db_path),
                "execution_control_artifact_store_provider": "minio",
                "execution_control_artifact_bucket": "phase2-bucket",
                "execution_control_artifact_object_prefix": "phase2/demo",
                "execution_control_sync_referenced_files": True,
            }
        )
    )
    task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    browser_once = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "browser_once",
                "execution_control_db_path": str(db_path),
                "execution_control_artifact_store_provider": "minio",
                "execution_control_artifact_bucket": "phase2-bucket",
                "execution_control_artifact_object_prefix": "phase2/demo",
                "execution_control_sync_referenced_files": True,
            }
        )
    )

    assert browser_once.data["daemon_status"] == "processed"
    assert browser_once.data["request_id"] == submitted.data["request_id"]
    assert browser_once.data["artifact_count"] == 8
    assert browser_once.data["artifact_store_provider"] == "minio"
    assert browser_once.data["artifact_uri_prefix"].startswith("s3://phase2-bucket/phase2/demo/runs/")
    assert len(uploaded_keys) == 8
    assert all(key.startswith("phase2/demo/runs/") for key in uploaded_keys)
    assert any(key.endswith("/referenced/main_image_file/main-image.png") for key in uploaded_keys)
    assert any(key.endswith("/referenced/product_page_screenshot_file/product-page.png") for key in uploaded_keys)
    assert any(key.endswith("/referenced/detail_page_screenshot_file/fastmoss-page.png") for key in uploaded_keys)

    store = module.Phase1RuntimeStore(db_path=str(db_path))
    execution_records = store.list_task_executions(request_id=submitted.data["request_id"])
    assert len(execution_records) == 1
    stored_artifacts = store.list_artifacts(run_id=execution_records[0].run_id)
    assert len(stored_artifacts) == 8
    assert {record.request_id for record in stored_artifacts} == {submitted.data["request_id"]}
    assert {record.execution_id for record in stored_artifacts} == {execution_records[0].execution_id}
    assert all(record.bucket == "phase2-bucket" for record in stored_artifacts)
    assert all(record.object_key.startswith("phase2/demo/runs/") for record in stored_artifacts)
    assert all(record.metadata.get("storage_backend") == "minio" for record in stored_artifacts)


def test_phase1_refresh_task_dedupes_active_browser_leaf(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_tiktok_product_link_cleanup",
            "run_feishu_pending_rows_scan",
        ],
    )
    task = RefreshCurrentCompetitorTableTask()
    db_path = tmp_path / "phase1_dedupe.sqlite3"

    monkeypatch.setattr(
        module,
        "run_tiktok_product_link_cleanup",
        lambda params: {"summary": {"total": 1, "counts": {"keep": 1}}, "items": [{"record_id": "rec-a"}]},
    )
    monkeypatch.setattr(
        module,
        "run_feishu_pending_rows_scan",
        lambda params: {
            "summary": {"total": 1, "counts": {"pending": 1}},
            "items": [{"record_id": "rec-a", "status": "pending"}],
            "target_rows": [{"record_id": "rec-a", "source_url": "https://example.com/a"}],
        },
    )

    task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "submit",
                "profile_ref": "shared",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "noop",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    first_executor = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert first_executor.data["request_status"] == "waiting_children"

    second_submit = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "submit",
                "profile_ref": "shared",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "noop",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    second_executor = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert second_executor.data["request_id"] == second_submit.data["request_id"]
    assert second_executor.data["request_status"] == "success"
    assert second_executor.data["summary"]["total"] == 1
    assert second_executor.data["summary"]["counts"] == {"deduped_active": 1}
    assert len(second_executor.data["executions"]) == 0


def test_phase1_refresh_task_creates_incremental_product_snapshots(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_tiktok_product_link_cleanup",
            "run_feishu_pending_rows_scan",
            "run_feishu_single_row_update",
            "Phase1RuntimeStore",
        ],
    )
    task = RefreshCurrentCompetitorTableTask()
    db_path = tmp_path / "phase1_entity.sqlite3"
    versions = iter(
        [
            {
                "fields": {"Fastmoss价格": "10.19", "记录日期": 1776009600000},
                "logical_fields": {
                    "product_id": "1731098351299629802",
                    "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "title": "Product A",
                    "price_amount": "9.99",
                },
                "fastmoss_snapshot": {
                    "product_id": "1731098351299629802",
                    "fastmoss_price_amount": "10.19",
                    "sales_7d": "499",
                },
            },
            {
                "fields": {"Fastmoss价格": "12.49", "记录日期": 1776096000000},
                "logical_fields": {
                    "product_id": "1731098351299629802",
                    "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "title": "Product A Updated",
                    "price_amount": "11.99",
                },
                "fastmoss_snapshot": {
                    "product_id": "1731098351299629802",
                    "fastmoss_price_amount": "12.49",
                    "sales_7d": "520",
                },
            },
        ]
    )

    monkeypatch.setattr(
        module,
        "run_tiktok_product_link_cleanup",
        lambda params: {"summary": {"total": 1, "counts": {"keep": 1}}, "items": [{"record_id": "rec-a"}]},
    )
    monkeypatch.setattr(
        module,
        "run_feishu_pending_rows_scan",
        lambda params: {
            "summary": {"total": 1, "counts": {"pending": 1}},
            "items": [{"record_id": "rec-a", "status": "pending"}],
            "target_rows": [
                {
                    "record_id": "rec-a",
                    "source_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
                    "sku_id": "1731098351299629802",
                }
            ],
        },
    )

    def fake_single_row_update(params):
        version = next(versions)
        item = {
            "record_id": "rec-a",
            "status": "updated",
            "source_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
            "normalized_url": "https://www.tiktok.com/shop/pdp/1731098351299629802",
            "product_id": "1731098351299629802",
            **version,
        }
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": item,
            "items": [item],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_single_row_update)

    for _ in range(2):
        submitted = task.execute_workflow_step(
            _refresh_context(
                {
                    "control_action": "submit",
                    "profile_ref": "main",
                    "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                    "access_token": "token-demo",
                    "notification_channel_code": "noop",
                    "execution_control_db_path": str(db_path),
                }
            )
        )
        task.execute_workflow_step(
            _refresh_context(
                {
                    "control_action": "executor_once",
                    "execution_control_db_path": str(db_path),
                }
            )
        )
        task.execute_workflow_step(
            _refresh_context(
                {
                    "control_action": "browser_loop",
                    "execution_control_db_path": str(db_path),
                    "execution_control_stop_when_idle": True,
                    "execution_control_max_idle_cycles": 1,
                }
            )
        )
        task.execute_workflow_step(
            _refresh_context(
                {
                    "control_action": "executor_once",
                    "execution_control_db_path": str(db_path),
                }
            )
        )
        final_result = task.execute_workflow_step(
            _refresh_context(
                {
                    "control_action": "result",
                    "request_id": submitted.data["request_id"],
                    "execution_control_db_path": str(db_path),
                }
            )
        )
        assert final_result.data["request_status"] == "success"

    store = module.Phase1RuntimeStore(db_path=str(db_path))
    fact_store = TKFactStore(runtime_store=store)
    product = fact_store.get_product(product_id="1731098351299629802")
    assert product["title"] == "Product A Updated"
    assert product["facts"]["fields"]["Fastmoss价格"] == "12.49"
    assert product["facts"]["logical_fields"]["title"] == "Product A Updated"
    assert "entity_snapshot" not in fact_store.table_names()


def test_phase1_outbox_dispatches_via_openclaw_message(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=[
            "run_tiktok_product_link_cleanup",
            "run_feishu_pending_rows_scan",
            "run_feishu_single_row_update",
        ],
    )
    task = RefreshCurrentCompetitorTableTask()
    db_path = tmp_path / "phase1_outbox.sqlite3"
    dispatched_commands: list[list[str]] = []

    monkeypatch.setattr(
        module,
        "run_tiktok_product_link_cleanup",
        lambda params: {"summary": {"total": 1, "counts": {"keep": 1}}, "items": [{"record_id": "rec-a"}]},
    )
    monkeypatch.setattr(
        module,
        "run_feishu_pending_rows_scan",
        lambda params: {
            "summary": {"total": 1, "counts": {"pending": 1}},
            "items": [{"record_id": "rec-a", "status": "pending"}],
            "target_rows": [{"record_id": "rec-a", "source_url": "https://example.com/a"}],
        },
    )
    monkeypatch.setattr(
        module,
        "run_feishu_single_row_update",
        lambda params: {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": str(params["record_id"]), "status": "updated"},
            "items": [{"record_id": str(params["record_id"]), "status": "updated"}],
        },
    )
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/local/bin/openclaw")

    def fake_subprocess_run(command, capture_output, text, check, timeout):
        assert capture_output is True
        assert text is True
        assert check is False
        assert timeout >= 1
        dispatched_commands.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "messageId": "msg-001"}, ensure_ascii=False),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    reply_target = json.dumps(
        {
            "channel": "feishu",
            "to": "user:ou_test_user",
            "accountId": "default",
            "sessionId": "session-123",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    submitted = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "submit",
                "profile_ref": "main",
                "table_url": "https://example.feishu.cn/base/appXXX?table=tblXXX",
                "access_token": "token-demo",
                "notification_channel_code": "openclaw_message",
                "reply_target": reply_target,
                "source_session_id": "session-123",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "browser_loop",
                "execution_control_db_path": str(db_path),
                "execution_control_stop_when_idle": True,
                "execution_control_max_idle_cycles": 1,
            }
        )
    )
    task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "executor_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    outbox_once = task.execute_workflow_step(
        _refresh_context(
            {
                "control_action": "outbox_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert outbox_once.data["dispatcher_status"] == "processed"
    assert outbox_once.data["summary"]["counts"] == {"sent": 1}
    assert outbox_once.data["channel_code"] == "openclaw_message"
    assert len(dispatched_commands) == 1
    assert dispatched_commands[0] == [
        "/usr/local/bin/openclaw",
        "message",
        "send",
        "--channel",
        "feishu",
        "--target",
        "user:ou_test_user",
        "--message",
        f"任务 {submitted.data['request_id']} 已完成；目标 1 条；success=1",
        "--json",
        "--account",
        "default",
    ]


def test_phase1_outbox_dispatches_via_feishu_bot_api(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=["dispatch_phase1_outbox_once", "Phase1RuntimeStore"],
    )
    db_path = tmp_path / "phase1_feishu_outbox.sqlite3"
    store = module.Phase1RuntimeStore(db_path=str(db_path))
    dispatched_messages: list[dict[str, str]] = []

    monkeypatch.setattr(
        module,
        "_dispatch_via_feishu_bot_api",
        lambda *, message_text, reply_target: (
            dispatched_messages.append({"message_text": message_text, "reply_target": reply_target}) or {"code": 0}
        ),
    )

    reply_target = json.dumps(
        {
            "channel": "feishu",
            "to": "user:ou_test_user",
            "accountId": "default",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    outbox_record = store.create_notification_outbox(
        channel_code="feishu_bot_api",
        event_type="task_request.completed",
        ref_id="request-feishu",
        reply_target=reply_target,
        payload={"message_text": "阶段一汇总测试"},
        dedupe_key="request-feishu:summary",
    )

    payload = module.dispatch_phase1_outbox_once({"execution_control_db_path": str(db_path)})

    assert payload["dispatcher_status"] == "processed"
    assert payload["summary"]["counts"] == {"sent": 1}
    assert payload["outbox_id"] == outbox_record.outbox_id
    assert dispatched_messages == [
        {
            "message_text": "阶段一汇总测试",
            "reply_target": reply_target,
        }
    ]


def test_parse_reply_target_accepts_python_dict_repr():
    module = __import__(
        "automation_business_scaffold.flows.refresh_current_competitor_table_flow",
        fromlist=["_parse_reply_target"],
    )

    payload = module._parse_reply_target(
        "{'channel': 'feishu', 'to': 'user:ou_test_user', 'accountId': 'default'}"
    )

    assert payload == {
        "channel": "feishu",
        "to": "user:ou_test_user",
        "accountId": "default",
    }


def test_controlled_task_execute_next_runs_requests_in_queue_order(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control.sqlite3"
    executed_record_ids: list[str] = []

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        executed_record_ids.append(record_id)
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    first_submit = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-first",
                "profile_ref": "shared",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    second_submit = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-second",
                "profile_ref": "shared",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    queued_status = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "status",
                "request_id": second_submit.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert queued_status.data["queue_position"] == 2

    first_run = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "execute_next",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert first_run.data["request_id"] == first_submit.data["request_id"]
    assert first_run.data["execution_status"] == "success"

    second_status = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "status",
                "request_id": second_submit.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert second_status.data["execution_status"] == "queued"
    assert second_status.data["queue_position"] == 1

    second_run = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "execute_next",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    assert second_run.data["request_id"] == second_submit.data["request_id"]
    assert second_run.data["execution_status"] == "success"
    assert executed_record_ids == ["rec-first", "rec-second"]


def test_controlled_task_daemon_once_reports_processed_execution(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control.sqlite3"

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    submitted = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-daemon-once",
                "profile_ref": "shared",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    daemon_once = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "daemon_once",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert daemon_once.data["control_action"] == "daemon_once"
    assert daemon_once.data["daemon_status"] == "processed"
    assert daemon_once.data["processed_count"] == 1
    assert daemon_once.data["request_id"] == submitted.data["request_id"]
    assert daemon_once.data["execution_status"] == "success"
    assert daemon_once.data["last_execution"]["request_id"] == submitted.data["request_id"]
    assert daemon_once.data["artifact_count"] == 5
    assert daemon_once.data["run_object_key"].endswith("/run.json")
    assert daemon_once.data["stdout_object_key"].endswith("/stdout.log")
    assert all(Path(item["source_path"]).exists() for item in daemon_once.data["artifacts"])


def test_controlled_task_daemon_loop_drains_queue_until_idle(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control.sqlite3"
    executed_record_ids: list[str] = []

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        executed_record_ids.append(record_id)
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    first_submit = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-loop-first",
                "profile_ref": "shared",
                "execution_control_db_path": str(db_path),
            }
        )
    )
    second_submit = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-loop-second",
                "profile_ref": "shared",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    daemon_loop = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "daemon_loop",
                "execution_control_db_path": str(db_path),
                "execution_control_stop_when_idle": True,
                "execution_control_max_idle_cycles": 1,
            }
        )
    )

    assert daemon_loop.data["control_action"] == "daemon_loop"
    assert daemon_loop.data["daemon_status"] == "completed"
    assert daemon_loop.data["processed_count"] == 2
    assert daemon_loop.data["success_count"] == 2
    assert daemon_loop.data["failed_count"] == 0
    assert executed_record_ids == ["rec-loop-first", "rec-loop-second"]
    assert daemon_loop.data["last_execution"]["request_id"] == second_submit.data["request_id"]

    first_status = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "status",
                "request_id": first_submit.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )
    second_status = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "status",
                "request_id": second_submit.data["request_id"],
                "execution_control_db_path": str(db_path),
            }
        )
    )

    assert first_status.data["execution_status"] == "success"
    assert second_status.data["execution_status"] == "success"


def test_controlled_task_run_executes_and_returns_result(monkeypatch, tmp_path):
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control.sqlite3"

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    run_result = task.execute_workflow_step(
        _controlled_context(
            {
                "record_id": "rec-direct",
                "profile_ref": "pilot",
                "execution_control_db_path": str(db_path),
                "execution_poll_interval_seconds": 0.01,
                "execution_wait_timeout_seconds": 2,
            }
        )
    )

    assert run_result.data["control_action"] == "run"
    assert run_result.data["execution_status"] == "success"
    assert run_result.data["summary"]["counts"] == {"updated": 1}
    assert run_result.data["result"]["item"]["record_id"] == "rec-direct"
    assert run_result.data["artifact_count"] == 5
    assert run_result.data["artifact_uri_prefix"].startswith("file://")
    assert run_result.data["run_object_key"].endswith("/run.json")
    assert run_result.data["steps_object_key"].endswith("/steps.json")
    assert run_result.data["signals_object_key"].endswith("/signals.json")
    assert run_result.data["stdout_object_key"].endswith("/stdout.log")
    assert {item["kind"] for item in run_result.data["artifacts"]} == {
        "run_json",
        "signals_json",
        "state_json",
        "steps_json",
        "stdout_log",
    }
    assert all(Path(item["source_path"]).exists() for item in run_result.data["artifacts"])


def test_controlled_task_supports_sqlalchemy_db_url(monkeypatch, tmp_path):
    pytest.importorskip("sqlalchemy")
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    task = FeishuSingleRowUpdateTask()
    db_url = f"sqlite:///{(tmp_path / 'control_sqlalchemy.sqlite3').resolve()}"

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    submitted = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-sqlalchemy-url",
                "profile_ref": "main",
                "execution_control_db_url": db_url,
            }
        )
    )

    daemon_once = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "daemon_once",
                "execution_control_db_url": db_url,
            }
        )
    )

    result_payload = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "result",
                "request_id": submitted.data["request_id"],
                "execution_control_db_url": db_url,
            }
        )
    )

    assert daemon_once.data["daemon_status"] == "processed"
    assert daemon_once.data["request_id"] == submitted.data["request_id"]
    assert result_payload.data["execution_status"] == "success"
    assert result_payload.data["artifact_count"] == 5
    assert result_payload.data["result"]["item"]["record_id"] == "rec-sqlalchemy-url"


def test_executor_daemon_cli_processes_one_request(monkeypatch, tmp_path, capsys):
    module = __import__(
        "automation_business_scaffold.flows.execution_control_flow",
        fromlist=["run_feishu_single_row_update"],
    )
    from automation_business_scaffold.executor_daemon import main as executor_main

    task = FeishuSingleRowUpdateTask()
    db_path = tmp_path / "control_executor.sqlite3"

    def fake_run_feishu_single_row_update(params):
        record_id = str(params["record_id"])
        return {
            "summary": {"total": 1, "counts": {"updated": 1}},
            "item": {"record_id": record_id, "status": "updated"},
            "items": [{"record_id": record_id, "status": "updated"}],
        }

    monkeypatch.setattr(module, "run_feishu_single_row_update", fake_run_feishu_single_row_update)

    submitted = task.execute_workflow_step(
        _controlled_context(
            {
                "control_action": "submit",
                "record_id": "rec-cli-daemon",
                "profile_ref": "main",
                "execution_control_db_path": str(db_path),
            }
        )
    )

    exit_code = executor_main(["--once", "--db-path", str(db_path)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["daemon_status"] == "processed"
    assert payload["request_id"] == submitted.data["request_id"]
    assert payload["execution_status"] == "success"
