from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping

from automation_business_scaffold.business.flows.runtime_common import (
    FORMAL_TASK_CODES,
    INFLUENCER_POOL_TASK_CODE,
    KEYWORD_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
    REFRESH_TASK_CODE,
    build_idle_payload,
    build_outbox_message_text,
    build_request_payload,
    build_runtime_settings,
    create_runtime_store,
    ensure_formal_task_code,
    normalize_control_action,
)
from automation_business_scaffold.business.flows.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    ExecutionSupervisorOutcome,
    run_supervised_handler,
)
from automation_business_scaffold.business.flows.child_runner import ChildRunnerConfig
from automation_business_scaffold.business.flows.runtime_workflow_registry import load_workflow_runtime
from automation_business_scaffold.business.handlers.contract import HandlerContext
from automation_business_scaffold.business.workflow_defs import WorkflowDefinition, get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_API_JOB_STATUSES = {"success", "failed", "cancelled"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_EXECUTION_STATUSES = {"success", "failed", "skipped", "cancelled"}
MAX_EXECUTOR_STAGE_HOPS = 16
DEFAULT_CHILD_TIMEOUT_SECONDS_BY_WORKER = {
    "api_worker": 300.0,
    "browser_worker": 900.0,
    "outbox_dispatcher": 60.0,
}
PHASE2_NOT_READY_MESSAGE = (
    "Phase 2 runtime orchestration is currently wired only for tiktok_fastmoss_product_ingest."
)
API_HANDLER_REGISTRY: Any | None = None
BROWSER_HANDLER_REGISTRY: Any | None = None
OUTBOX_HANDLER_REGISTRY: Any | None = None


class _LegacyProductIngestRuntime:
    @staticmethod
    def advance_stage(
        *,
        store: RuntimeStore,
        request: Any,
        workflow: WorkflowDefinition,
        stage_code: str,
    ) -> dict[str, Any]:
        return _advance_product_ingest_stage(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=stage_code,
        )

    @staticmethod
    def finalize_request(
        *,
        store: RuntimeStore,
        request: Any,
        workflow: WorkflowDefinition,
        force_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _finalize_product_ingest_request(
            store=store,
            request=request,
            workflow=workflow,
            force_result=force_result,
        )

    @staticmethod
    def release_request_after_child_completion(
        store: RuntimeStore,
        *,
        request_id: str,
    ) -> list[dict[str, Any]]:
        return _release_product_ingest_request_after_child_completion(store=store, request_id=request_id)


def submit_task_request(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_task_code = ensure_formal_task_code(task_code)
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=normalized_task_code,
        payload=_sanitize_task_payload(params),
        requested_by=settings.requested_by,
        trigger_mode=str(params.get("trigger_mode") or "manual"),
        source_channel_code=str(params.get("notification_channel_code") or params.get("source_channel_code") or "noop"),
        source_session_id=str(params.get("source_session_id") or ""),
        reply_target=str(params.get("reply_target") or ""),
        idempotency_key=str(params.get("idempotency_key") or "").strip(),
    )
    if not str(request.current_stage or "").strip():
        store.update_task_request(
            request_id=request.request_id,
            current_stage=_initial_stage_for_task_code(normalized_task_code),
        )
    _refresh_request_aggregate_counts(store, request_id=request.request_id)
    return build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="submit",
        message=f"Accepted {normalized_task_code} task request.",
    )


def get_task_request_status(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    del task_code
    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required for status/result.")
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return build_request_payload(
        store=store,
        request_id=request_id,
        control_action=normalize_control_action(params.get("control_action")),
        message="Loaded task request status.",
    )


def run_task_request(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_task_code = ensure_formal_task_code(task_code)
    action = normalize_control_action(params.get("control_action"))
    if action == "submit":
        return submit_task_request(normalized_task_code, params)
    if action in {"status", "result"}:
        return get_task_request_status(normalized_task_code, params)
    if action == "executor_once":
        return execute_executor_once(params)
    if action == "api_worker_once":
        return execute_api_worker_once(params)
    if action == "browser_once":
        return execute_browser_once(params)
    if action == "browser_loop":
        return run_browser_runloop(params)
    if action == "outbox_once":
        return dispatch_outbox_once(params)
    if action == "outbox_loop":
        return run_outbox_dispatcher(params)
    raise ValueError(f"Unsupported control_action '{action}' for {normalized_task_code}.")


def run_refresh_current_competitor_table_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(REFRESH_TASK_CODE, params)


def run_search_keyword_competitor_products_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(KEYWORD_TASK_CODE, params)


def run_sync_tk_influencer_pool_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(INFLUENCER_POOL_TASK_CODE, params)


def run_tiktok_fastmoss_product_ingest_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(PRODUCT_INGEST_TASK_CODE, params)


def _dispatch_api_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_api_handler_registry().dispatch(context.handler_code, context)


def _dispatch_browser_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_browser_handler_registry().dispatch(context.handler_code, context)


def _dispatch_outbox_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_outbox_handler_registry().dispatch("outbox_dispatch", context)


def execute_executor_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    request = store.claim_next_task_request(
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
    )
    if request is None:
        return build_idle_payload(
            control_action="executor_once",
            actor="daemon",
            message="No task_request is ready for executor processing.",
        )

    _refresh_request_aggregate_counts(store, request_id=request.request_id)
    request = store.load_task_request(request_id=request.request_id)
    workflow = get_workflow_definition(request.task_code)
    runtime = _resolve_workflow_runtime(request.task_code)
    current_stage = str(request.current_stage or "").strip() or workflow.entry_stage_code

    if runtime is None:
        finalized = _finalize_not_ready_request(
            store=store,
            request_id=request.request_id,
            current_stage=current_stage,
            message=f"{PHASE2_NOT_READY_MESSAGE} task_code={request.task_code}",
        )
        finalized.update(
            {
                "daemon_status": "not_ready",
                "processed_count": 1,
                "success_count": 0,
                "failed_count": 1,
            }
        )
        return finalized

    details: dict[str, Any] = {}
    for _ in range(MAX_EXECUTOR_STAGE_HOPS):
        request = store.load_task_request(request_id=request.request_id)
        current_stage = str(request.current_stage or "").strip() or workflow.entry_stage_code

        if current_stage == workflow.summary_policy.summary_stage_code:
            payload = runtime.finalize_request(store=store, request=request, workflow=workflow)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1 if payload.get("request_status") in {"success", "partial_success"} else 0,
                    "failed_count": 1 if payload.get("request_status") == "failed" else 0,
                }
            )
            return payload

        stage_result = runtime.advance_stage(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=current_stage,
        )
        details = dict(stage_result.get("details") or {})
        action = str(stage_result["action"])
        if action == "advance":
            next_stage = str(stage_result["next_stage"])
            store.update_task_request(
                request_id=request.request_id,
                status="pending",
                current_stage=next_stage,
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                error_text="",
            )
            continue
        if action == "waiting":
            waiting_stage = str(stage_result.get("current_stage") or current_stage)
            store.update_task_request(
                request_id=request.request_id,
                status="waiting_children",
                current_stage=waiting_stage,
                worker_id="",
                lease_until=0.0,
                heartbeat_at=0.0,
                error_text="",
            )
            payload = _build_runtime_request_payload(
                store=store,
                request_id=request.request_id,
                control_action="executor_once",
                message=str(stage_result.get("message") or "Executor dispatched runtime child work."),
            )
            payload.update(details)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1,
                    "failed_count": 0,
                }
            )
            return payload
        if action == "finalize":
            payload = runtime.finalize_request(
                store=store,
                request=store.load_task_request(request_id=request.request_id),
                workflow=workflow,
                force_result=stage_result,
            )
            payload.update(details)
            payload.update(
                {
                    "daemon_status": "processed",
                    "processed_count": 1,
                    "success_count": 1 if payload.get("request_status") in {"success", "partial_success"} else 0,
                    "failed_count": 1 if payload.get("request_status") == "failed" else 0,
                }
            )
            return payload
        raise RuntimeError(f"Unsupported executor action '{action}' for stage {current_stage}.")

    exhausted = _finalize_not_ready_request(
        store=store,
        request_id=request.request_id,
        current_stage=current_stage,
        message="Executor exhausted the stage hop budget without reaching a stable state.",
    )
    exhausted.update(
        {
            "daemon_status": "failed",
            "processed_count": 1,
            "success_count": 0,
            "failed_count": 1,
        }
    )
    return exhausted


