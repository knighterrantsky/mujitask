from __future__ import annotations

from pathlib import Path

from automation_business_scaffold.apps.cli.main import run_registered_task
from automation_business_scaffold.domains.tiktok.tasks.refresh_competitor_row_by_url import (
    RefreshCompetitorRowByUrlTask,
)


PRODUCT_ID = "123456789"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"
SOURCE_TABLE_REF = "tbl_competitor_source"


def _run_task(*, params: dict[str, object], run_dir: Path) -> dict[str, object]:
    payload = run_registered_task(
        "refresh_competitor_row_by_url",
        params=params,
        run_dir=run_dir,
    )
    assert payload["status"] == "success", payload.get("error", "")
    return payload


def _extract_step_output(payload: dict[str, object]) -> dict[str, object]:
    result = payload["result"]
    assert isinstance(result, dict)
    data = result["data"]
    assert isinstance(data, dict)
    step_outputs = data["step_outputs"]
    assert isinstance(step_outputs, dict)
    step_output = step_outputs["dispatch_task_request"]
    assert isinstance(step_output, dict)
    return step_output


def test_refresh_competitor_row_by_url_workflow_builder_uses_full_auto_runtime_shell() -> None:
    task = RefreshCompetitorRowByUrlTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "refresh_competitor_row_by_url"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]
    assert workflow.steps[0].action.type == "dispatch_task_request"


def test_refresh_competitor_row_by_url_submit_via_registered_task_creates_pending_request(
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    payload = _run_task(
        params={
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            "source_table_ref": SOURCE_TABLE_REF,
            "product_url": PRODUCT_URL,
            "reply_target": "reply://pytest-competitor-row",
        },
        run_dir=tmp_path / "submit-runs",
    )

    step = _extract_step_output(payload)

    assert step["task_code"] == "refresh_competitor_row_by_url"
    assert step["request_status"] == "pending"
    assert step["current_stage"] == "read_competitor_rows"
    assert step["request_id"]


def test_refresh_competitor_row_by_url_submit_then_executor_once_dispatches_lookup_read(
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    submitted = _run_task(
        params={
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            "source_table_ref": SOURCE_TABLE_REF,
            "product_url": PRODUCT_URL,
            "fallback_allowed": True,
        },
        run_dir=tmp_path / "submit-runs",
    )
    request_id = str(_extract_step_output(submitted)["request_id"])

    dispatched = _run_task(
        params={
            "control_action": "executor_once",
            "execution_control_db_url": runtime_db_url,
        },
        run_dir=tmp_path / "executor-runs",
    )
    step = _extract_step_output(dispatched)

    assert step["request_id"] == request_id
    assert step["request_status"] == "waiting_children"
    assert step["current_stage"] == "read_competitor_rows"
    read_jobs = [
        job
        for job in step["api_worker_jobs"]
        if job["job_code"] == "feishu_table_read"
        and str((job.get("payload") or {}).get("stage_code") or "") == "read_competitor_rows"
    ]
    assert len(read_jobs) == 1
    assert read_jobs[0]["payload"]["product_url"] == PRODUCT_URL
    assert read_jobs[0]["payload"]["filter_spec"] == {}
