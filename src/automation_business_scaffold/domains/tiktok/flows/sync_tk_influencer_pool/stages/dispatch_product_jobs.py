from __future__ import annotations

from typing import Any

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "dispatch_product_jobs"

def _advance_stage_dispatch_product_jobs(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    request_payload = dict(request.payload or {})
    candidates = _collect_product_candidates(store=store, request=request)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(DISCOVER_CREATORS_STAGE_CODE)[0]
    jobs_to_enqueue: list[dict[str, Any]] = []
    for candidate in candidates:
        template_context = _build_product_job_context(request=request, candidate=candidate, stage_code=DISCOVER_CREATORS_STAGE_CODE)
        keys = render_job_keys(
            resolved_job,
            template_context,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=DISCOVER_CREATORS_STAGE_CODE,
        )
        jobs_to_enqueue.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": DISCOVER_CREATORS_STAGE_CODE,
                    "product_identity": dict(candidate["product_identity"]),
                    "discovery_plan": {"detail_level": "related_creators", "internal_handler": "fastmoss_product_fetch"},
                    "detail_level": "related_creators",
                    **_fastmoss_common_payload(request_payload),
                    "relation_policy": _relation_policy_from_request(request_payload),
                    "source_context": {
                        "source_record_id": candidate["source_record_id"],
                        "product_id": candidate["product_id"],
                        "product_key": candidate["product_key"],
                        "candidate_row": dict(candidate["candidate_row"]),
                        **_candidate_business_context(candidate),
                    },
                },
            }
        )
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if jobs_to_enqueue:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=jobs_to_enqueue,
        )
    if _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code=resolved_job.job_code,
    ):
        return _waiting_stage_result(
            current_stage=DISCOVER_CREATORS_STAGE_CODE,
            message="Executor dispatched product discovery jobs.",
            details={"dispatch_payload": {"product_creator_discovery": enqueue_result}, "candidate_count": len(candidates)},
        )
    return _advance_stage_result(
        next_stage=DISCOVER_CREATORS_STAGE_CODE,
        details={"dispatch_payload": {"product_creator_discovery": enqueue_result}, "candidate_count": len(candidates)},
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_dispatch_product_jobs(store=store, request=request)
