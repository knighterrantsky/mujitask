from __future__ import annotations

from automation_business_scaffold.control_plane.reconciler.views import (
    build_request_child_views,
    summarize_child_status_counts,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text,
)

from .context.models import *  # noqa: F403
from .context.runtime_views import *  # noqa: F403
from .context.stage_inputs import *  # noqa: F403
from .context.decision_models import *  # noqa: F403
from .context.summary_inputs import *  # noqa: F403


def _refresh_request_counts(*, store: RuntimeStore, request_id: str) -> None:
    request = store.load_task_request(request_id=request_id)
    child_summary = _summarize_request_children_from_store(store=store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        child_total_count=int(child_summary["total_count"]),
        child_terminal_count=int(child_summary["terminal_count"]),
        child_success_count=int(child_summary["success_count"]),
        child_failed_count=int(child_summary["failed_count"]),
        child_skipped_count=int(child_summary["skipped_count"]),
        progress_stage=_current_stage(request),
    )


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
        "request_status": request.result_status or request.status,
        "status": request.status,
        "result_status": request.result_status,
        "current_stage": request.current_stage,
        "request": request.to_dict(),
        "child_summary": child_summary.to_dict(),
        "api_worker_jobs": api_jobs,
        "executions": executions,
        "outbox": outbox,
    }
    if details:
        payload.update(details)
    return payload


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
        "request_status": finalized_request.result_status or finalized_request.status,
        "status": finalized_request.status,
        "result_status": finalized_request.result_status,
        "current_stage": finalized_request.current_stage,
        "summary": dict(finalized_request.summary or {}),
        "result": dict(finalized_request.result or {}),
        "final_status": str(finalized_request.result_status or finalized_request.status or ""),
        "message": "Executor finalized the influencer pool sync request.",
        "outbox": payload.get("outbox", []),
    }


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
            "task_code": TASK_CODE,
            "workflow_code": WORKFLOW_CODE,
            "summary_payload": summary_payload,
            "result": result_payload,
            "message_text": build_tiktok_outbox_message_text(
                request_id=request.request_id,
                task_code=TASK_CODE,
                summary=summary_payload,
                result=result_payload,
                message_format=str(request.payload.get("outbox_message_format") or ""),
                message_template=str(request.payload.get("outbox_message_template") or ""),
            ),
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
