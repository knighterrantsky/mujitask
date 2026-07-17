from __future__ import annotations

import os
from typing import Any

from automation_business_scaffold.control_plane.executor.looping import (
    build_child_runner_config,
    supervisor_error_payload,
)
from automation_business_scaffold.control_plane.executor.request_aggregation import (
    build_runtime_request_payload,
)
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
from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CODES
from automation_business_scaffold.contracts.handler.contract import HandlerContext
from automation_business_scaffold.contracts.handler.domain_mapping import (
    RuntimeFailureProjection,
    RuntimeStorageProjection,
    get_runtime_result_projection,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.models import ArtifactObjectRecord


def execute_api_worker_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    job = store.claim_next_api_worker_job(
        worker_id=settings.worker_id,
        worker_pid=os.getpid(),
        lease_seconds=settings.lease_seconds,
        request_id=str(params.get("request_id") or ""),
        job_code=str(params.get("job_code") or ""),
    )
    if job is None:
        return build_idle_payload(
            control_action="api_worker_once",
            actor="daemon",
            message="No api_worker_job is ready for processing.",
        )

    run_id = str(job.get("run_id") or "")
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
        metadata={
            "request_payload": dict((job.get("payload") or {}).get("request_payload") or {}),
            "run_id": run_id,
        },
    )
    projection = get_runtime_result_projection(context.handler_code)
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
            on_progress=lambda event: _update_api_worker_progress(
                store=store,
                job_id=str(job["job_id"]),
                run_id=run_id,
                handler_code=context.handler_code,
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

    payload = build_runtime_request_payload(
        store=store,
        request_id=str(job["request_id"]),
        control_action="api_worker_once",
        message="API worker processed one runtime handler job.",
    )
    if projection is None:
        worker_result = outcome.worker_result.to_dict()
        supervisor = outcome.to_dict()
        error_payload = supervisor_error_payload(outcome) if outcome.error is not None else {}
    else:
        worker_result, supervisor, error_payload = projection.project_response(
            context.handler_code,
            marked_job.get("summary"),
            marked_job.get("result"),
            marked_job.get("error_type"),
            marked_job.get("error_code"),
        )
    payload.update(
        {
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": success_count,
            "failed_count": failed_count,
            "api_worker_job": marked_job,
            "worker_result": worker_result,
            "supervisor": supervisor,
        }
    )
    payload.update(error_payload)
    return payload


def execute_browser_once(params: dict[str, Any]) -> dict[str, Any]:
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    execution = store.claim_next_browser_execution(
        worker_id=settings.worker_id,
        worker_pid=os.getpid(),
        lease_seconds=settings.lease_seconds,
        request_id=str(params.get("request_id") or ""),
        item_codes=tuple(sorted(BROWSER_HANDLER_CODES)),
    )
    if execution is None:
        return build_idle_payload(
            control_action="browser_once",
            actor="daemon",
            message="No browser execution is ready for processing.",
        )

    payload_data = dict(execution.payload or {})
    run_id = str(execution.run_id or "")
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
        metadata={
            "request_payload": dict(payload_data.get("request_payload") or {}),
            "run_id": run_id,
        },
    )
    projection = get_runtime_result_projection(context.handler_code)
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
            on_progress=lambda event: _update_browser_progress(
                store=store,
                execution_id=execution.execution_id,
                run_id=run_id,
                handler_code=context.handler_code,
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

    payload = build_runtime_request_payload(
        store=store,
        request_id=execution.request_id,
        control_action="browser_once",
        message="Browser worker processed one runtime execution.",
    )
    if projection is None:
        worker_result = outcome.worker_result.to_dict()
        supervisor = outcome.to_dict()
        error_payload = supervisor_error_payload(outcome) if outcome.error is not None else {}
    else:
        worker_result, supervisor, error_payload = projection.project_response(
            context.handler_code,
            stored_execution.summary,
            stored_execution.result,
            stored_execution.error_type,
            stored_execution.error_code,
        )
    payload.update(
        {
            "daemon_status": "processed",
            "processed_count": 1,
            "success_count": success_count,
            "failed_count": failed_count,
            "execution": stored_execution.to_dict(),
            "execution_status": stored_execution.result_status or stored_execution.status,
            "worker_result": worker_result,
            "supervisor": supervisor,
        }
    )
    payload.update(error_payload)
    return payload


def persist_api_worker_outcome(
    *,
    store: RuntimeStore,
    job_id: str,
    run_id: str,
    outcome: ExecutionSupervisorOutcome,
    retry_delay_seconds: float,
) -> tuple[dict[str, Any], int, int]:
    context = getattr(outcome, "context", None)
    projection = get_runtime_result_projection(str(getattr(context, "handler_code", "")))
    try:
        storage = (
            projection.project_storage(outcome)
            if projection is not None
            else RuntimeStorageProjection(
                summary=outcome.storage_summary(),
                result=outcome.storage_result(),
            )
        )
    except Exception as exc:
        if projection is None:
            raise
        failure = projection.projection_failure(outcome, exc, phase="validation")
        marked_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=failure.error_text,
            summary=failure.summary,
            result=failure.result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=failure.error_type,
            error_code=failure.error_code,
            dead_letter_reason=failure.dead_letter_reason,
            force_terminal=failure.force_terminal,
        )
        return marked_job, 0, 1 if marked_job.get("result_status") == "failed" else 0

    if outcome.should_mark_failed:
        failure = (
            projection.failure_policy(outcome)
            if projection is not None
            else _default_failure_projection(outcome, storage)
        )
        marked_job = store.mark_api_worker_job_retry_or_failed(
            job_id=job_id,
            run_id=run_id,
            error_text=failure.error_text,
            summary=failure.summary,
            result=failure.result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=failure.error_type,
            error_code=failure.error_code,
            dead_letter_reason=failure.dead_letter_reason,
            force_terminal=failure.force_terminal,
        )
        return marked_job, 0, 1 if marked_job.get("result_status") == "failed" else 0

    if outcome.worker_result.status in {"fallback_required", "browser_required"}:
        failure = (
            _default_failure_projection(outcome, storage)
            if outcome.worker_result.status == "fallback_required"
            else None
        )
        marked_job = store.mark_api_worker_job_waiting(
            job_id=job_id,
            run_id=run_id,
            summary=storage.summary,
            result=storage.result,
            stage=_api_worker_stage_from_handler_result(outcome.worker_result.status),
            error_text=(failure.error_text if failure is not None and outcome.error is not None else ""),
            error_type=failure.error_type if failure is not None else "",
            error_code=failure.error_code if failure is not None else "",
        )
        return marked_job, 0, 0

    marked_job = store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=run_id,
        summary=storage.summary,
        result=storage.result,
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
    context = getattr(outcome, "context", None)
    projection = get_runtime_result_projection(str(getattr(context, "handler_code", "")))
    try:
        storage = (
            projection.project_storage(outcome)
            if projection is not None
            else RuntimeStorageProjection(
                summary=outcome.storage_summary(),
                result=outcome.storage_result(),
            )
        )
    except Exception as exc:
        if projection is None:
            raise
        failure = projection.projection_failure(outcome, exc, phase="validation")
        execution = store.mark_browser_execution_failed(
            execution_id=execution_id,
            run_id=run_id,
            error_text=failure.error_text,
            summary=failure.summary,
            result=failure.result,
            error_type=failure.error_type,
            error_code=failure.error_code,
            dead_letter_reason=failure.dead_letter_reason,
        )
        return execution, 0, 1 if execution.result_status == "failed" else 0

    try:
        _replace_projected_artifacts(store=store, projection=storage)
    except Exception as exc:
        if projection is None:
            raise
        failure = projection.projection_failure(outcome, exc, phase="artifact_index")
        execution = store.mark_browser_execution_retry_or_failed(
            execution_id=execution_id,
            run_id=run_id,
            error_text=failure.error_text,
            summary=failure.summary,
            result=failure.result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=failure.error_type,
            error_code=failure.error_code,
            dead_letter_reason=failure.dead_letter_reason,
        )
        return execution, 0, 1 if execution.result_status == "failed" else 0

    if outcome.should_mark_failed:
        failure = (
            projection.failure_policy(outcome)
            if projection is not None
            else _default_failure_projection(outcome, storage)
        )
        if failure.terminal:
            execution = store.mark_browser_execution_failed(
                execution_id=execution_id,
                run_id=run_id,
                summary=failure.summary,
                result=failure.result,
                error_text=failure.error_text,
                error_type=failure.error_type,
                error_code=failure.error_code,
                dead_letter_reason=failure.dead_letter_reason,
            )
            return execution, 0, 1 if execution.result_status == "failed" else 0
        execution = store.mark_browser_execution_retry_or_failed(
            execution_id=execution_id,
            run_id=run_id,
            error_text=failure.error_text,
            summary=failure.summary,
            result=failure.result,
            retry_delay_seconds=retry_delay_seconds,
            error_type=failure.error_type,
            error_code=failure.error_code,
            dead_letter_reason=failure.dead_letter_reason,
        )
        return execution, 0, 1 if execution.result_status == "failed" else 0

    if outcome.worker_result.status == "skipped":
        execution = store.mark_browser_execution_skipped(
            execution_id=execution_id,
            run_id=run_id,
            summary=storage.summary,
            result=storage.result,
        )
        return execution, 1 if execution.result_status == "skipped" else 0, 0
    execution = store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=run_id,
        summary=storage.summary,
        result=storage.result,
    )
    return execution, 1 if execution.result_status in {"success", "partial_success"} else 0, 0


