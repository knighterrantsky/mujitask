from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_mapping, merge_fact_bundles
from automation_business_scaffold.control_plane.runtime_config.settings import (
    KEYWORD_TASK_CODE,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
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
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)

OPTIONAL_FINAL_STATUS_CODES = ("tiktok_product_browser_fetch",)
FASTMOSS_SEARCH_PASSTHROUGH_KEYS = (
    "fastmoss_search_response",
    "product_search_response",
    "search_response",
    "mock_fastmoss_search_response",
    "fastmoss_search_pages",
    "product_search_pages",
    "search_pages",
    "mock_fastmoss_search_pages",
)
TIKTOK_REQUEST_PASSTHROUGH_KEYS = (
    "fallback_reason",
    "force_failure",
    "force_fallback",
    "mock_response",
    "normalized_product_result",
    "raw_request_result",
    "request_result",
    "source_payload",
    "tiktok_request_result",
)
FASTMOSS_PRODUCT_PASSTHROUGH_KEYS = (
    "fastmoss_bundle",
    "fastmoss_result",
    "mock_fastmoss_bundle",
    "product_fact_bundle",
    "required",
)
RUNTIME_DB_PASSTHROUGH_KEYS = (
    "execution_control_db_url",
    "db_url",
)
FASTMOSS_BROWSER_PASSTHROUGH_KEYS = (
    "browser_profile_ref",
    "browser_profile_id",
    "browser_provider_name",
    "browser_workspace_id",
    "browser_headless",
    "browser_force_open",
    "browser_timeout_ms",
    "fastmoss_browser_profile_ref",
    "fastmoss_browser_profile_id",
    "fastmoss_browser_provider_name",
    "fastmoss_browser_workspace_id",
    "fastmoss_browser_timeout_ms",
    "fastmoss_slider_max_attempts",
    "fastmoss_slider_appear_timeout_ms",
    "fastmoss_slider_settle_ms",
    "fastmoss_slider_confirm_ms",
    "mock_fastmoss_security_browser_resolve",
)
FACT_PERSISTENCE_PASSTHROUGH_KEYS = (
    "db_url",
    "fact_db_url",
    "persistence",
)
ARTIFACT_PASSTHROUGH_KEYS = (
    "artifact_bucket",
    "artifact_object_prefix",
    "artifact_root",
    "artifact_store",
    "artifact_store_provider",
    "db_url",
    "execution_control_fact_db_url",
    "fact_db_url",
    "minio_access_key",
    "minio_create_bucket",
    "minio_endpoint",
    "minio_region",
    "minio_secret_key",
    "minio_secure",
    "persistence",
)


def advance_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    if request.task_code != KEYWORD_TASK_CODE:
        raise ValueError(f"Unsupported task_code for keyword runtime: {request.task_code}")
    if stage_code == "keyword_seed_import":
        return _advance_keyword_seed_import(store=store, request=request, workflow=workflow)
    if stage_code == "fastmoss_security_browser_fallback":
        return _advance_fastmoss_security_browser_fallback(store=store, request=request, workflow=workflow)
    if stage_code == "dispatch_row_refresh_jobs":
        return _advance_dispatch_row_refresh_jobs(store=store, request=request, workflow=workflow)
    if stage_code == "refresh_competitor_rows":
        return _advance_refresh_competitor_rows(store=store, request=request, workflow=workflow)
    if stage_code == "browser_fallback":
        return _advance_browser_fallback(store=store, request=request, workflow=workflow)
    if stage_code == "resume_competitor_rows_after_browser_fallback":
        return _advance_resume_competitor_rows_after_browser_fallback(
            store=store,
            request=request,
            workflow=workflow,
        )
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
    explicit_final_status = str((force_result or {}).get("final_status") or "")
    computed_status = compute_final_status(
        workflow.summary_policy,
        child_records=child_records,
        optional_codes=OPTIONAL_FINAL_STATUS_CODES,
        explicit_status=explicit_final_status,
    )
    final_status = _derive_final_status(row_results=row_results, fallback_status=computed_status)
    if not explicit_final_status and not row_results and int(child_outcome["failed_count"]) == 0:
        final_status = "success"
    warnings = list(dict.fromkeys(_collect_warnings(row_results)))
    search_query = _first_text(request.payload.get("search_query"), request.payload.get("search_keyword"), request.payload.get("keyword"))
    seed_import_payload = _keyword_seed_import_payload(store=store, request_id=request.request_id)
    search_parameters = dict(seed_import_payload.get("search_parameters") or {})
    search_filter_info = {
        "search_query": search_query,
        "filters": dict(search_parameters.get("filters") or request.payload.get("filters") or {}),
        "output_conditions": dict(search_parameters.get("output_conditions") or request.payload.get("output_conditions") or {}),
        "condition_context": dict(search_parameters.get("condition_context") or {}),
        "sort": dict(search_parameters.get("sort") or {}),
        "pagination": dict(search_parameters.get("pagination") or {}),
    }
    seed_write_results = [dict(item) for item in seed_import_payload.get("seed_write_results", []) if isinstance(item, Mapping)]

    summary = {
        "final_status": final_status,
        "search_query": search_query,
        "search_filter_info": search_filter_info,
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
        "search_query": search_query,
        "search_filter_info": search_filter_info,
        "search_parameters": search_parameters,
        "candidate_total_count": len(candidate_contexts),
        "seed_total_count": len(_seed_contexts(store=store, request_id=request.request_id)),
        "seed_write_results": seed_write_results,
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
                message_format=str(request.payload.get("outbox_message_format") or ""),
                message_template=str(request.payload.get("outbox_message_template") or ""),
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
            store.update_task_request(
                request_id=request_id,
                status="pending",
                current_stage=recovered_stage,
                progress_stage=recovered_stage,
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                last_progress_at=time.time(),
            )
            return [
                {
                    "request_id": request_id,
                    "stage_code": recovered_stage,
                    "released": True,
                    "next_executor_status": "pending",
                }
            ]
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
        continuation_stage_codes=("resume_competitor_rows_after_browser_fallback",),
        continuation_candidate_ready=bool(_browser_resume_candidates(store=store, request_id=request_id)),
        browser_stage_code="browser_fallback",
        resume_stage_code="resume_competitor_rows_after_browser_fallback",
    )