def run_executor_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        params=params,
        actor="daemon",
        once_func=execute_executor_once,
        idle_status_key="daemon_status",
    )


def execute_api_worker_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    job = store.claim_next_api_worker_job(
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
    )
    if job is None:
        return build_idle_payload(
            control_action="api_worker_once",
            actor="daemon",
            message="No api_worker_job is ready for processing.",
        )

    context = HandlerContext(
        request_id=str(job["request_id"]),
        job_id=str(job["job_id"]),
        handler_code=str(job["job_code"]),
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=dict(job.get("payload") or {}),
        workflow_code=str((job.get("payload") or {}).get("workflow_code") or ""),
        stage_code=str((job.get("payload") or {}).get("stage_code") or ""),
        job_code=str(job["job_code"]),
        business_key=str(job.get("business_key") or ""),
        dedupe_key=str(job.get("dedupe_key") or ""),
        worker_id=settings.worker_id,
        attempt_count=int(job.get("attempt_count") or 0),
        max_attempts=int(job.get("max_attempts") or 0),
        metadata={"request_payload": dict((job.get("payload") or {}).get("request_payload") or {})},
    )

    outcome = run_supervised_handler(
        context=context,
        dispatch=_dispatch_api_runtime_handler,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: store.heartbeat_api_worker_job(
                job_id=str(job["job_id"]),
                lease_seconds=settings.lease_seconds,
            ),
            on_progress=lambda event: store.update_api_worker_job_progress(
                job_id=str(job["job_id"]),
                progress_stage=event.progress_stage,
                lease_seconds=settings.lease_seconds,
            ),
        ),
        child_runner_config=_build_child_runner_config(
            params,
            worker_type="api_worker",
            handler_code=str(job["job_code"]),
            runtime_timeout_seconds=job.get("max_execution_seconds"),
        ),
    )
    marked_job, success_count, failed_count = _persist_api_worker_outcome(
        store=store,
        job_id=str(job["job_id"]),
        run_id=f"api-worker-{job['job_id']}",
        outcome=outcome,
        retry_delay_seconds=settings.retry_delay_seconds,
    )

    parent_updates = _release_request_after_child_completion(store, request_id=str(job["request_id"]))
    payload = _build_runtime_request_payload(
        store=store,
        request_id=str(job["request_id"]),
        control_action="api_worker_once",
        message="API worker processed one runtime handler job.",
    )
    payload.update(
        {
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": success_count,
            "failed_count": failed_count,
            "api_worker_job": marked_job,
            "worker_result": outcome.worker_result.to_dict(),
            "supervisor": outcome.to_dict(),
            "parent_updates": parent_updates,
        }
    )
    if outcome.error is not None:
        payload.update(_supervisor_error_payload(outcome))
    return payload