def _default_failure_projection(
    outcome: ExecutionSupervisorOutcome,
    storage: RuntimeStorageProjection,
) -> RuntimeFailureProjection:
    error = getattr(outcome, "error", None)
    return RuntimeFailureProjection(
        summary=storage.summary,
        result=storage.result,
        error_text=str(getattr(outcome, "error_text", "")),
        error_type=error.error_type if error is not None else "",
        error_code=error.error_code if error is not None else "",
        dead_letter_reason=("supervisor_failed" if error is not None and error.terminal else ""),
        terminal=bool(error.terminal) if error is not None else False,
    )


def _runtime_progress(
    handler_code: str,
    progress_stage: Any,
    message: Any,
) -> tuple[str, str]:
    projection = get_runtime_result_projection(handler_code)
    if projection is None:
        return str(progress_stage or ""), str(message or "")
    return projection.project_progress(handler_code, progress_stage, message)


def _update_api_worker_progress(
    *,
    store: RuntimeStore,
    job_id: str,
    run_id: str,
    handler_code: str,
    progress_stage: Any,
    message: Any,
) -> None:
    safe_stage, safe_message = _runtime_progress(handler_code, progress_stage, message)
    store.update_api_worker_job_progress(
        job_id=job_id,
        run_id=run_id,
        progress_stage=safe_stage,
        message=safe_message,
    )


