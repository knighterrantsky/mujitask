from __future__ import annotations

import os
from typing import Any, Mapping

from automation_business_scaffold.business.handlers._shared import (
    bundle_entity_keys,
    merge_fact_bundles,
)
from automation_business_scaffold.business.flows.runtime_views import (
    build_request_child_views,
    build_request_view_fragment,
    summarize_child_status_counts,
)
from automation_business_scaffold.business.workflow_defs import get_workflow_definition
from automation_business_scaffold.business.workflow_defs.execution_helpers import (
    extract_effective_result_payload,
    extract_handler_result_status,
    render_job_keys,
    select_latest_successful_api_job_result,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

SYNC_TK_INFLUENCER_POOL_WORKFLOW = get_workflow_definition("sync_tk_influencer_pool")
WORKFLOW_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.workflow_code
TASK_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.task_code
ENTRY_STAGE_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.entry_stage_code
SUMMARY_STAGE_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.summary_policy.summary_stage_code
FINAL_STAGE_CODE = "completed"

READ_STAGE_CODE = "read_competitor_candidates"
DISPATCH_PRODUCT_STAGE_CODE = "dispatch_product_jobs"
DISCOVER_CREATORS_STAGE_CODE = "discover_related_creators"
COLLECT_CREATOR_STAGE_CODE = "collect_creator_detail"
PERSIST_FACTS_STAGE_CODE = "persist_creator_facts"
WRITE_POOL_STAGE_CODE = "write_influencer_pool"
FINALIZE_PRODUCT_STAGE_CODE = "finalize_product"
WRITEBACK_STAGE_CODE = "writeback_competitor_status"

STAGE_TO_JOB_CODE = {
    READ_STAGE_CODE: "feishu_table_read",
    DISCOVER_CREATORS_STAGE_CODE: "fastmoss_product_fetch",
    COLLECT_CREATOR_STAGE_CODE: "fastmoss_creator_fetch",
    PERSIST_FACTS_STAGE_CODE: "fact_bundle_upsert",
    WRITE_POOL_STAGE_CODE: "feishu_table_write",
    WRITEBACK_STAGE_CODE: "feishu_table_write",
}
WAITING_STAGES = {
    READ_STAGE_CODE,
    DISCOVER_CREATORS_STAGE_CODE,
    COLLECT_CREATOR_STAGE_CODE,
    PERSIST_FACTS_STAGE_CODE,
    WRITE_POOL_STAGE_CODE,
    WRITEBACK_STAGE_CODE,
}
ACTIVE_STATUSES = {"pending", "running", "retry_wait"}
SUCCESSFUL_HANDLER_STATUSES = {"success", "partial_success"}
TERMINAL_HANDLER_STATUSES = {"success", "skipped", "partial_success", "failed", "fallback_required"}


def advance_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: Any,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    if stage_code == READ_STAGE_CODE:
        return _advance_stage_read_competitor_candidates(store=store, request=request)
    if stage_code == DISPATCH_PRODUCT_STAGE_CODE:
        return _advance_stage_dispatch_product_jobs(store=store, request=request)
    if stage_code == DISCOVER_CREATORS_STAGE_CODE:
        return _advance_stage_discover_related_creators(store=store, request=request)
    if stage_code == COLLECT_CREATOR_STAGE_CODE:
        return _advance_stage_collect_creator_detail(store=store, request=request)
    if stage_code == PERSIST_FACTS_STAGE_CODE:
        return _advance_stage_persist_creator_facts(store=store, request=request)
    if stage_code == WRITE_POOL_STAGE_CODE:
        return _advance_stage_write_influencer_pool(store=store, request=request)
    if stage_code == FINALIZE_PRODUCT_STAGE_CODE:
        return _advance_stage_finalize_product(store=store, request=request)
    if stage_code == WRITEBACK_STAGE_CODE:
        return _advance_stage_writeback_competitor_status(store=store, request=request)
    if stage_code == SUMMARY_STAGE_CODE:
        return {"action": "finalize"}
    return {
        "action": "finalize",
        "final_status": "failed",
        "summary": {"final_status": "failed", "warnings": [f"unsupported_stage:{stage_code}"]},
        "result": {"message": f"Unsupported sync_tk_influencer_pool stage {stage_code}."},
        "details": {"unsupported_stage": stage_code},
    }


def finalize_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: Any,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    payload = finalize_sync_tk_influencer_pool_request(store=store, request_id=request.request_id)
    if force_result:
        if isinstance(force_result.get("summary"), Mapping):
            merged_summary = dict(payload.get("summary_payload") or {})
            merged_summary.update(dict(force_result.get("summary") or {}))
            payload["summary_payload"] = merged_summary
            store.update_task_request(request_id=request.request_id, summary=merged_summary)
        if isinstance(force_result.get("result"), Mapping):
            merged_result = dict(payload.get("result_payload") or {})
            merged_result.update(dict(force_result.get("result") or {}))
            payload["result_payload"] = merged_result
            store.update_task_request(request_id=request.request_id, result=merged_result)
        if force_result.get("final_status"):
            payload["final_status"] = str(force_result.get("final_status"))
    finalized_request = store.load_task_request(request_id=request.request_id)
    return {
        "request_id": finalized_request.request_id,
        "task_code": finalized_request.task_code,
        "request_status": finalized_request.status,
        "current_stage": finalized_request.current_stage,
        "summary": dict(finalized_request.summary or {}),
        "result": dict(finalized_request.result or {}),
        "final_status": str(finalized_request.status or ""),
        "message": "Executor finalized the influencer pool sync request.",
        "outbox": payload.get("outbox", []),
    }


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
            details={"dispatch_payload": {"fastmoss_product_fetch": enqueue_result}, "candidate_count": len(candidates)},
        )
    return _advance_stage_result(
        next_stage=DISCOVER_CREATORS_STAGE_CODE,
        details={"dispatch_payload": {"fastmoss_product_fetch": enqueue_result}, "candidate_count": len(candidates)},
    )


