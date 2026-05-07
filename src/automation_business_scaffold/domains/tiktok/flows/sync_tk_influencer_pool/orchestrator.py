from __future__ import annotations

from importlib import import_module
from typing import Any

from .context import (
    COLLECT_CREATOR_STAGE_CODE,
    FINALIZE_PRODUCT_STAGE_CODE,
    PERSIST_FACTS_STAGE_CODE,
    SUMMARY_STAGE_CODE,
    TASK_CODE,
    WRITE_POOL_STAGE_CODE,
    _advance_stage_collect_creator_detail,
    _advance_stage_finalize_product,
    _advance_stage_persist_creator_facts,
    _advance_stage_write_influencer_pool,
)
from .context import *  # noqa: F403

_STAGE_MODULES = {
    "read_competitor_candidates": "read_competitor_candidates",
    "dispatch_product_jobs": "dispatch_product_jobs",
    "discover_related_creators": "discover_related_creators",
    "sync_influencer_pool": "sync_influencer_pool",
    "writeback_competitor_status": "writeback_competitor_status",
}


def advance_stage(*, store: Any, request: Any, workflow: Any, stage_code: str) -> dict[str, Any]:
    del workflow
    module_name = _STAGE_MODULES.get(stage_code)
    if module_name:
        stage_module = import_module(f"{__package__}.stages.{module_name}")
        return stage_module.advance(store=store, request=request, workflow=None)
    if stage_code == COLLECT_CREATOR_STAGE_CODE:
        return _advance_stage_collect_creator_detail(store=store, request=request)
    if stage_code == PERSIST_FACTS_STAGE_CODE:
        return _advance_stage_persist_creator_facts(store=store, request=request)
    if stage_code == WRITE_POOL_STAGE_CODE:
        return _advance_stage_write_influencer_pool(store=store, request=request)
    if stage_code == FINALIZE_PRODUCT_STAGE_CODE:
        return _advance_stage_finalize_product(store=store, request=request)
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
    if current_stage == READ_STAGE_CODE:
        return _dispatch_read_competitor_candidates(store=store, request=request)
    if current_stage == DISPATCH_PRODUCT_STAGE_CODE:
        return _dispatch_product_jobs(store=store, request=request)
    if current_stage == FINALIZE_PRODUCT_STAGE_CODE:
        return _finalize_product_groups(store=store, request=request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)

def release_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    current_stage = _current_stage(request)
    if current_stage == READ_STAGE_CODE:
        return _release_read_competitor_candidates(store=store, request=request)
    if current_stage == DISCOVER_CREATORS_STAGE_CODE:
        return _release_discover_related_creators(store=store, request=request)
    if current_stage == SYNC_INFLUENCER_POOL_STAGE_CODE:
        return _release_sync_influencer_pool(store=store, request=request)
    if current_stage == COLLECT_CREATOR_STAGE_CODE:
        return _release_collect_creator_detail(store=store, request=request)
    if current_stage == PERSIST_FACTS_STAGE_CODE:
        return _release_persist_creator_facts(store=store, request=request)
    if current_stage == WRITE_POOL_STAGE_CODE:
        return _release_write_influencer_pool(store=store, request=request)
    if current_stage == WRITEBACK_STAGE_CODE:
        return _release_writeback_competitor_status(store=store, request=request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    return _build_payload(
        store=store,
        request_id=request_id,
        action="noop",
        message=f"Stage {current_stage} has no release action.",
        details={"stage_code": current_stage},
    )
