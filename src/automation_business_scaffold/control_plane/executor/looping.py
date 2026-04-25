from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_idle_payload,
    build_runtime_settings,
)
from automation_business_scaffold.control_plane.supervisor.child_runner import ChildRunnerConfig
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorOutcome,
)

DEFAULT_CHILD_TIMEOUT_SECONDS_BY_WORKER = {
    "api_worker": 300.0,
    "browser_worker": 900.0,
    "outbox_dispatcher": 60.0,
}


def run_control_loop(
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


def supervisor_error_payload(outcome: ExecutionSupervisorOutcome) -> dict[str, Any]:
    if outcome.error is None:
        return {}
    return {
        "worker_error": outcome.error.message,
        "error_type": outcome.error.error_type,
        "error_code": outcome.error.error_code,
        "retryable": outcome.error.retryable,
        "terminal_error": outcome.error.terminal,
    }


def build_child_runner_config(
    params: Mapping[str, Any],
    *,
    worker_type: str = "",
    handler_code: str = "",
    runtime_timeout_seconds: Any = None,
) -> ChildRunnerConfig | None:
    explicit_mode = str(params.get("execution_child_runner_mode") or "").strip()
    mode = explicit_mode or _default_child_runner_mode(
        worker_type=worker_type,
        handler_code=handler_code,
    )
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
    del worker_type, handler_code
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


__all__ = [
    "DEFAULT_CHILD_TIMEOUT_SECONDS_BY_WORKER",
    "build_child_runner_config",
    "run_control_loop",
    "supervisor_error_payload",
]
