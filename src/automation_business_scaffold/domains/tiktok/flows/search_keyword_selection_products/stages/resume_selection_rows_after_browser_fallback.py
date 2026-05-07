from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_api_jobs_active as _any_api_jobs_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    build_stage_local_dedupe_key,
    render_job_keys,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.runtime_views import (
    _pending_selection_seed_contexts,
)
from ..context.decision_models import (
    _waiting,
)
from .selection_row_browser_fallback import (
    _selection_row_browser_resume_candidates,
    _selection_row_resume_payload,
)


STAGE_CODE = "resume_selection_rows_after_browser_fallback"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "resume_selection_rows_after_browser_fallback"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _selection_row_browser_resume_candidates(
            store=store,
            request_id=request.request_id,
        )
        if not candidates:
            return {
                "action": "advance",
                "next_stage": "ready_for_summary",
                "details": {"resumable_count": 0},
            }
        row_job_def = workflow.require_job("selection_row_refresh")
        row_jobs: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = _selection_row_resume_payload(stage_code=stage_code, candidate=candidate)
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
            message="Enqueued selection row refresh retries after browser fallback.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(
            stage_code=stage_code,
            message="Waiting for selection row refresh retries after browser fallback to finish.",
        )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"resumed_job_count": len(jobs)},
    )
    next_stage = "refresh_selection_rows" if _pending_selection_seed_contexts(
        store=store,
        request_id=request.request_id,
    ) else "ready_for_summary"
    return {"action": "advance", "next_stage": next_stage, "details": {"resumed_job_count": len(jobs)}}