def _advance_stage_discover_related_creators(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in product_jobs):
        return _waiting_stage_result(
            current_stage=DISCOVER_CREATORS_STAGE_CODE,
            message="Product discovery jobs are still running.",
        )
    if not product_jobs:
        return _advance_stage_result(
            next_stage=COLLECT_CREATOR_STAGE_CODE,
            details={"creator_job_count": 0, "reason": "no_product_jobs"},
        )
    creator_jobs = _build_creator_detail_jobs(request=request, product_jobs=product_jobs)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(COLLECT_CREATOR_STAGE_CODE)[0]
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
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code=resolved_job.job_code,
    ):
        return _waiting_stage_result(
            current_stage=COLLECT_CREATOR_STAGE_CODE,
            message="Creator detail jobs were fanned out from product discovery.",
            details={"dispatch_payload": {"fastmoss_creator_fetch": enqueue_result}, "creator_job_count": len(creator_jobs)},
        )
    return _advance_stage_result(
        next_stage=COLLECT_CREATOR_STAGE_CODE,
        details={"dispatch_payload": {"fastmoss_creator_fetch": enqueue_result}, "creator_job_count": len(creator_jobs)},
    )


def _advance_stage_collect_creator_detail(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    creator_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code="fastmoss_creator_fetch",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in creator_jobs):
        return _waiting_stage_result(
            current_stage=COLLECT_CREATOR_STAGE_CODE,
            message="Creator detail jobs are still running.",
        )
    if not creator_jobs:
        return _advance_stage_result(
            next_stage=WRITE_POOL_STAGE_CODE,
            details={"write_job_count": 0, "reason": "no_creator_jobs"},
        )
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
    )
    fact_jobs = _build_fact_upsert_jobs(
        request=request,
        product_jobs=product_jobs,
        creator_jobs=creator_jobs,
    )
    if fact_jobs:
        resolved_fact_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.require_job("fact_bundle_upsert")
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_fact_job.job_code,
            jobs=fact_jobs,
        )
        return _waiting_stage_result(
            current_stage=PERSIST_FACTS_STAGE_CODE,
            message="Fact persistence jobs were dispatched before influencer pool projection.",
            details={"dispatch_payload": {"fact_bundle_upsert": enqueue_result}, "fact_job_count": len(fact_jobs)},
        )

    write_jobs = _build_influencer_pool_write_jobs(request=request, creator_jobs=creator_jobs)
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
            message="Influencer pool write jobs were dispatched.",
            details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "write_job_count": len(write_jobs)},
        )
    return _advance_stage_result(
        next_stage=WRITE_POOL_STAGE_CODE,
        details={"dispatch_payload": {"feishu_table_write": enqueue_result}, "write_job_count": len(write_jobs)},
    )


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


def _advance_stage_write_influencer_pool(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    write_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in write_jobs):
        return _waiting_stage_result(
            current_stage=WRITE_POOL_STAGE_CODE,
            message="Influencer pool write jobs are still running.",
        )
    return _advance_stage_result(next_stage=FINALIZE_PRODUCT_STAGE_CODE)


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


def _advance_stage_writeback_competitor_status(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    writeback_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code="feishu_table_write",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in writeback_jobs):
        return _waiting_stage_result(
            current_stage=WRITEBACK_STAGE_CODE,
            message="Competitor status writeback jobs are still running.",
        )
    return _advance_stage_result(next_stage=SUMMARY_STAGE_CODE)


