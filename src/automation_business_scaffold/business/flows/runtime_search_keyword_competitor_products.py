from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from automation_business_scaffold.business.flows.runtime_common import (
    KEYWORD_TASK_CODE,
    build_outbox_message_text,
)
from automation_business_scaffold.business.workflow_defs import WorkflowDefinition
from automation_business_scaffold.business.workflow_defs.execution_helpers import (
    all_child_records as _all_child_records,
    any_api_jobs_active as _any_api_jobs_active,
    any_browser_executions_active as _any_browser_executions_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_projection_record,
    build_projection_write_payload,
    build_stage_local_dedupe_key,
    compute_final_status,
    extract_effective_result_payload,
    extract_handler_result_status,
    has_active_records as _has_active_children,
    recover_browser_fallback_resume_stage,
    render_job_keys,
    select_latest_successful_api_job,
    stage_child_records as _stage_child_records,
    summarize_stage_children,
    summarize_child_outcomes,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

OPTIONAL_FINAL_STATUS_CODES = ("tiktok_product_browser_fetch",)


def advance_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    if request.task_code != KEYWORD_TASK_CODE:
        raise ValueError(f"Unsupported task_code for keyword runtime: {request.task_code}")
    if stage_code == "search_product_candidates":
        return _advance_search_product_candidates(store=store, request=request, workflow=workflow)
    if stage_code == "process_product_candidates":
        return _advance_process_product_candidates(store=store, request=request, workflow=workflow)
    if stage_code == "insert_seed_rows":
        return _advance_insert_seed_rows(store=store, request=request, workflow=workflow)
    if stage_code == "dispatch_product_collection":
        return _advance_dispatch_product_collection(store=store, request=request, workflow=workflow)
    if stage_code == "collect_product_data":
        return _advance_collect_product_data(store=store, request=request, workflow=workflow)
    if stage_code == "browser_fallback":
        return _advance_browser_fallback(store=store, request=request, workflow=workflow)
    if stage_code == "sync_media":
        return _advance_sync_media(store=store, request=request, workflow=workflow)
    if stage_code == "persist_facts":
        return _advance_persist_facts(store=store, request=request, workflow=workflow)
    if stage_code == "writeback_competitor_rows":
        return _advance_writeback_competitor_rows(store=store, request=request, workflow=workflow)
    if stage_code == workflow.summary_policy.summary_stage_code:
        return {"action": "advance", "next_stage": workflow.summary_policy.summary_stage_code}
    raise KeyError(f"Unsupported stage_code for keyword runtime: {stage_code}")


def finalize_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_contexts = _candidate_contexts(store=store, request_id=request.request_id)
    row_results = [_build_row_result(store=store, request_id=request.request_id, candidate_context=row) for row in candidate_contexts]
    child_records = _all_child_records(store=store, request_id=request.request_id)
    child_outcome = summarize_child_outcomes(child_records, optional_codes=OPTIONAL_FINAL_STATUS_CODES)
    computed_status = compute_final_status(
        workflow.summary_policy,
        child_records=child_records,
        optional_codes=OPTIONAL_FINAL_STATUS_CODES,
        explicit_status=str((force_result or {}).get("final_status") or ""),
    )
    final_status = _derive_final_status(row_results=row_results, fallback_status=computed_status)
    warnings = list(dict.fromkeys(_collect_warnings(row_results)))

    summary = {
        "final_status": final_status,
        "search_query": str(request.payload.get("search_query") or ""),
        "candidate_total_count": len(candidate_contexts),
        "child_total_count": int(child_outcome["total_count"]),
        "child_success_count": int(child_outcome["success_count"]),
        "child_failed_count": int(child_outcome["failed_count"]),
        "child_skipped_count": int(child_outcome["skipped_count"]),
        "row_success_count": sum(1 for item in row_results if item["row_status"] == "success"),
        "row_failed_count": sum(1 for item in row_results if item["row_status"] == "failed"),
        "row_partial_count": sum(1 for item in row_results if item["row_status"] == "partial_success"),
        "warnings": warnings,
    }
    result = {
        "workflow_code": workflow.workflow_code,
        "search_query": str(request.payload.get("search_query") or ""),
        "candidate_total_count": len(candidate_contexts),
        "seed_total_count": len(_seed_contexts(store=store, request_id=request.request_id)),
        "row_results": row_results,
        "stage_summary": {
            stage.stage_code: summarize_stage_children(
                store,
                request_id=request.request_id,
                stage_code=stage.stage_code,
                optional_codes=OPTIONAL_FINAL_STATUS_CODES,
            )
            for stage in workflow.stages
            if stage.execution_mode == "worker_jobs"
        },
    }
    if force_result:
        result["force_result"] = dict(force_result)

    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=workflow.summary_policy.summary_stage_code,
        progress_stage=workflow.summary_policy.summary_stage_code,
        summary=summary,
        result=result,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        error_text="",
        error_type="",
        error_code="",
        dead_letter_reason="",
        finished_at=time.time(),
    )
    outbox = store.create_notification_outbox(
        channel_code=str(request.source_channel_code or "noop"),
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(request.reply_target or ""),
        payload={
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": workflow.workflow_code,
            "summary_payload": summary,
            "result": result,
            "message_text": build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
            ),
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    return {
        "action": "finalized",
        "request_id": request.request_id,
        "request_status": updated.status,
        "current_stage": updated.current_stage,
        "summary": updated.summary,
        "result": updated.result,
        "task_request": updated.to_dict(),
        "outbox": [outbox.to_dict()],
    }


def release_request_after_child_completion(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != KEYWORD_TASK_CODE:
        return []
    workflow = _require_keyword_workflow()
    current_stage = str(request.current_stage or "").strip()
    if current_stage == workflow.summary_policy.summary_stage_code:
        recovered_stage = _recover_stage_after_browser_summary_promotion(store=store, request_id=request_id)
        if recovered_stage:
            current_stage = recovered_stage
            request = store.update_task_request(
                request_id=request_id,
                status="pending",
                current_stage=current_stage,
                progress_stage=current_stage,
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                last_progress_at=time.time(),
            )
    if not current_stage:
        return []
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


def _recover_stage_after_browser_summary_promotion(
    *,
    store: RuntimeStore,
    request_id: str,
) -> str:
    return recover_browser_fallback_resume_stage(
        store,
        request_id=request_id,
        current_stage="ready_for_summary",
        summary_stage_code="ready_for_summary",
        continuation_stage_codes=("sync_media", "persist_facts", "writeback_competitor_rows"),
        continuation_candidate_ready=True,
    )


def _advance_search_product_candidates(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "search_product_candidates"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    job_def = workflow.require_job("fastmoss_product_search")
    if not jobs:
        search_query = str(
            request.payload.get("search_query")
            or request.payload.get("search_keyword")
            or request.payload.get("keyword")
            or ""
        ).strip()
        filters = dict(request.payload.get("filters") or {})
        output_conditions = dict(request.payload.get("output_conditions") or {})
        sales_7d_threshold = str(request.payload.get("sales_7d_threshold") or "").strip()
        if sales_7d_threshold:
            business_conditions = dict(output_conditions.get("business_conditions") or {})
            business_conditions.setdefault("min_day7_sold_count", sales_7d_threshold)
            output_conditions["business_conditions"] = business_conditions
        max_candidates = _positive_int_param(
            request.payload.get("max_candidates") or output_conditions.get("max_candidates"),
            20,
        )
        payload = {
            "stage_code": stage_code,
            "search_mode": "keyword",
            "keyword": search_query,
            "search_query": search_query,
            "filters": filters,
            "limit": max_candidates,
            "condition_context": output_conditions,
            "output_conditions": output_conditions,
            "sort": {
                "field": "day7_sold_count",
                "direction": "desc",
                "source_order": str(request.payload.get("fastmoss_search_order") or "2,2"),
            },
            "pagination": {
                "page": _positive_int_param(request.payload.get("fastmoss_search_page"), 1),
                "page_size": _positive_int_param(request.payload.get("fastmoss_search_page_size"), 10),
                "max_pages": _positive_int_param(request.payload.get("fastmoss_search_max_pages"), 50),
                "stop_when_no_new_product": True,
            },
            "session_policy": {
                "require_login": True,
                "degraded_preview_allowed": _bool_param(
                    request.payload.get("degraded_preview_allowed"),
                    False,
                ),
            },
            "raw_capture_policy": {"store_raw_response": True},
            "search_digest": _search_digest(search_query=search_query, filters=filters),
        }
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
        for source_key, target_key in (
            ("execution_control_artifact_root", "artifact_root"),
            ("execution_control_artifact_bucket", "artifact_bucket"),
            ("execution_control_artifact_store_provider", "artifact_store_provider"),
            ("execution_control_artifact_object_prefix", "artifact_object_prefix"),
            ("execution_control_minio_endpoint", "minio_endpoint"),
            ("execution_control_minio_access_key", "minio_access_key"),
            ("execution_control_minio_secret_key", "minio_secret_key"),
            ("execution_control_minio_region", "minio_region"),
            ("execution_control_minio_secure", "minio_secure"),
            ("execution_control_minio_create_bucket", "minio_create_bucket"),
        ):
            if request.payload.get(source_key) not in (None, ""):
                payload[target_key] = request.payload.get(source_key)
        keys = render_job_keys(
            job_def,
            request.payload,
            payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
            job_code=job_def.job_code,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued FastMoss product search.",
            details={"dispatch_payload": {"fastmoss_product_search": dispatch}},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for candidate search to finish.")

    payload = extract_effective_result_payload(select_latest_successful_api_job(jobs, "fastmoss_product_search"))
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "raw_candidate_count": len(candidates),
            "search_job_statuses": [str(job.get("status") or "") for job in jobs],
            "condition_context": dict(payload.get("condition_context") or {}),
        },
    )
    return {
        "action": "advance",
        "next_stage": "process_product_candidates",
        "details": {"raw_candidate_count": len(candidates)},
    }


def _fastmoss_search_settings_from_request_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {}) if isinstance(request_payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("browser_cookies", "browser_cookies"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", [], {}):
            settings.setdefault(target_key, value)
    settings.setdefault("live_fetch", True)
    settings.setdefault("ensure_logged_in", True)
    return {key: value for key, value in settings.items() if value not in (None, "", [], {})}


def _positive_int_param(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _advance_process_product_candidates(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    del workflow
    stage_code = "process_product_candidates"
    search_jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code="search_product_candidates")
    search_job = select_latest_successful_api_job(search_jobs, "fastmoss_product_search")
    search_payload = extract_effective_result_payload(search_job)
    candidates = _normalize_search_candidates(
        search_payload.get("candidates"),
        search_query=str(request.payload.get("search_query") or ""),
        output_conditions=dict(request.payload.get("output_conditions") or {}),
        max_candidates=int(request.payload.get("max_candidates") or 0),
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "candidate_contexts": candidates,
            "candidate_total_count": len(candidates),
            "search_condition_context": dict(search_payload.get("condition_context") or {}),
        },
    )
    return {
        "action": "advance",
        "next_stage": "insert_seed_rows",
        "details": {"candidate_total_count": len(candidates)},
    }


def _advance_insert_seed_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "insert_seed_rows"
    candidates = _candidate_contexts(store=store, request_id=request.request_id)
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not candidates:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"seed_contexts": []})
        return {"action": "advance", "next_stage": "dispatch_product_collection", "details": {"seed_row_count": 0}}

    job_def = workflow.require_job("feishu_table_write")
    if not jobs:
        seed_table_ref = str(request.payload.get("seed_table_ref") or "")
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = build_projection_write_payload(
                stage_code=stage_code,
                request_id=request.request_id,
                target_table_ref=seed_table_ref,
                records=[
                    {
                        "business_entity_key": candidate["business_entity_key"],
                        "product_id": candidate.get("product_id") or "",
                        "product_url": candidate.get("normalized_product_url") or candidate.get("product_url") or "",
                        "search_query": candidate.get("search_query") or "",
                        "search_rank": candidate.get("search_rank") or 0,
                        "candidate_key": candidate["candidate_key"],
                    }
                ],
                mapper_code="competitor_seed_projection_mapper",
                write_mode="insert_if_absent",
                request_payload=request.payload,
                candidate_key=candidate["candidate_key"],
                business_entity_key=candidate["business_entity_key"],
            )
            keys = render_job_keys(
                job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=job_def.job_code,
            )
            items.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        keys["dedupe_key"],
                        job_def.job_code,
                        stage_scope=stage_code,
                    ),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=items,
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued seed row writes.",
            details={"dispatch_payload": {"feishu_table_write": dispatch}},
        )

    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for seed row writes to finish.")

    seed_contexts = _build_seed_contexts(candidates=candidates, jobs=jobs)
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "seed_contexts": seed_contexts,
            "seed_row_count": len(seed_contexts),
            "successful_seed_count": sum(1 for item in seed_contexts if item["seed_status"] in {"success", "partial_success"}),
        },
    )
    return {
        "action": "advance",
        "next_stage": "dispatch_product_collection",
        "details": {"seed_row_count": len(seed_contexts)},
    }


