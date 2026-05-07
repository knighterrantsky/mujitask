from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "read_competitor_rows"

def _advance_read_competitor_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "read_competitor_rows"
    source_table_ref = _source_table_ref_from_request_payload(request.payload)
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    stage_job = workflow.require_stage(stage_code).job_bindings[0]
    job_def = workflow.require_job(stage_job.job_code)
    explicit_identity_lookup = _has_explicit_identity_lookup(request.payload)
    explicit_row_selection = explicit_identity_lookup or _has_explicit_record_selection(request.payload)
    if not jobs:
        field_names = list(request.payload.get("field_names") or ()) or list(DEFAULT_COMPETITOR_READ_FIELDS)
        filter_spec = dict(request.payload.get("refresh_filter") or request.payload.get("filter_spec") or {})
        if not filter_spec and not explicit_row_selection:
            filter_spec = dict(DEFAULT_COMPETITOR_FILTER_SPEC)
        payload = {
            **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
            **_payload_subset(request.payload, FEISHU_READ_PASSTHROUGH_KEYS),
            "stage_code": stage_code,
            "source_table_ref": source_table_ref,
            "view_ref": str(request.payload.get("view_ref") or ""),
            "field_names": field_names,
            "filter_spec": filter_spec,
            "product_id": str(request.payload.get("product_id") or ""),
            "product_url": str(request.payload.get("product_url") or ""),
            "source_record_ids": list(_list_text(request.payload.get("source_record_ids"))),
            "adapter_code": stage_job.adapter_code,
            "cursor_context": dict(request.stage_cursor.get(stage_code) or {}),
        }
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
        store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued Feishu competitor table read.",
            details={"job_code": job_def.job_code},
        )
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row read job to finish.")

    source_rows = select_latest_successful_api_job(jobs, "feishu_table_read")
    read_payload = extract_effective_result_payload(source_rows) if isinstance(source_rows, Mapping) else {}
    empty_row_deletes = _empty_row_delete_records(read_payload)
    cleanup_jobs = [
        job
        for job in jobs
        if str(job.get("job_code") or "") == "feishu_table_write"
        and str((job.get("payload") or {}).get("cleanup_kind") or "") == "delete_empty_rows"
    ]
    if empty_row_deletes and not cleanup_jobs:
        cleanup_job_def = workflow.require_job("feishu_table_write")
        cleanup_payload = build_projection_write_payload(
            stage_code=stage_code,
            request_id=request.request_id,
            target_table_ref=source_table_ref,
            records=empty_row_deletes,
            mapper_code="",
            write_mode="delete",
        )
        cleanup_payload.update(_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code))
        cleanup_payload.update(_payload_subset(request.payload, FEISHU_WRITE_PASSTHROUGH_KEYS))
        cleanup_payload["cleanup_kind"] = "delete_empty_rows"
        keys = render_job_keys(
            cleanup_job_def,
            request.payload,
            cleanup_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
            job_code=cleanup_job_def.job_code,
        )
        store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=cleanup_job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], cleanup_job_def.job_code),
                    "payload": cleanup_payload,
                    "max_execution_seconds": _timeout_seconds(workflow, cleanup_job_def.job_code),
                }
            ],
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued empty competitor row cleanup.",
            details={"empty_row_delete_count": len(empty_row_deletes)},
        )
    if _any_api_jobs_active(cleanup_jobs):
        return _waiting(stage_code=stage_code, message="Waiting for empty row cleanup to finish.")

    row_contexts = _normalize_source_rows(
        read_payload.get("source_rows")
    )
    adapter_summary = dict(read_payload.get("adapter_summary") or {})
    if explicit_identity_lookup:
        lookup_status = str(adapter_summary.get("lookup_status") or "")
        if lookup_status == "ambiguous_match":
            return {
                "action": "finalize",
                "final_status": "failed",
                "result": {
                    "status": "failed",
                    "message": "product_url matched multiple competitor rows",
                    "product_url": str(request.payload.get("product_url") or ""),
                    "matched_row_count": int(adapter_summary.get("matched_row_count") or 0),
                    "matched_record_ids": list(adapter_summary.get("matched_record_ids") or []),
                },
                "summary": {"total": 1, "counts": {"ambiguous_competitor_row": 1}},
                "details": {
                    "product_url": str(request.payload.get("product_url") or ""),
                    "matched_row_count": int(adapter_summary.get("matched_row_count") or 0),
                },
            }
        if not row_contexts:
            return {
                "action": "finalize",
                "final_status": "failed",
                "result": {
                    "status": "failed",
                    "message": "product_url was not found in the competitor table",
                    "product_url": str(request.payload.get("product_url") or ""),
                },
                "summary": {"total": 1, "counts": {"competitor_row_not_found": 1}},
                "details": {"product_url": str(request.payload.get("product_url") or "")},
            }
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "row_contexts": row_contexts,
            "row_total_count": len(row_contexts),
            "read_job_statuses": [str(job.get("status") or "") for job in jobs],
        },
    )
    return {
        "action": "advance",
        "next_stage": "dispatch_product_collection",
        "details": {
            "row_total_count": len(row_contexts),
            "read_job_count": len(jobs),
        },
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_read_competitor_rows(store=store, request=request, workflow=workflow)