def advance_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    current_stage = _current_stage(request)
    if current_stage == SUMMARY_STAGE_CODE:
        return finalize_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    if current_stage in WAITING_STAGES and str(request.status or "") == "waiting_children":
        return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)
    if current_stage in {
        DISCOVER_CREATORS_STAGE_CODE,
        COLLECT_CREATOR_STAGE_CODE,
        PERSIST_FACTS_STAGE_CODE,
        WRITE_POOL_STAGE_CODE,
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
    if current_stage == PERSIST_FACTS_STAGE_CODE:
        return release_sync_tk_influencer_pool_request(store=store, request_id=request_id)
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


def finalize_sync_tk_influencer_pool_request(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = _load_request(store=store, request_id=request_id)
    group_summaries = _build_product_group_summaries(store=store, request=request)
    group_counts = _count_product_group_statuses(group_summaries)
    final_status = _derive_final_status(group_summaries)
    warnings = _build_summary_warnings(group_summaries)
    summary_payload = {
        "final_status": final_status,
        "product_group_count": len(group_summaries),
        "product_groups": group_summaries,
        "product_group_status_counts": group_counts,
        "child_total_count": int(request.child_total_count or 0),
        "child_success_count": int(request.child_success_count or 0),
        "child_failed_count": int(request.child_failed_count or 0),
        "child_skipped_count": int(request.child_skipped_count or 0),
        "warnings": warnings,
    }
    result_payload = {
        "workflow_code": WORKFLOW_CODE,
        "task_code": TASK_CODE,
        "product_groups": group_summaries,
        "final_status": final_status,
    }
    channel_code = str(request.source_channel_code or "noop")
    outbox = store.create_notification_outbox(
        channel_code=channel_code,
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(request.reply_target or ""),
        payload={
            "request_id": request.request_id,
            "workflow_code": WORKFLOW_CODE,
            "summary_payload": summary_payload,
            "reply_target": str(request.reply_target or ""),
            "channel_code": channel_code,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=FINAL_STAGE_CODE,
        progress_stage=FINAL_STAGE_CODE,
        summary=summary_payload,
        result=result_payload,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    _refresh_request_counts(store=store, request_id=request.request_id)
    return _build_payload(
        store=store,
        request_id=request.request_id,
        action="finalized",
        message="sync_tk_influencer_pool request finalized.",
        details={
            "final_status": final_status,
            "summary_payload": summary_payload,
            "result_payload": result_payload,
            "outbox_record": outbox.to_dict(),
            "request": updated.to_dict(),
        },
    )


def _dispatch_read_competitor_candidates(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    stage_code = READ_STAGE_CODE
    jobs = _stage_api_jobs(store=store, request_id=request.request_id, stage_code=stage_code, job_code="feishu_table_read")
    active_jobs = [job for job in jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=stage_code)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Waiting for competitor candidate read job.",
            details={"stage_code": stage_code, "existing_jobs": jobs},
        )

    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(stage_code)[0]
    request_payload = dict(request.payload or {})
    job_keys = render_job_keys(
        resolved_job,
        request_payload,
        request_id=request.request_id,
        task_code=TASK_CODE,
        workflow_code=WORKFLOW_CODE,
        stage_code=stage_code,
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
                    "stage_code": stage_code,
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
    _set_waiting_state(store=store, request=request, stage_code=stage_code)
    return _build_payload(
        store=store,
        request_id=request.request_id,
        action="waiting",
        message="Enqueued Feishu competitor candidate read job.",
        details={"stage_code": stage_code, "enqueue_result": enqueue_result},
    )


def _release_read_competitor_candidates(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    stage_jobs = _stage_api_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read")
    active_jobs = [job for job in stage_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=READ_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Competitor candidate read is still running.",
            details={"stage_code": READ_STAGE_CODE, "active_jobs": active_jobs},
        )

    _set_pending_state(store=store, request=request, stage_code=DISPATCH_PRODUCT_STAGE_CODE)
    return _build_payload(
        store=store,
        request_id=request.request_id,
        action="advance",
        message="Competitor candidates are ready for product job fan-out.",
        details={"from_stage": READ_STAGE_CODE, "to_stage": DISPATCH_PRODUCT_STAGE_CODE},
    )


def _dispatch_product_jobs(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
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

    stage_has_children = _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code=resolved_job.job_code,
    )
    if stage_has_children:
        _set_waiting_state(store=store, request=request, stage_code=DISCOVER_CREATORS_STAGE_CODE)
        action = "waiting"
        message = "Dispatched product discovery jobs for related creator lookup."
    else:
        _set_pending_state(store=store, request=request, stage_code=DISCOVER_CREATORS_STAGE_CODE)
        action = "advance"
        message = "No product discovery jobs were required; advancing to reconciler release."

    return _build_payload(
        store=store,
        request_id=request.request_id,
        action=action,
        message=message,
        details={
            "stage_code": DISPATCH_PRODUCT_STAGE_CODE,
            "next_stage": DISCOVER_CREATORS_STAGE_CODE,
            "candidate_count": len(candidates),
            "enqueue_result": enqueue_result,
        },
    )


def _release_discover_related_creators(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
    )
    active_jobs = [job for job in product_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=DISCOVER_CREATORS_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Product discovery jobs are still running.",
            details={"stage_code": DISCOVER_CREATORS_STAGE_CODE, "active_jobs": active_jobs},
        )

    creator_jobs = _build_creator_detail_jobs(request=request, product_jobs=product_jobs)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(COLLECT_CREATOR_STAGE_CODE)[0]
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if creator_jobs:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=creator_jobs,
        )

    stage_has_children = _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code=resolved_job.job_code,
    )
    if stage_has_children:
        _set_waiting_state(store=store, request=request, stage_code=COLLECT_CREATOR_STAGE_CODE)
        action = "waiting"
        message = "Creator detail fan-out is ready."
    else:
        _set_pending_state(store=store, request=request, stage_code=COLLECT_CREATOR_STAGE_CODE)
        action = "advance"
        message = "No creator detail jobs were required; advancing."

    return _build_payload(
        store=store,
        request_id=request.request_id,
        action=action,
        message=message,
        details={
            "stage_code": DISCOVER_CREATORS_STAGE_CODE,
            "next_stage": COLLECT_CREATOR_STAGE_CODE,
            "enqueue_result": enqueue_result,
            "product_job_count": len(product_jobs),
            "creator_job_count": len(creator_jobs),
        },
    )


def _release_collect_creator_detail(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    creator_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code="fastmoss_creator_fetch",
    )
    active_jobs = [job for job in creator_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=COLLECT_CREATOR_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Creator detail jobs are still running.",
            details={"stage_code": COLLECT_CREATOR_STAGE_CODE, "active_jobs": active_jobs},
        )

    write_jobs = _build_influencer_pool_write_jobs(request=request, creator_jobs=creator_jobs)
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITE_POOL_STAGE_CODE)[0]
    enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "created_records": [], "updated_records": [], "skipped_records": []}
    if write_jobs:
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=resolved_job.job_code,
            jobs=write_jobs,
        )

    stage_has_children = _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code=resolved_job.job_code,
    )
    if stage_has_children:
        _set_waiting_state(store=store, request=request, stage_code=WRITE_POOL_STAGE_CODE)
        action = "waiting"
        message = "Influencer pool write jobs were enqueued."
    else:
        _set_pending_state(store=store, request=request, stage_code=WRITE_POOL_STAGE_CODE)
        action = "advance"
        message = "No influencer pool write jobs were required; advancing."

    return _build_payload(
        store=store,
        request_id=request.request_id,
        action=action,
        message=message,
        details={
            "stage_code": COLLECT_CREATOR_STAGE_CODE,
            "next_stage": WRITE_POOL_STAGE_CODE,
            "enqueue_result": enqueue_result,
            "creator_job_count": len(creator_jobs),
            "write_job_count": len(write_jobs),
        },
    )


def _release_persist_creator_facts(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    fact_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_FACTS_STAGE_CODE,
        job_code="fact_bundle_upsert",
    )
    active_jobs = [job for job in fact_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=PERSIST_FACTS_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Fact persistence jobs are still running.",
            details={"stage_code": PERSIST_FACTS_STAGE_CODE, "active_jobs": active_jobs},
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

    stage_has_children = _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code=resolved_job.job_code,
    )
    if stage_has_children:
        _set_waiting_state(store=store, request=request, stage_code=WRITE_POOL_STAGE_CODE)
        action = "waiting"
        message = "Influencer pool write jobs were enqueued after fact persistence."
    else:
        _set_pending_state(store=store, request=request, stage_code=WRITE_POOL_STAGE_CODE)
        action = "advance"
        message = "No influencer pool write jobs were required after fact persistence; advancing."

    return _build_payload(
        store=store,
        request_id=request.request_id,
        action=action,
        message=message,
        details={
            "stage_code": PERSIST_FACTS_STAGE_CODE,
            "next_stage": WRITE_POOL_STAGE_CODE,
            "enqueue_result": enqueue_result,
            "fact_job_count": len(fact_jobs),
            "write_job_count": len(write_jobs),
        },
    )