def _advance_dispatch_product_collection(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_product_collection"
    seed_contexts = [item for item in _seed_contexts(store=store, request_id=request.request_id) if item["seed_status"] in {"success", "partial_success"}]
    if not seed_contexts:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"collection_contexts": []})
        return {"action": "advance", "next_stage": "collect_product_data", "details": {"dispatched_row_count": 0}}

    tiktok_job_def = workflow.require_job("tiktok_product_request_fetch")
    fastmoss_job_def = workflow.require_job("fastmoss_product_fetch")
    tiktok_jobs: list[dict[str, Any]] = []
    fastmoss_jobs: list[dict[str, Any]] = []
    for seed in seed_contexts:
        tiktok_payload = {
            "stage_code": "collect_product_data",
            "candidate_key": seed["candidate_key"],
            "business_entity_key": seed["business_entity_key"],
            "source_record_id": seed["source_record_id"],
            "product_identity": dict(seed["product_identity"]),
            "normalized_product_url": seed.get("normalized_product_url") or "",
            "source_context": dict(seed["source_context"]),
            "fallback_allowed": True,
        }
        tiktok_keys = render_job_keys(
            tiktok_job_def,
            request.payload,
            seed,
            tiktok_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code="collect_product_data",
            job_code=tiktok_job_def.job_code,
        )
        tiktok_jobs.append(
            {
                "business_key": tiktok_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(tiktok_keys["dedupe_key"], tiktok_job_def.job_code),
                "payload": tiktok_payload,
                "max_execution_seconds": _timeout_seconds(workflow, tiktok_job_def.job_code),
            }
        )

        fastmoss_payload = {
            "stage_code": "collect_product_data",
            "candidate_key": seed["candidate_key"],
            "business_entity_key": seed["business_entity_key"],
            "source_record_id": seed["source_record_id"],
            "product_identity": dict(seed["product_identity"]),
            "source_context": dict(seed["source_context"]),
            "detail_level": "standard",
        }
        fastmoss_keys = render_job_keys(
            fastmoss_job_def,
            request.payload,
            seed,
            fastmoss_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code="collect_product_data",
            job_code=fastmoss_job_def.job_code,
        )
        fastmoss_jobs.append(
            {
                "business_key": fastmoss_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(fastmoss_keys["dedupe_key"], fastmoss_job_def.job_code),
                "payload": fastmoss_payload,
                "max_execution_seconds": _timeout_seconds(workflow, fastmoss_job_def.job_code),
            }
        )

    tiktok_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=tiktok_job_def.job_code,
        jobs=tiktok_jobs,
    )
    fastmoss_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=fastmoss_job_def.job_code,
        jobs=fastmoss_jobs,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "collection_contexts": seed_contexts,
            "dispatched_row_count": len(seed_contexts),
            "tiktok_dispatch": tiktok_dispatch,
            "fastmoss_dispatch": fastmoss_dispatch,
        },
    )
    return {
        "action": "advance",
        "next_stage": "collect_product_data",
        "details": {
            "dispatched_row_count": len(seed_contexts),
            "tiktok_created_count": int(tiktok_dispatch["created_count"]),
            "fastmoss_created_count": int(fastmoss_dispatch["created_count"]),
        },
    }


