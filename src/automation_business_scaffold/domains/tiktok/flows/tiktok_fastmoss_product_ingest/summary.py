from __future__ import annotations

import time

from automation_business_scaffold.control_plane.runtime_config.settings import build_request_payload
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)

from .context.models import *  # noqa: F403
from .context.runtime_views import *  # noqa: F403
from .context.stage_inputs import *  # noqa: F403
from .context.decision_models import *  # noqa: F403
from .context.summary_inputs import *  # noqa: F403


def _refresh_request_aggregate_counts(store: RuntimeStore, *, request_id: str) -> None:
    counts = _aggregate_request_children(store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        child_total_count=counts["total"],
        child_terminal_count=counts["terminal_count"],
        child_success_count=counts["success_count"],
        child_failed_count=counts["failed_count"],
        child_skipped_count=counts["skipped_count"],
    )

def finalize_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    row_jobs = _row_refresh_jobs_for_summary(store=store, request_id=request.request_id)

    row_results_by_key: dict[str, dict[str, Any]] = {}
    for job in row_jobs:
        handler_result = _job_handler_result(job)
        handler_summary = _mapping(handler_result.get("summary"))
        row_result = extract_effective_result_payload(job)
        source_record_id = (
            row_result.get("source_record_id")
            or handler_result.get("source_record_id")
            or handler_summary.get("source_record_id")
            or (job.get("payload") or {}).get("source_record_id", "")
        )
        product_id = (
            row_result.get("product_business_key")
            or row_result.get("business_entity_key")
            or handler_result.get("product_business_key")
            or handler_summary.get("product_business_key")
            or ""
        )
        row_key = _first_non_empty(source_record_id, product_id, job.get("business_key"), job.get("job_id"))
        row_results_by_key[row_key] = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "row_status": row_result.get("row_status")
            or handler_result.get("row_status")
            or job.get("status", ""),
        }
    row_results = list(row_results_by_key.values())

    counts = _aggregate_request_children(store, request_id=request.request_id)
    final_status = _determine_final_status(
        force_result=force_result,
        row_jobs=row_jobs,
        row_results=row_results,
        counts=counts,
    )
    summary = {
        "final_status": final_status,
        "total": counts["total"],
        "counts": counts["counts"],
        "child_success_count": counts["success_count"],
        "child_failed_count": counts["failed_count"],
        "child_skipped_count": counts["skipped_count"],
    }
    if force_result and isinstance(force_result.get("summary"), Mapping):
        summary.update(dict(force_result.get("summary") or {}))

    final_result = {
        "row_count": len(row_results),
        "rows": row_results,
        "selection_table_read_summary": _selection_table_read_summary(
            store=store,
            request_id=request.request_id,
        ),
    }
    if force_result and isinstance(force_result.get("result"), Mapping):
        final_result.update(dict(force_result.get("result") or {}))

    error_text = "" if final_status != "failed" else str(final_result.get("message") or "")
    store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage="completed",
        summary=summary,
        result=final_result,
        error_text=error_text,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    _ensure_request_outbox(store=store, request_id=request.request_id)
    _refresh_request_aggregate_counts(store, request_id=request.request_id)
    payload = build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Executor finalized the product ingest request.",
    )
    payload["final_status"] = final_status
    return payload


def _ensure_request_outbox(*, store: RuntimeStore, request_id: str) -> None:
    request = store.load_task_request(request_id=request_id)
    summary = dict(request.summary or {})
    result = dict(request.result or {})
    store.create_notification_outbox(
        channel_code=request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=request.reply_target,
        payload={
            "message_text": build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
                message_format=str(request.payload.get("outbox_message_format") or ""),
                message_template=str(request.payload.get("outbox_message_template") or ""),
            ),
            "request_id": request.request_id,
            "task_code": request.task_code,
            "summary": summary,
            "result": result,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )


def _selection_table_read_summary(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    list_summaries = getattr(store, "list_api_worker_job_summaries_for_request", None)
    jobs = (
        [
            job
            for job in list_summaries(request_id=request_id, job_code="feishu_table_read")
            if str((job.get("payload") or {}).get("stage_code") or "") == "read_selection_rows"
        ]
        if callable(list_summaries)
        else _api_jobs_for_stage(store, request_id=request_id, stage_code="read_selection_rows")
    )
    read_job = _latest_api_job_by_code(jobs, "feishu_table_read")
    summary = _mapping(read_job.get("summary")) if isinstance(read_job, Mapping) else {}
    return {
        key: value
        for key, value in {
            "status": read_job.get("result_status") or read_job.get("status") if isinstance(read_job, Mapping) else "",
            "raw_row_count": summary.get("raw_row_count"),
            "source_row_count": summary.get("source_row_count"),
            "empty_row_count": summary.get("empty_row_count"),
            "raw_snapshot_ref": summary.get("raw_snapshot_ref"),
        }.items()
        if value not in ("", None, [], {})
    }