def _release_write_influencer_pool(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    write_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
    )
    active_jobs = [job for job in write_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=WRITE_POOL_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Influencer pool write jobs are still running.",
            details={"stage_code": WRITE_POOL_STAGE_CODE, "active_jobs": active_jobs},
        )

    _set_pending_state(store=store, request=request, stage_code=FINALIZE_PRODUCT_STAGE_CODE)
    return _build_payload(
        store=store,
        request_id=request.request_id,
        action="advance",
        message="Influencer pool writes are terminal; ready for product finalization.",
        details={"from_stage": WRITE_POOL_STAGE_CODE, "to_stage": FINALIZE_PRODUCT_STAGE_CODE},
    )


def _finalize_product_groups(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
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

    stage_has_children = _stage_has_children(
        store=store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code=resolved_job.job_code,
    )
    if stage_has_children:
        _set_waiting_state(store=store, request=request, stage_code=WRITEBACK_STAGE_CODE)
        action = "waiting"
        message = "Competitor status writeback jobs were enqueued."
    else:
        _set_pending_state(store=store, request=request, stage_code=WRITEBACK_STAGE_CODE)
        action = "advance"
        message = "No competitor writeback jobs were required; advancing."

    return _build_payload(
        store=store,
        request_id=request.request_id,
        action=action,
        message=message,
        details={
            "stage_code": FINALIZE_PRODUCT_STAGE_CODE,
            "next_stage": WRITEBACK_STAGE_CODE,
            "group_summaries": group_summaries,
            "enqueue_result": enqueue_result,
        },
    )


def _release_writeback_competitor_status(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    writeback_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code="feishu_table_write",
    )
    active_jobs = [job for job in writeback_jobs if str(job.get("status") or "") in ACTIVE_STATUSES]
    if active_jobs:
        _set_waiting_state(store=store, request=request, stage_code=WRITEBACK_STAGE_CODE)
        return _build_payload(
            store=store,
            request_id=request.request_id,
            action="waiting",
            message="Competitor status writeback jobs are still running.",
            details={"stage_code": WRITEBACK_STAGE_CODE, "active_jobs": active_jobs},
        )

    _set_pending_state(store=store, request=request, stage_code=SUMMARY_STAGE_CODE)
    return _build_payload(
        store=store,
        request_id=request.request_id,
        action="advance",
        message="Competitor writeback is terminal; ready for summary.",
        details={"from_stage": WRITEBACK_STAGE_CODE, "to_stage": SUMMARY_STAGE_CODE},
    )


def _collect_product_candidates(*, store: RuntimeStore, request: Any) -> list[dict[str, Any]]:
    read_result = select_latest_successful_api_job_result(
        _stage_api_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read"),
        "feishu_table_read",
    )
    rows = list(read_result.get("source_rows") or [])
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_record_id = _first_non_empty(
            row.get("source_record_id"),
            row.get("record_id"),
            row.get("row_id"),
        )
        product_identity = _normalize_product_identity(row.get("product_identity"), row)
        product_id = _first_non_empty(product_identity.get("product_id"), row.get("product_id"))
        if not source_record_id or not product_id:
            continue
        candidates.append(
            {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
                "product_identity": product_identity,
                "candidate_row": dict(row),
            }
        )
    return candidates


def _build_creator_detail_jobs(*, request: Any, product_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(COLLECT_CREATOR_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    jobs_to_enqueue: list[dict[str, Any]] = []
    for product_job in product_jobs:
        if extract_handler_result_status(product_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(product_job)
        source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(
            source_context.get("product_id"),
            result_payload.get("product_id"),
            ((product_job.get("payload") or {}).get("product_identity") or {}).get("product_id"),
        )
        product_context = _product_job_business_context(product_job)
        if not source_record_id or not product_id:
            continue
        for creator in list(result_payload.get("related_creators") or []):
            if not isinstance(creator, Mapping):
                continue
            creator_identity = _normalize_creator_identity(creator.get("creator_identity"), creator)
            creator_id = _first_non_empty(
                creator_identity.get("creator_id"),
                creator.get("creator_id"),
                creator.get("influencer_id"),
            )
            if not creator_id:
                continue
            template_context = {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "creator_id": creator_id,
                "product_id_or_group": product_id,
            }
            keys = render_job_keys(
                resolved_job,
                template_context,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=COLLECT_CREATOR_STAGE_CODE,
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
                        "stage_code": COLLECT_CREATOR_STAGE_CODE,
                        "creator_identity": creator_identity,
                        "detail_level": _creator_detail_level_from_request(request_payload),
                        **_fastmoss_common_payload(request_payload),
                        "fetch_plan": _creator_fetch_plan_from_request(request_payload),
                        "relation_policy": _creator_relation_policy_from_request(request_payload),
                        "source_context": {
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                            "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
                            "creator_candidate": dict(creator),
                            "product_job_id": str(product_job.get("job_id") or ""),
                            **product_context,
                            **_creator_candidate_business_context(creator),
                        },
                    },
                }
            )
    return jobs_to_enqueue


def _build_fact_upsert_jobs(
    *,
    request: Any,
    product_jobs: list[dict[str, Any]],
    creator_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.require_job("fact_bundle_upsert")
    request_payload = dict(request.payload or {})
    fact_db_url = _fact_db_url_from_request(request_payload)
    jobs_to_enqueue: list[dict[str, Any]] = []
    seen_dedupe: set[str] = set()

    for product_job in product_jobs:
        if extract_handler_result_status(product_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(product_job)
        fact_bundle = merge_fact_bundles(dict(result_payload.get("product_fact_bundle") or {}))
        entity_keys = bundle_entity_keys(fact_bundle)
        if not entity_keys:
            continue
        source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"), _job_product_id(product_job))
        dedupe_key = f"{request.request_id}:{PERSIST_FACTS_STAGE_CODE}:product:{source_record_id}:{product_id}"
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        jobs_to_enqueue.append(
            {
                "business_key": ",".join(entity_keys),
                "dedupe_key": dedupe_key,
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_payload": request_payload,
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": PERSIST_FACTS_STAGE_CODE,
                    "source_job_ids": [str(product_job.get("job_id") or "")],
                    "source_context": {
                        **source_context,
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                    },
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "fact_subject": "product",
                    },
                    "entity_business_keys": ",".join(entity_keys),
                    "observation_at": _first_non_empty(result_payload.get("observed_at")),
                    "mapper_code": "influencer_product_fact_mapper",
                    "fact_bundle": fact_bundle,
                    "fact_db_url": fact_db_url,
                },
            }
        )

    for creator_job in creator_jobs:
        if extract_handler_result_status(creator_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(creator_job)
        fact_bundle = merge_fact_bundles(dict(result_payload.get("fact_bundle") or {}))
        entity_keys = bundle_entity_keys(fact_bundle)
        if not entity_keys:
            continue
        payload = dict(creator_job.get("payload") or {})
        source_context = dict(payload.get("source_context") or {})
        creator_identity = dict(payload.get("creator_identity") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"), _job_product_id(creator_job))
        creator_id = _first_non_empty(
            dict(result_payload.get("creator_fact_bundle") or {}).get("creator_id"),
            creator_identity.get("creator_id"),
            creator_identity.get("unique_id"),
            creator_identity.get("uid"),
        )
        dedupe_key = f"{request.request_id}:{PERSIST_FACTS_STAGE_CODE}:creator:{source_record_id}:{product_id}:{creator_id}"
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        jobs_to_enqueue.append(
            {
                "business_key": ",".join(entity_keys),
                "dedupe_key": dedupe_key,
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_payload": request_payload,
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": PERSIST_FACTS_STAGE_CODE,
                    "source_job_ids": [str(creator_job.get("job_id") or "")],
                    "source_context": {
                        **source_context,
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                    },
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                        "fact_subject": "creator",
                    },
                    "entity_business_keys": ",".join(entity_keys),
                    "observation_at": _first_non_empty(result_payload.get("observed_at")),
                    "mapper_code": "creator_fact_mapper",
                    "fact_bundle": fact_bundle,
                    "fact_db_url": fact_db_url,
                },
            }
        )

    return jobs_to_enqueue


def _build_influencer_pool_write_jobs(
    *,
    request: Any,
    creator_jobs: list[dict[str, Any]],
    fact_jobs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITE_POOL_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    target_table_ref = _first_non_empty(
        request.payload.get("influencer_pool_table_ref"),
        request.payload.get("target_table_ref"),
        request.payload.get("target_table_url"),
        request.payload.get("source_table_ref"),
        request.payload.get("table_url"),
    )
    fact_success_keys = _successful_fact_persist_keys(fact_jobs or [])
    require_fact_success = bool(fact_jobs)
    jobs_to_enqueue: list[dict[str, Any]] = []
    for creator_job in creator_jobs:
        if extract_handler_result_status(creator_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(creator_job)
        creator_fact_bundle = dict(result_payload.get("creator_fact_bundle") or {})
        source_context = dict((creator_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"))
        creator_id = _first_non_empty(
            creator_fact_bundle.get("creator_id"),
            ((creator_job.get("payload") or {}).get("creator_identity") or {}).get("creator_id"),
        )
        if require_fact_success and _creator_fact_key(source_record_id, product_id, creator_id) not in fact_success_keys:
            continue
        if not target_table_ref or not source_record_id or not product_id or not creator_id:
            continue
        record = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "creator_id": creator_id,
            "creator_name": _first_non_empty(
                creator_fact_bundle.get("display_name"),
                creator_fact_bundle.get("nickname"),
                source_context.get("creator_candidate", {}).get("display_name") if isinstance(source_context.get("creator_candidate"), Mapping) else "",
            ),
            "creator_fact_bundle": creator_fact_bundle,
            "fact_bundle": dict(result_payload.get("fact_bundle") or {}),
            "entities": dict(result_payload.get("entities") or {}),
            "relations": list(result_payload.get("relations") or []),
            "observations": list(result_payload.get("observations") or []),
            "media_refs": list(result_payload.get("media_refs") or []),
            "product_relations": list(result_payload.get("product_relations") or []),
            "source_context": source_context,
            **_write_record_business_context(source_context, result_payload),
            "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
        }
        keys = render_job_keys(
            resolved_job,
            {
                "target_table_ref": target_table_ref,
                "business_entity_key": creator_id,
                "creator_id": creator_id,
                "product_id": product_id,
                "source_record_id": source_record_id,
            },
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=WRITE_POOL_STAGE_CODE,
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
                    "stage_code": WRITE_POOL_STAGE_CODE,
                    "target_table_ref": target_table_ref,
                    "request_payload": request_payload,
                    "mapper_code": "influencer_pool_projection_mapper",
                    "write_mode": "upsert",
                    "records": [record],
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                    },
                    "business_entity_key": creator_id,
                    **_feishu_common_payload(request_payload),
                },
            }
        )
    return jobs_to_enqueue


def _build_competitor_status_write_jobs(*, request: Any, group_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITEBACK_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    target_table_ref = _first_non_empty(
        request.payload.get("competitor_status_table_ref"),
        request.payload.get("source_table_ref"),
        request.payload.get("source_table_url"),
        request.payload.get("table_url"),
    )
    jobs_to_enqueue: list[dict[str, Any]] = []
    for group in group_summaries:
        source_record_id = _first_non_empty(group.get("source_record_id"))
        product_id = _first_non_empty(group.get("product_id"))
        product_key = _first_non_empty(group.get("product_key"))
        if not target_table_ref or not source_record_id or not product_id or not product_key:
            continue
        record = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "product_key": product_key,
            "influencer_sync_status": group.get("final_status"),
            "creator_candidate_count": int(group.get("creator_candidate_count") or 0),
            "creator_detail_success_count": int(group.get("creator_detail_success_count") or 0),
            "creator_detail_failed_count": int(group.get("creator_detail_failed_count") or 0),
            "influencer_write_success_count": int(group.get("influencer_write_success_count") or 0),
            "warning_count": len(list(group.get("warnings") or [])),
            "warnings": list(group.get("warnings") or []),
        }
        keys = render_job_keys(
            resolved_job,
            {
                "target_table_ref": target_table_ref,
                "business_entity_key": product_key,
                "source_record_id": source_record_id,
                "product_id": product_id,
            },
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=WRITEBACK_STAGE_CODE,
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
                    "stage_code": WRITEBACK_STAGE_CODE,
                    "target_table_ref": target_table_ref,
                    "request_payload": request_payload,
                    "mapper_code": "competitor_influencer_status_projection_mapper",
                    "write_mode": "upsert",
                    "records": [record],
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                    },
                    "business_entity_key": product_key,
                    **_feishu_common_payload(request_payload),
                },
            }
        )
    return jobs_to_enqueue


