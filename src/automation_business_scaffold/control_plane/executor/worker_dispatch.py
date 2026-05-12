from __future__ import annotations

import os
from typing import Any

from automation_business_scaffold.control_plane.executor.looping import (
    build_child_runner_config,
    supervisor_error_payload,
)
from automation_business_scaffold.control_plane.executor.request_aggregation import build_runtime_request_payload
from automation_business_scaffold.control_plane.executor.request_dispatch import release_request_after_child_completion
from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_idle_payload,
    build_runtime_settings,
    create_runtime_store,
)
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    ExecutionSupervisorOutcome,
    run_supervised_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


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
    marked_job, success_count, failed_count = persist_api_worker_outcome(
        store=store,
        job_id=str(job["job_id"]),
        run_id=run_id,
        outcome=outcome,
        retry_delay_seconds=settings.retry_delay_seconds,
    )

    parent_request = store.load_task_request(request_id=str(job["request_id"]))
    if parent_request.status == "cancelling":
        parent_updates = [store.reconcile_cancelling_request(request_id=str(job["request_id"]))]
    else:
        parent_updates = release_request_after_child_completion(store, request_id=str(job["request_id"]))
    payload = build_runtime_request_payload(
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
    stored_execution, success_count, failed_count = persist_browser_execution_outcome(
        store=store,
        execution_id=execution.execution_id,
        run_id=run_id,
        outcome=outcome,
        retry_delay_seconds=settings.retry_delay_seconds,
    )

    parent_request = store.load_task_request(request_id=execution.request_id)
    if parent_request.status == "cancelling":
        parent_updates = [store.reconcile_cancelling_request(request_id=execution.request_id)]
    else:
        parent_updates = release_request_after_child_completion(store, request_id=execution.request_id)
    payload = build_runtime_request_payload(
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
            "execution_status": stored_execution.result_status or stored_execution.status,
            "worker_result": outcome.worker_result.to_dict(),
            "supervisor": outcome.to_dict(),
            "parent_updates": parent_updates,
        }
    )
    if outcome.error is not None:
        payload.update(supervisor_error_payload(outcome))
    return payload


def persist_api_worker_outcome(
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
        return marked_job, 0, 1 if marked_job.get("result_status") == "failed" else 0

    if outcome.worker_result.status == "fallback_required":
        marked_job = store.mark_api_worker_job_waiting(
            job_id=job_id,
            run_id=run_id,
            summary=stored_summary,
            result=stored_result,
            stage=_api_worker_stage_from_handler_result(outcome.worker_result.status),
            error_text=outcome.error_text if outcome.error is not None else "",
            error_type=outcome.error.error_type if outcome.error is not None else "",
            error_code=outcome.error.error_code if outcome.error is not None else "",
        )
        return marked_job, 0, 0

    marked_job = store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=run_id,
        summary=stored_summary,
        result=stored_result,
        stage=_api_worker_stage_from_handler_result(outcome.worker_result.status),
    )
    marked_result_status = str(marked_job.get("result_status") or marked_job.get("status") or "")
    return marked_job, 1 if marked_result_status in {"success", "partial_success"} else 0, 0


def persist_browser_execution_outcome(
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
        return execution, 0, 1 if execution.result_status == "failed" else 0
    if outcome.worker_result.status == "skipped":
        execution = store.mark_browser_execution_skipped(
            execution_id=execution_id,
            run_id=run_id,
            summary=stored_summary,
            result=stored_result,
        )
        return execution, 1 if execution.result_status == "skipped" else 0, 0
    execution = store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=run_id,
        summary=stored_summary,
        result=stored_result,
    )
    return execution, 1 if execution.result_status in {"success", "partial_success"} else 0, 0


def _dispatch_api_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_api_handler_registry().dispatch(context.handler_code, context)


def _dispatch_browser_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_browser_handler_registry().dispatch(context.handler_code, context)


def _build_bound_api_handler_registry() -> Any:
    from automation_business_scaffold.control_plane.executor import runner as runner_facade

    if runner_facade.API_HANDLER_REGISTRY is not None:
        return runner_facade.API_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.api import build_bound_api_handler_registry

    return build_bound_api_handler_registry()


def _build_bound_browser_handler_registry() -> Any:
    from automation_business_scaffold.control_plane.executor import runner as runner_facade

    if runner_facade.BROWSER_HANDLER_REGISTRY is not None:
        return runner_facade.BROWSER_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.browser import (
        build_bound_browser_handler_registry,
    )

    return build_bound_browser_handler_registry()


def _api_worker_stage_from_handler_result(status: str) -> str:
    mapping = {
        "success": "completed",
        "partial_success": "partial_success",
        "skipped": "skipped",
        "fallback_required": "browser_fallback_required",
        "failed": "failed",
    }
    return mapping.get(status, status or "completed")
