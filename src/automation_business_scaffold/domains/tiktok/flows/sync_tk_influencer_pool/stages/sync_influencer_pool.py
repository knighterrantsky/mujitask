from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "sync_influencer_pool"

def _advance_stage_sync_influencer_pool(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    sync_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        job_code="influencer_creator_sync",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in sync_jobs):
        return _waiting_stage_result(
            current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
            message="Influencer creator sync jobs are still running.",
        )
    group_summaries = _build_product_group_summaries(store=store, request=request)
    writeback_jobs = _build_competitor_status_write_jobs(request=request, group_summaries=group_summaries)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITEBACK_STAGE_CODE)[0]
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if writeback_jobs:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=writeback_jobs,
        )
    if _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code=resolved_job.job_code,
    ):
        return _waiting_stage_result(
            current_stage=WRITEBACK_STAGE_CODE,
            message="Residual competitor status writeback jobs were enqueued.",
            details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "group_summaries": group_summaries},
        )
    return _advance_stage_result(
        next_stage=WRITEBACK_STAGE_CODE,
        details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "group_summaries": group_summaries},
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_sync_influencer_pool(store=store, request=request)