def _advance_keyword_seed_import(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "keyword_seed_import"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    job_def = workflow.require_job("keyword_seed_import")
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for keyword seed import to finish.")

    retry_after_fastmoss_browser = False
    latest_import_job = _latest_job(jobs, job_code="keyword_seed_import") if jobs else None
    if latest_import_job and extract_handler_result_status(latest_import_job) == "fallback_required":
        if _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs):
            return _finalize_fastmoss_security_required(
                latest_import_job,
                details={"reason": "retry_after_browser_fallback_still_requires_security_verification"},
            )
        fallback_cursor = _fastmoss_security_browser_fallback_cursor(store=store, request_id=request.request_id)
        if fallback_cursor.get("status") == "success":
            retry_after_fastmoss_browser = True
        elif fallback_cursor.get("status") in {"failed", "partial_success"}:
            return _finalize_fastmoss_security_required(latest_import_job, details={"reason": "browser_fallback_failed"})
        elif _fastmoss_security_browser_fallback_attempted(store=store, request_id=request.request_id):
            return _finalize_fastmoss_security_required(latest_import_job, details={"reason": "browser_fallback_attempted"})
        else:
            fallback_payload = _fastmoss_security_fallback_payload_from_job(latest_import_job)
            _update_request_cursor(
                store=store,
                request=request,
                stage_code=stage_code,
                payload={
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_search_security_verification",
                    "fastmoss_security_fallback": fallback_payload,
                },
            )
            return {
                "action": "advance",
                "next_stage": "fastmoss_security_browser_fallback",
                "details": {
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_search_security_verification",
                },
            }

    if not jobs or retry_after_fastmoss_browser:
        search_request = _keyword_seed_import_search_request(
            request.payload,
            latest_import_job=latest_import_job,
            retry_after_fastmoss_browser=retry_after_fastmoss_browser,
        )
        search_query = str(search_request.get("search_query") or "")
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            search_request["fastmoss"] = fastmoss_settings

        seed_table_ref = str(request.payload.get("seed_table_ref") or request.payload.get("target_table_ref") or request.payload.get("table_url") or "")
        payload = {
            "stage_code": stage_code,
            "search_query": search_query,
            "search_digest": search_request["search_digest"],
            "search_request": search_request,
            "seed_write": {
                "target_table_ref": seed_table_ref,
                "write_mode": "insert_if_absent",
                "mapper_code": "competitor_seed_projection_mapper",
            },
            **_payload_subset(
                request.payload,
                ("table_refs", "access_token", "access_token_env", "validate_schema")
                + RUNTIME_DB_PASSTHROUGH_KEYS,
            ),
        }
        if retry_after_fastmoss_browser:
            payload["fastmoss_security_browser_fallback_attempt"] = 1
            payload["fallback_source_job_id"] = str((latest_import_job or {}).get("job_id") or "")
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
        dedupe_key = keys["dedupe_key"]
        if retry_after_fastmoss_browser:
            dedupe_key = f"{dedupe_key}:after-fastmoss-security-browser-fallback"
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": dedupe_key,
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued keyword seed import.",
            details={"dispatch_payload": {"keyword_seed_import": dispatch}},
        )

    import_job = latest_import_job
    payload = extract_effective_result_payload(import_job)
    candidates = [dict(item) for item in payload.get("normalized_candidates", []) if isinstance(item, Mapping)]
    seeds = [dict(item) for item in payload.get("seed_contexts", []) if isinstance(item, Mapping)]
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "candidate_contexts": candidates,
            "candidate_total_count": len(candidates),
            "seed_contexts": seeds,
            "seed_total_count": len(seeds),
            "successful_seed_count": sum(1 for item in seeds if item.get("seed_status") == "success"),
            "search_parameters": dict(payload.get("search_parameters") or {}),
            "search_filter_info": {
                "filters": dict((payload.get("search_parameters") or {}).get("filters") or {}),
                "output_conditions": dict((payload.get("search_parameters") or {}).get("output_conditions") or {}),
                "condition_context": dict((payload.get("search_parameters") or {}).get("condition_context") or {}),
                "sort": dict((payload.get("search_parameters") or {}).get("sort") or {}),
                "pagination": dict((payload.get("search_parameters") or {}).get("pagination") or {}),
            },
            "seed_write_results": [dict(item) for item in payload.get("seed_write_results", []) if isinstance(item, Mapping)],
        },
    )
    return {
        "action": "advance",
        "next_stage": "dispatch_row_refresh_jobs",
        "details": {
            "candidate_total_count": len(candidates),
            "seed_total_count": len(seeds),
        },
    }


