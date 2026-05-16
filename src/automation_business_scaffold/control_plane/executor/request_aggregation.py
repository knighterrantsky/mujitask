from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running", "waiting"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running", "waiting"}


def build_runtime_request_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    refresh_request_aggregate_counts(store, request_id=request_id)
    request = store.load_task_request(request_id=request_id)
    result = dict(request.result or {})
    summary = dict(request.summary or {})
    exposed_request_status = request.result_status or request.status
    api_worker_job_summary = store.summarize_api_worker_jobs_for_request(request_id=request_id)
    task_execution_summary = store.summarize_task_executions_for_request(request_id=request_id)
    api_worker_jobs = store.list_api_worker_job_summaries_for_request(request_id=request_id)
    executions = store.list_task_execution_summaries_for_request(request_id=request_id)
    outbox = [record.to_dict() for record in store.list_request_outbox(request_id=request_id)]
    return {
        "control_action": control_action,
        "message": message,
        "request_id": request.request_id,
        "task_code": request.task_code,
        "request_status": exposed_request_status,
        "status": request.status,
        "result_status": request.result_status,
        "current_stage": request.current_stage,
        "summary": summary or {"total": 0, "counts": {}},
        "result": result,
        "error": request.error_text,
        "child_total_count": request.child_total_count,
        "child_terminal_count": request.child_terminal_count,
        "child_success_count": request.child_success_count,
        "child_failed_count": request.child_failed_count,
        "child_skipped_count": request.child_skipped_count,
        "task_request": request.to_dict(),
        "executions": executions,
        "task_execution_summary": task_execution_summary,
        "api_worker_jobs": api_worker_jobs,
        "api_worker_job_summary": api_worker_job_summary,
        "outbox": outbox,
        "item": {
            "request_id": request.request_id,
            "status": request.status,
            "result_status": request.result_status,
            "current_stage": request.current_stage,
            "task_code": request.task_code,
        },
        "items": _request_items(result),
    }


def refresh_request_aggregate_counts(store: RuntimeStore, *, request_id: str) -> None:
    counts = aggregate_request_children(store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        child_total_count=counts["total"],
        child_terminal_count=counts["terminal_count"],
        child_success_count=counts["success_count"],
        child_failed_count=counts["failed_count"],
        child_skipped_count=counts["skipped_count"],
    )


def aggregate_request_children(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    if hasattr(store, "summarize_api_worker_jobs_for_request") and hasattr(
        store, "summarize_task_executions_for_request"
    ):
        api_summary = store.summarize_api_worker_jobs_for_request(request_id=request_id)
        execution_summary = store.summarize_task_executions_for_request(request_id=request_id)
        return _merge_child_summaries(api_summary, execution_summary)

    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    counts: dict[str, int] = {}
    success_count = 0
    failed_count = 0
    skipped_count = 0
    fallback_required_count = 0
    active_count = 0

    for job in api_jobs:
        handler_status = _handler_status_from_api_job(job)
        if str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            fallback_required_count += 1
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or str(job.get("result_status") or job.get("status") or "unknown")
        counts[status_key] = counts.get(status_key, 0) + 1

    for execution in executions:
        handler_status = _handler_status_from_execution(execution)
        if execution.status in ACTIVE_EXECUTION_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            fallback_required_count += 1
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or getattr(execution, "result_status", "") or execution.status or "unknown"
        counts[status_key] = counts.get(status_key, 0) + 1

    total = len(api_jobs) + len(executions)
    terminal_count = max(total - active_count, 0)
    return {
        "total": total,
        "counts": counts,
        "terminal_count": terminal_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "fallback_required_count": fallback_required_count,
        "active_count": active_count,
    }


def _merge_child_summaries(*summaries: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    total = 0
    active_count = 0
    success_count = 0
    failed_count = 0
    skipped_count = 0
    fallback_required_count = 0
    for summary in summaries:
        total += int(summary.get("total") or 0)
        active_count += int(summary.get("active_count") or 0)
        success_count += int(summary.get("success_count") or 0)
        failed_count += int(summary.get("failed_count") or 0)
        skipped_count += int(summary.get("skipped_count") or 0)
        fallback_required_count += int(summary.get("fallback_required_count") or 0)
        for status, count in dict(summary.get("counts") or {}).items():
            status_key = str(status or "unknown")
            counts[status_key] = counts.get(status_key, 0) + int(count or 0)
    return {
        "total": total,
        "counts": counts,
        "terminal_count": max(total - active_count, 0),
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "fallback_required_count": fallback_required_count,
        "active_count": active_count,
    }


def _request_items(request_result: dict[str, Any]) -> list[dict[str, Any]]:
    items = request_result.get("items")
    if isinstance(items, list):
        return [dict(item) for item in items if isinstance(item, dict)]
    return []


def _handler_status_from_api_job(job: Mapping[str, Any] | None) -> str:
    if not job:
        return ""
    handler_result = _job_handler_result(job)
    return str(handler_result.get("status") or job.get("result_status") or job.get("status") or "")


def _handler_status_from_execution(execution: Any) -> str:
    if execution is None:
        return ""
    result = dict(execution.result or {})
    handler_result = result.get("handler_result")
    if isinstance(handler_result, Mapping):
        return str(handler_result.get("status") or getattr(execution, "result_status", "") or execution.status or "")
    return str(getattr(execution, "result_status", "") or execution.status or "")


def _job_handler_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    handler_result = result.get("handler_result")
    return dict(handler_result or {}) if isinstance(handler_result, Mapping) else {}