def _advance_collect_product_data(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    del workflow
    stage_code = "collect_product_data"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        return {"action": "advance", "next_stage": "sync_media", "details": {"dispatched_row_count": 0}}
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for product collection jobs to finish.")

    fallback_candidates = _browser_fallback_candidates(store=store, request_id=request.request_id)
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "collect_job_count": len(jobs),
            "fallback_candidate_count": len(fallback_candidates),
        },
    )
    if fallback_candidates:
        return {
            "action": "advance",
            "next_stage": "browser_fallback",
            "details": {"fallback_candidate_count": len(fallback_candidates)},
        }
    return {"action": "advance", "next_stage": "sync_media", "details": {"collect_job_count": len(jobs)}}


def _advance_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "browser_fallback"
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    fallback_candidates = _browser_fallback_candidates(store=store, request_id=request.request_id)
    if not fallback_candidates and not executions:
        return {"action": "advance", "next_stage": "sync_media", "details": {"fallback_candidate_count": 0}}
    if not executions and fallback_candidates:
        job_def = workflow.require_job("tiktok_product_browser_fetch")
        items: list[dict[str, Any]] = []
        for candidate in fallback_candidates:
            payload = {
                "stage_code": stage_code,
                "candidate_key": candidate["candidate_key"],
                "business_entity_key": candidate["business_entity_key"],
                "source_record_id": candidate["source_record_id"],
                "product_identity": dict(candidate["product_identity"]),
                "normalized_product_url": candidate.get("normalized_product_url") or "",
                "fallback_source_job_id": candidate.get("fallback_source_job_id") or "",
            }
            keys = render_job_keys(
                job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                item_code=job_def.job_code,
            )
            items.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        keys["dedupe_key"],
                        job_def.job_code,
                        stage_scope=stage_code,
                    ),
                    "resource_code": _browser_resource_code(candidate),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )
        dispatch = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code=job_def.job_code,
            workflow_code=workflow.workflow_code,
            items=items,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"browser_dispatch": dispatch, "fallback_candidate_count": len(fallback_candidates)},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued browser fallback executions.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for browser fallback executions to finish.")
    return {"action": "advance", "next_stage": "sync_media", "details": {"execution_count": len(executions)}}


