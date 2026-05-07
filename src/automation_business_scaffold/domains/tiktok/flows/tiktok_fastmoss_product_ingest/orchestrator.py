from __future__ import annotations

from importlib import import_module
from typing import Any

from automation_business_scaffold.control_plane.runtime_config.settings import PRODUCT_INGEST_TASK_CODE

from .context import *  # noqa: F403

_STAGE_MODULES = {
    "read_selection_rows": "read_selection_rows",
    "dispatch_selection_row_refresh": "dispatch_selection_row_refresh",
    "collect_selection_rows": "collect_selection_rows",
    "selection_row_browser_fallback": "selection_row_browser_fallback",
    "resume_selection_rows_after_browser_fallback": "resume_selection_rows_after_browser_fallback",
}


def advance_stage(*, store: Any, request: Any, workflow: Any, stage_code: str) -> dict[str, Any]:
    if request.task_code != PRODUCT_INGEST_TASK_CODE:
        raise ValueError(f"Unsupported task_code for product ingest runtime: {request.task_code}")
    stage = workflow.require_stage(stage_code)
    module_name = _STAGE_MODULES.get(stage.stage_code)
    if stage.stage_code == workflow.summary_policy.summary_stage_code:
        module_name = "ready_for_summary"
    if not module_name:
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": f"Unsupported ingest stage {stage_code}."},
            "summary": {"total": 0, "counts": {"unsupported_stage": 1}},
            "details": {"unsupported_stage": stage_code},
        }
    stage_module = import_module(f"{__package__}.stages.{module_name}")
    return stage_module.advance(store=store, request=request, workflow=workflow)


def finalize_request(*, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None) -> dict[str, Any]:
    from .summary import finalize_request as _finalize_request

    return _finalize_request(store=store, request=request, workflow=workflow, force_result=force_result)

def release_request_after_child_completion(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != PRODUCT_INGEST_TASK_CODE:
        return []
    workflow = get_workflow_definition(request.task_code)
    current_stage = str(request.current_stage or "").strip()
    if not current_stage:
        return []
    if (
        current_stage == workflow.summary_policy.summary_stage_code
        and _selection_row_browser_resume_candidates(store=store, request_id=request_id)
        and not _api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        )
    ):
        next_stage = "resume_selection_rows_after_browser_fallback"
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage=next_stage,
            progress_stage=next_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            last_progress_at=time.time(),
        )
        _refresh_request_aggregate_counts(store, request_id=request_id)
        return [
            {
                "request_id": request_id,
                "stage_code": next_stage,
                "released": True,
                "next_executor_status": "pending",
            }
        ]
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    child_records = _stage_child_records(store, request_id=request_id, stage_code=current_stage)
    if not child_records:
        return []
    if _has_active_children(child_records):
        return []
    next_stage = current_stage
    if current_stage == "selection_row_browser_fallback" and _selection_row_browser_resume_candidates(
        store=store,
        request_id=request_id,
    ):
        next_stage = "resume_selection_rows_after_browser_fallback"

    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=next_stage,
        progress_stage=next_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        last_progress_at=time.time(),
    )
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return [
        {
            "request_id": request_id,
            "stage_code": next_stage,
            "released": True,
            "next_executor_status": "pending",
        }
    ]
