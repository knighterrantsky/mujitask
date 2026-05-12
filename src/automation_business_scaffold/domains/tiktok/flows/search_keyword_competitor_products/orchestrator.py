from __future__ import annotations

import time
from importlib import import_module
from typing import Any

from automation_business_scaffold.control_plane.runtime_config.settings import (
    KEYWORD_TASK_CODE,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    api_jobs_for_stage as _api_jobs_for_stage,
    has_active_records as _has_active_children,
    stage_child_records as _stage_child_records,
)

from .stages.browser_fallback import (
    _browser_fallback_candidates,
)
from .stages.dispatch_row_refresh_jobs import (
    _pending_row_seeds,
    _successful_seed_contexts,
)

_STAGE_MODULES = {
    "keyword_seed_import": "keyword_seed_import",
    "fastmoss_security_browser_fallback": "fastmoss_security_browser_fallback",
    "dispatch_row_refresh_jobs": "dispatch_row_refresh_jobs",
    "refresh_competitor_rows": "refresh_competitor_rows",
    "browser_fallback": "browser_fallback",
}


def advance_stage(
    *,
    store: Any,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    if request.task_code != KEYWORD_TASK_CODE:
        raise ValueError(f"Unsupported task_code for keyword runtime: {request.task_code}")
    module_name = _STAGE_MODULES.get(stage_code)
    if stage_code == workflow.summary_policy.summary_stage_code:
        module_name = "ready_for_summary"
    if not module_name:
        raise KeyError(f"Unsupported stage_code for keyword runtime: {stage_code}")
    stage_module = import_module(f"{__package__}.stages.{module_name}")
    return stage_module.advance(store=store, request=request, workflow=workflow)


def finalize_request(
    *,
    store: Any,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .summary import finalize_request as _finalize_request

    return _finalize_request(
        store=store,
        request=request,
        workflow=workflow,
        force_result=force_result,
    )


def release_request_after_child_completion(
    store: Any,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != KEYWORD_TASK_CODE:
        return []
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

    workflow = get_workflow_definition(KEYWORD_TASK_CODE)
    current_stage = str(request.current_stage or "").strip()
    if current_stage == workflow.summary_policy.summary_stage_code:
        if _browser_fallback_candidates(store=store, request_id=request_id):
            return _release_to_stage(store=store, request_id=request_id, stage_code="browser_fallback")
        if _has_pending_success_seed_rows(store=store, request_id=request_id):
            return _release_to_stage(store=store, request_id=request_id, stage_code="refresh_competitor_rows")
    if not current_stage:
        return []
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    child_records = _stage_child_records(store=store, request_id=request_id, stage_code=current_stage)
    if not child_records or _has_active_children(child_records):
        return []

    return _release_to_stage(store=store, request_id=request_id, stage_code=current_stage)


def _has_pending_success_seed_rows(*, store: Any, request_id: str) -> bool:
    seed_contexts = _successful_seed_contexts(store=store, request_id=request_id)
    row_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="refresh_competitor_rows")
    return bool(_pending_row_seeds(seed_contexts=seed_contexts, row_jobs=row_jobs))


def _release_to_stage(*, store: Any, request_id: str, stage_code: str) -> list[dict[str, Any]]:
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=stage_code,
        progress_stage=stage_code,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        last_progress_at=time.time(),
    )
    return [
        {
            "request_id": request_id,
            "stage_code": stage_code,
            "released": True,
            "next_executor_status": "pending",
        }
    ]
