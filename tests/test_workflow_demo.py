from pathlib import Path

from fastapi.testclient import TestClient
from automation_framework.agent.server import create_app
from automation_framework.runtime import RunRegistry

from automation_business_scaffold.cli import list_registered_tasks, run_registered_task
from automation_business_scaffold.registry import build_task_registry
from automation_business_scaffold.tasks import SourceToTargetPublishDemoTask


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
                "Log into FastMoss if needed, search a product_id, open the detail page, and collect "
                "yesterday/7d/28d/90d sales metrics."
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
            "name": "source_to_target_publish_demo",
            "description": "Demo workflow showing extract -> map -> fill -> draft/submit on top of automation-framework.",
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