def run_api_worker_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        params=params,
        actor="daemon",
        once_func=execute_api_worker_once,
        idle_status_key="daemon_status",
    )


def execute_browser_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    execution = store.claim_next_browser_execution(
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        item_codes=("tiktok_product_browser_fetch",),
    )
    if execution is None:
        return build_idle_payload(
            control_action="browser_once",
            actor="daemon",
            message="No browser execution is ready for processing.",
        )

    payload_data = dict(execution.payload or {})
    context = HandlerContext(
        request_id=execution.request_id,
        job_id=execution.execution_id,
        handler_code=execution.item_code,
        worker_type="browser_worker",
        runtime_table="task_execution",
        payload=payload_data,
        workflow_code=str(payload_data.get("workflow_code") or execution.workflow_code or ""),
        stage_code=str(payload_data.get("stage_code") or ""),
        item_code=execution.item_code,
        business_key=execution.business_key,
        dedupe_key=execution.dedupe_key,
        resource_code=execution.resource_code,
        worker_id=settings.worker_id,
        attempt_count=execution.attempt_count,
        max_attempts=execution.max_attempts,
        metadata={"request_payload": dict(payload_data.get("request_payload") or {})},
    )

    outcome = run_supervised_handler(
        context=context,
        dispatch=_dispatch_browser_runtime_handler,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: store.heartbeat_browser_execution(
                execution_id=execution.execution_id,
                lease_seconds=settings.lease_seconds,
            ),
            on_progress=lambda event: store.update_task_execution_progress(
                execution_id=execution.execution_id,
                progress_stage=event.progress_stage,
                lease_seconds=settings.lease_seconds,
            ),
        ),
        child_runner_config=_build_child_runner_config(
            params,
            worker_type="browser_worker",
            handler_code=execution.item_code,
            runtime_timeout_seconds=execution.max_execution_seconds,
        ),
    )
    stored_execution, success_count, failed_count = _persist_browser_execution_outcome(
        store=store,
        execution_id=execution.execution_id,
        run_id=f"browser-{execution.execution_id}",
        outcome=outcome,
        retry_delay_seconds=settings.retry_delay_seconds,
    )

    parent_updates = _release_request_after_child_completion(store, request_id=execution.request_id)
    payload = _build_runtime_request_payload(
        store=store,
        request_id=execution.request_id,
        control_action="browser_once",
        message="Browser worker processed one runtime execution.",
    )
    payload.update(
        {
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": success_count,
            "failed_count": failed_count,
            "execution": stored_execution.to_dict(),
            "execution_status": stored_execution.status,
            "worker_result": outcome.worker_result.to_dict(),
            "supervisor": outcome.to_dict(),
            "parent_updates": parent_updates,
        }
    )
    if outcome.error is not None:
        payload.update(_supervisor_error_payload(outcome))
    return payload


