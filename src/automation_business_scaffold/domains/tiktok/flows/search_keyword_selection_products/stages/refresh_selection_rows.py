from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_api_jobs_active as _any_api_jobs_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.runtime_views import (
    _pending_selection_seed_contexts,
)
from ..context.decision_models import (
    _waiting,
)
from .dispatch_selection_row_refresh_jobs import _dispatch_next_selection_row_refresh_job
from .selection_row_browser_fallback import _selection_row_browser_fallback_candidates


STAGE_CODE = "refresh_selection_rows"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "refresh_selection_rows"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for selection row refresh jobs to finish.")
    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "collect_job_count": len(jobs),
            "fallback_candidate_count": len(fallback_candidates),
        },
    )
    if fallback_candidates:
        workflow.require_stage("selection_row_browser_fallback")
        return {
            "action": "advance",
            "next_stage": "selection_row_browser_fallback",
            "details": {"fallback_candidate_count": len(fallback_candidates)},
        }
    next_seed_contexts = _pending_selection_seed_contexts(store=store, request_id=request.request_id)
    if next_seed_contexts:
        row_dispatch = _dispatch_next_selection_row_refresh_job(
            store=store,
            request=request,
            workflow=workflow,
            seed_contexts=next_seed_contexts,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "collect_job_count": len(jobs),
                "pending_row_count": len(next_seed_contexts),
                "row_dispatch": row_dispatch,
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued next selection row refresh job.",
            details={
                "created_count": int(row_dispatch["created_count"]),
                "pending_row_count": len(next_seed_contexts),
            },
        )
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"collect_job_count": len(jobs)}}
