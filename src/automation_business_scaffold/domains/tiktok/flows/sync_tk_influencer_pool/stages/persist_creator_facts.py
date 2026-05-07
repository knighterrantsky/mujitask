from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import render_job_keys

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403

STAGE_CODE = "persist_creator_facts"


def _advance_stage_persist_creator_facts(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    fact_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_FACTS_STAGE_CODE,
        job_code="fact_bundle_upsert",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in fact_jobs):
        return _waiting_stage_result(
            current_stage=PERSIST_FACTS_STAGE_CODE,
            message="Fact persistence jobs are still running.",
        )

    creator_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code="fastmoss_creator_fetch",
    )
    write_jobs = _build_influencer_pool_write_jobs(
        request=request,
        creator_jobs=creator_jobs,
        fact_jobs=fact_jobs,
    )
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITE_POOL_STAGE_CODE)[0]
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if write_jobs:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=write_jobs,
        )
    if _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code=resolved_job.job_code,
    ):
        return _waiting_stage_result(
            current_stage=WRITE_POOL_STAGE_CODE,
            message="Influencer pool write jobs were dispatched after fact persistence.",
            details={
                "dispatch_payload": {"feishu_table_write": enqueue_result},
                "fact_job_count": len(fact_jobs),
                "write_job_count": len(write_jobs),
            },
        )
    return _advance_stage_result(
        next_stage=WRITE_POOL_STAGE_CODE,
        details={
            "dispatch_payload": {"feishu_table_write": enqueue_result},
            "fact_job_count": len(fact_jobs),
            "write_job_count": len(write_jobs),
        },
    )


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_persist_creator_facts(store=store, request=request)
