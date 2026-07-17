from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    api_jobs_for_stage,
    browser_executions_for_stage,
    extract_effective_result_payload,
    extract_handler_result_status,
    render_job_keys,
    timeout_seconds_for_workflow,
    update_request_stage_cursor,
)
from automation_business_scaffold.control_plane.executor.request_aggregation import (
    build_runtime_request_payload,
)
from automation_business_scaffold.domains.amazon.mappers.feishu_product_source_mapper import (
    AMAZON_PRODUCT_SOURCE_FIELDS,
)


TASK_CODE = "refresh_current_amazon_product_table"
READ_STAGE_CODE = "read_amazon_product_rows"
DISPATCH_STAGE_CODE = "dispatch_amazon_product_rows"
ROW_STAGE_CODE = "collect_amazon_product_rows"
BROWSER_STAGE_CODE = "collect_amazon_product_browsers"
SUMMARY_STAGE_CODE = "ready_for_summary"
ROW_JOB_CODE = "amazon_product_row_refresh"
BROWSER_JOB_CODE = "amazon_product_browser_fetch"
ACTIVE_REQUEST_STATUSES = {"pending", "running", "waiting", "cancelling"}
ACTIVE_JOB_STATUSES = {"pending", "running"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running"}
ROW_STATUS_CODES = (
    "success",
    "partial_success",
    "unavailable",
    "blocked",
    "failed",
    "skipped",
)


def advance_stage(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    stage_code: str,
) -> dict[str, Any]:
    if stage_code == READ_STAGE_CODE:
        return _advance_read(store=store, request=request, workflow=workflow)
    if stage_code == DISPATCH_STAGE_CODE:
        return _advance_dispatch(store=store, request=request, workflow=workflow)
    if stage_code == ROW_STAGE_CODE:
        return _advance_rows(store=store, request=request)
    if stage_code == BROWSER_STAGE_CODE:
        return _advance_browsers(store=store, request=request, workflow=workflow)
    if stage_code == SUMMARY_STAGE_CODE:
        return {"action": "advance", "next_stage": SUMMARY_STAGE_CODE}
    return _failure("unsupported_amazon_batch_stage")


def release_request_after_child_completion(
    store: Any,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if (
        request.task_code != TASK_CODE
        or str(request.status or "") not in ACTIVE_REQUEST_STATUSES
    ):
        return []
    stage_code = str(request.current_stage or "").strip() or READ_STAGE_CODE
    next_stage = stage_code
    ready = False
    if stage_code == READ_STAGE_CODE:
        jobs = api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code=READ_STAGE_CODE,
        )
        ready = bool(jobs) and not _has_active_jobs(jobs)
    elif stage_code == ROW_STAGE_CODE:
        jobs = _row_jobs(store=store, request_id=request_id)
        candidates = _browser_candidates(jobs)
        if candidates:
            ready = True
            next_stage = BROWSER_STAGE_CODE
        elif jobs and not _has_active_jobs(jobs) and not _has_waiting_jobs(jobs):
            ready = True
    elif stage_code == BROWSER_STAGE_CODE:
        executions = browser_executions_for_stage(
            store,
            request_id=request_id,
            stage_code=BROWSER_STAGE_CODE,
        )
        ready = bool(executions) and not _has_active_executions(executions)
    if not ready:
        return []
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=next_stage,
        progress_stage=next_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return [
        {
            "request_id": request_id,
            "stage_code": next_stage,
            "released": True,
        }
    ]


def finalize_request(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    row_jobs = _row_jobs(store=store, request_id=request.request_id)
    executions = browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=BROWSER_STAGE_CODE,
    )
    if (
        not force_result
        and (
            _has_active_jobs(row_jobs)
            or _has_waiting_jobs(row_jobs)
            or _has_active_executions(executions)
        )
    ):
        waiting_stage = BROWSER_STAGE_CODE if _browser_candidates(row_jobs) else ROW_STAGE_CODE
        store.update_task_request(
            request_id=request.request_id,
            status="waiting",
            current_stage=waiting_stage,
            progress_stage=waiting_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return build_runtime_request_payload(
            store=store,
            request_id=request.request_id,
            control_action="executor_once",
            message="Amazon batch is waiting for row or browser jobs.",
        )

    adapter_summary = _adapter_summary(request)
    row_results = [_compact_row_result(job) for job in row_jobs]
    counts = {status: 0 for status in ROW_STATUS_CODES}
    for row_result in row_results:
        counts[row_result["row_status"]] += 1
    if force_result:
        final_status = "failed"
        error_code = str(force_result.get("error_code") or "amazon_batch_failed")
    elif not row_results:
        final_status = "success"
        error_code = ""
    elif counts["failed"] + counts["blocked"] == 0 and counts["partial_success"] == 0:
        final_status = "success"
        error_code = ""
    elif counts["success"] + counts["partial_success"] + counts["unavailable"] > 0:
        final_status = "partial_success"
        error_code = ""
    else:
        final_status = "failed"
        error_code = "amazon_batch_rows_failed"

    summary = {
        "final_status": final_status,
        "row_total_count": len(row_results),
        "row_status_counts": counts,
        "adapter_summary": adapter_summary,
    }
    result = {
        "workflow_code": TASK_CODE,
        "selection": {"field": "采集标签", "value": "T"},
        "row_results": row_results,
    }
    finished_at = time.time()
    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
        summary=summary,
        result=result,
        error_text="Amazon batch collection failed." if final_status == "failed" else "",
        error_type="business_workflow_failure" if final_status == "failed" else "",
        error_code=error_code,
        dead_letter_reason="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=finished_at,
    )
    store.create_notification_outbox(
        channel_code=str(getattr(request, "source_channel_code", "") or "noop"),
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(getattr(request, "reply_target", "") or ""),
        payload={
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": TASK_CODE,
            "summary_payload": summary,
            "result": result,
            "message_text": (
                f"Amazon竞品表批量采集完成：T标签 {len(row_results)} 条，"
                f"成功 {counts['success']}，部分成功 {counts['partial_success']}，"
                f"不可用 {counts['unavailable']}，失败 {counts['failed'] + counts['blocked']}。"
            ),
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    payload = build_runtime_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Executor finalized the Amazon competitor-table batch request.",
    )
    payload["request_status"] = updated.result_status or updated.status
    return payload


def _advance_read(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    jobs = api_jobs_for_stage(
        store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
    )
    if not jobs:
        job_def = workflow.require_job("feishu_table_read")
        payload = {
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": workflow.workflow_code,
            "stage_code": READ_STAGE_CODE,
            "source_table_ref": str(request.payload.get("table_ref") or "").strip(),
            "request_payload": dict(request.payload or {}),
            "adapter_code": "amazon_product_batch_source_adapter",
            "field_names": list(AMAZON_PRODUCT_SOURCE_FIELDS),
        }
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=READ_STAGE_CODE,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow,
                        job_def.job_code,
                    ),
                }
            ],
        )
        return _waiting(
            READ_STAGE_CODE,
            "Dispatched Amazon竞品表 T-tagged row read.",
            dispatch,
        )
    if _has_active_jobs(jobs):
        return _waiting(READ_STAGE_CODE, "Amazon竞品表 row read is still running.")
    read_job = jobs[-1]
    if extract_handler_result_status(read_job) not in {"success", "partial_success"}:
        return _failure("feishu_table_read_failed")
    read_result = extract_effective_result_payload(read_job)
    source_rows = [
        dict(row)
        for row in read_result.get("source_rows") or []
        if isinstance(row, Mapping)
    ]
    update_request_stage_cursor(
        store=store,
        request=request,
        stage_code=READ_STAGE_CODE,
        payload={
            "source_rows": source_rows,
            "source_table_identity": dict(
                read_result.get("source_table_identity") or {}
            ),
            "adapter_summary": dict(read_result.get("adapter_summary") or {}),
        },
    )
    return {"action": "advance", "next_stage": DISPATCH_STAGE_CODE}


