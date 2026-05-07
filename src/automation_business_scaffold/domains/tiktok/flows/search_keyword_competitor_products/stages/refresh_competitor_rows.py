from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_api_jobs_active as _any_api_jobs_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.decision_models import _waiting
from .browser_fallback import _browser_fallback_candidates


STAGE_CODE = "refresh_competitor_rows"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "refresh_competitor_rows"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        return {"action": "advance", "next_stage": "ready_for_summary", "details": {"dispatched_row_count": 0}}
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row refresh jobs to finish.")
    fallback_candidates = _browser_fallback_candidates(store=store, request_id=request.request_id)
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
        workflow.require_stage("browser_fallback")
        return {
            "action": "advance",
            "next_stage": "browser_fallback",
            "details": {"fallback_candidate_count": len(fallback_candidates)},
        }
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"collect_job_count": len(jobs)}}
