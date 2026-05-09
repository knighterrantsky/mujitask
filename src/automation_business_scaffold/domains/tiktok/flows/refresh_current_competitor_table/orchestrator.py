from __future__ import annotations

import time
from importlib import import_module
from typing import Any

from .context.models import *  # noqa: F403
from .context.runtime_views import *  # noqa: F403
from .context.stage_inputs import *  # noqa: F403
from .context.decision_models import *  # noqa: F403
from .context.summary_inputs import *  # noqa: F403

_STAGE_MODULES = {
    "read_competitor_rows": "read_competitor_rows",
    "dispatch_product_collection": "dispatch_product_collection",
    "collect_product_data": "collect_product_data",
    "browser_fallback": "browser_fallback",
    "resume_competitor_rows_after_browser_fallback": "resume_competitor_rows_after_browser_fallback",
}


def advance_stage(*, store: Any, request: Any, workflow: Any, stage_code: str) -> dict[str, Any]:
    if request.task_code not in SUPPORTED_REFRESH_TASK_CODES:
        raise ValueError(f"Unsupported task_code for refresh runtime: {request.task_code}")
    module_name = _STAGE_MODULES.get(stage_code)
    if stage_code == workflow.summary_policy.summary_stage_code:
        module_name = "ready_for_summary"
    if not module_name:
        raise KeyError(f"Unsupported stage_code for refresh runtime: {stage_code}")
    stage_module = import_module(f"{__package__}.stages.{module_name}")
    return stage_module.advance(store=store, request=request, workflow=workflow)


def finalize_request(*, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None) -> dict[str, Any]:
    from .summary import finalize_request as _finalize_request

    return _finalize_request(store=store, request=request, workflow=workflow, force_result=force_result)


def _require_refresh_workflow(task_code: str) -> WorkflowDefinition:
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

    workflow = get_workflow_definition(task_code)
    if workflow.workflow_code not in SUPPORTED_REFRESH_TASK_CODES:
        raise ValueError(f"Expected refresh workflow definition, got {workflow.workflow_code}")
    return workflow

def release_request_after_child_completion(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code not in SUPPORTED_REFRESH_TASK_CODES:
        return []
    workflow = _require_refresh_workflow(request.task_code)
    current_stage = str(request.current_stage or "").strip()
    if not current_stage:
        return []
    resumed_stage = _resume_stage_from_premature_summary(
        store=store,
        request=request,
        workflow=workflow,
        current_stage=current_stage,
    )
    if resumed_stage:
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage=resumed_stage,
            progress_stage=resumed_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            last_progress_at=time.time(),
        )
        return [
            {
                "request_id": request_id,
                "stage_code": resumed_stage,
                "released": True,
                "next_executor_status": "pending",
            }
        ]
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    child_records = _stage_child_records(store=store, request_id=request_id, stage_code=current_stage)
    if not child_records:
        return []
    if _has_active_children(child_records):
        return []

    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=current_stage,
        progress_stage=current_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        last_progress_at=time.time(),
    )
    return [
        {
            "request_id": request_id,
            "stage_code": current_stage,
            "released": True,
            "next_executor_status": "pending",
        }
    ]