def run_browser_runloop(params: dict[str, Any]) -> dict[str, Any]:
    return _run_loop(
        params=params,
        actor="daemon",
        once_func=execute_browser_once,
        idle_status_key="daemon_status",
    )


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
        child_runner_config=_build_child_runner_config(
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
            dead_letter_reason="supervisor_failed" if outcome.error is not None and outcome.error.terminal else "",
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
            payload.update(_supervisor_error_payload(outcome))
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
    return _run_loop(
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
            "message_text": build_outbox_message_text(
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


def _run_loop(
    *,
    params: dict[str, Any],
    actor: str,
    once_func: Callable[[dict[str, Any]], dict[str, Any]],
    idle_status_key: str,
) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    processed_count = 0
    success_count = 0
    failed_count = 0
    idle_cycles = 0
    iterations = 0
    last_payload = build_idle_payload(
        control_action="loop",
        actor=actor,
        message=f"{actor} loop has not processed any work yet.",
    )

    while True:
        iterations += 1
        payload = once_func(params)
        last_payload = payload
        status = str(payload.get(idle_status_key, "") or "")
        if status == "idle":
            idle_cycles += 1
            if settings.stop_when_idle and idle_cycles >= settings.max_idle_cycles:
                return payload
            if settings.max_iterations and iterations >= settings.max_iterations:
                return payload
            time.sleep(settings.poll_interval_seconds)
            continue

        idle_cycles = 0
        processed_count += int(payload.get("processed_count", 0) or 0)
        success_count += int(payload.get("success_count", 0) or 0)
        failed_count += int(payload.get("failed_count", 0) or 0)
        last_payload["processed_count"] = processed_count
        last_payload["success_count"] = success_count
        last_payload["failed_count"] = failed_count
        if settings.max_iterations and iterations >= settings.max_iterations:
            return last_payload
        if settings.stop_when_idle and processed_count > 0:
            return last_payload


def _persist_api_worker_outcome(
    *,
    store: RuntimeStore,
    job_id: str,
    run_id: str,
    outcome: ExecutionSupervisorOutcome,
    retry_delay_seconds: float,
) -> tuple[dict[str, Any], int, int]:
    stored_summary = outcome.storage_summary()
    stored_result = outcome.storage_result()
    if outcome.should_mark_failed:
        marked_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=outcome.error_text,
            summary=stored_summary,
            result=stored_result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=outcome.error.error_type if outcome.error is not None else "",
            error_code=outcome.error.error_code if outcome.error is not None else "",
            dead_letter_reason="supervisor_failed" if outcome.error is not None and outcome.error.terminal else "",
        )
        return marked_job, 0, 1 if marked_job.get("status") == "failed" else 0

    marked_job = store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=run_id,
        summary=stored_summary,
        result=stored_result,
        stage=_api_worker_stage_from_handler_result(outcome.worker_result.status),
    )
    return marked_job, 1, 0


def _persist_browser_execution_outcome(
    *,
    store: RuntimeStore,
    execution_id: str,
    run_id: str,
    outcome: ExecutionSupervisorOutcome,
    retry_delay_seconds: float,
) -> tuple[Any, int, int]:
    stored_summary = outcome.storage_summary()
    stored_result = outcome.storage_result()
    if outcome.should_mark_failed:
        execution = store.mark_browser_execution_retry_or_failed(
            execution_id=execution_id,
            run_id=run_id,
            error_text=outcome.error_text,
            summary=stored_summary,
            result=stored_result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=outcome.error.error_type if outcome.error is not None else "",
            error_code=outcome.error.error_code if outcome.error is not None else "",
            dead_letter_reason="supervisor_failed" if outcome.error is not None and outcome.error.terminal else "",
        )
        return execution, 0, 1 if execution.status == "failed" else 0
    if outcome.worker_result.status == "skipped":
        execution = store.mark_browser_execution_skipped(
            execution_id=execution_id,
            run_id=run_id,
            summary=stored_summary,
            result=stored_result,
        )
        return execution, 1, 0
    execution = store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=run_id,
        summary=stored_summary,
        result=stored_result,
    )
    return execution, 1, 0


def _supervisor_error_payload(outcome: ExecutionSupervisorOutcome) -> dict[str, Any]:
    if outcome.error is None:
        return {}
    return {
        "worker_error": outcome.error.message,
        "error_type": outcome.error.error_type,
        "error_code": outcome.error.error_code,
        "retryable": outcome.error.retryable,
        "terminal_error": outcome.error.terminal,
    }


def _build_child_runner_config(
    params: Mapping[str, Any],
    *,
    worker_type: str = "",
    handler_code: str = "",
    runtime_timeout_seconds: Any = None,
) -> ChildRunnerConfig | None:
    explicit_mode = str(params.get("execution_child_runner_mode") or "").strip()
    mode = explicit_mode or _default_child_runner_mode(worker_type=worker_type, handler_code=handler_code)
    if mode != "child_process":
        return None

    timeout_raw = params.get("execution_child_timeout_seconds")
    timeout_seconds = (
        _default_child_timeout_seconds(
            worker_type=worker_type,
            runtime_timeout_seconds=runtime_timeout_seconds,
        )
        if timeout_raw in (None, "")
        else max(float(timeout_raw), 0.01)
    )
    poll_raw = params.get("execution_child_poll_interval_seconds")
    poll_interval_seconds = 0.02 if poll_raw in (None, "") else max(float(poll_raw), 0.005)
    grace_raw = params.get("execution_child_terminate_grace_seconds")
    terminate_grace_seconds = 0.2 if grace_raw in (None, "") else max(float(grace_raw), 0.01)
    start_method = str(params.get("execution_child_start_method") or "").strip() or None
    return ChildRunnerConfig(
        mode="child_process",
        timeout_seconds=timeout_seconds,
        start_method=start_method,
        poll_interval_seconds=poll_interval_seconds,
        terminate_grace_seconds=terminate_grace_seconds,
    )


def _default_child_runner_mode(*, worker_type: str, handler_code: str) -> str:
    del handler_code
    if worker_type in DEFAULT_CHILD_TIMEOUT_SECONDS_BY_WORKER:
        return "child_process"
    return "inline"


def _default_child_timeout_seconds(*, worker_type: str, runtime_timeout_seconds: Any) -> float | None:
    runtime_timeout = _coerce_positive_float(runtime_timeout_seconds)
    if runtime_timeout is not None:
        return runtime_timeout
    return DEFAULT_CHILD_TIMEOUT_SECONDS_BY_WORKER.get(worker_type)


def _coerce_positive_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    if normalized <= 0:
        return None
    return max(normalized, 0.01)