def _update_browser_progress(
    *,
    store: RuntimeStore,
    execution_id: str,
    run_id: str,
    handler_code: str,
    progress_stage: Any,
    message: Any,
) -> None:
    safe_stage, safe_message = _runtime_progress(handler_code, progress_stage, message)
    store.update_task_execution_progress(
        execution_id=execution_id,
        run_id=run_id,
        progress_stage=safe_stage,
        message=safe_message,
    )


def _replace_projected_artifacts(
    *,
    store: RuntimeStore,
    projection: RuntimeStorageProjection,
) -> None:
    if not projection.artifact_records:
        return
    records = [ArtifactObjectRecord(**record) for record in projection.artifact_records]
    by_coordinate = {
        (record.bucket, record.object_key): record
        for record in store.list_artifacts(run_id=projection.artifact_run_id)
    }
    for record in records:
        by_coordinate[(record.bucket, record.object_key)] = record
    store.replace_artifacts(
        run_id=projection.artifact_run_id,
        records=sorted(
            by_coordinate.values(),
            key=lambda record: (record.created_at, record.artifact_id),
        ),
    )


def _dispatch_api_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_api_handler_registry().dispatch(context.handler_code, context)


def _dispatch_browser_runtime_handler(context: HandlerContext) -> Any:
    return _build_bound_browser_handler_registry().dispatch(context.handler_code, context)


def _build_bound_api_handler_registry() -> Any:
    from automation_business_scaffold.control_plane.executor import runner as runner_facade

    if runner_facade.API_HANDLER_REGISTRY is not None:
        return runner_facade.API_HANDLER_REGISTRY

    from automation_business_scaffold.contracts.handler.api import (
        build_bound_api_handler_registry,
    )

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
        "browser_required": "browser_required",
        "failed": "failed",
    }
    return mapping.get(status, status or "completed")