def _advance_dispatch(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    read_cursor = _stage_result(request, READ_STAGE_CODE)
    rows = [
        dict(row)
        for row in read_cursor.get("source_rows") or []
        if isinstance(row, Mapping)
    ]
    runtime_context = dict((request.stage_cursor or {}).get("runtime_context") or {})
    source_table_identity = dict(read_cursor.get("source_table_identity") or {})
    if rows and not runtime_context:
        return _failure("amazon_browser_resource_context_missing")
    if rows and not all(
        str(source_table_identity.get(key) or "").strip()
        for key in ("base_id", "table_id")
    ):
        return _failure("source_table_identity_missing")

    row_job_def = workflow.require_job(ROW_JOB_CODE)
    jobs: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    row_refs: list[dict[str, str]] = []
    for row in rows:
        source_record_id = str(row.get("source_record_id") or "").strip()
        requested_asin = str(row.get("requested_asin") or "").strip()
        canonical_url = str(row.get("canonical_url") or "").strip()
        if not source_record_id or source_record_id in seen_record_ids:
            continue
        seen_record_ids.add(source_record_id)
        payload = {
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": workflow.workflow_code,
            "stage_code": ROW_STAGE_CODE,
            "table_ref": str(request.payload.get("table_ref") or "").strip(),
            "source_record_id": source_record_id,
            "requested_asin": requested_asin,
            "canonical_url": canonical_url,
            "source_table_identity": source_table_identity,
            "runtime_context": runtime_context,
        }
        keys = render_job_keys(
            row_job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=ROW_STAGE_CODE,
        )
        jobs.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "payload": payload,
                "max_attempts": 3,
                "max_execution_seconds": timeout_seconds_for_workflow(
                    workflow,
                    row_job_def.job_code,
                ),
            }
        )
        row_refs.append(
            {
                "source_record_id": source_record_id,
                "requested_asin": requested_asin,
            }
        )
    dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code=row_job_def.job_code,
        jobs=jobs,
    )
    update_request_stage_cursor(
        store=store,
        request=request,
        stage_code=DISPATCH_STAGE_CODE,
        payload={
            "row_jobs": row_refs,
            "dispatched_row_count": len(row_refs),
        },
    )
    return {
        "action": "advance",
        "next_stage": ROW_STAGE_CODE,
        "details": {
            "dispatched_row_count": len(row_refs),
            "dispatch": dispatch,
        },
    }