def _advance_fastmoss_security_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "fastmoss_security_browser_fallback"
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not executions:
        import_job = _latest_job(
            _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code="keyword_seed_import"),
            job_code="keyword_seed_import",
        )
        if not import_job or extract_handler_result_status(import_job) != "fallback_required":
            return {"action": "advance", "next_stage": "keyword_seed_import", "details": {"fallback_candidate_count": 0}}

        fallback_payload = _fastmoss_security_fallback_payload_from_job(import_job)
        job_def = workflow.require_job("fastmoss_security_browser_resolve")
        payload = {
            **_payload_subset(request.payload, FASTMOSS_BROWSER_PASSTHROUGH_KEYS + RUNTIME_DB_PASSTHROUGH_KEYS),
            "stage_code": stage_code,
            "search_query": str(fallback_payload.get("search_query") or request.payload.get("search_query") or ""),
            "search_digest": str(fallback_payload.get("search_digest") or (import_job.get("payload") or {}).get("search_digest") or ""),
            "search_request": dict(fallback_payload.get("search_request") or {}),
            "security_context": dict(fallback_payload.get("security_context") or {}),
            "fallback_source_job_id": str(import_job.get("job_id") or ""),
            "request_payload": dict(request.payload or {}),
        }
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
        keys = render_job_keys(
            job_def,
            request.payload,
            fallback_payload,
            payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
            item_code=job_def.job_code,
        )
        dispatch = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code=job_def.job_code,
            workflow_code=workflow.workflow_code,
            items=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        keys["dedupe_key"],
                        job_def.job_code,
                        stage_scope=stage_code,
                    ),
                    "resource_code": _fastmoss_browser_resource_code(payload),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "status": "pending",
                "browser_dispatch": dispatch,
                "fallback_source_job_id": str(import_job.get("job_id") or ""),
                "search_request": dict(fallback_payload.get("search_request") or {}),
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued FastMoss security browser fallback.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for FastMoss security browser fallback to finish.")

    execution = executions[-1]
    handler_status = extract_handler_result_status(execution)
    if handler_status == "success":
        execution_payload = extract_effective_result_payload(execution)
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "status": "success",
                "verified_path": str(execution_payload.get("verified_path") or "/api/goods/V2/search"),
                "cookie_cache": dict(execution_payload.get("cookie_cache") or {}),
                "fallback_source_job_id": str((execution.payload or {}).get("fallback_source_job_id") or ""),
            },
        )
        return {
            "action": "advance",
            "next_stage": "keyword_seed_import",
            "details": {"fastmoss_security_browser_fallback": "success"},
        }

    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"status": "failed", "execution_count": len(executions)},
    )
    return {
        "action": "finalize",
        "final_status": "failed",
        "details": {
            "error_code": "fastmoss_security_verification_required",
            "reason": "fastmoss_security_browser_fallback_failed",
        },
    }


def _advance_dispatch_row_refresh_jobs(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_row_refresh_jobs"
    seed_contexts = [item for item in _seed_contexts(store=store, request_id=request.request_id) if item.get("seed_status") == "success"]
    if not seed_contexts:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"dispatched_row_count": 0})
        return {"action": "advance", "next_stage": "refresh_competitor_rows", "details": {"dispatched_row_count": 0}}

    row_job_def = workflow.require_job("competitor_row_refresh")
    source_table_ref = str(request.payload.get("seed_table_ref") or request.payload.get("target_table_ref") or request.payload.get("table_url") or "")
    row_jobs: list[dict[str, Any]] = []
    for seed in seed_contexts:
        product_identity = dict(seed.get("product_identity") or {})
        row_payload = {
            **_payload_subset(
                request.payload,
                TIKTOK_REQUEST_PASSTHROUGH_KEYS
                + FASTMOSS_PRODUCT_PASSTHROUGH_KEYS
                + FACT_PERSISTENCE_PASSTHROUGH_KEYS
                + ARTIFACT_PASSTHROUGH_KEYS
                + ("table_refs", "access_token", "access_token_env", "validate_schema"),
            ),
            "request_payload": dict(request.payload or {}),
            "stage_code": "refresh_competitor_rows",
            "source_record_id": seed["source_record_id"],
            "source_record_id_or_product_id": _first_text(seed.get("source_record_id"), seed.get("product_id")),
            "business_key": seed.get("business_entity_key") or seed.get("candidate_key") or "",
            "product_identity": product_identity,
            "normalized_product_url": seed.get("normalized_product_url") or product_identity.get("normalized_product_url") or "",
            "source_table_ref": source_table_ref,
            "source_context": dict(seed.get("source_context") or {}),
            "fallback_allowed": bool(request.payload.get("fallback_allowed", True)),
        }
        row_keys = render_job_keys(
            row_job_def,
            request.payload,
            seed,
            row_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code="refresh_competitor_rows",
            job_code=row_job_def.job_code,
        )
        row_jobs.append(
            {
                "business_key": row_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
                "payload": row_payload,
                "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
            }
        )

    row_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=row_jobs,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"dispatched_row_count": len(seed_contexts), "row_dispatch": row_dispatch},
    )
    return {
        "action": "advance",
        "next_stage": "refresh_competitor_rows",
        "details": {
            "dispatched_row_count": len(seed_contexts),
            "row_refresh_created_count": int(row_dispatch["created_count"]),
        },
    }