def _advance_sync_media(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "sync_media"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _media_sync_candidates(store=store, request_id=request.request_id)
        if not candidates:
            return {"action": "advance", "next_stage": "persist_facts", "details": {"media_candidate_count": 0}}
        job_def = workflow.require_job("media_asset_sync")
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            asset_refs = list(candidate.get("asset_refs") or [])
            payload = {
                "stage_code": stage_code,
                "candidate_key": candidate["candidate_key"],
                "business_entity_key": candidate["business_entity_key"],
                "source_record_id": candidate["source_record_id"],
                "entity_key": candidate["business_entity_key"],
                "asset_source": _asset_source(asset_refs[0]) if asset_refs else candidate["business_entity_key"],
                "asset_refs": asset_refs,
                "entity_keys": [candidate["business_entity_key"]],
                "source_context": dict(candidate["source_context"]),
            }
            keys = render_job_keys(
                job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=job_def.job_code,
            )
            items.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=items,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"media_candidate_count": len(candidates), "media_dispatch": dispatch},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued media sync jobs.",
            details={"media_created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for media sync jobs to finish.")
    return {"action": "advance", "next_stage": "persist_facts", "details": {"media_job_count": len(jobs)}}


def _advance_persist_facts(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "persist_facts"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _fact_persist_candidates(store=store, request_id=request.request_id)
        if not candidates:
            return {"action": "advance", "next_stage": "writeback_competitor_rows", "details": {"persist_candidate_count": 0}}
        job_def = workflow.require_job("fact_bundle_upsert")
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = {
                "stage_code": stage_code,
                "candidate_key": candidate["candidate_key"],
                "business_entity_key": candidate["business_entity_key"],
                "source_record_id": candidate["source_record_id"],
                "entity_business_keys": candidate["business_entity_key"],
                "observation_at": str(candidate.get("observation_at") or ""),
                "fact_bundle": dict(candidate["fact_bundle"]),
                "mapper_code": "competitor_fact_relation_mapper",
                "observation_context": {
                    "source_record_id": candidate["source_record_id"],
                    "search_query": candidate.get("search_query") or "",
                },
            }
            keys = render_job_keys(
                job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=job_def.job_code,
            )
            items.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=items,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"persist_candidate_count": len(candidates), "fact_dispatch": dispatch},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued fact persistence jobs.",
            details={"fact_created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for fact persistence jobs to finish.")
    return {"action": "advance", "next_stage": "writeback_competitor_rows", "details": {"persist_job_count": len(jobs)}}


