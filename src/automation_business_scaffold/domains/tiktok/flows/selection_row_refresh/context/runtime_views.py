from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult


def child_context(
    parent: HandlerContext,
    *,
    handler_code: str,
    payload: dict[str, Any],
    step_code: str,
    worker_type: str = "api_worker",
    runtime_table: str = "api_worker_job",
    item_code: str = "",
) -> HandlerContext:
    return HandlerContext(
        request_id=parent.request_id,
        job_id=f"{parent.job_id}:{step_code}",
        handler_code=handler_code,
        worker_type=worker_type,  # type: ignore[arg-type]
        runtime_table=runtime_table,  # type: ignore[arg-type]
        payload=payload,
        workflow_code=parent.workflow_code,
        stage_code=parent.stage_code,
        job_code=handler_code if worker_type == "api_worker" else "",
        item_code=item_code,
        business_key=parent.business_key,
        dedupe_key=f"{parent.dedupe_key}:{step_code}" if parent.dedupe_key else f"{parent.job_id}:{step_code}",
        resource_code=parent.resource_code,
        worker_id=parent.worker_id,
        metadata=dict(parent.metadata),
    )


def emit_progress(
    context: HandlerContext,
    progress_stage: str,
    *,
    message: str = "",
    details: Mapping[str, Any] | None = None,
) -> None:
    callback = context.metadata.get("progress_callback")
    if callable(callback):
        callback(progress_stage, message=message, details=dict(details or {}))


def timeline_entry(step: str, handler_result: HandlerResult, *, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {"step": step, "status": handler_result.status}
    if detail:
        payload.update({str(key): value for key, value in detail.items() if value not in ("", None, [], {})})
    if handler_result.error is not None:
        payload["error_type"] = handler_result.error.error_type
        payload["error_code"] = handler_result.error.error_code
    return payload


def skipped_timeline_entry(step: str, *, reason: str) -> dict[str, Any]:
    return {"step": step, "status": "skipped", "reason": reason}
