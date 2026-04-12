import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from automation_framework.agent.server import create_app
from automation_framework.runtime import RunRegistry

from automation_business_scaffold.cli import list_registered_tasks, run_registered_task
from automation_business_scaffold.registry import build_task_registry
from automation_business_scaffold.tasks import (
    FeishuSingleRowUpdateTask,
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
