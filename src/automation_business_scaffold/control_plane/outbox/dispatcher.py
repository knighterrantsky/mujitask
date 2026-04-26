from __future__ import annotations

import json
from typing import Any

from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.control_plane.executor.looping import (
    build_child_runner_config,
    run_control_loop,
    supervisor_error_payload,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_idle_payload,
    build_runtime_settings,
    create_runtime_store,
)
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    run_supervised_handler,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

OUTBOX_HANDLER_REGISTRY: Any | None = None


def dispatch_outbox_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    outbox = store.claim_next_outbox(worker_id=settings.worker_id, lease_seconds=settings.lease_seconds)
    if outbox is None:
        return build_idle_payload(
            control_action="outbox_once",
            actor="dispatcher",
            message="No outbox message is ready to dispatch.",
        )

    context = HandlerContext(
        request_id=outbox.ref_id,
        job_id=outbox.outbox_id,
        handler_code="outbox_dispatch",
        worker_type="outbox_dispatcher",
        runtime_table="notification_outbox",
        payload=dict(outbox.payload or {}),
        workflow_code="",
        stage_code="ready_for_summary",
        job_code="outbox_dispatch",
        worker_id=settings.worker_id,
        metadata={"channel_code": outbox.channel_code, "reply_target": outbox.reply_target},
    )

    outcome = run_supervised_handler(
        context=context,
        dispatch=_dispatch_outbox_runtime_handler,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: store.heartbeat_outbox(
                outbox_id=outbox.outbox_id,
                lease_seconds=settings.lease_seconds,
            ),
            on_progress=lambda event: store.update_outbox_progress(
                outbox_id=outbox.outbox_id,
                progress_stage=event.progress_stage,
                lease_seconds=settings.lease_seconds,
            ),
        ),
        child_runner_config=build_child_runner_config(
            params,
            worker_type="outbox_dispatcher",
            handler_code="outbox_dispatch",
            runtime_timeout_seconds=outbox.max_execution_seconds,
        ),
    )
    if outcome.should_mark_failed:
        retryable = outcome.error.retryable if outcome.error is not None else True
        failed = store.mark_outbox_retry_or_failed(
            outbox_id=outbox.outbox_id,
            error_text=outcome.error_text,
            retry_delay_seconds=settings.retry_delay_seconds,
            retryable=retryable,
            error_type=outcome.error.error_type if outcome.error is not None else "",
            error_code=outcome.error.error_code if outcome.error is not None else "",
            dead_letter_reason="supervisor_failed"
            if outcome.error is not None and outcome.error.terminal
            else "",
        )
        payload = {
            "control_action": "outbox_once",
            "dispatcher_status": "processed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1 if failed.status == "failed" else 0,
            "message": "Outbox dispatcher handler returned failure.",
            "summary": {"total": 1, "counts": {failed.status: 1}},
            "item": failed.to_dict(),
            "items": [failed.to_dict()],
            "request_id": failed.ref_id,
            "outbox_id": failed.outbox_id,
            "channel_code": failed.channel_code,
            "retry_count": failed.retry_count,
            "retry_scheduled_count": 1 if failed.status == "retry_wait" else 0,
            "worker_result": outcome.worker_result.to_dict(),
            "supervisor": outcome.to_dict(),
            "error": failed.last_error_text,
        }
        if outcome.error is not None:
            payload.update(supervisor_error_payload(outcome))
        return payload

    sent = store.mark_outbox_sent(outbox_id=outbox.outbox_id)
    return {
        "control_action": "outbox_once",
        "dispatcher_status": "processed",
        "processed_count": 1,
        "success_count": 1,
        "failed_count": 0,
        "message": "Outbox dispatcher sent one message through the runtime handler.",
        "summary": {"total": 1, "counts": {"sent": 1}},
        "item": sent.to_dict(),
        "items": [sent.to_dict()],
        "request_id": sent.ref_id,
        "outbox_id": sent.outbox_id,
        "channel_code": sent.channel_code,
        "worker_result": outcome.worker_result.to_dict(),
        "supervisor": outcome.to_dict(),
    }


def run_outbox_dispatcher(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
        params=params,
        actor="dispatcher",
        once_func=dispatch_outbox_once,
        idle_status_key="dispatcher_status",
    )


def ensure_request_outbox(
    *,
    store: RuntimeStore,
    request_id: str,
) -> None:
    request = store.load_task_request(request_id=request_id)
    summary = dict(request.summary or {})
    result = dict(request.result or {})
    store.create_notification_outbox(
        channel_code=request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=request.reply_target,
        payload={
            "message_text": _build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
            ),
            "request_id": request.request_id,
            "task_code": request.task_code,
            "summary": summary,
            "result": result,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )


def _dispatch_outbox_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_outbox_handler_registry().dispatch("outbox_dispatch", context)


def _build_outbox_message_text(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> str:
    preview = {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "result_keys": sorted(result.keys()),
    }
    return json.dumps(preview, ensure_ascii=False)


def _build_bound_outbox_handler_registry() -> Any:
    if OUTBOX_HANDLER_REGISTRY is not None:
        return OUTBOX_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.outbox import (
        build_bound_outbox_handler_registry,
    )

    return build_bound_outbox_handler_registry()


__all__ = ["dispatch_outbox_once", "ensure_request_outbox", "run_outbox_dispatcher"]
