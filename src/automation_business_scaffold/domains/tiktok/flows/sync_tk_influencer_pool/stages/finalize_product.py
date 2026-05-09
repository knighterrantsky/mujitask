from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import render_job_keys

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403

STAGE_CODE = "finalize_product"


def _advance_stage_finalize_product(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
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
            message="Competitor status writeback jobs were dispatched.",
            details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "group_summaries": group_summaries},
        )
    return _advance_stage_result(
        next_stage=WRITEBACK_STAGE_CODE,
        details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "group_summaries": group_summaries},
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_finalize_product(store=store, request=request)
