from __future__ import annotations

from pathlib import Path

from automation_business_scaffold.domains.tiktok.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.apps.cli.main import run_registered_task


DIRECT_PRODUCT_ID = "123"
DIRECT_PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{DIRECT_PRODUCT_ID}"


def _strict_persistence_params(runtime_db_url: str) -> dict[str, object]:
    return {
        "allow_test_persistence_overrides": True,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": "pytest-runtime-artifacts",
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
    }


def _run_task(*, params: dict[str, object], run_dir: Path) -> dict[str, object]:
    payload = run_registered_task(
        "tiktok_fastmoss_product_ingest",
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


def test_tiktok_fastmoss_product_ingest_workflow_builder_uses_full_auto_runtime_shell() -> None:
    task = TikTokFastMossProductIngestTask()

    workflow = task.build_workflow({})

    assert workflow.workflow_id == "tiktok_fastmoss_product_ingest"
    assert workflow.run_mode == "full_auto"
    assert [step.step_id for step in workflow.steps] == ["dispatch_task_request"]
    assert workflow.steps[0].action.type == "dispatch_task_request"


def test_tiktok_fastmoss_product_ingest_submit_via_registered_task_creates_pending_request(
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    payload = _run_task(
        params={
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            **_strict_persistence_params(runtime_db_url),
            "product_url": DIRECT_PRODUCT_URL,
            "product_id": DIRECT_PRODUCT_ID,
            "reply_target": "reply://pytest-product-ingest",
        },
        run_dir=tmp_path / "submit-runs",
    )

    step = _extract_step_output(payload)

    assert step["task_code"] == "tiktok_fastmoss_product_ingest"
    assert step["request_status"] == "pending"
    assert step["current_stage"] == "read_selection_rows"
    assert step["request_id"]
    assert step["summary"] == {"total": 0, "counts": {}}


def test_tiktok_fastmoss_product_ingest_submit_then_executor_once_dispatches_request_first_jobs(
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    submitted = _run_task(
        params={
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            **_strict_persistence_params(runtime_db_url),
            "product_url": DIRECT_PRODUCT_URL,
            "product_id": DIRECT_PRODUCT_ID,
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
    assert step["current_stage"] == "collect_selection_rows"
    job_codes = {job["job_code"] for job in step["api_worker_jobs"]}
    assert job_codes == {"selection_row_refresh"}


def test_tiktok_fastmoss_product_ingest_status_round_trip_returns_current_request_state(
    runtime_db_url: str,
    tmp_path: Path,
) -> None:
    submitted = _run_task(
        params={
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            **_strict_persistence_params(runtime_db_url),
            "product_url": DIRECT_PRODUCT_URL,
            "product_id": DIRECT_PRODUCT_ID,
        },
        run_dir=tmp_path / "submit-runs",
    )
    request_id = str(_extract_step_output(submitted)["request_id"])

    status_payload = _run_task(
        params={
            "control_action": "status",
            "execution_control_db_url": runtime_db_url,
            "request_id": request_id,
        },
        run_dir=tmp_path / "status-runs",
    )
    step = _extract_step_output(status_payload)

    assert step["request_id"] == request_id
    assert step["request_status"] == "pending"
    assert step["current_stage"] == "read_selection_rows"
