from __future__ import annotations

import os
import time
from typing import Any, Mapping

from automation_business_scaffold.control_plane.executor.looping import (
    build_child_runner_config,
    run_control_loop,
    supervisor_error_payload,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.control_plane.runtime_config.settings import (
    FORMAL_TASK_CODES,
    INFLUENCER_POOL_TASK_CODE,
    KEYWORD_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE,
    REFRESH_TASK_CODE,
    build_idle_payload,
    build_request_payload,
    build_runtime_settings,
    create_runtime_store,
    ensure_formal_task_code,
    normalize_control_action,
)
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    ExecutionSupervisorOutcome,
    run_supervised_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running", "retry_wait"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}
MAX_EXECUTOR_STAGE_HOPS = 16
WORKFLOW_RUNTIME_NOT_READY_MESSAGE = "No workflow runtime is registered for this task_code."
API_HANDLER_REGISTRY: Any | None = None
BROWSER_HANDLER_REGISTRY: Any | None = None


def submit_task_request(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_task_code = ensure_formal_task_code(task_code)
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    preflight = _runtime_db_health_preflight(store=store, settings=settings)
    if preflight:
        return {
            "status": "failed",
            "control_action": "submit",
            "request_id": "",
            "task_code": normalized_task_code,
            "request_status": "rejected",
            "current_stage": "",
            "message": preflight["message"],
            "error": preflight["message"],
            "error_type": "infrastructure",
            "error_code": "runtime_db_connection_unhealthy",
            "retryable": True,
            "db_connection_health": preflight["db_connection_health"],
            "summary": {"total": 0, "counts": {"rejected": 1}},
            "result": {},
            "item": {},
            "items": [],
        }
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


def dispatch_outbox_once(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        dispatch_outbox_once as _dispatch_outbox_once,
    )

    return _dispatch_outbox_once(params)


def ensure_request_outbox(*args: Any, **kwargs: Any) -> Any:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        ensure_request_outbox as _ensure_request_outbox,
    )

    return _ensure_request_outbox(*args, **kwargs)


def run_outbox_dispatcher(params: dict[str, Any]) -> dict[str, Any]:
    from automation_business_scaffold.control_plane.outbox.dispatcher import (
        run_outbox_dispatcher as _run_outbox_dispatcher,
    )

    return _run_outbox_dispatcher(params)


def run_refresh_current_competitor_table_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(REFRESH_TASK_CODE, params)


def run_refresh_competitor_row_by_url_request(params: dict[str, Any]) -> dict[str, Any]:
    return run_task_request(REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE, params)


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
            message=f"{WORKFLOW_RUNTIME_NOT_READY_MESSAGE} task_code={request.task_code}",
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
    return run_control_loop(
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
        worker_pid=os.getpid(),
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
    run_id = str(job.get("run_id") or "")
    store.update_api_worker_job_progress(
        job_id=str(job["job_id"]),
        run_id=run_id,
        progress_stage="handler_started",
        message=f"Starting api handler {job['job_code']}.",
    )

    outcome = run_supervised_handler(
        context=context,
        dispatch=_dispatch_api_runtime_handler,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: store.heartbeat_api_worker_job(
                job_id=str(job["job_id"]),
                run_id=run_id,
                lease_seconds=settings.lease_seconds,
            ),
            on_progress=lambda event: store.update_api_worker_job_progress(
                job_id=str(job["job_id"]),
                run_id=run_id,
                progress_stage=event.progress_stage,
                message=event.message,
            ),
        ),
        child_runner_config=build_child_runner_config(
            params,
            worker_type="api_worker",
            handler_code=str(job["job_code"]),
            runtime_timeout_seconds=job.get("max_execution_seconds"),
        ),
    )
    marked_job, success_count, failed_count = _persist_api_worker_outcome(
        store=store,
        job_id=str(job["job_id"]),
        run_id=run_id,
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
        payload.update(supervisor_error_payload(outcome))
    return payload


def run_api_worker_daemon(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
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
        worker_pid=os.getpid(),
        lease_seconds=settings.lease_seconds,
        item_codes=("fastmoss_security_browser_resolve", "tiktok_product_browser_fetch"),
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
    run_id = str(execution.run_id or "")
    store.update_task_execution_progress(
        execution_id=execution.execution_id,
        run_id=run_id,
        progress_stage="handler_started",
        message=f"Starting browser handler {execution.item_code}.",
    )

    outcome = run_supervised_handler(
        context=context,
        dispatch=_dispatch_browser_runtime_handler,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: store.heartbeat_browser_execution(
                execution_id=execution.execution_id,
                run_id=run_id,
                lease_seconds=settings.lease_seconds,
            ),
            on_progress=lambda event: store.update_task_execution_progress(
                execution_id=execution.execution_id,
                run_id=run_id,
                progress_stage=event.progress_stage,
                message=event.message,
            ),
        ),
        child_runner_config=build_child_runner_config(
            params,
            worker_type="browser_worker",
            handler_code=execution.item_code,
            runtime_timeout_seconds=execution.max_execution_seconds,
        ),
    )
    stored_execution, success_count, failed_count = _persist_browser_execution_outcome(
        store=store,
        execution_id=execution.execution_id,
        run_id=run_id,
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
        payload.update(supervisor_error_payload(outcome))
    return payload


def run_browser_runloop(params: dict[str, Any]) -> dict[str, Any]:
    return run_control_loop(
        params=params,
        actor="daemon",
        once_func=execute_browser_once,
        idle_status_key="daemon_status",
    )


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
    return marked_job, 1 if marked_job.get("status") == "success" else 0, 0


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
        return execution, 1 if execution.status == "skipped" else 0, 0
    execution = store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=run_id,
        summary=stored_summary,
        result=stored_result,
    )
    return execution, 1 if execution.status == "success" else 0, 0


def _sanitize_task_payload(params: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(params)
    sanitized.pop("control_action", None)
    return sanitized


def _runtime_db_health_preflight(*, store: RuntimeStore, settings: Any) -> dict[str, Any]:
    if not bool(getattr(settings, "db_health_preflight_enabled", True)):
        return {}
    health = store.collect_db_connection_health(
        max_connection_ratio=float(getattr(settings, "db_health_max_connection_ratio", 0.8) or 0.8),
        max_idle_in_transaction=int(getattr(settings, "db_health_max_idle_in_transaction", -1)),
    )
    if bool(health.get("healthy", False)):
        return {}
    warnings = ", ".join(str(item) for item in health.get("warnings", []) or [])
    return {
        "message": f"Runtime DB connection health check failed: {warnings or 'unhealthy'}.",
        "db_connection_health": health,
    }


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


def _release_request_after_child_completion(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    runtime = _resolve_workflow_runtime(request.task_code)
    if runtime is None:
        return []
    return runtime.release_request_after_child_completion(store=store, request_id=request_id)


def _resolve_workflow_runtime(task_code: str) -> Any | None:
    return load_workflow_runtime(ensure_formal_task_code(task_code))


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

    from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry

    return build_bound_api_handler_registry()


def _build_bound_browser_handler_registry() -> Any:
    if BROWSER_HANDLER_REGISTRY is not None:
        return BROWSER_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.browser import (
        build_bound_browser_handler_registry,
    )

    return build_bound_browser_handler_registry()


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
