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
