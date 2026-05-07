from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "collect_selection_rows"

def _advance_collect_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "No selection row refresh jobs found."},
            "summary": {"total": 0, "counts": {"no_row_jobs": 1}},
        }

    if _any_api_jobs_active(stage_jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Selection row refresh jobs are still running.",
        }

    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "collect_job_count": len(stage_jobs),
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

    return {"action": "advance", "next_stage": "ready_for_summary"}


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_collect_selection_rows(store=store, request=request, workflow=workflow, stage_code=STAGE_CODE)
