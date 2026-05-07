from __future__ import annotations

from typing import Any

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "discover_related_creators"

def _advance_stage_discover_related_creators(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="product_creator_discovery",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in product_jobs):
        return _waiting_stage_result(
            current_stage=DISCOVER_CREATORS_STAGE_CODE,
            message="Product discovery jobs are still running.",
        )
    if not product_jobs:
        return _advance_stage_result(
            next_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
            details={"creator_sync_job_count": 0, "reason": "no_product_jobs"},
        )
    creator_jobs = _build_influencer_creator_sync_jobs(request=request, product_jobs=product_jobs)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(SYNC_INFLUENCER_POOL_STAGE_CODE)[0]
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if creator_jobs:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=creator_jobs,
        )
    if _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        job_code=resolved_job.job_code,
    ):
        return _waiting_stage_result(
            current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
            message="Creator sync jobs were fanned out from product discovery.",
            details={"dispatch_payload": {"influencer_creator_sync": enqueue_result}, "creator_sync_job_count": len(creator_jobs)},
        )
    return _advance_stage_result(
        next_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
        details={"dispatch_payload": {"influencer_creator_sync": enqueue_result}, "creator_sync_job_count": len(creator_jobs)},
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_discover_related_creators(store=store, request=request)
