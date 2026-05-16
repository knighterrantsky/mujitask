from __future__ import annotations

from typing import Any

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "ready_for_summary"

def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    fallback_candidates = _fastmoss_browser_fallback_candidates(store=store, request_id=request.request_id)
    if fallback_candidates:
        return _advance_stage_result(
            next_stage=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
            details={"fallback_candidate_count": len(fallback_candidates), "summary_blocked": True},
        )
    active_stage = _active_child_stage(store=store, request_id=request.request_id)
    if active_stage:
        return _waiting_stage_result(
            current_stage=active_stage,
            message="Summary is blocked until all child jobs are terminal.",
            details={"summary_blocked": True, "active_stage": active_stage},
        )
    return {"action": "finalize"}


def _active_child_stage(*, store: Any, request_id: str) -> str:
    list_job_summaries = getattr(store, "list_api_worker_job_summaries_for_request", None)
    api_jobs = (
        list_job_summaries(request_id=request_id)
        if callable(list_job_summaries)
        else store.list_api_worker_jobs_for_request(request_id=request_id)
    )
    for job in api_jobs:
        if str(job.get("status") or "") in ACTIVE_STATUSES:
            return _job_stage_code(job) or str(job.get("progress_stage") or "")

    list_execution_summaries = getattr(store, "list_task_execution_summaries_for_request", None)
    executions = (
        list_execution_summaries(request_id=request_id)
        if callable(list_execution_summaries)
        else [execution.to_dict() for execution in store.list_task_executions(request_id=request_id)]
    )
    for execution in executions:
        if str(execution.get("status") or "") in ACTIVE_STATUSES:
            payload = coerce_mapping(execution.get("payload"))
            return str(payload.get("stage_code") or execution.get("progress_stage") or "")
    return ""