def _advance_rows(*, store: Any, request: Any) -> dict[str, Any]:
    jobs = _row_jobs(store=store, request_id=request.request_id)
    if not jobs:
        return {"action": "advance", "next_stage": SUMMARY_STAGE_CODE}
    if _has_active_jobs(jobs):
        return _waiting(ROW_STAGE_CODE, "Waiting for Amazon row refresh jobs.")
    candidates = _browser_candidates(jobs)
    update_request_stage_cursor(
        store=store,
        request=request,
        stage_code=ROW_STAGE_CODE,
        payload={
            "row_job_count": len(jobs),
            "browser_required_count": len(candidates),
        },
    )
    if candidates:
        return {
            "action": "advance",
            "next_stage": BROWSER_STAGE_CODE,
            "details": {"browser_required_count": len(candidates)},
        }
    if _has_waiting_jobs(jobs):
        return _failure("amazon_row_wait_state_invalid")
    return {"action": "advance", "next_stage": SUMMARY_STAGE_CODE}


def _advance_browsers(
    *,
    store: Any,
    request: Any,
    workflow: Any,
) -> dict[str, Any]:
    row_jobs = _row_jobs(store=store, request_id=request.request_id)
    candidates = _browser_candidates(row_jobs)
    executions = browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=BROWSER_STAGE_CODE,
    )
    if not candidates:
        return {"action": "advance", "next_stage": ROW_STAGE_CODE}

    execution_by_row = {
        str((execution.payload or {}).get("source_record_id") or ""): execution
        for execution in executions
    }
    dispatch_items: list[dict[str, Any]] = []
    browser_job_def = workflow.require_job(BROWSER_JOB_CODE)
    for candidate in candidates:
        source_record_id = candidate["source_record_id"]
        if source_record_id in execution_by_row:
            continue
        browser_request = candidate["browser_request"]
        browser_payload = dict(browser_request["payload"])
        keys = render_job_keys(
            browser_job_def,
            browser_payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=BROWSER_STAGE_CODE,
            item_code=browser_job_def.job_code,
        )
        dispatch_items.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "resource_code": browser_request["resource_code"],
                "payload": browser_payload,
                "max_attempts": 3,
                "max_execution_seconds": timeout_seconds_for_workflow(
                    workflow,
                    browser_job_def.job_code,
                ),
            }
        )
    if dispatch_items:
        dispatch = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code=browser_job_def.job_code,
            workflow_code=workflow.workflow_code,
            items=dispatch_items,
        )
        update_request_stage_cursor(
            store=store,
            request=request,
            stage_code=BROWSER_STAGE_CODE,
            payload={
                "browser_required_count": len(candidates),
                "browser_dispatch": dispatch,
            },
        )
        return _waiting(
            BROWSER_STAGE_CODE,
            "Dispatched primary Amazon browser collection.",
            dispatch,
        )
    if _has_active_executions(executions):
        return _waiting(
            BROWSER_STAGE_CODE,
            "Waiting for primary Amazon browser collection.",
        )

    requeued = 0
    for candidate in candidates:
        execution = execution_by_row.get(candidate["source_record_id"])
        if execution is None or str(execution.status or "") not in {"finished", "cancelled"}:
            continue
        row_payload = dict(candidate["row_payload"])
        row_payload["browser_execution"] = {
            "execution_id": str(execution.execution_id),
            "status": extract_handler_result_status(execution, default="failed"),
            "result": extract_effective_result_payload(execution),
            "error_code": str(getattr(execution, "error_code", "") or ""),
        }
        store.requeue_waiting_api_worker_job(
            job_id=candidate["row_job_id"],
            payload=row_payload,
            stage=ROW_STAGE_CODE,
        )
        requeued += 1
    update_request_stage_cursor(
        store=store,
        request=request,
        stage_code=BROWSER_STAGE_CODE,
        payload={
            "browser_execution_count": len(executions),
            "requeued_row_count": requeued,
        },
    )
    if requeued:
        return _waiting(
            ROW_STAGE_CODE,
            "Requeued Amazon row refresh jobs with browser results.",
        )
    return _failure("amazon_browser_result_not_requeued")


