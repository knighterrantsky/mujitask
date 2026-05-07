from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "read_competitor_candidates"

def _advance_stage_read_competitor_candidates(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    stage_jobs = _stage_api_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read")
    if not stage_jobs:
        resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(READ_STAGE_CODE)[0]
        request_payload = dict(request.payload or {})
        job_keys = render_job_keys(
            resolved_job,
            request_payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=READ_STAGE_CODE,
        )
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=[
                {
                    "business_key": job_keys["business_key"],
                    "dedupe_key": job_keys["dedupe_key"],
                    "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": READ_STAGE_CODE,
                        "request_payload": request_payload,
                        "source_table_ref": _source_table_ref_from_request(request_payload),
                        "view_ref": _view_ref_from_request(request_payload),
                        "filter_spec": _build_candidate_filter(request_payload),
                        "adapter_code": "influencer_pool_source_adapter",
                        "cursor_context": dict(request.stage_cursor or {}),
                        "reply_target": str(request.reply_target or ""),
                        "source_record_ids": list(request_payload.get("source_record_ids") or []),
                        **_feishu_common_payload(request_payload),
                    },
                }
            ],
        )
        return _waiting_stage_result(
            current_stage=READ_STAGE_CODE,
            message="Executor dispatched the competitor candidate read stage.",
            details={"dispatch_payload": {"feishu_table_read": enqueue_result}},
        )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in stage_jobs):
        return _waiting_stage_result(
            current_stage=READ_STAGE_CODE,
            message="Competitor candidate read is still running.",
        )
    return _advance_stage_result(
        next_stage=DISPATCH_PRODUCT_STAGE_CODE,
        details={"stage_transition": "competitor_candidates_ready"},
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_read_competitor_candidates(store=store, request=request)