def _sanitize_task_payload(params: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(params)
    sanitized.pop("control_action", None)
    return sanitized


def _build_runtime_request_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return build_request_payload(
        store=store,
        request_id=request_id,
        control_action=control_action,
        message=message,
    )


def _finalize_not_ready_request(
    *,
    store: RuntimeStore,
    request_id: str,
    current_stage: str,
    message: str,
) -> dict[str, Any]:
    store.update_task_request(
        request_id=request_id,
        status="failed",
        current_stage=current_stage,
        result={"status": "not_ready", "message": message},
        summary={"total": 0, "counts": {"not_ready": 1}},
        error_text=message,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    return _build_runtime_request_payload(
        store=store,
        request_id=request_id,
        control_action="executor_once",
        message=message,
    )


def _advance_product_ingest_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    stage = workflow.require_stage(stage_code)
    if stage.stage_code == "read_selection_rows":
        return _advance_ingest_read_selection_rows(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "collect_product_data":
        return _advance_ingest_collect_product_data(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "browser_fallback":
        return _advance_ingest_browser_fallback(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "sync_media":
        return _advance_ingest_sync_media(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "persist_facts":
        return _advance_ingest_persist_facts(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "writeback_selection_rows":
        return _advance_ingest_writeback_selection_rows(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=stage_code,
        )
    if stage.stage_code == workflow.summary_policy.summary_stage_code:
        return {"action": "finalize"}
    return {
        "action": "finalize",
        "final_status": "failed",
        "result": {"status": "failed", "message": f"Unsupported ingest stage {stage_code}."},
        "summary": {"total": 0, "counts": {"unsupported_stage": 1}},
        "details": {"unsupported_stage": stage_code},
    }


def _advance_ingest_read_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    if not _selection_mode_enabled(request_payload):
        return {
            "action": "advance",
            "next_stage": "collect_product_data",
            "details": {"stage_transition": "direct_ingest_skip_selection_read"},
        }

    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="feishu_table_read",
            jobs=[
                {
                    "business_key": str(request_payload.get("selection_record_id") or request_payload.get("selection_table_ref") or request.request_id),
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_table_read",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "selection_record_id": str(request_payload.get("selection_record_id") or ""),
                        "product_url": str(request_payload.get("product_url") or ""),
                        "product_id": str(request_payload.get("product_id") or ""),
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the selection table read stage.",
            "details": {"dispatch_payload": {"feishu_table_read": enqueue_payload}},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Selection table read is still running."}

    if _any_failed_api_jobs(stage_jobs):
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "Selection table read failed."},
            "summary": {"total": 1, "counts": {"selection_table_read_failed": 1}},
            "details": {"failed_jobs": stage_jobs},
        }

    return {
        "action": "advance",
        "next_stage": "collect_product_data",
        "details": {"selection_table_read": _latest_api_job_by_code(stage_jobs, "feishu_table_read")},
    }


def _advance_ingest_collect_product_data(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    table_read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )
    identity = _resolve_product_identity(request_payload, table_read_job)

    if not stage_jobs:
        common_payload = {
            "request_payload": request_payload,
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": request.task_code,
            "stage_code": stage_code,
            "product_identity": identity,
            "normalized_product_url": identity["normalized_product_url"],
            "product_id": identity["product_id"],
            "selection_context": _job_effective_result(table_read_job),
            "fallback_allowed": bool(request_payload.get("fallback_allowed", True)),
        }
        dispatches: dict[str, Any] = {}
        dispatches["tiktok_product_request_fetch"] = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="tiktok_product_request_fetch",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:tiktok:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": dict(common_payload),
                }
            ],
        )
        dispatches["fastmoss_product_fetch"] = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="fastmoss_product_fetch",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:fastmoss:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": dict(common_payload),
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched TikTok request and FastMoss product jobs.",
            "details": {"dispatch_payload": dispatches, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Product collection jobs are still running."}

    tiktok_job = _latest_api_job_by_code(stage_jobs, "tiktok_product_request_fetch")
    fastmoss_job = _latest_api_job_by_code(stage_jobs, "fastmoss_product_fetch")
    tiktok_handler_status = _handler_status_from_api_job(tiktok_job)
    if tiktok_handler_status == "fallback_required":
        return {
            "action": "advance",
            "next_stage": "browser_fallback",
            "details": {
                "tiktok_product_request_fetch": tiktok_job,
                "fastmoss_product_fetch": fastmoss_job,
                "product_identity": identity,
            },
        }
    if _job_failed(tiktok_job):
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "TikTok request-first collection failed."},
            "summary": {"total": 2, "counts": {"tiktok_request_failed": 1}},
            "details": {"tiktok_product_request_fetch": tiktok_job, "fastmoss_product_fetch": fastmoss_job},
        }
    return {
        "action": "advance",
        "next_stage": "sync_media",
        "details": {
            "tiktok_product_request_fetch": tiktok_job,
            "fastmoss_product_fetch": fastmoss_job,
            "product_identity": identity,
        },
    }


def _advance_ingest_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_executions = _browser_executions_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    tiktok_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data"),
        "tiktok_product_request_fetch",
    )
    identity = _resolve_product_identity(request_payload, tiktok_job)

    if not stage_executions:
        handler_result = _job_handler_result(tiktok_job)
        next_action_payload = dict((handler_result.get("next_action") or {}).get("payload") or {})
        enqueue_payload = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code="tiktok_product_browser_fetch",
            workflow_code=request.task_code,
            items=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:browser:{identity['business_key']}",
                    "resource_code": str(request_payload.get("browser_profile_ref") or "roxy-tiktok"),
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "product_identity": identity,
                        "normalized_product_url": identity["normalized_product_url"],
                        "product_id": identity["product_id"],
                        **next_action_payload,
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched a TikTok browser fallback execution.",
            "details": {"dispatch_payload": {"tiktok_product_browser_fetch": enqueue_payload}, "product_identity": identity},
        }

    if _any_browser_executions_active(stage_executions):
        return {"action": "waiting", "current_stage": stage_code, "message": "TikTok browser fallback is still running."}

    browser_execution = stage_executions[-1]
    if str(browser_execution.status) == "failed":
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "TikTok browser fallback failed."},
            "summary": {"total": 1, "counts": {"browser_fallback_failed": 1}},
            "details": {"browser_execution": browser_execution.to_dict()},
        }
    return {
        "action": "advance",
        "next_stage": "sync_media",
        "details": {"browser_execution": browser_execution.to_dict(), "product_identity": identity},
    }


