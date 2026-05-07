from __future__ import annotations

import time

from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)

from .context.models import *  # noqa: F403
from .context.runtime_views import *  # noqa: F403
from .context.stage_inputs import *  # noqa: F403
from .context.decision_models import *  # noqa: F403
from .context.summary_inputs import *  # noqa: F403

def finalize_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row_contexts = _row_contexts(store, request_id=request.request_id)
    all_child_records = _all_child_records(store, request_id=request.request_id)
    outcome = summarize_child_outcomes(all_child_records, optional_codes=OPTIONAL_FINAL_STATUS_CODES)
    explicit_status = str((force_result or {}).get("final_status") or "")
    row_results = [_build_row_result(store=store, request_id=request.request_id, row_context=row) for row in row_contexts]
    final_status = _resolve_final_status_from_rows(
        workflow=workflow,
        row_results=row_results,
        child_records=all_child_records,
        explicit_status=explicit_status,
    )
    warnings = list(dict.fromkeys(_collect_warnings(row_results)))
    summary = {
        "final_status": final_status,
        "child_total_count": int(outcome["total_count"]),
        "child_success_count": int(outcome["success_count"]),
        "child_failed_count": int(outcome["failed_count"]),
        "child_skipped_count": int(outcome["skipped_count"]),
        "warnings": warnings,
    }
    result = {
        "workflow_code": workflow.workflow_code,
        "row_total_count": len(row_contexts),
        "row_success_count": sum(1 for item in row_results if item["row_status"] == "success"),
        "row_failed_count": sum(1 for item in row_results if item["row_status"] == "failed"),
        "row_partial_count": sum(1 for item in row_results if item["row_status"] == "partial_success"),
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
