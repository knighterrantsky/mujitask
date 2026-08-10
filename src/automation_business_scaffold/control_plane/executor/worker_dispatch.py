from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from typing import Any, Mapping

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
    ExecutionSupervisorError,
    ExecutionSupervisorOutcome,
    run_supervised_handler,
)
from automation_business_scaffold.contracts.handler.allowlist import BROWSER_HANDLER_CODES
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
)
from automation_business_scaffold.contracts.handler.domain_mapping import (
    RuntimeFailureProjection,
    RuntimeStorageProjection,
    get_runtime_result_projection,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.infrastructure.browser.browser_bridge import (
    classify_browser_stall,
    ensure_browser_healthy,
    probe_browser_health,
    resolve_automation_browser_target_digest,
)
from automation_business_scaffold.models import ArtifactObjectRecord
from automation_business_scaffold.project_env import PROJECT_ROOT


_BROWSER_RUNLOOP_QUARANTINE_PATH = (
    PROJECT_ROOT / "runtime" / "daemons" / "browser_runloop.quarantine.json"
)


def _browser_runloop_quarantine_payload() -> dict[str, Any] | None:
    path = _BROWSER_RUNLOOP_QUARANTINE_PATH
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {
            "event": "quarantine_marker_unreadable",
            "execution_id": "",
            "child_pid": 0,
            "child_pids": [],
            "created_at": 0.0,
            "error_class": type(exc).__name__,
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    raw_child_pids = raw.get("child_pids")
    candidates = raw_child_pids if isinstance(raw_child_pids, list) else []
    candidates = [*candidates, raw.get("child_pid")]
    child_pids: list[int] = []
    for candidate in candidates:
        try:
            child_pid = int(candidate or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if child_pid > 0 and child_pid not in child_pids:
            child_pids.append(child_pid)
    try:
        created_at = float(raw.get("created_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        created_at = 0.0
    return {
        "event": "child_exit_unconfirmed",
        "execution_id": str(raw.get("execution_id") or ""),
        "child_pid": child_pids[0] if child_pids else 0,
        "child_pids": child_pids,
        "created_at": created_at,
    }


def _write_browser_runloop_quarantine(
    *,
    execution_id: str,
    run_id: str,
    resource_code: str,
    child_pid: int | None = None,
    child_pids: tuple[int, ...] | list[int] | None = None,
) -> None:
    path = _BROWSER_RUNLOOP_QUARANTINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_child_pids: list[int] = []
    for candidate in [*(child_pids or ()), child_pid]:
        try:
            value = int(candidate or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0 and value not in normalized_child_pids:
            normalized_child_pids.append(value)
    payload = {
        "schema_version": 1,
        "event": "child_exit_unconfirmed",
        "execution_id": str(execution_id or ""),
        "run_id": str(run_id or ""),
        "resource_digest": hashlib.sha256(str(resource_code or "").encode("utf-8")).hexdigest(),
        "child_pid": normalized_child_pids[0] if normalized_child_pids else 0,
        "child_pids": normalized_child_pids,
        "created_at": time.time(),
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _hold_browser_runloop_fail_closed(
    *,
    store: RuntimeStore,
    execution_id: str,
    run_id: str,
    resource_code: str,
    child_pid: int | None = None,
    child_pids: tuple[int, ...] | list[int] | None = None,
    lease_seconds: float,
    heartbeat_interval_seconds: float,
    error_class: str,
) -> None:
    print(
        json.dumps(
            {
                "component": "browser_execution_recovery",
                "event": "browser_runloop_quarantine_write_failed",
                "execution_id": str(execution_id or ""),
                "error_class": str(error_class or "RuntimeError"),
                "reported_at": time.time(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    while True:
        try:
            store.update_task_execution_progress(
                execution_id=execution_id,
                run_id=run_id,
                progress_stage="browser_runloop_quarantine_write_failed",
                message=(
                    "Child exit is unconfirmed and the browser runloop is held fail-closed."
                ),
            )
            store.heartbeat_browser_execution(
                execution_id=execution_id,
                run_id=run_id,
                lease_seconds=lease_seconds,
            )
        except Exception:
            pass
        try:
            _write_browser_runloop_quarantine(
                execution_id=execution_id,
                run_id=run_id,
                resource_code=resource_code,
                child_pid=child_pid,
                child_pids=child_pids,
            )
        except Exception:
            time.sleep(max(float(heartbeat_interval_seconds), 0.2))
            continue
        return


def _quarantine_unconfirmed_browser_child(
    *,
    store: RuntimeStore,
    execution_id: str,
    run_id: str,
    resource_code: str,
    child_pid: int | None = None,
    child_pids: tuple[int, ...] | list[int] | None = None,
    lease_seconds: float,
    heartbeat_interval_seconds: float,
) -> None:
    try:
        _write_browser_runloop_quarantine(
            execution_id=execution_id,
            run_id=run_id,
            resource_code=resource_code,
            child_pid=child_pid,
            child_pids=child_pids,
        )
    except Exception as exc:
        _hold_browser_runloop_fail_closed(
            store=store,
            execution_id=execution_id,
            run_id=run_id,
            resource_code=resource_code,
            child_pid=child_pid,
            child_pids=child_pids,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            error_class=type(exc).__name__,
        )


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
    quarantine = _browser_runloop_quarantine_payload()
    if quarantine is not None:
        payload = build_idle_payload(
            control_action="browser_once",
            actor="daemon",
            message=(
                "Browser runloop is quarantined because a prior child process exit could not "
                "be confirmed."
            ),
        )
        payload.update(
            {
                "daemon_status": "quarantined",
                "error_type": "internal",
                "error_code": "child_termination_failed",
                "retryable": False,
                "terminal_error": True,
                "browser_runloop_quarantine": quarantine,
            }
        )
        return payload

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
    stall_probe = _build_browser_stall_probe(
        store=store,
        context=context,
        execution_id=execution.execution_id,
        run_id=run_id,
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
        on_stall=stall_probe,
    )
    outcome = _attach_browser_stall_diagnosis(outcome)
    diagnosis = outcome.worker_result.result.get("browser_diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, Mapping) else {}
    unconfirmed_child_pids = list(_unconfirmed_probe_pids(diagnosis))
    if (
        outcome.child_runner is not None
        and outcome.child_runner.status == "termination_failed"
    ):
        business_child_pid = int(outcome.child_runner.child_pid or 0)
        if business_child_pid > 0 and business_child_pid not in unconfirmed_child_pids:
            unconfirmed_child_pids.append(business_child_pid)
    requires_quarantine = (
        outcome.child_runner is not None
        and outcome.child_runner.status == "termination_failed"
    ) or str(getattr(outcome.error, "error_code", "")) == "browser_probe_termination_failed"
    if requires_quarantine:
        _quarantine_unconfirmed_browser_child(
            store=store,
            execution_id=execution.execution_id,
            run_id=run_id,
            resource_code=execution.resource_code,
            child_pids=unconfirmed_child_pids,
            lease_seconds=settings.lease_seconds,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
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


def _build_browser_stall_probe(
    *,
    store: RuntimeStore,
    context: HandlerContext,
    execution_id: str,
    run_id: str,
) -> Any:
    target_request = _browser_health_target_request(context)
    evidence: dict[str, Any] = {}

    def still_owned() -> bool:
        try:
            current = store.load_task_execution(execution_id=execution_id)
        except Exception:
            return False
        return current.status == "running" and current.run_id == run_id

    def report(stage: str) -> bool:
        if not still_owned():
            return False
        _update_browser_progress(
            store=store,
            execution_id=execution_id,
            run_id=run_id,
            handler_code=context.handler_code,
            progress_stage=stage,
            message="Browser stall diagnosis updated.",
        )
        return still_owned()

    def unavailable_probe(reason: str) -> dict[str, Any]:
        return {
            "healthy": False,
            "status": "unavailable",
            "phase": "target_resolution",
            "error_class": reason,
        }

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        last_operation = str(details.get("last_progress_stage") or "")
        last_operation_state = str(details.get("last_progress_state") or "unknown")
        if stage == "pre_kill":
            if not report("browser_probe_before_kill"):
                probe = unavailable_probe("execution_ownership_lost")
            elif not target_request:
                probe = unavailable_probe("browser_target_unavailable")
            else:
                probe = probe_browser_health(**target_request)
            evidence["probe_before_kill"] = probe
            return {"probe_before_kill": probe}

        child_exit_confirmed = bool(
            (details.get("termination") or {}).get("confirmed_exited")
            if isinstance(details.get("termination"), Mapping)
            else False
        )
        if not child_exit_confirmed:
            return {
                "browser_diagnosis": classify_browser_stall(
                    last_operation=last_operation,
                    last_operation_state=last_operation_state,
                    probe_before_kill=evidence.get("probe_before_kill"),
                    probe_after_kill=None,
                    probe_after_restart=None,
                    child_exit_confirmed=False,
                    restart_count=0,
                )
            }

        probe_before_kill = evidence.get("probe_before_kill")
        if (
            isinstance(probe_before_kill, Mapping)
            and probe_before_kill.get("probe_exit_confirmed") is False
        ):
            diagnosis = classify_browser_stall(
                last_operation=last_operation,
                last_operation_state=last_operation_state,
                probe_before_kill=probe_before_kill,
                probe_after_kill=None,
                probe_after_restart=None,
                child_exit_confirmed=True,
                restart_count=0,
            )
            return {
                "recovery": {},
                "browser_diagnosis": _apply_unconfirmed_probe_failure(diagnosis),
            }

        if not report("browser_probe_after_kill"):
            probe_after_kill = unavailable_probe("execution_ownership_lost")
        elif not target_request:
            probe_after_kill = unavailable_probe("browser_target_unavailable")
        else:
            probe_after_kill = probe_browser_health(**target_request)
        evidence["probe_after_kill"] = probe_after_kill

        recovery: dict[str, Any] = {}
        if (
            target_request
            and isinstance(probe_before_kill, Mapping)
            and probe_before_kill.get("status") == "unhealthy"
            and probe_after_kill.get("status") == "unhealthy"
        ):
            recovery = ensure_browser_healthy(
                **target_request,
                max_restarts=1,
                initial_probe=probe_after_kill,
                before_restart=lambda: report("browser_restart"),
            )
        probe_after_restart = recovery.get("probe_after_restart")
        restart_count = int(recovery.get("restart_count") or 0)
        if isinstance(probe_after_restart, Mapping):
            report("browser_probe_after_restart")

        diagnosis = classify_browser_stall(
            last_operation=last_operation,
            last_operation_state=last_operation_state,
            probe_before_kill=evidence.get("probe_before_kill"),
            probe_after_kill=probe_after_kill,
            probe_after_restart=(
                probe_after_restart if isinstance(probe_after_restart, Mapping) else None
            ),
            child_exit_confirmed=True,
            restart_count=restart_count,
            recovery_status=str(recovery.get("status") or "not_attempted"),
        )
        return {
            "probe_after_kill": probe_after_kill,
            "recovery": recovery,
            "browser_diagnosis": diagnosis,
        }

    return on_stall


def _browser_health_target_request(context: HandlerContext) -> dict[str, Any]:
    payload = dict(context.payload)
    if context.handler_code == "amazon_product_browser_fetch":
        profile_ref = str(
            os.environ.get("AMAZON_US_BROWSER_PROFILE_REF")
            or os.environ.get("DEFAULT_PROFILE_REF")
            or ""
        ).strip()
        if not profile_ref:
            return {}
        try:
            expected_digest = str(context.resource_code or "").removeprefix("browser:amazon:")
            if (
                not expected_digest
                or resolve_automation_browser_target_digest(profile_ref=profile_ref)
                != expected_digest
            ):
                return {}
        except Exception:
            return {}
        return {"profile_ref": profile_ref}

    is_fastmoss = context.handler_code == "fastmoss_security_browser_resolve"
    prefix = "fastmoss" if is_fastmoss else "tiktok"
    env_prefix = "FASTMOSS" if is_fastmoss else "TIKTOK"
    profile_ref = _first_browser_value(
        payload,
        (f"{prefix}_browser_profile_ref", "browser_profile_ref", "profile_ref"),
    ) or str(
        os.environ.get(f"{env_prefix}_BROWSER_PROFILE_REF")
        or os.environ.get("BROWSER_PROFILE_REF")
        or os.environ.get("DEFAULT_PROFILE_REF")
        or ""
    ).strip()
    provider_name = _first_browser_value(
        payload,
        (f"{prefix}_browser_provider_name", "browser_provider_name"),
    ) or str(
        os.environ.get(f"{env_prefix}_BROWSER_PROVIDER_NAME")
        or os.environ.get("BROWSER_PROVIDER_NAME")
        or ""
    ).strip()
    profile_id = _first_browser_value(
        payload,
        (f"{prefix}_browser_profile_id", "browser_profile_id"),
    ) or str(
        os.environ.get(f"{env_prefix}_BROWSER_PROFILE_ID")
        or os.environ.get("BROWSER_PROFILE_ID")
        or ""
    ).strip()
    if provider_name and profile_id:
        workspace_value = _first_browser_value(
            payload,
            (f"{prefix}_browser_workspace_id", "browser_workspace_id"),
        ) or str(
            os.environ.get(f"{env_prefix}_BROWSER_WORKSPACE_ID")
            or os.environ.get("BROWSER_WORKSPACE_ID")
            or ""
        )
        try:
            workspace_id = int(workspace_value) if workspace_value else None
        except (TypeError, ValueError):
            workspace_id = None
        return {
            "workspace_id": workspace_id,
            "profile_id": profile_id,
            "provider_name": provider_name,
        }
    return {"profile_ref": profile_ref} if profile_ref else {}


def _first_browser_value(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _unconfirmed_probe_pids(diagnosis: Mapping[str, Any]) -> tuple[int, ...]:
    probe_pids: list[int] = []
    for key in ("probe_before_kill", "probe_after_kill", "probe_after_restart"):
        probe = diagnosis.get(key)
        if not isinstance(probe, Mapping) or probe.get("probe_exit_confirmed") is not False:
            continue
        try:
            probe_pid = max(int(probe.get("probe_pid") or 0), 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if probe_pid > 0 and probe_pid not in probe_pids:
            probe_pids.append(probe_pid)
    return tuple(probe_pids)


def _unconfirmed_probe_pid(diagnosis: Mapping[str, Any]) -> int:
    probe_pids = _unconfirmed_probe_pids(diagnosis)
    return probe_pids[0] if probe_pids else 0


def _apply_unconfirmed_probe_failure(diagnosis: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(diagnosis)
    has_unconfirmed_probe = any(
        isinstance(result.get(key), Mapping)
        and result[key].get("probe_exit_confirmed") is False
        for key in ("probe_before_kill", "probe_after_kill", "probe_after_restart")
    )
    if has_unconfirmed_probe:
        result.update(
            {
                "failure_scope": "unknown",
                "diagnosis_code": "browser_probe_exit_unconfirmed",
                "diagnosis_confidence": "low",
                "root_cause_confirmed": False,
                "final_error_code": "browser_probe_termination_failed",
            }
        )
    return result


def _attach_browser_stall_diagnosis(
    outcome: ExecutionSupervisorOutcome,
) -> ExecutionSupervisorOutcome:
    child_runner = outcome.child_runner
    if child_runner is None or child_runner.status not in {"stalled", "termination_failed"}:
        return outcome

    details = dict(child_runner.details)
    hooks = details.get("stall_hooks") if isinstance(details.get("stall_hooks"), Mapping) else {}
    post_hook = hooks.get("post_kill") if isinstance(hooks, Mapping) else {}
    post_result = post_hook.get("result") if isinstance(post_hook, Mapping) else {}
    diagnosis = (
        dict(post_result.get("browser_diagnosis") or {})
        if isinstance(post_result, Mapping)
        else {}
    )
    if not diagnosis:
        stall = details.get("stall") if isinstance(details.get("stall"), Mapping) else {}
        pre_hook = hooks.get("pre_kill") if isinstance(hooks, Mapping) else {}
        pre_result = pre_hook.get("result") if isinstance(pre_hook, Mapping) else {}
        diagnosis = classify_browser_stall(
            last_operation=str(stall.get("last_progress_stage") or ""),
            last_operation_state=str(stall.get("last_progress_state") or "unknown"),
            probe_before_kill=(
                pre_result.get("probe_before_kill")
                if isinstance(pre_result, Mapping)
                else None
            ),
            probe_after_kill=None,
            probe_after_restart=None,
            child_exit_confirmed=False,
            restart_count=0,
        )

    diagnosis = _apply_unconfirmed_probe_failure(diagnosis)

    final_error_code = str(diagnosis.get("final_error_code") or "child_process_stalled")
    if final_error_code == "browser_recovery_failed":
        message = "Browser recovery failed after the child process stalled."
    elif final_error_code in {
        "browser_probe_termination_failed",
        "child_termination_failed",
    }:
        message = (
            "Browser process exit could not be confirmed; browser recovery was not "
            "attempted."
        )
    else:
        message = "Browser child process stalled and was terminated by the supervisor."
    handler_error = HandlerError(
        error_type=(
            "internal"
            if final_error_code
            in {"browser_probe_termination_failed", "child_termination_failed"}
            else "timeout"
        ),
        error_code=final_error_code,
        message=message,
        retryable=False,
        details={"browser_diagnosis": diagnosis},
    )
    worker_result = replace(
        outcome.worker_result,
        summary={**dict(outcome.worker_result.summary), "browser_diagnosis": diagnosis},
        result={**dict(outcome.worker_result.result), "browser_diagnosis": diagnosis},
        error=handler_error,
    )
    supervisor_error = ExecutionSupervisorError(
        error_type=handler_error.error_type,
        error_code=handler_error.error_code,
        message=handler_error.message,
        retryable=False,
        terminal=True,
        details=dict(handler_error.details),
    )
    return replace(outcome, worker_result=worker_result, error=supervisor_error)


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