def _row_jobs(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    return [
        job
        for job in api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code=ROW_STAGE_CODE,
        )
        if str(job.get("job_code") or "") == ROW_JOB_CODE
    ]


def _browser_candidates(jobs: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in jobs:
        if str(job.get("status") or "") != "waiting":
            continue
        result = extract_effective_result_payload(job)
        browser_request = (
            dict(result.get("browser_request"))
            if isinstance(result.get("browser_request"), Mapping)
            else {}
        )
        browser_payload = (
            dict(browser_request.get("payload"))
            if isinstance(browser_request.get("payload"), Mapping)
            else {}
        )
        source_record_id = str(
            result.get("source_record_id")
            or (job.get("payload") or {}).get("source_record_id")
            or ""
        ).strip()
        if (
            not bool(result.get("browser_required"))
            or browser_request.get("handler_code") != BROWSER_JOB_CODE
            or not str(browser_request.get("resource_code") or "").strip()
            or browser_payload.get("source_record_id") != source_record_id
            or browser_payload.get("stage_code") != BROWSER_STAGE_CODE
        ):
            continue
        candidates.append(
            {
                "row_job_id": str(job.get("job_id") or ""),
                "source_record_id": source_record_id,
                "row_payload": dict(job.get("payload") or {}),
                "browser_request": browser_request,
            }
        )
    return candidates


def _compact_row_result(job: Mapping[str, Any]) -> dict[str, str]:
    result = extract_effective_result_payload(job)
    status = str(result.get("row_status") or "").strip()
    if status not in ROW_STATUS_CODES:
        effective = extract_handler_result_status(job, default="failed")
        status = effective if effective in ROW_STATUS_CODES else "failed"
    return {
        "job_id": str(job.get("job_id") or ""),
        "source_record_id": str(
            result.get("source_record_id")
            or (job.get("payload") or {}).get("source_record_id")
            or ""
        ),
        "requested_asin": str(
            result.get("requested_asin")
            or (job.get("payload") or {}).get("requested_asin")
            or ""
        ),
        "row_status": status,
        "error_code": str(job.get("error_code") or ""),
    }


def _adapter_summary(request: Any) -> dict[str, Any]:
    cursor = _stage_result(request, READ_STAGE_CODE)
    summary = dict(cursor.get("adapter_summary") or {})
    allowed = {
        "adapter_code",
        "input_row_count",
        "tagged_row_count",
        "source_row_count",
        "selection_field",
        "selection_value",
        "invalid_asin_count",
        "identity_mismatch_count",
        "unsupported_marketplace_count",
        "missing_record_id_count",
    }
    return {key: summary[key] for key in allowed if key in summary}


def _stage_result(request: Any, stage_code: str) -> dict[str, Any]:
    cursor = dict(getattr(request, "stage_cursor", {}) or {})
    stage_results = dict(cursor.get("stage_results") or {})
    return dict(stage_results.get(stage_code) or {})


def _has_active_jobs(jobs: list[Mapping[str, Any]]) -> bool:
    return any(str(job.get("status") or "") in ACTIVE_JOB_STATUSES for job in jobs)


def _has_waiting_jobs(jobs: list[Mapping[str, Any]]) -> bool:
    return any(str(job.get("status") or "") == "waiting" for job in jobs)


def _has_active_executions(executions: list[Any]) -> bool:
    return any(
        str(getattr(execution, "status", "") or "") in ACTIVE_EXECUTION_STATUSES
        for execution in executions
    )


def _waiting(
    stage_code: str,
    message: str,
    dispatch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    details = {"dispatch": dict(dispatch)} if dispatch else {}
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": details,
    }


def _failure(error_code: str) -> dict[str, Any]:
    return {
        "action": "finalize",
        "final_status": "failed",
        "error_code": error_code,
        "result": {"status": "failed", "error_code": error_code},
    }


__all__ = [
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
]