def _advance_writeback_competitor_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "writeback_competitor_rows"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _candidate_contexts(store=store, request_id=request.request_id)
        if not candidates:
            return {"action": "advance", "next_stage": "ready_for_summary", "details": {"writeback_count": 0}}
        job_def = workflow.require_job("feishu_table_write")
        target_table_ref = str(request.payload.get("seed_table_ref") or "")
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            seed_context = _seed_context_by_candidate_key(store=store, request_id=request.request_id).get(candidate["candidate_key"], {})
            source_record_id = str(seed_context.get("source_record_id") or candidate["candidate_key"])
            payload = build_projection_write_payload(
                stage_code=stage_code,
                request_id=request.request_id,
                target_table_ref=target_table_ref,
                records=[_build_writeback_projection(store=store, request_id=request.request_id, candidate_context=candidate)],
                mapper_code="competitor_table_projection_mapper",
                write_mode="upsert",
                source_record_id=source_record_id,
                candidate_key=candidate["candidate_key"],
                business_entity_key=candidate["business_entity_key"],
            )
            keys = render_job_keys(
                job_def,
                request.payload,
                candidate,
                seed_context,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=job_def.job_code,
            )
            items.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=items,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"writeback_count": len(items), "writeback_dispatch": dispatch},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued competitor row writeback jobs.",
            details={"writeback_created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row writeback jobs to finish.")
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"writeback_job_count": len(jobs)}}


def _candidate_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    processed = dict(stage_results.get("process_product_candidates") or {})
    candidates = processed.get("candidate_contexts")
    if isinstance(candidates, list):
        return [dict(item) for item in candidates if isinstance(item, Mapping)]

    search_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="search_product_candidates")
    search_job = select_latest_successful_api_job(search_jobs, "fastmoss_product_search")
    search_payload = extract_effective_result_payload(search_job)
    return _normalize_search_candidates(
        search_payload.get("candidates"),
        search_query=str(request.payload.get("search_query") or ""),
        output_conditions=dict(request.payload.get("output_conditions") or {}),
        max_candidates=int(request.payload.get("max_candidates") or 0),
    )


def _seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    inserted = dict(stage_results.get("insert_seed_rows") or {})
    seeds = inserted.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]
    return _build_seed_contexts(
        candidates=_candidate_contexts(store=store, request_id=request_id),
        jobs=_api_jobs_for_stage(store=store, request_id=request_id, stage_code="insert_seed_rows"),
    )


def _seed_context_by_candidate_key(store: RuntimeStore, *, request_id: str) -> dict[str, dict[str, Any]]:
    return {str(item.get("candidate_key") or ""): item for item in _seed_contexts(store=store, request_id=request_id)}