def _advance_refresh_competitor_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "refresh_competitor_rows"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        return {"action": "advance", "next_stage": "ready_for_summary", "details": {"dispatched_row_count": 0}}
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row refresh jobs to finish.")
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
        workflow.require_stage("browser_fallback")
        return {
            "action": "advance",
            "next_stage": "browser_fallback",
            "details": {"fallback_candidate_count": len(fallback_candidates)},
        }
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"collect_job_count": len(jobs)}}


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
        raw_max_candidates = request.payload.get("max_candidates")
        if raw_max_candidates in (None, ""):
            raw_max_candidates = output_conditions.get("max_candidates")
        max_candidates = _non_negative_int_param(raw_max_candidates, 20)
        output_conditions["max_candidates"] = max_candidates
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
        payload.update(_payload_subset(request.payload, FASTMOSS_SEARCH_PASSTHROUGH_KEYS))
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
        ("execution_control_db_url", "execution_control_db_url"),
        ("db_url", "db_url"),
        ("fastmoss_cookie_cache_namespace", "cookie_cache_namespace"),
        ("fastmoss_cookie_cache_enabled", "cookie_cache_enabled"),
        ("fastmoss_cookie_cache_ttl_seconds", "cookie_cache_ttl_seconds"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", [], {}):
            settings.setdefault(target_key, value)
    settings.setdefault("live_fetch", True)
    settings.setdefault("ensure_logged_in", True)
    return {key: value for key, value in settings.items() if value not in (None, "", [], {})}


def _keyword_seed_import_search_request(
    request_payload: Mapping[str, Any],
    *,
    latest_import_job: Mapping[str, Any] | None,
    retry_after_fastmoss_browser: bool,
) -> dict[str, Any]:
    previous_payload = coerce_mapping((latest_import_job or {}).get("payload"))
    previous_search_request = coerce_mapping(previous_payload.get("search_request"))
    search_request = dict(previous_search_request) if retry_after_fastmoss_browser and previous_search_request else keyword_search_parameter_mapper(request_payload)
    for key in RUNTIME_DB_PASSTHROUGH_KEYS:
        if request_payload.get(key) not in (None, ""):
            search_request[key] = request_payload.get(key)
    if retry_after_fastmoss_browser:
        search_request["fastmoss_security_browser_fallback_attempt"] = 1
    return search_request


def _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs: list[dict[str, Any]]) -> bool:
    return any(
        int(coerce_mapping(job.get("payload")).get("fastmoss_security_browser_fallback_attempt") or 0) > 0
        for job in jobs
    )


