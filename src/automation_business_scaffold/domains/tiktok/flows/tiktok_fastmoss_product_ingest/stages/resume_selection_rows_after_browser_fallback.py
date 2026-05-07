from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "resume_selection_rows_after_browser_fallback"

def _advance_resume_selection_rows_after_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    candidates = _selection_row_browser_resume_candidates(
        store=store,
        request_id=request.request_id,
    )
    if candidates:
        row_job_def = workflow.require_job("selection_row_refresh")
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=row_job_def.job_code,
            jobs=[
                _selection_row_resume_job(
                    request=request,
                    workflow=workflow,
                    stage_code=stage_code,
                    row_job_def=row_job_def,
                    candidate=candidate,
                )
                for candidate in candidates
            ],
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "resumable_count": len(candidates),
                "existing_job_count": len(jobs),
                "row_dispatch": dispatch,
            },
        )
        if int(dispatch["created_count"]) > 0:
            return {
                "action": "waiting",
                "current_stage": stage_code,
                "message": "Enqueued missing selection row refresh retries after browser fallback.",
                "details": {
                    "created_count": int(dispatch["created_count"]),
                    "resumable_count": len(candidates),
                    "existing_job_count": len(jobs),
                },
            }
    elif not jobs:
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"resumable_count": 0},
        }

    jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if _any_api_jobs_active(jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Waiting for selection row refresh retries after browser fallback to finish.",
        }
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"resumed_job_count": len(jobs)},
    )
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"resumed_job_count": len(jobs)}}


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_resume_selection_rows_after_browser_fallback(store=store, request=request, workflow=workflow, stage_code=STAGE_CODE)