def _build_product_group_summaries(*, store: RuntimeStore, request: Any) -> list[dict[str, Any]]:
    candidates = _collect_product_candidates(store=store, request=request)
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
    )
    creator_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code="fastmoss_creator_fetch",
    )
    fact_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_FACTS_STAGE_CODE,
        job_code="fact_bundle_upsert",
    )
    influencer_write_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
    )
    groups: list[dict[str, Any]] = []
    for candidate in candidates:
        source_record_id = candidate["source_record_id"]
        product_id = candidate["product_id"]
        product_key = candidate["product_key"]
        matched_product_jobs = [
            job for job in product_jobs if _job_product_key(job) == product_key
        ]
        matched_creator_jobs = [
            job for job in creator_jobs if _job_product_key(job) == product_key
        ]
        matched_fact_jobs = [
            job for job in fact_jobs if _job_product_key(job) == product_key
        ]
        matched_write_jobs = [
            job for job in influencer_write_jobs if _job_product_key(job) == product_key
        ]
        creator_candidates = _collect_creator_candidates_from_product_jobs(matched_product_jobs)
        creator_detail_success_count = sum(
            1 for job in matched_creator_jobs if extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES
        )
        creator_detail_failed_count = sum(
            1
            for job in matched_creator_jobs
            if str(job.get("status") or "") in {"failed", "cancelled"}
            or extract_handler_result_status(job) in {"failed", "fallback_required"}
        )
        influencer_write_success_count = sum(
            1 for job in matched_write_jobs if extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES
        )
        fact_persist_success_count = sum(
            1 for job in matched_fact_jobs if extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES
        )
        fact_persist_failed_count = sum(
            1
            for job in matched_fact_jobs
            if str(job.get("status") or "") in {"failed", "cancelled"}
            or extract_handler_result_status(job) in {"failed", "fallback_required"}
        )
        product_job_success = any(
            extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES for job in matched_product_jobs
        )
        product_job_failed = any(
            str(job.get("status") or "") in {"failed", "cancelled"}
            or extract_handler_result_status(job) in {"failed", "fallback_required"}
            for job in matched_product_jobs
        )
        final_status = "success"
        warnings: list[str] = []
        if product_job_failed and not product_job_success:
            final_status = "failed"
            warnings.append("product_discovery_failed")
        elif fact_persist_failed_count > 0 and fact_persist_success_count == 0:
            final_status = "failed"
            warnings.append("fact_persist_failed")
        elif creator_detail_failed_count > 0 and influencer_write_success_count == 0:
            final_status = "failed"
            warnings.append("creator_detail_failed")
        elif creator_detail_failed_count > 0 or product_job_failed or fact_persist_failed_count > 0:
            final_status = "partial_success"
            warnings.append("partial_creator_projection")
        elif not creator_candidates:
            final_status = "success"
            warnings.append("no_related_creators")
        groups.append(
            {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "product_key": product_key,
                "creator_candidate_count": len(creator_candidates),
                "creator_detail_success_count": creator_detail_success_count,
                "creator_detail_failed_count": creator_detail_failed_count,
                "fact_persist_success_count": fact_persist_success_count,
                "fact_persist_failed_count": fact_persist_failed_count,
                "influencer_write_success_count": influencer_write_success_count,
                "final_status": final_status,
                "warnings": warnings,
            }
        )
    return groups