def _fastmoss_security_browser_fallback_cursor(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    return dict(stage_results.get("fastmoss_security_browser_fallback") or {})


def _fastmoss_security_browser_fallback_attempted(*, store: RuntimeStore, request_id: str) -> bool:
    if _fastmoss_security_browser_fallback_cursor(store=store, request_id=request_id):
        return True
    return bool(
        _browser_executions_for_stage(
            store=store,
            request_id=request_id,
            stage_code="fastmoss_security_browser_fallback",
        )
    )


def _fastmoss_security_fallback_payload_from_job(import_job: Mapping[str, Any]) -> dict[str, Any]:
    job_payload = coerce_mapping(import_job.get("payload"))
    result_payload = extract_effective_result_payload(import_job)
    search_request = coerce_mapping(job_payload.get("search_request")) or coerce_mapping(result_payload.get("search_request"))
    security_context = coerce_mapping(result_payload.get("security_context"))
    return {
        "search_query": _first_text(
            search_request.get("search_query"),
            search_request.get("keyword"),
            job_payload.get("search_query"),
        ),
        "search_digest": _first_text(job_payload.get("search_digest"), search_request.get("search_digest")),
        "search_request": search_request,
        "security_context": security_context,
        "fallback_source_job_id": _first_text(result_payload.get("fallback_source_job_id"), import_job.get("job_id")),
    }


def _finalize_fastmoss_security_required(
    import_job: Mapping[str, Any],
    *,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    result_payload = extract_effective_result_payload(import_job)
    return {
        "action": "finalize",
        "final_status": "failed",
        "details": {
            "error_code": "fastmoss_security_verification_required",
            "fallback_required": True,
            "fallback_reason": "fastmoss_search_security_verification",
            "security_context": dict(result_payload.get("security_context") or {}),
            **dict(details),
        },
    }


def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )


def _runtime_child_context(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "task_code": request.task_code,
        "workflow_code": workflow.workflow_code,
        "stage_code": stage_code,
    }


def _positive_int_param(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int_param(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


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


def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}


def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in values.items() if value not in (None, "", [], {})}


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
        tiktok_payload.update(_payload_subset(request.payload, TIKTOK_REQUEST_PASSTHROUGH_KEYS))
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
        fastmoss_payload.update(_payload_subset(request.payload, FASTMOSS_PRODUCT_PASSTHROUGH_KEYS))
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
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"fallback_candidate_count": 0},
        }
    if not executions and fallback_candidates:
        dispatches: dict[str, Any] = {}
        for fallback_handler in sorted(
            {str(candidate.get("fallback_handler") or "") for candidate in fallback_candidates}
        ):
            if not fallback_handler:
                continue
            job_def = workflow.require_job(fallback_handler)
            items: list[dict[str, Any]] = []
            for candidate in fallback_candidates:
                if str(candidate.get("fallback_handler") or "") != fallback_handler:
                    continue
                payload = _browser_execution_payload(
                    request=request,
                    workflow=workflow,
                    stage_code=stage_code,
                    candidate=candidate,
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
                    item_code=job_def.job_code,
                )
                items.append(
                    {
                        "business_key": keys["business_key"]
                        or str(candidate.get("business_entity_key") or ""),
                        "dedupe_key": build_stage_local_dedupe_key(
                            keys["dedupe_key"],
                            job_def.job_code,
                            stage_scope=stage_code,
                        ),
                        "resource_code": _row_browser_resource_code(
                            fallback_handler=fallback_handler,
                            payload=payload,
                            candidate=candidate,
                        ),
                        "payload": payload,
                        "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                    }
                )
            if not items:
                continue
            dispatches[fallback_handler] = store.enqueue_task_executions(
                request_id=request.request_id,
                item_code=job_def.job_code,
                workflow_code=workflow.workflow_code,
                items=items,
            )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"browser_dispatches": dispatches, "fallback_candidate_count": len(fallback_candidates)},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued browser fallback executions.",
            details={
                "created_count": sum(int(dispatch.get("created_count") or 0) for dispatch in dispatches.values())
            },
        )
    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for browser fallback executions to finish.")
    resumable = _browser_resume_candidates(store=store, request_id=request.request_id)
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "resumable_count": len(resumable),
            "status": "success" if resumable else "failed",
        },
    )
    if resumable:
        return {
            "action": "advance",
            "next_stage": "resume_competitor_rows_after_browser_fallback",
            "details": {"resumable_count": len(resumable)},
        }
    return {
        "action": "advance",
        "next_stage": "ready_for_summary",
        "details": {"execution_count": len(executions), "resumable_count": 0},
    }


def _advance_resume_competitor_rows_after_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "resume_competitor_rows_after_browser_fallback"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _browser_resume_candidates(store=store, request_id=request.request_id)
        if not candidates:
            return {
                "action": "advance",
                "next_stage": "ready_for_summary",
                "details": {"resumable_count": 0},
            }
        row_job_def = workflow.require_job("competitor_row_refresh")
        row_jobs: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = _resume_row_payload(stage_code=stage_code, candidate=candidate)
            keys = render_job_keys(
                row_job_def,
                request.payload,
                candidate,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=row_job_def.job_code,
            )
            row_jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        f"{keys['dedupe_key']}:after-browser-fallback",
                        row_job_def.job_code,
                    ),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
                }
            )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=row_job_def.job_code,
            jobs=row_jobs,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"resumable_count": len(candidates), "row_dispatch": dispatch},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued competitor row refresh retries after browser fallback.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(
            stage_code=stage_code,
            message="Waiting for competitor row refresh retries after browser fallback to finish.",
        )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"resumed_job_count": len(jobs)},
    )
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"resumed_job_count": len(jobs)}}


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
            payload.update(_payload_subset(request.payload, ARTIFACT_PASSTHROUGH_KEYS))
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
                "observation_context": {
                    "source_record_id": candidate["source_record_id"],
                    "search_query": candidate.get("search_query") or "",
                },
            }
            payload.update(_payload_subset(request.payload, FACT_PERSISTENCE_PASSTHROUGH_KEYS))
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
                request_payload=request.payload,
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
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_candidates = keyword_import.get("candidate_contexts")
    if isinstance(import_candidates, list):
        return [dict(item) for item in import_candidates if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    candidates = import_payload.get("normalized_candidates")
    if isinstance(candidates, list):
        return [dict(item) for item in candidates if isinstance(item, Mapping)]

    processed = dict(stage_results.get("process_product_candidates") or {})
    legacy_candidates = processed.get("candidate_contexts")
    if isinstance(legacy_candidates, list):
        return [dict(item) for item in legacy_candidates if isinstance(item, Mapping)]

    search_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="search_product_candidates")
    search_job = select_latest_successful_api_job(search_jobs, "fastmoss_product_search")
    search_payload = extract_effective_result_payload(search_job)
    return _normalize_search_candidates(
        search_payload.get("candidates"),
        search_query=str(request.payload.get("search_query") or ""),
        output_conditions=dict(request.payload.get("output_conditions") or {}),
        max_candidates=int(request.payload.get("max_candidates") or 0),
    )


