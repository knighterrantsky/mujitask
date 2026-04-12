from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_openclaw_result_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "openclaw_result.py"
    )
    spec = importlib.util.spec_from_file_location("openclaw_result", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_run_summary_reads_single_step_summary_when_top_level_summary_is_missing(tmp_path):
    module = _load_openclaw_result_module()
    run_file = tmp_path / "run.json"
    run_file.write_text(
        json.dumps(
            {
                "status": "success",
                "task_name": "feishu_pending_rows_scan",
                "run_id": "run-123",
                "result": {
                    "message": "Workflow feishu_pending_rows_scan_v1 completed.",
                    "data": {
                        "workflow_id": "feishu_pending_rows_scan_v1",
                        "step_outputs": {
                            "scan_pending_rows": {
                                "summary": {
                                    "total": 28,
                                    "counts": {
                                        "skipped_completed": 21,
                                        "pending": 7,
                                    },
                                },
                                "items": [{"record_id": "rec-1"}],
                                "target_rows": [{"record_id": "rec-1"}],
                            }
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    args = type(
        "Args",
        (),
        {
            "run_file": str(run_file),
            "steps_file": "",
            "signals_file": "",
            "stdout_file": "",
            "run_id": "run-123",
            "fallback_task": "feishu_pending_rows_scan",
            "status": "success",
            "error_message": "",
        },
    )()

    payload = module.build_run_summary(args)

    assert payload["summary"] == {
        "total": 28,
        "counts": {
            "skipped_completed": 21,
            "pending": 7,
        },
    }
    assert payload["summary_text"] == "pending=7, skipped_completed=21, total=28"


def test_build_run_summary_prefers_emit_summary_when_present(tmp_path):
    module = _load_openclaw_result_module()
    run_file = tmp_path / "run.json"
    run_file.write_text(
        json.dumps(
            {
                "status": "success",
                "task_name": "demo_task",
                "run_id": "run-456",
                "result": {
                    "message": "Workflow demo completed.",
                    "data": {
                        "summary": {"total": 0, "counts": {}},
                        "step_outputs": {
                            "emit_summary": {
                                "summary": {
                                    "total": 2,
                                    "counts": {"updated": 2},
                                },
                                "failed_items": [{"id": "x"}],
                            },
                            "other_step": {
                                "summary": {
                                    "total": 99,
                                    "counts": {"wrong": 99},
                                }
                            },
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    args = type(
        "Args",
        (),
        {
            "run_file": str(run_file),
            "steps_file": "",
            "signals_file": "",
            "stdout_file": "",
            "run_id": "run-456",
            "fallback_task": "demo_task",
            "status": "success",
            "error_message": "",
        },
    )()

    payload = module.build_run_summary(args)

    assert payload["summary"] == {
        "total": 2,
        "counts": {"updated": 2},
    }
    assert payload["failed_item_count"] == 1


def test_build_run_summary_exposes_control_and_artifact_fields_from_single_step_output(tmp_path):
    module = _load_openclaw_result_module()
    run_file = tmp_path / "run.json"
    run_file.write_text(
        json.dumps(
            {
                "status": "success",
                "task_name": "feishu_single_row_update",
                "run_id": "run-controlled-1",
                "result": {
                    "message": "Controlled execution finished.",
                    "data": {
                        "workflow_id": "feishu_single_row_update_v1",
                        "step_outputs": {
                            "update_single_row": {
                                "control_action": "daemon_once",
                                "request_id": "req-123",
                                "execution_id": "exec-123",
                                "request_status": "success",
                                "execution_status": "success",
                                "resource_code": "browser.tiktok.main",
                                "queue_position": 0,
                                "daemon_status": "processed",
                                "processed_count": 1,
                                "success_count": 1,
                                "failed_count": 0,
                                "artifact_count": 5,
                                "artifact_uri_prefix": "file:///tmp/object_store/runs/managed-exec-123",
                                "run_object_key": "runs/managed-exec-123/run.json",
                                "steps_object_key": "runs/managed-exec-123/steps.json",
                                "signals_object_key": "runs/managed-exec-123/signals.json",
                                "stdout_object_key": "runs/managed-exec-123/stdout.log",
                                "artifacts_dir": "/tmp/object_store/runs/managed-exec-123/artifacts",
                                "worker_id": "worker-1",
                                "artifacts": [
                                    {
                                        "kind": "run_json",
                                        "source_path": "/tmp/object_store/runs/managed-exec-123/run.json",
                                    }
                                ],
                                "summary": {"total": 1, "counts": {"updated": 1}},
                            }
                        },
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    args = type(
        "Args",
        (),
        {
            "run_file": str(run_file),
            "steps_file": "",
            "signals_file": "",
            "stdout_file": "",
            "run_id": "run-controlled-1",
            "fallback_task": "feishu_single_row_update",
            "status": "success",
            "error_message": "",
        },
    )()

    payload = module.build_run_summary(args)

    assert payload["control_action"] == "daemon_once"
    assert payload["request_id"] == "req-123"
    assert payload["execution_status"] == "success"
    assert payload["daemon_status"] == "processed"
    assert payload["artifact_count"] == 5
    assert payload["run_object_key"] == "runs/managed-exec-123/run.json"
    assert payload["artifacts_dir"] == "/tmp/object_store/runs/managed-exec-123/artifacts"
    assert payload["artifacts"] == [
        {
            "kind": "run_json",
            "source_path": "/tmp/object_store/runs/managed-exec-123/run.json",
        }
    ]