def _collect_creator_candidates_from_product_jobs(product_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in product_jobs:
        if extract_handler_result_status(job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(job)
        for candidate in list(result_payload.get("related_creators") or []):
            if not isinstance(candidate, Mapping):
                continue
            creator_id = _first_non_empty(
                candidate.get("creator_id"),
                candidate.get("influencer_id"),
                (candidate.get("creator_identity") or {}).get("creator_id") if isinstance(candidate.get("creator_identity"), Mapping) else "",
            )
            if not creator_id or creator_id in seen:
                continue
            seen.add(creator_id)
            candidates.append(dict(candidate))
    return candidates


def _load_request(*, store: RuntimeStore, request_id: str) -> Any:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        raise ValueError(f"Request {request_id} is not a {TASK_CODE} runtime request.")
    return request


def _current_stage(request: Any) -> str:
    return str(request.current_stage or "").strip() or ENTRY_STAGE_CODE


def _build_payload(*, store: RuntimeStore, request_id: str, action: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    _refresh_request_counts(store=store, request_id=request_id)
    request = store.load_task_request(request_id=request_id)
    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = [execution.to_dict() for execution in store.list_task_executions(request_id=request_id)]
    outbox = [record.to_dict() for record in store.list_request_outbox(request_id=request_id)]
    child_summary = summarize_child_status_counts(
        build_request_child_views(api_worker_jobs=api_jobs, task_executions=store.list_task_executions(request_id=request_id))
    )
    payload = {
        "action": action,
        "message": message,
        "request_id": request.request_id,
        "request_status": request.status,
        "current_stage": request.current_stage,
        "request": build_request_view_fragment(request),
        "child_summary": child_summary.to_dict(),
        "api_worker_jobs": api_jobs,
        "executions": executions,
        "outbox": outbox,
    }
    if details:
        payload.update(details)
    return payload


def _waiting_stage_result(*, current_stage: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "waiting", "current_stage": current_stage, "message": message}
    if details:
        payload["details"] = details
    return payload


def _advance_stage_result(*, next_stage: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "advance", "next_stage": next_stage}
    if details:
        payload["details"] = details
    return payload


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


def _set_waiting_state(*, store: RuntimeStore, request: Any, stage_code: str) -> None:
    _refresh_request_counts(store=store, request_id=request.request_id)
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )


def _set_pending_state(*, store: RuntimeStore, request: Any, stage_code: str) -> None:
    _refresh_request_counts(store=store, request_id=request.request_id)
    store.update_task_request(
        request_id=request.request_id,
        status="pending",
        current_stage=stage_code,
        progress_stage=stage_code,
        error_text="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )


def _stage_api_jobs(*, store: RuntimeStore, request_id: str, stage_code: str, job_code: str = "") -> list[dict[str, Any]]:
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
    return [job for job in jobs if _job_stage_code(job) == stage_code]


def _job_stage_code(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    return str(payload.get("stage_code") or job.get("stage") or "").strip()


def _stage_has_children(*, store: RuntimeStore, request_id: str, stage_code: str, job_code: str) -> bool:
    return bool(_stage_api_jobs(store=store, request_id=request_id, stage_code=stage_code, job_code=job_code))


def _timeout_seconds_for(job_code: str) -> float:
    for rule in SYNC_TK_INFLUENCER_POOL_WORKFLOW.timeout_policy:
        if str(rule.target_code or "") == job_code:
            return float(rule.timeout_seconds)
    return 0.0


def _build_candidate_filter(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    filter_spec = dict(request_payload.get("candidate_filter") or {})
    filter_spec.setdefault("candidate_status", ["", "待查找", "失败重试", "处理中"])
    filter_spec.setdefault("skip_product_status", ["已下架/区域不可售"])
    source_record_ids = list(request_payload.get("source_record_ids") or [])
    if source_record_ids:
        filter_spec["source_record_ids"] = source_record_ids
    return filter_spec


def _source_table_ref_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("source_table_ref"),
        request_payload.get("source_table_url"),
        request_payload.get("table_url"),
    )


def _view_ref_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("view_ref"),
        request_payload.get("view_id"),
        request_payload.get("source_view_ref"),
        request_payload.get("source_view_id"),
    )


def _feishu_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "access_token",
        "access_token_env",
        "feishu_access_token",
        "feishu_access_token_env",
        "table_refs",
        "feishu_table",
        "source_table_url",
        "target_table_url",
    ):
        value = request_payload.get(key)
        if value not in (None, "", {}, []):
            payload[key] = value
    return payload


def _fastmoss_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {})
    phone_env = _first_non_empty(request_payload.get("fastmoss_phone_env"), settings.get("phone_env"))
    password_env = _first_non_empty(request_payload.get("fastmoss_password_env"), settings.get("password_env"))
    phone = _first_non_empty(settings.get("phone"), request_payload.get("fastmoss_phone"), os.environ.get(phone_env, ""))
    password = _first_non_empty(
        settings.get("password"),
        request_payload.get("fastmoss_password"),
        os.environ.get(password_env, ""),
    )
    if phone:
        settings["phone"] = phone
    if password:
        settings["password"] = password
    for source_key, target_key in (
        ("fastmoss_region", "region"),
        ("fastmoss_base_url", "base_url"),
        ("fastmoss_timeout", "timeout"),
        ("fastmoss_window_days", "window_days"),
        ("fastmoss_ensure_logged_in", "ensure_logged_in"),
        ("verify_fastmoss_login", "ensure_logged_in"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", {}, []):
            settings.setdefault(target_key, value)
    if "live_fetch" not in settings and settings:
        settings["live_fetch"] = True
    return {"fastmoss": settings} if settings else {}


def _relation_policy_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = dict(request_payload.get("relation_policy") or {})
    for key in ("creator_sold_count_min", "creator_follower_count_min"):
        value = request_payload.get(key)
        if value not in (None, ""):
            policy.setdefault(key, value)
    return policy


def _creator_relation_policy_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = _relation_policy_from_request(request_payload)
    policy.setdefault("include_source_product_relation", True)
    if "min_source_product_sold_count" not in policy and policy.get("creator_sold_count_min") not in (None, ""):
        policy["min_source_product_sold_count"] = policy["creator_sold_count_min"]
    return policy


def _creator_fetch_plan_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    fetch_plan = dict(request_payload.get("creator_fetch_plan") or request_payload.get("fetch_plan") or {})
    fetch_plan.setdefault("date_type", request_payload.get("fastmoss_window_days") or 28)
    fetch_plan.setdefault(
        "endpoints",
        ["base_info", "author_index", "stat_info", "contact", "cargo_summary", "shop_list", "goods_list", "video_list"],
    )
    return fetch_plan


def _creator_detail_level_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("creator_detail_level"),
        request_payload.get("detail_level"),
        "profile_metrics_contact_goods_video",
    )


def _candidate_business_context(candidate: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(candidate.get("candidate_row") or {})
    business_fields = dict(row.get("business_fields") or {})
    source_context = dict(row.get("source_context") or {})
    source_fields = dict(source_context.get("source_fields") or {})
    return {
        "source_table_ref": _first_non_empty(row.get("source_table_ref"), source_context.get("source_table_ref")),
        "holiday": _first_non_empty(business_fields.get("holiday"), source_fields.get("节日")),
        "product_status": _first_non_empty(business_fields.get("product_status"), source_fields.get("商品状态")),
        "source_product_images": _source_product_images_from_fields(source_fields),
    }


def _product_job_business_context(product_job: Mapping[str, Any]) -> dict[str, Any]:
    source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
    candidate_row = dict(source_context.get("candidate_row") or {})
    business_fields = dict(candidate_row.get("business_fields") or {})
    nested_source_context = dict(candidate_row.get("source_context") or {})
    source_fields = dict(nested_source_context.get("source_fields") or {})
    return {
        "source_table_ref": _first_non_empty(source_context.get("source_table_ref"), candidate_row.get("source_table_ref")),
        "holiday": _first_non_empty(source_context.get("holiday"), business_fields.get("holiday"), source_fields.get("节日")),
        "product_status": _first_non_empty(source_context.get("product_status"), business_fields.get("product_status"), source_fields.get("商品状态")),
        "source_product_images": source_context.get("source_product_images") or _source_product_images_from_fields(source_fields),
    }


def _creator_candidate_business_context(creator: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(creator.get("metrics") or {})
    return {
        "matched_product_sold_count": _first_non_empty(
            metrics.get("sold_count"),
            creator.get("sold_count"),
            creator.get("product_sold_count"),
        ),
        "candidate_follower_count": _first_non_empty(
            metrics.get("follower_count"),
            creator.get("follower_count"),
            creator.get("fans_count"),
        ),
    }


def _write_record_business_context(source_context: Mapping[str, Any], result_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "holiday": _first_non_empty(source_context.get("holiday")),
        "matched_product_sold_count": _first_non_empty(source_context.get("matched_product_sold_count")),
        "source_product_images": source_context.get("source_product_images") or [],
        "quality": dict(result_payload.get("quality") or {}),
    }


def _source_product_images_from_fields(source_fields: Mapping[str, Any]) -> list[Any]:
    for key in ("图片", "商品图片", "带货商品图", "image", "image_url"):
        value = source_fields.get(key)
        if isinstance(value, list):
            return list(value)
        if value not in (None, "", {}, []):
            return [value]
    return []


def _fact_db_url_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_db_url"),
        os.environ.get("TK_FACT_DB_URL", ""),
        os.environ.get("BUSINESS_EXECUTION_CONTROL_DB_URL", ""),
        os.environ.get("EXECUTION_CONTROL_DB_URL", ""),
    )


def _normalize_product_identity(raw_identity: Any, fallback_row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_identity, Mapping):
        identity = dict(raw_identity)
    else:
        identity = {}
    product_id = _first_non_empty(identity.get("product_id"), fallback_row.get("product_id"))
    if product_id:
        identity["product_id"] = product_id
    product_url = _first_non_empty(identity.get("product_url"), fallback_row.get("product_url"))
    if product_url:
        identity["product_url"] = product_url
    return identity


def _normalize_creator_identity(raw_identity: Any, fallback_row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_identity, Mapping):
        identity = dict(raw_identity)
    else:
        identity = {}
    creator_id = _first_non_empty(
        identity.get("creator_id"),
        fallback_row.get("creator_id"),
        fallback_row.get("influencer_id"),
    )
    if creator_id:
        identity["creator_id"] = creator_id
    uid = _first_non_empty(identity.get("uid"), fallback_row.get("uid"), fallback_row.get("author_uid"))
    if uid:
        identity["uid"] = uid
    unique_id = _first_non_empty(identity.get("unique_id"), fallback_row.get("unique_id"), fallback_row.get("author_unique_id"))
    if unique_id:
        identity["unique_id"] = unique_id
    profile_url = _first_non_empty(identity.get("profile_url"), fallback_row.get("profile_url"), fallback_row.get("author_url"))
    if profile_url:
        identity["profile_url"] = profile_url
    return identity


def _build_product_job_context(*, request: Any, candidate: Mapping[str, Any], stage_code: str) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "task_code": TASK_CODE,
        "workflow_code": WORKFLOW_CODE,
        "stage_code": stage_code,
        "source_record_id": candidate["source_record_id"],
        "product_id": candidate["product_id"],
        "product_identity": dict(candidate["product_identity"]),
    }


def _product_group_key(*, source_record_id: str, product_id: str) -> str:
    return f"{source_record_id}:{product_id}"


def _job_product_key(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    source_context = dict(payload.get("source_context") or {})
    source_record_id = _first_non_empty(source_context.get("source_record_id"))
    product_id = _first_non_empty(
        source_context.get("product_id"),
        (payload.get("product_identity") or {}).get("product_id") if isinstance(payload.get("product_identity"), Mapping) else "",
    )
    if not source_record_id or not product_id:
        first_record = _first_payload_record(payload)
        source_record_id = _first_non_empty(
            source_record_id,
            first_record.get("source_record_id"),
            dict(payload.get("idempotency_context") or {}).get("source_record_id"),
        )
        product_id = _first_non_empty(
            product_id,
            first_record.get("product_id"),
            dict(payload.get("idempotency_context") or {}).get("product_id"),
        )
        product_key = _first_non_empty(
            first_record.get("product_key"),
            payload.get("product_key"),
            payload.get("business_entity_key"),
        )
        if product_key:
            return product_key
    if not source_record_id or not product_id:
        return ""
    return _product_group_key(source_record_id=source_record_id, product_id=product_id)


def _job_product_id(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    source_context = dict(payload.get("source_context") or {})
    return _first_non_empty(
        source_context.get("product_id"),
        dict(payload.get("product_identity") or {}).get("product_id") if isinstance(payload.get("product_identity"), Mapping) else "",
        dict(payload.get("idempotency_context") or {}).get("product_id"),
    )


def _successful_fact_persist_keys(fact_jobs: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for job in fact_jobs:
        if extract_handler_result_status(job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        payload = dict(job.get("payload") or {})
        idempotency_context = dict(payload.get("idempotency_context") or {})
        if idempotency_context.get("fact_subject") != "creator":
            continue
        key = _creator_fact_key(
            _first_non_empty(idempotency_context.get("source_record_id")),
            _first_non_empty(idempotency_context.get("product_id")),
            _first_non_empty(idempotency_context.get("creator_id")),
        )
        if key:
            keys.add(key)
    return keys


def _creator_fact_key(source_record_id: str, product_id: str, creator_id: str) -> str:
    if not source_record_id or not product_id or not creator_id:
        return ""
    return f"{source_record_id}:{product_id}:{creator_id}"


def _first_payload_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    records = list(payload.get("records") or [])
    if not records:
        return {}
    first_record = records[0]
    return dict(first_record) if isinstance(first_record, Mapping) else {}


def _count_product_group_statuses(group_summaries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in group_summaries:
        status = str(group.get("final_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _derive_final_status(group_summaries: list[dict[str, Any]]) -> str:
    if not group_summaries:
        return "failed"
    status_counts = _count_product_group_statuses(group_summaries)
    if status_counts.get("failed", 0) == len(group_summaries):
        return "failed"
    if status_counts.get("failed", 0) > 0 or status_counts.get("partial_success", 0) > 0:
        return "partial_success"
    return "success"


def _build_summary_warnings(group_summaries: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for group in group_summaries:
        for warning in list(group.get("warnings") or []):
            if isinstance(warning, str) and warning and warning not in warnings:
                warnings.append(warning)
    return warnings


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = [
    "SYNC_TK_INFLUENCER_POOL_WORKFLOW",
    "advance_stage",
    "advance_sync_tk_influencer_pool_request",
    "dispatch_sync_tk_influencer_pool_request",
    "finalize_request",
    "finalize_sync_tk_influencer_pool_request",
    "PERSIST_FACTS_STAGE_CODE",
    "release_request_after_child_completion",
    "release_sync_tk_influencer_pool_request",
]
