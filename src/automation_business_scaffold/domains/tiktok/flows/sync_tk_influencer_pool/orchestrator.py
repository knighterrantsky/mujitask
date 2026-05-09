from __future__ import annotations

from importlib import import_module
from typing import Any

from automation_business_scaffold.control_plane.reconciler.views import (
    build_request_child_views,
    summarize_child_status_counts,
)
from .context.models import *  # noqa: F403
from .context.runtime_views import *  # noqa: F403
from .context.stage_inputs import *  # noqa: F403
from .context.decision_models import *  # noqa: F403
from .context.summary_inputs import *  # noqa: F403

_STAGE_MODULES = {
    "read_competitor_candidates": "read_competitor_candidates",
    "dispatch_product_jobs": "dispatch_product_jobs",
    "discover_related_creators": "discover_related_creators",
    "sync_influencer_pool": "sync_influencer_pool",
    "writeback_competitor_status": "writeback_competitor_status",
    "collect_creator_detail": "collect_creator_detail",
    "persist_creator_facts": "persist_creator_facts",
    "write_influencer_pool": "write_influencer_pool",
    "finalize_product": "finalize_product",
}


def advance_stage(*, store: Any, request: Any, workflow: Any, stage_code: str) -> dict[str, Any]:
    del workflow
    module_name = _STAGE_MODULES.get(stage_code)
    if module_name:
        stage_module = import_module(f"{__package__}.stages.{module_name}")
        return stage_module.advance(store=store, request=request, workflow=None)
    if stage_code == SUMMARY_STAGE_CODE:
        stage_module = import_module(f"{__package__}.stages.ready_for_summary")
        return stage_module.advance(store=store, request=request, workflow=None)
    return {
        "action": "finalize",
        "final_status": "failed",
        "summary": {"final_status": "failed", "warnings": [f"unsupported_stage:{stage_code}"]},
        "result": {"message": f"Unsupported sync_tk_influencer_pool stage {stage_code}."},
        "details": {"unsupported_stage": stage_code},
    }


def finalize_request(*, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None) -> dict[str, Any]:
    from .summary import finalize_request as _finalize_request

    return _finalize_request(store=store, request=request, workflow=workflow, force_result=force_result)

def release_request_after_child_completion(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        return []
    current_stage = _current_stage(request)
    if current_stage not in WAITING_STAGES:
        return []
    stage_job_code = STAGE_TO_JOB_CODE.get(current_stage, "")
    stage_jobs = _stage_api_jobs(store=store, request_id=request_id, stage_code=current_stage, job_code=stage_job_code)
    if not stage_jobs:
        return []
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in stage_jobs):
        return []
    _refresh_request_counts(store=store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=current_stage,
        progress_stage=current_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return [
        {
            "request_id": request_id,
            "stage_code": current_stage,
            "released": True,
            "next_executor_status": "pending",
        }
    ]

def advance_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    current_stage = _current_stage(request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    if current_stage in WAITING_STAGES and str(request.status or "") == "waiting_children":
        return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    if current_stage in {
        DISCOVER_CREATORS_STAGE_CODE,
        SYNC_INFLUENCER_POOL_STAGE_CODE,
        WRITEBACK_STAGE_CODE,
    }:
        return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    return dispatch_sync_tk_influencer_pool_request(store=store, request_id=request_id)

def dispatch_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    current_stage = _current_stage(request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    module_name = _STAGE_MODULES.get(current_stage)
    if module_name:
        stage_module = import_module(f"{__package__}.stages.{module_name}")
        return stage_module.advance(store=store, request=request, workflow=None)
    return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)

def release_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    current_stage = _current_stage(request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    module_name = _STAGE_MODULES.get(current_stage)
    if module_name:
        stage_module = import_module(f"{__package__}.stages.{module_name}")
        return stage_module.advance(store=store, request=request, workflow=None)
    return {
        "action": "noop",
        "request_id": request_id,
        "current_stage": current_stage,
        "message": f"Stage {current_stage} has no release action.",
        "details": {"stage_code": current_stage},
    }


def _refresh_request_counts(*, store: RuntimeStore, request_id: str) -> None:
    request = store.load_task_request(request_id=request_id)
    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    child_summary = summarize_child_status_counts(
        build_request_child_views(api_worker_jobs=api_jobs, task_executions=executions)
    )
    store.update_task_request(
        request_id=request_id,
        child_total_count=child_summary.total_count,
        child_terminal_count=child_summary.terminal_count,
        child_success_count=child_summary.success_count,
        child_failed_count=child_summary.failed_count,
        child_skipped_count=child_summary.skipped_count,
        progress_stage=_current_stage(request),
    )