def _advance_ingest_sync_media(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
        browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
        tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
        fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
        identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job)
        asset_refs = _collect_asset_refs(
            tiktok_result=_job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
            fastmoss_result=_job_effective_result(fastmoss_job),
        )
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="media_asset_sync",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:media:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "asset_refs": asset_refs,
                        "entity_keys": [identity["product_id"]] if identity["product_id"] else [identity["business_key"]],
                        "product_identity": identity,
                        "tiktok_result": _job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
                        "fastmoss_result": _job_effective_result(fastmoss_job),
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the media sync stage.",
            "details": {"dispatch_payload": {"media_asset_sync": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Media sync is still running."}
    return {"action": "advance", "next_stage": "persist_facts", "details": {"media_asset_sync": stage_jobs[-1]}}


def _advance_ingest_persist_facts(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
        browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
        media_job = _latest_api_job_by_code(
            _api_jobs_for_stage(store, request_id=request.request_id, stage_code="sync_media"),
            "media_asset_sync",
        )
        tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
        fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
        identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job)
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="fact_bundle_upsert",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:facts:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "product_identity": identity,
                        "tiktok_result": _job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
                        "fastmoss_result": _job_effective_result(fastmoss_job),
                        "media_sync_result": _job_effective_result(media_job),
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the fact persistence stage.",
            "details": {"dispatch_payload": {"fact_bundle_upsert": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Fact persistence is still running."}

    if _selection_mode_enabled(request_payload) and bool(request_payload.get("writeback_enabled", True)):
        return {"action": "advance", "next_stage": "writeback_selection_rows", "details": {"fact_bundle_upsert": stage_jobs[-1]}}
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"fact_bundle_upsert": stage_jobs[-1]}}


def _advance_ingest_writeback_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    if not (_selection_mode_enabled(request_payload) and bool(request_payload.get("writeback_enabled", True))):
        return {"action": "advance", "next_stage": "ready_for_summary"}

    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        fact_job = _latest_api_job_by_code(
            _api_jobs_for_stage(store, request_id=request.request_id, stage_code="persist_facts"),
            "fact_bundle_upsert",
        )
        identity = _resolve_product_identity(request_payload, fact_job)
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="feishu_table_write",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_write:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "target_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "records": [],
                        "fact_result": _job_effective_result(fact_job),
                        "product_identity": identity,
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the selection writeback stage.",
            "details": {"dispatch_payload": {"feishu_table_write": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Selection writeback is still running."}
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"feishu_table_write": stage_jobs[-1]}}


def _finalize_product_ingest_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
    media_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="sync_media"),
        "media_asset_sync",
    )
    fact_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="persist_facts"),
        "fact_bundle_upsert",
    )
    writeback_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="writeback_selection_rows"),
        "feishu_table_write",
    )
    tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
    fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
    identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job, fact_job)

    final_result = {
        "product_id": identity["product_id"],
        "normalized_product_url": identity["normalized_product_url"],
        "tiktok_product": _job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
        "fastmoss_product": _job_effective_result(fastmoss_job),
        "media_asset_sync": _job_effective_result(media_job),
        "fact_bundle_upsert": _job_effective_result(fact_job),
        "selection_writeback": _job_effective_result(writeback_job),
        "browser_fallback_executions": [execution.to_dict() for execution in browser_execs],
    }
    if force_result and isinstance(force_result.get("result"), Mapping):
        final_result.update(dict(force_result.get("result") or {}))

    counts = _aggregate_request_children(store, request_id=request.request_id)
    summary = {
        "total": counts["total"],
        "counts": counts["counts"],
        "child_success_count": counts["success_count"],
        "child_failed_count": counts["failed_count"],
        "child_skipped_count": counts["skipped_count"],
    }
    if force_result and isinstance(force_result.get("summary"), Mapping):
        summary.update(dict(force_result.get("summary") or {}))

    final_status = _determine_product_ingest_final_status(
        force_result=force_result,
        fact_job=fact_job,
        tiktok_source=tiktok_source,
        counts=counts,
    )
    store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage="completed",
        summary=summary,
        result=final_result,
        error_text="" if final_status != "failed" else str(final_result.get("message") or ""),
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    ensure_request_outbox(store=store, request_id=request.request_id)
    payload = _build_runtime_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Executor finalized the product ingest request.",
    )
    payload["final_status"] = final_status
    return payload


