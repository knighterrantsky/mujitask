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
from .dispatch_product_collection import enqueue_next_competitor_row_refresh

STAGE_CODE = "collect_product_data"

def _advance_collect_product_data(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "collect_product_data"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
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
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row refresh jobs to finish.")

    row_dispatch = enqueue_next_competitor_row_refresh(
        store=store,
        request=request,
        workflow=workflow,
    )
    if int(row_dispatch.get("created_count") or 0) > 0 or bool(row_dispatch.get("already_active")):
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "collect_job_count": len(jobs),
                "pending_row_count": int(row_dispatch.get("pending_row_count") or 0),
                "row_dispatch": row_dispatch,
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Waiting for next competitor row refresh job to finish.",
            details={
                "created_count": int(row_dispatch.get("created_count") or 0),
                "pending_row_count": int(row_dispatch.get("pending_row_count") or 0),
            },
        )
    return {
        "action": "advance",
        "next_stage": "ready_for_summary",
        "details": {"collect_job_count": len(jobs)},
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_collect_product_data(store=store, request=request, workflow=workflow)