def _keyword_seed_import_payload(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    return {**import_payload, **keyword_import}


def _seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_seeds = keyword_import.get("seed_contexts")
    if isinstance(import_seeds, list):
        return [dict(item) for item in import_seeds if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    seeds = import_payload.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]

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
    seed_by_source_record = {
        str(seed.get("source_record_id") or ""): seed
        for seed in _seed_contexts(store=store, request_id=request_id)
        if str(seed.get("source_record_id") or "")
    }
    seed_by_candidate_key = _seed_context_by_candidate_key(store=store, request_id=request_id)
    for job in _api_jobs_for_stage(store=store, request_id=request_id, stage_code="refresh_competitor_rows"):
        if str(job.get("job_code") or "") != "competitor_row_refresh":
            continue
        if not _is_fallback_required(job):
            continue
        payload = dict(job.get("payload") or {})
        result = extract_effective_result_payload(job)
        handler_result = coerce_mapping(coerce_mapping(job.get("result")).get("handler_result"))
        handler_result_payload = coerce_mapping(handler_result.get("result"))
        next_action = coerce_mapping(handler_result.get("next_action"))
        next_action_payload = coerce_mapping(next_action.get("payload"))
        fallback_handler = _first_text(
            result.get("fallback_handler"),
            handler_result_payload.get("fallback_handler"),
            "tiktok_product_browser_fetch" if str(next_action.get("type") or "") == "browser_fallback" else "",
        )
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = coerce_mapping(result.get("browser_fallback_payload")) or next_action_payload
        source_record_id = _first_text(
            result.get("source_record_id"),
            browser_payload.get("source_record_id"),
            payload.get("source_record_id"),
        )
        candidate_key = _first_text(
            result.get("candidate_key"),
            browser_payload.get("candidate_key"),
            payload.get("candidate_key"),
            payload.get("business_key"),
        )
        seed = dict(seed_by_source_record.get(source_record_id) or seed_by_candidate_key.get(candidate_key) or {})
        if not seed:
            seed = _minimal_seed_context(payload)
        business_entity_key = _first_text(
            result.get("business_entity_key"),
            browser_payload.get("business_entity_key"),
            payload.get("business_entity_key"),
            payload.get("business_key"),
            seed.get("business_entity_key"),
            candidate_key,
            source_record_id,
        )
        fallback_source_job_id = _first_text(
            browser_payload.get("fallback_source_job_id"),
            result.get("fallback_source_job_id"),
            job.get("job_id"),
        )
        browser_payload = {
            **browser_payload,
            "candidate_key": _first_text(seed.get("candidate_key"), candidate_key, business_entity_key),
            "source_record_id": source_record_id,
            "business_entity_key": business_entity_key,
            "fallback_source_job_id": fallback_source_job_id,
        }
        candidate = dict(seed)
        candidate.update(
            {
                "candidate_key": _first_text(seed.get("candidate_key"), candidate_key, business_entity_key),
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    business_entity_key=business_entity_key,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_text(result.get("fallback_reason")),
                "fallback_source_job_id": fallback_source_job_id,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": payload,
                "row_result": result,
                "source_record_id": source_record_id,
                "business_entity_key": business_entity_key,
                "browser_fallback_payload": _compact_mapping(browser_payload),
                "normalized_product_result": (
                    dict(result.get("normalized_product_result"))
                    if isinstance(result.get("normalized_product_result"), Mapping)
                    else {}
                ),
            }
        )
        candidates.append(candidate)
    return candidates


def _browser_execution_payload(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    fallback_payload = (
        dict(candidate.get("browser_fallback_payload"))
        if isinstance(candidate.get("browser_fallback_payload"), Mapping)
        else {}
    )
    payload = {
        **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
        **_payload_subset(
            request.payload,
            TIKTOK_REQUEST_PASSTHROUGH_KEYS
            + FASTMOSS_BROWSER_PASSTHROUGH_KEYS
            + RUNTIME_DB_PASSTHROUGH_KEYS
            + ARTIFACT_PASSTHROUGH_KEYS,
        ),
        **fallback_payload,
        "stage_code": stage_code,
        "candidate_key": str(candidate.get("candidate_key") or fallback_payload.get("candidate_key") or ""),
        "source_record_id": str(candidate.get("source_record_id") or fallback_payload.get("source_record_id") or ""),
        "business_entity_key": str(
            candidate.get("business_entity_key") or fallback_payload.get("business_entity_key") or ""
        ),
        "fallback_handler": fallback_handler,
        "fallback_source_job_id": _first_text(
            fallback_payload.get("fallback_source_job_id"),
            candidate.get("row_job_id"),
        ),
    }
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault(
            "search_query",
            _first_text(candidate.get("search_query"), request.payload.get("search_query")),
        )
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        if not isinstance(payload.get("search_request"), Mapping):
            payload["search_request"] = {}
        if not isinstance(payload.get("verification_request"), Mapping):
            payload["verification_request"] = {}
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
    return _compact_mapping(payload)


def _browser_resume_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    fallback_by_key = {
        str(candidate.get("fallback_key") or ""): candidate
        for candidate in _browser_fallback_candidates(store=store, request_id=request_id)
    }
    candidates: list[dict[str, Any]] = []
    for execution in _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback"):
        if _record_effective_status(execution) != "success":
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_text(payload.get("source_record_id"))
        business_entity_key = _first_text(payload.get("business_entity_key"), payload.get("candidate_key"))
        fallback_key = _row_fallback_key(
            source_record_id=source_record_id,
            business_entity_key=business_entity_key,
            fallback_handler=fallback_handler,
        )
        fallback_candidate = fallback_by_key.get(fallback_key)
        if not fallback_candidate:
            continue
        execution_payload = extract_effective_result_payload(execution)
        if fallback_handler == "tiktok_product_browser_fetch":
            normalized = execution_payload.get("normalized_product_result")
            if not isinstance(normalized, Mapping) or not normalized:
                continue
        candidates.append(
            {
                **dict(fallback_candidate),
                "browser_execution_id": str(execution.execution_id),
                "browser_execution_payload": execution_payload,
            }
        )
    return candidates


def _resume_row_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(candidate.get("row_payload") or {}) if isinstance(candidate.get("row_payload"), Mapping) else {}
    browser_payload = (
        dict(candidate.get("browser_execution_payload"))
        if isinstance(candidate.get("browser_execution_payload"), Mapping)
        else {}
    )
    payload.update(
        {
            "stage_code": stage_code,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": fallback_handler,
            "browser_execution_id": str(candidate.get("browser_execution_id") or ""),
            "fallback_source_job_id": str(candidate.get("row_job_id") or ""),
            "force_fallback": False,
            "fallback_reason": "",
        }
    )
    if fallback_handler == "tiktok_product_browser_fetch":
        normalized = browser_payload.get("normalized_product_result")
        if isinstance(normalized, Mapping):
            payload["normalized_product_result"] = dict(normalized)
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized = candidate.get("normalized_product_result")
        if isinstance(normalized, Mapping) and normalized:
            payload["normalized_product_result"] = dict(normalized)
    return _compact_mapping(payload)


def _row_fallback_key(*, source_record_id: str, business_entity_key: str, fallback_handler: str) -> str:
    row_key = _first_text(source_record_id, business_entity_key)
    return f"{fallback_handler}:{row_key}"


def _search_digest_for_row_fallback(candidate: Mapping[str, Any]) -> str:
    value = _first_text(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("row_job_id"),
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _row_browser_resource_code(
    *,
    fallback_handler: str,
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    if fallback_handler == "fastmoss_security_browser_resolve":
        return _fastmoss_browser_resource_code(payload)
    return _browser_resource_code(candidate)


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
        fact_bundle = merge_fact_bundles(
            dict(product_result.get("fact_bundle") or {}),
            dict(fastmoss_result.get("product_fact_bundle") or {}),
            dict(media_result.get("media_fact_bundle") or {}),
        )
        has_payload = bool(fact_bundle)
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
    row_jobs = _api_jobs_for_stage(
        store=store,
        request_id=request_id,
        stage_code="refresh_competitor_rows",
    ) + _api_jobs_for_stage(
        store=store,
        request_id=request_id,
        stage_code="resume_competitor_rows_after_browser_fallback",
    )
    row_job = _latest_row_job(
        row_jobs,
        source_record_id=str(seed_context.get("source_record_id") or ""),
        job_code="competitor_row_refresh",
    )
    if row_job:
        row_payload = extract_effective_result_payload(row_job)
        step_timeline = row_payload.get("step_timeline") if isinstance(row_payload.get("step_timeline"), list) else []
        step_statuses = {
            str(item.get("step") or ""): str(item.get("status") or "")
            for item in step_timeline
            if isinstance(item, Mapping)
        }
        row_status = str(row_payload.get("row_status") or _record_effective_status(row_job) or "failed")
        return {
            "candidate_key": candidate_key,
            "product_id": str(candidate_context.get("product_id") or ""),
            "source_record_id": str(seed_context.get("source_record_id") or ""),
            "feishu_row": dict(seed_context.get("feishu_row") or {}),
            "row_status": row_status,
            "seed_status": str(seed_context.get("seed_status") or ""),
            "failure_reason": _row_failure_reason(row_job=row_job, row_payload=row_payload, row_status=row_status),
            "competitor_row_refresh_status": _record_effective_status(row_job),
            "tiktok_status": step_statuses.get("tiktok_request", ""),
            "browser_status": step_statuses.get("browser_fallback", ""),
            "media_status": step_statuses.get("media_sync", ""),
            "fastmoss_status": step_statuses.get("fastmoss_fetch", ""),
            "fact_status": step_statuses.get("fact_db_upsert", ""),
            "writeback_status": step_statuses.get("feishu_writeback", ""),
        }
    if seed_context and str(seed_context.get("seed_status") or "") == "skipped":
        return {
            "candidate_key": candidate_key,
            "product_id": str(candidate_context.get("product_id") or ""),
            "source_record_id": str(seed_context.get("source_record_id") or ""),
            "feishu_row": dict(seed_context.get("feishu_row") or {}),
            "row_status": "skipped",
            "seed_status": "skipped",
            "failure_reason": str((seed_context.get("seed_result") or {}).get("message") or "existing_record")
            if isinstance(seed_context.get("seed_result"), Mapping)
            else "existing_record",
            "competitor_row_refresh_status": "",
            "tiktok_status": "",
            "browser_status": "",
            "media_status": "",
            "fastmoss_status": "",
            "fact_status": "",
            "writeback_status": "",
        }
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
        "feishu_row": dict(seed_context.get("feishu_row") or {}),
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
    projection_fields = _build_competitor_projection_fields(
        store=store,
        request_id=request_id,
        candidate_context=candidate_context,
        row_status=str(row_result["row_status"]),
    )
    return build_projection_record(
        request_id=request_id,
        source_record_id=str(row_result["source_record_id"]),
        candidate_key=str(row_result["candidate_key"]),
        product_id=str(row_result["product_id"]),
        product_url=str(candidate_context.get("normalized_product_url") or candidate_context.get("product_url") or ""),
        refresh_status=str(row_result["row_status"]),
        details=row_result,
        extra_fields={"projection_fields": projection_fields},
    )


def _build_competitor_projection_fields(
    *,
    store: RuntimeStore,
    request_id: str,
    candidate_context: Mapping[str, Any],
    row_status: str,
) -> dict[str, Any]:
    candidate_key = str(candidate_context.get("candidate_key") or "")
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    tiktok_job = _latest_candidate_job(collect_jobs, candidate_key=candidate_key, job_code="tiktok_product_request_fetch")
    fastmoss_job = _latest_candidate_job(collect_jobs, candidate_key=candidate_key, job_code="fastmoss_product_fetch")
    browser_execution = _latest_candidate_execution(browser_execs, candidate_key=candidate_key)

    tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
    fastmoss_result = extract_effective_result_payload(fastmoss_job)
    product_result = dict(tiktok_result.get("normalized_product_result") or {})
    tiktok_product = dict(product_result.get("product") or {})
    logical_fields = dict(product_result.get("logical_fields") or {})
    fact_bundle = dict(fastmoss_result.get("product_fact_bundle") or {})
    fastmoss_product = _fact_bundle_product(
        fact_bundle,
        product_id=str(candidate_context.get("product_id") or ""),
    )
    metrics_snapshot = dict(fastmoss_result.get("metrics_snapshot") or {})
    overview_metrics = dict(metrics_snapshot.get("overview") or {})
    source_context = dict(candidate_context.get("source_context") or {})

    product_id = _first_text(
        tiktok_product.get("product_id"),
        product_result.get("product_id"),
        fastmoss_product.get("product_id"),
        candidate_context.get("product_id"),
    )
    product_url = _first_text(
        tiktok_product.get("normalized_url"),
        tiktok_product.get("product_url"),
        product_result.get("normalized_product_url"),
        candidate_context.get("normalized_product_url"),
        candidate_context.get("product_url"),
        fastmoss_product.get("product_url"),
    )
    title = _first_text(
        logical_fields.get("title"),
        tiktok_product.get("title"),
        fastmoss_product.get("title"),
        candidate_context.get("title"),
        source_context.get("title"),
    )
    fields = {
        "SKU-ID": product_id,
        "产品链接": _normalize_product_url(product_url),
        "商品名称": title,
        "标题": title,
        "商品状态": _keyword_product_status_text(row_status),
        "近7天销量": _metric_text(
            overview_metrics,
            "day7_sold_count",
            "sales_7d",
            "day7_sales",
            "sold_count_7d",
        ),
        "关联节日": _associated_holidays(source_context),
    }
    return {key: value for key, value in fields.items() if value not in ("", None, [], {})}


def _keyword_product_status_text(row_status: str) -> str:
    return {
        "success": "已入库",
        "partial_success": "部分入库",
        "failed": "入库失败",
        "skipped": "已存在",
    }.get(str(row_status or ""), "")


def _fact_bundle_product(fact_bundle: Mapping[str, Any], *, product_id: str) -> dict[str, Any]:
    products = fact_bundle.get("products") if isinstance(fact_bundle, Mapping) else []
    fallback: dict[str, Any] = {}
    for item in products if isinstance(products, list) else []:
        if not isinstance(item, Mapping):
            continue
        current = dict(item)
        if not fallback:
            fallback = current
        if product_id and str(current.get("product_id") or "") == product_id:
            return current
    return fallback


def _metric_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
    return ""


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _associated_holidays(source_context: Mapping[str, Any]) -> list[str]:
    for key in ("associated_holidays", "holiday_tags", "holidays", "关联节日"):
        value = source_context.get(key)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if text:
            return [part.strip() for part in re.split(r"[,，/|]", text) if part.strip()]
    return []


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
        return fallback_status if fallback_status in {"success", "partial_success", "failed"} else "success"
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


def _latest_job(jobs: list[dict[str, Any]], *, job_code: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        selected = job
    return selected


def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("source_record_id") or "") != source_record_id:
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


def _row_failure_reason(
    *,
    row_job: Mapping[str, Any],
    row_payload: Mapping[str, Any],
    row_status: str,
) -> str:
    if row_status == "success":
        return ""
    for source in (row_payload, row_job):
        for key in ("failure_reason", "error_text", "error_message", "error_code"):
            value = _first_text(source.get(key) if isinstance(source, Mapping) else "")
            if value:
                return value
    result = row_job.get("result") if isinstance(row_job, Mapping) else {}
    handler_result = result.get("handler_result") if isinstance(result, Mapping) else {}
    error = handler_result.get("error") if isinstance(handler_result, Mapping) else {}
    if isinstance(error, Mapping):
        return _first_text(error.get("message"), error.get("error_code"))
    return ""


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
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

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