def _release_request_after_child_completion(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    runtime = _resolve_workflow_runtime(request.task_code)
    if runtime is None:
        return []
    return runtime.release_request_after_child_completion(store=store, request_id=request_id)


def _release_product_ingest_request_after_child_completion(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != PRODUCT_INGEST_TASK_CODE:
        return []
    workflow = get_workflow_definition(request.task_code)
    current_stage = str(request.current_stage or "").strip()
    if not current_stage:
        return []
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    api_jobs = _api_jobs_for_stage(store, request_id=request_id, stage_code=current_stage)
    executions = _browser_executions_for_stage(store, request_id=request_id, stage_code=current_stage)
    has_children = bool(api_jobs or executions)
    if not has_children:
        return []
    if _any_api_jobs_active(api_jobs) or _any_browser_executions_active(executions):
        return []

    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=current_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return [
        {
            "request_id": request_id,
            "stage_code": current_stage,
            "released": True,
            "next_executor_status": "pending",
        }
    ]


def _resolve_workflow_runtime(task_code: str) -> Any | None:
    runtime = load_workflow_runtime(task_code)
    if runtime is not None:
        return runtime
    if ensure_formal_task_code(task_code) == PRODUCT_INGEST_TASK_CODE:
        return _LegacyProductIngestRuntime
    return None


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


def _aggregate_request_children(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    counts: dict[str, int] = {}
    success_count = 0
    failed_count = 0
    skipped_count = 0
    active_count = 0

    for job in api_jobs:
        handler_status = _handler_status_from_api_job(job)
        if str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status in {"success", "partial_success", "fallback_required"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or str(job.get("status") or "unknown")
        counts[status_key] = counts.get(status_key, 0) + 1

    for execution in executions:
        handler_status = _handler_status_from_execution(execution)
        if execution.status in ACTIVE_EXECUTION_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status in {"success", "partial_success", "fallback_required"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or execution.status or "unknown"
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
        "active_count": active_count,
    }


def _api_jobs_for_stage(store: RuntimeStore, *, request_id: str, stage_code: str) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _browser_executions_for_stage(store: RuntimeStore, *, request_id: str, stage_code: str) -> list[Any]:
    return [
        execution
        for execution in store.list_task_executions(request_id=request_id)
        if str((execution.payload or {}).get("stage_code") or "") == stage_code
    ]


def _latest_api_job_by_code(jobs: list[dict[str, Any]], job_code: str) -> dict[str, Any]:
    for job in reversed(jobs):
        if str(job.get("job_code") or "") == job_code:
            return job
    return {}


def _any_api_jobs_active(jobs: list[dict[str, Any]]) -> bool:
    return any(str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES for job in jobs)


def _any_failed_api_jobs(jobs: list[dict[str, Any]]) -> bool:
    return any(_job_failed(job) for job in jobs)


def _any_browser_executions_active(executions: list[Any]) -> bool:
    return any(execution.status in ACTIVE_EXECUTION_STATUSES for execution in executions)


def _handler_status_from_api_job(job: Mapping[str, Any] | None) -> str:
    if not job:
        return ""
    handler_result = _job_handler_result(job)
    return str(handler_result.get("status") or job.get("status") or "")


def _handler_status_from_execution(execution: Any) -> str:
    if execution is None:
        return ""
    result = dict(execution.result or {})
    handler_result = result.get("handler_result")
    if isinstance(handler_result, Mapping):
        return str(handler_result.get("status") or execution.status or "")
    return str(execution.status or "")


def _job_handler_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    handler_result = result.get("handler_result")
    return dict(handler_result or {}) if isinstance(handler_result, Mapping) else {}


def _job_effective_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    if "handler_result" in result:
        result = {key: value for key, value in result.items() if key != "handler_result"}
    return result


def _execution_effective_result(execution: Any) -> dict[str, Any]:
    if execution is None:
        return {}
    result = dict(execution.result or {})
    if "handler_result" in result:
        result = {key: value for key, value in result.items() if key != "handler_result"}
    return result


def _job_failed(job: Mapping[str, Any] | None) -> bool:
    if not job:
        return False
    return str(job.get("status") or "") == "failed" or _handler_status_from_api_job(job) == "failed"


def _selection_mode_enabled(request_payload: Mapping[str, Any]) -> bool:
    return bool(
        str(request_payload.get("selection_table_ref") or "").strip()
        or str(request_payload.get("selection_record_id") or "").strip()
    )


def _resolve_product_identity(*sources: Any) -> dict[str, str]:
    product_url = _first_non_empty(
        *[_lookup_nested(source, "normalized_product_url", "normalized_url", "product_url", "source_url") for source in sources]
    )
    product_id = _first_non_empty(
        *[_lookup_nested(source, "product_id") for source in sources]
    )
    if not product_id:
        product_id = _extract_tiktok_product_id(product_url)
    normalized_url = _normalize_tiktok_product_url(product_url) if product_url else ""
    if not product_url and normalized_url:
        product_url = normalized_url
    business_key = product_id or normalized_url or product_url
    return {
        "product_id": product_id,
        "product_url": product_url,
        "normalized_product_url": normalized_url or product_url,
        "business_key": business_key,
    }


def _effective_tiktok_result(*, collect_jobs: list[dict[str, Any]], browser_execs: list[Any]) -> Mapping[str, Any]:
    if browser_execs:
        for execution in reversed(browser_execs):
            if execution.status in {"success", "skipped"}:
                return {"result": _execution_effective_result(execution), "source": "browser"}
    tiktok_job = _latest_api_job_by_code(collect_jobs, "tiktok_product_request_fetch")
    return tiktok_job


def _collect_asset_refs(*, tiktok_result: Mapping[str, Any], fastmoss_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    asset_refs: list[dict[str, Any]] = []
    logical_fields = {}
    if isinstance(tiktok_result.get("normalized_product_result"), Mapping):
        logical_fields = dict((tiktok_result.get("normalized_product_result") or {}).get("logical_fields") or {})
    elif isinstance(tiktok_result.get("logical_fields"), Mapping):
        logical_fields = dict(tiktok_result.get("logical_fields") or {})

    product_id = _first_non_empty(
        logical_fields.get("product_id"),
        tiktok_result.get("product_id"),
        fastmoss_result.get("product_id"),
    )
    main_image_url = _first_non_empty(logical_fields.get("main_image_url"))
    main_image_local_path = _first_non_empty(logical_fields.get("main_image_local_path"))
    if main_image_url or main_image_local_path:
        asset_refs.append(
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": "product_main_image",
                "source_url": main_image_url,
                "local_path": main_image_local_path,
                "file_name": _first_non_empty(logical_fields.get("main_image_file_name")),
                "mime_type": _first_non_empty(logical_fields.get("main_image_mime_type")),
            }
        )
    return asset_refs


def _determine_product_ingest_final_status(
    *,
    force_result: Mapping[str, Any] | None,
    fact_job: Mapping[str, Any] | None,
    tiktok_source: Mapping[str, Any] | None,
    counts: Mapping[str, Any],
) -> str:
    if force_result and str(force_result.get("final_status") or "") in {"success", "partial_success", "failed"}:
        return str(force_result["final_status"])
    fact_status = _handler_status_from_api_job(fact_job)
    tiktok_status = _handler_status_from_api_job(tiktok_source) if isinstance(tiktok_source, Mapping) else ""
    if fact_status in {"success", "partial_success"}:
        return "partial_success" if int(counts.get("failed_count") or 0) > 0 else "success"
    if tiktok_status in {"success", "partial_success"}:
        return "partial_success"
    return "failed"


def _lookup_nested(source: Any, *keys: str) -> str:
    if source is None:
        return ""
    if hasattr(source, "to_dict"):
        source = source.to_dict()
    if not isinstance(source, Mapping):
        return ""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    result = source.get("result")
    if isinstance(result, Mapping):
        for key in keys:
            value = result.get(key)
            if value not in (None, ""):
                return str(value)
        normalized_product = result.get("normalized_product_result")
        if isinstance(normalized_product, Mapping):
            for key in keys:
                value = normalized_product.get(key)
                if value not in (None, ""):
                    return str(value)
            logical_fields = normalized_product.get("logical_fields")
            if isinstance(logical_fields, Mapping):
                for key in keys:
                    value = logical_fields.get(key)
                    if value not in (None, ""):
                        return str(value)
    payload = source.get("payload")
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product)/(\d+)", text)
    if match:
        return str(match.group(1))
    fallback = re.search(r"(\d{8,})", text)
    return str(fallback.group(1)) if fallback else ""


def _normalize_tiktok_product_url(value: str) -> str:
    product_id = _extract_tiktok_product_id(value)
    if not product_id:
        return str(value or "").strip()
    return f"https://www.tiktok.com/shop/pdp/{product_id}"


def _api_worker_stage_from_handler_result(status: str) -> str:
    mapping = {
        "success": "completed",
        "partial_success": "partial_success",
        "skipped": "skipped",
        "fallback_required": "browser_fallback_required",
        "failed": "failed",
    }
    return mapping.get(status, status or "completed")


def _build_bound_api_handler_registry() -> Any:
    if API_HANDLER_REGISTRY is not None:
        return API_HANDLER_REGISTRY

    from automation_business_scaffold.business.handlers import build_bound_api_handler_registry

    return build_bound_api_handler_registry()


def _build_bound_browser_handler_registry() -> Any:
    if BROWSER_HANDLER_REGISTRY is not None:
        return BROWSER_HANDLER_REGISTRY

    from automation_business_scaffold.business.handlers import build_bound_browser_handler_registry

    return build_bound_browser_handler_registry()


def _build_bound_outbox_handler_registry() -> Any:
    if OUTBOX_HANDLER_REGISTRY is not None:
        return OUTBOX_HANDLER_REGISTRY

    from automation_business_scaffold.business.handlers import build_bound_outbox_handler_registry

    return build_bound_outbox_handler_registry()


def _initial_stage_for_task_code(task_code: str) -> str:
    normalized = ensure_formal_task_code(task_code)
    return get_workflow_definition(normalized).entry_stage_code


__all__ = [
    "FORMAL_TASK_CODES",
    "dispatch_outbox_once",
    "ensure_request_outbox",
    "execute_api_worker_once",
    "execute_browser_once",
    "execute_executor_once",
    "get_task_request_status",
    "run_api_worker_daemon",
    "run_browser_runloop",
    "run_executor_daemon",
    "run_outbox_dispatcher",
    "run_refresh_current_competitor_table_request",
    "run_search_keyword_competitor_products_request",
    "run_sync_tk_influencer_pool_request",
    "run_task_request",
    "run_tiktok_fastmoss_product_ingest_request",
    "submit_task_request",
]
