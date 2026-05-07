from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "resume_competitor_rows_after_browser_fallback"

def _advance_resume_competitor_rows_after_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "resume_competitor_rows_after_browser_fallback"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _browser_resume_candidates(store=store, request_id=request.request_id)
        if not candidates:
            return {
                "action": "advance",
                "next_stage": "ready_for_summary",
                "details": {"resumable_count": 0},
            }
        row_job_def = workflow.require_job("competitor_row_refresh")
        row_jobs: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = _resume_row_payload(stage_code=stage_code, candidate=candidate)
            keys = render_job_keys(
                row_job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=row_job_def.job_code,
            )
            row_jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        f"{keys['dedupe_key']}:after-browser-fallback",
                        row_job_def.job_code,
                    ),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=row_job_def.job_code,
            jobs=row_jobs,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "resumable_count": len(candidates),
                "row_dispatch": dispatch,
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued competitor row refresh retries after browser fallback.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(
            stage_code=stage_code,
            message="Waiting for competitor row refresh retries after browser fallback to finish.",
        )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"resumed_job_count": len(jobs)},
    )
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"resumed_job_count": len(jobs)}}


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_resume_competitor_rows_after_browser_fallback(store=store, request=request, workflow=workflow)
