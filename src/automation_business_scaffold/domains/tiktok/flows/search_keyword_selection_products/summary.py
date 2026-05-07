from __future__ import annotations

import time
from typing import Any, Mapping

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    all_child_records as _all_child_records,
    api_jobs_for_stage as _api_jobs_for_stage,
    compute_final_status,
    extract_effective_result_payload,
    summarize_child_outcomes,
    summarize_stage_children,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)

from .context import (
    OPTIONAL_FINAL_STATUS_CODES,
    _candidate_contexts,
    _first_text,
    _keyword_seed_import_payload,
    _latest_row_job,
    _record_effective_status,
    _seed_context_by_candidate_key,
    _seed_contexts,
)


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


def _build_row_result(
    *,
    store: RuntimeStore,
    request_id: str,
    candidate_context: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_key = str(candidate_context.get("candidate_key") or "")
    seed_context = _seed_context_by_candidate_key(store=store, request_id=request_id).get(candidate_key, {})
    row_jobs = [
        *_api_jobs_for_stage(store=store, request_id=request_id, stage_code="refresh_selection_rows"),
        *_api_jobs_for_stage(
            store=store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        ),
    ]
    row_job = _latest_row_job(
        row_jobs,
        source_record_id=str(seed_context.get("source_record_id") or ""),
        job_code="selection_row_refresh",
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
            "selection_row_refresh_status": _record_effective_status(row_job),
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
            "selection_row_refresh_status": "",
            "tiktok_status": "",
            "browser_status": "",
            "media_status": "",
            "fastmoss_status": "",
            "fact_status": "",
            "writeback_status": "",
        }
    row_status = _derive_row_status(seed_status=str(seed_context.get("seed_status") or ""), row_job=None)
    return {
        "candidate_key": candidate_key,
        "product_id": str(candidate_context.get("product_id") or ""),
        "source_record_id": str(seed_context.get("source_record_id") or ""),
        "feishu_row": dict(seed_context.get("feishu_row") or {}),
        "row_status": row_status,
        "seed_status": str(seed_context.get("seed_status") or ""),
        "failure_reason": "selection_row_refresh_not_dispatched" if row_status == "failed" else "",
        "selection_row_refresh_status": "",
        "tiktok_status": "",
        "browser_status": "",
        "media_status": "",
        "fastmoss_status": "",
        "fact_status": "",
        "writeback_status": "",
    }


def _derive_row_status(
    *,
    seed_status: str,
    row_job: Mapping[str, Any] | None,
) -> str:
    row_status = _record_effective_status(row_job)
    if row_status in {"success", "partial_success", "failed"}:
        return row_status
    if seed_status == "skipped":
        return "skipped"
    if seed_status == "success":
        return "failed"
    if seed_status == "failed":
        return "failed"
    return "skipped"


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