def _build_seed_contexts(*, candidates: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    job_by_candidate = {
        str((job.get("payload") or {}).get("candidate_key") or ""): job
        for job in jobs
        if str((job.get("payload") or {}).get("candidate_key") or "")
    }
    seed_contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = candidate["candidate_key"]
        job = job_by_candidate.get(candidate_key)
        result_payload = extract_effective_result_payload(job)
        target_record_ids = result_payload.get("target_record_ids") if isinstance(result_payload.get("target_record_ids"), list) else []
        source_record_id = str(target_record_ids[0] if target_record_ids else candidate_key)
        seed_contexts.append(
            {
                **candidate,
                "source_record_id": source_record_id,
                "seed_status": _record_effective_status(job),
                "seed_result": result_payload,
                "target_record_ids": [str(item) for item in target_record_ids],
            }
        )
    return seed_contexts


def _browser_fallback_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seed_by_key = _seed_context_by_candidate_key(store=store, request_id=request_id)
    for job in _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data"):
        if str(job.get("job_code") or "") != "tiktok_product_request_fetch":
            continue
        if not _is_fallback_required(job):
            continue
        payload = dict(job.get("payload") or {})
        candidate_key = str(payload.get("candidate_key") or "")
        seed = dict(seed_by_key.get(candidate_key) or {})
        if not seed:
            seed = _minimal_seed_context(payload)
        result = extract_effective_result_payload(job)
        seed["fallback_source_job_id"] = str(result.get("fallback_source_job_id") or job.get("job_id") or "")
        candidates.append(seed)
    return candidates


def _media_sync_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    browser_by_candidate = {
        str((execution.payload or {}).get("candidate_key") or ""): execution for execution in browser_execs
    }
    tiktok_by_candidate: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        if str(job.get("job_code") or "") != "tiktok_product_request_fetch":
            continue
        candidate_key = str((job.get("payload") or {}).get("candidate_key") or "")
        if candidate_key:
            tiktok_by_candidate[candidate_key] = job

    candidates: list[dict[str, Any]] = []
    for seed in _seed_contexts(store=store, request_id=request_id):
        candidate_key = seed["candidate_key"]
        tiktok_job = tiktok_by_candidate.get(candidate_key)
        browser_execution = browser_by_candidate.get(candidate_key)
        tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
        normalized_product = dict(tiktok_result.get("normalized_product_result") or {})
        asset_refs = _collect_asset_refs(normalized_product)
        if not asset_refs:
            continue
        candidates.append(
            {
                **seed,
                "asset_refs": asset_refs,
            }
        )
    return candidates


def _fact_persist_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    browser_by_candidate = {
        str((execution.payload or {}).get("candidate_key") or ""): execution for execution in browser_execs
    }
    tiktok_by_candidate: dict[str, dict[str, Any]] = {}
    fastmoss_by_candidate: dict[str, dict[str, Any]] = {}
    media_by_candidate: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        candidate_key = str((job.get("payload") or {}).get("candidate_key") or "")
        if not candidate_key:
            continue
        if str(job.get("job_code") or "") == "tiktok_product_request_fetch":
            tiktok_by_candidate[candidate_key] = job
        if str(job.get("job_code") or "") == "fastmoss_product_fetch":
            fastmoss_by_candidate[candidate_key] = job
    for job in media_jobs:
        candidate_key = str((job.get("payload") or {}).get("candidate_key") or "")
        if candidate_key:
            media_by_candidate[candidate_key] = job

    candidates: list[dict[str, Any]] = []
    for seed in _seed_contexts(store=store, request_id=request_id):
        candidate_key = seed["candidate_key"]
        tiktok_result = _effective_tiktok_result(
            tiktok_job=tiktok_by_candidate.get(candidate_key),
            browser_execution=browser_by_candidate.get(candidate_key),
        )
        fastmoss_result = extract_effective_result_payload(fastmoss_by_candidate.get(candidate_key))
        media_result = extract_effective_result_payload(media_by_candidate.get(candidate_key))
        product_result = dict(tiktok_result.get("normalized_product_result") or {})
        fact_bundle = {
            "product_identity": dict(seed["product_identity"]),
            "tiktok_product": product_result,
            "fastmoss_product": dict(fastmoss_result.get("product_fact_bundle") or {}),
            "media_assets": list(media_result.get("synced_assets") or []),
            "seed_context": {
                "source_record_id": seed["source_record_id"],
                "candidate_key": candidate_key,
            },
        }
        has_payload = bool(product_result or fact_bundle["fastmoss_product"] or fact_bundle["media_assets"])
        if not has_payload:
            continue
        candidates.append(
            {
                **seed,
                "fact_bundle": fact_bundle,
                "observation_at": str(int(time.time())),
            }
        )
    return candidates


def _normalize_search_candidates(
    raw_candidates: Any,
    *,
    search_query: str,
    output_conditions: Mapping[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_candidates, start=1):
        if not isinstance(row, Mapping):
            continue
        product_identity = _resolve_product_identity(row)
        raw_entity_key = str(
            product_identity.get("product_id")
            or product_identity.get("normalized_product_url")
            or product_identity.get("product_url")
            or product_identity.get("product_key")
            or row.get("candidate_key")
            or index
        )
        business_entity_key = _product_business_entity_key(raw_entity_key)
        if not business_entity_key or business_entity_key in seen:
            continue
        candidate_context = {
            "candidate_key": business_entity_key,
            "business_entity_key": business_entity_key,
            "product_identity": product_identity,
            "product_id": str(product_identity.get("product_id") or ""),
            "product_url": str(product_identity.get("product_url") or ""),
            "normalized_product_url": str(product_identity.get("normalized_product_url") or ""),
            "search_query": search_query,
            "search_rank": int(row.get("rank") or index),
            "source_context": dict(row),
        }
        if not _candidate_allowed(candidate_context, output_conditions):
            continue
        normalized.append(candidate_context)
        seen.add(business_entity_key)
        if max_candidates > 0 and len(normalized) >= max_candidates:
            break
    return normalized


def _candidate_allowed(candidate: Mapping[str, Any], conditions: Mapping[str, Any]) -> bool:
    allowed_ids = {str(item) for item in conditions.get("allowed_product_ids") or [] if str(item)}
    excluded_ids = {str(item) for item in conditions.get("exclude_product_ids") or [] if str(item)}
    require_url = bool(conditions.get("require_product_url", False))
    product_id = str(candidate.get("product_id") or "")
    normalized_product_url = str(candidate.get("normalized_product_url") or "")
    if allowed_ids and product_id not in allowed_ids:
        return False
    if excluded_ids and product_id in excluded_ids:
        return False
    if require_url and not normalized_product_url:
        return False
    return True


def _build_row_result(
    *,
    store: RuntimeStore,
    request_id: str,
    candidate_context: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_key = str(candidate_context.get("candidate_key") or "")
    seed_context = _seed_context_by_candidate_key(store=store, request_id=request_id).get(candidate_key, {})
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    fact_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="persist_facts")
    write_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="writeback_competitor_rows")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")

    tiktok_job = _latest_candidate_job(collect_jobs, candidate_key=candidate_key, job_code="tiktok_product_request_fetch")
    fastmoss_job = _latest_candidate_job(collect_jobs, candidate_key=candidate_key, job_code="fastmoss_product_fetch")
    media_job = _latest_candidate_job(media_jobs, candidate_key=candidate_key, job_code="media_asset_sync")
    fact_job = _latest_candidate_job(fact_jobs, candidate_key=candidate_key, job_code="fact_bundle_upsert")
    write_job = _latest_candidate_job(write_jobs, candidate_key=candidate_key, job_code="feishu_table_write")
    browser_execution = _latest_candidate_execution(browser_execs, candidate_key=candidate_key)

    row_status = _derive_row_status(
        seed_status=str(seed_context.get("seed_status") or ""),
        tiktok_job=tiktok_job,
        fastmoss_job=fastmoss_job,
        browser_execution=browser_execution,
        media_job=media_job,
        fact_job=fact_job,
        write_job=write_job,
    )
    return {
        "candidate_key": candidate_key,
        "product_id": str(candidate_context.get("product_id") or ""),
        "source_record_id": str(seed_context.get("source_record_id") or ""),
        "row_status": row_status,
        "seed_status": str(seed_context.get("seed_status") or ""),
        "tiktok_status": _record_effective_status(tiktok_job),
        "fastmoss_status": _record_effective_status(fastmoss_job),
        "browser_status": _record_effective_status(browser_execution),
        "media_status": _record_effective_status(media_job),
        "fact_status": _record_effective_status(fact_job),
        "writeback_status": _record_effective_status(write_job),
    }


def _build_writeback_projection(
    *,
    store: RuntimeStore,
    request_id: str,
    candidate_context: Mapping[str, Any],
) -> dict[str, Any]:
    row_result = _build_row_result(store=store, request_id=request_id, candidate_context=candidate_context)
    return build_projection_record(
        request_id=request_id,
        source_record_id=str(row_result["source_record_id"]),
        candidate_key=str(row_result["candidate_key"]),
        product_id=str(row_result["product_id"]),
        product_url=str(candidate_context.get("normalized_product_url") or candidate_context.get("product_url") or ""),
        refresh_status=str(row_result["row_status"]),
        details=row_result,
    )


def _derive_row_status(
    *,
    seed_status: str,
    tiktok_job: Mapping[str, Any] | None,
    fastmoss_job: Mapping[str, Any] | None,
    browser_execution: Any,
    media_job: Mapping[str, Any] | None,
    fact_job: Mapping[str, Any] | None,
    write_job: Mapping[str, Any] | None,
) -> str:
    statuses = [
        seed_status,
        _record_effective_status(tiktok_job),
        _record_effective_status(fastmoss_job),
        _record_effective_status(browser_execution),
        _record_effective_status(media_job),
        _record_effective_status(fact_job),
        _record_effective_status(write_job),
    ]
    if seed_status == "skipped" and not any(status for status in statuses[1:]):
        return "skipped"
    if "success" in {_record_effective_status(write_job), _record_effective_status(fact_job)} and "failed" not in statuses:
        return "success"
    if "success" in statuses or "partial_success" in statuses:
        if "failed" in statuses or "fallback_required" in statuses:
            return "partial_success"
        if _record_effective_status(write_job) == "success" or _record_effective_status(fact_job) == "success":
            return "success"
        return "partial_success"
    if "failed" in statuses or seed_status == "failed":
        return "failed"
    if all(not status for status in statuses):
        return "skipped"
    return "failed"


def _derive_final_status(*, row_results: list[dict[str, Any]], fallback_status: str) -> str:
    if not row_results:
        return "failed"
    statuses = {str(item.get("row_status") or "") for item in row_results}
    if statuses <= {"success"}:
        return "success"
    if "success" in statuses or "partial_success" in statuses:
        return "partial_success"
    if fallback_status in {"success", "partial_success", "failed"}:
        return fallback_status
    return "failed"


def _latest_candidate_job(
    jobs: list[dict[str, Any]],
    *,
    candidate_key: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("candidate_key") or "") != candidate_key:
            continue
        selected = job
    return selected


def _latest_candidate_execution(
    executions: list[Any],
    *,
    candidate_key: str,
) -> Any:
    selected = None
    for execution in executions:
        if str((execution.payload or {}).get("candidate_key") or "") != candidate_key:
            continue
        selected = execution
    return selected


def _effective_tiktok_result(
    *,
    tiktok_job: Mapping[str, Any] | None,
    browser_execution: Any | None,
) -> dict[str, Any]:
    browser_payload = extract_effective_result_payload(browser_execution)
    if isinstance(browser_payload.get("normalized_product_result"), Mapping):
        return browser_payload
    return extract_effective_result_payload(tiktok_job)


def _collect_asset_refs(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_assets: list[Any] = []
    media_assets = product_result.get("media_assets")
    if isinstance(media_assets, list):
        raw_assets.extend(media_assets)
    images = product_result.get("images")
    if isinstance(images, list):
        raw_assets.extend(images)
    videos = product_result.get("videos")
    if isinstance(videos, list):
        raw_assets.extend(videos)

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_assets:
        if isinstance(item, Mapping):
            asset = dict(item)
        elif isinstance(item, str):
            asset = {"source_url": item, "source_type": "image"}
        else:
            continue
        source_url = str(asset.get("source_url") or asset.get("url") or "").strip()
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        normalized.append(
            {
                "source_url": source_url,
                "source_type": str(asset.get("source_type") or asset.get("type") or "image"),
                "mime_type": str(asset.get("mime_type") or ""),
            }
        )
    return normalized


def _is_fallback_required(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    if extract_handler_result_status(job) == "fallback_required":
        return True
    payload = extract_effective_result_payload(job)
    return bool(payload.get("fallback_required"))


def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if isinstance(record, Mapping):
        status = str(record.get("status") or "")
        handler_status = extract_handler_result_status(record)
        return handler_status or status
    status = str(getattr(record, "status", "") or "")
    handler_status = extract_handler_result_status(record)
    return handler_status or status


def _collect_warnings(row_results: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for row in row_results:
        if row["row_status"] == "partial_success":
            warnings.append(f"partial_success:{row['candidate_key']}")
        if row["row_status"] == "failed":
            warnings.append(f"failed:{row['candidate_key']}")
    return warnings


def _waiting(*, stage_code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": dict(details or {}),
    }


def _require_keyword_workflow() -> WorkflowDefinition:
    from automation_business_scaffold.business.workflow_defs import get_workflow_definition

    return get_workflow_definition(KEYWORD_TASK_CODE)


def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_entity_key") or candidate.get("candidate_key") or "")
    return f"tiktok_product:{business_key}" if business_key else ""


def _search_digest(*, search_query: str, filters: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {
            "search_query": str(search_query or "").strip(),
            "filters": dict(filters or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _resolve_product_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("product_identity")
    if isinstance(nested, Mapping):
        base = dict(nested)
    else:
        base = {}
    product_url = str(
        base.get("product_url")
        or row.get("product_url")
        or row.get("url")
        or row.get("normalized_product_url")
        or ""
    ).strip()
    normalized_product_url = _normalize_product_url(product_url)
    product_id = str(
        base.get("product_id")
        or row.get("product_id")
        or row.get("id")
        or row.get("productId")
        or _extract_tiktok_product_id(normalized_product_url)
        or ""
    ).strip()
    if not normalized_product_url and product_id:
        normalized_product_url = _tiktok_product_url(product_id)
    if not product_url and normalized_product_url:
        product_url = normalized_product_url
    product_key = str(base.get("product_key") or row.get("product_key") or row.get("fastmoss_product_key") or "").strip()
    return {
        "product_id": product_id,
        "product_key": product_key or product_id or normalized_product_url,
        "product_url": product_url,
        "normalized_product_url": normalized_product_url,
    }


def _normalize_product_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[?#].*$", "", text)
    product_id = _extract_tiktok_product_id(normalized)
    if product_id:
        return _tiktok_product_url(product_id)
    return normalized


def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product|detail)/(\d+)", text)
    if match:
        return str(match.group(1))
    return ""


def _product_business_entity_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("product:"):
        return text
    return f"product:{text}"


def _tiktok_product_url(product_id: str) -> str:
    return f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else ""


def _asset_source(asset: Mapping[str, Any]) -> str:
    return str(asset.get("source_url") or asset.get("url") or "")


def _minimal_seed_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    product_identity = dict(payload.get("product_identity") or {})
    business_entity_key = str(payload.get("business_entity_key") or payload.get("candidate_key") or "")
    return {
        "candidate_key": str(payload.get("candidate_key") or business_entity_key),
        "business_entity_key": business_entity_key,
        "source_record_id": str(payload.get("source_record_id") or business_entity_key),
        "product_identity": product_identity,
        "product_id": str(product_identity.get("product_id") or ""),
        "normalized_product_url": str(product_identity.get("normalized_product_url") or payload.get("normalized_product_url") or ""),
        "source_context": dict(payload.get("source_context") or {}),
    }
