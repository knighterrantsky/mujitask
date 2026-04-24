from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from automation_business_scaffold.control_plane.supervisor.child_runner import (
    ChildRunner,
    ChildRunnerConfig,
    ChildRunnerEnvelope,
    ChildRunnerProgressEvent,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerError, HandlerResult
from automation_business_scaffold.contracts.handler.registry import HandlerInvocationContractError, HandlerRegistryError

FailureDisposition = Literal["none", "retryable", "terminal"]
DispatchCallable = Callable[[HandlerContext], HandlerResult]
HeartbeatCallback = Callable[[], None]
ProgressCallback = Callable[["ExecutionProgressEvent"], None]


@dataclass(frozen=True, slots=True)
class ExecutionProgressEvent:
    progress_stage: str
    message: str = ""
    percent: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    reported_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "progress_stage": self.progress_stage,
            "message": self.message,
            "details": dict(self.details),
            "reported_at": self.reported_at,
        }
        if self.percent is not None:
            payload["percent"] = self.percent
        return payload


@dataclass(frozen=True, slots=True)
class ExecutionSupervisorError:
    error_type: str
    error_code: str
    message: str
    retryable: bool
    terminal: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ExecutionSupervisorCallbacks:
    heartbeat: HeartbeatCallback | None = None
    on_progress: ProgressCallback | None = None


@dataclass(frozen=True, slots=True)
class ExecutionSupervisorOutcome:
    context: HandlerContext
    worker_result: HandlerResult
    supervisor_status: str
    started_at: float
    finished_at: float
    heartbeat_count: int
    execution_mode: str = "inline"
    progress_events: tuple[ExecutionProgressEvent, ...] = ()
    error: ExecutionSupervisorError | None = None
    child_runner: ChildRunnerEnvelope | None = None

    @property
    def duration_seconds(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)

    @property
    def progress_stage(self) -> str:
        if self.progress_events:
            return self.progress_events[-1].progress_stage
        return ""

    @property
    def failure_disposition(self) -> FailureDisposition:
        if self.worker_result.status != "failed" or self.error is None:
            return "none"
        return "retryable" if self.error.retryable else "terminal"

    @property
    def should_mark_failed(self) -> bool:
        return self.worker_result.status == "failed"

    @property
    def error_text(self) -> str:
        if self.error is not None:
            return self.error.message
        if self.worker_result.error is not None:
            return self.worker_result.error.message
        return "handler_failed"

    def storage_summary(self) -> dict[str, Any]:
        summary = {
            "handler_status": self.worker_result.status,
            "supervisor_status": self.supervisor_status,
            "heartbeat_count": self.heartbeat_count,
            "execution_mode": self.execution_mode,
            **dict(self.worker_result.summary),
        }
        if self.progress_stage:
            summary["progress_stage"] = self.progress_stage
        if self.error is not None:
            summary.setdefault("error_type", self.error.error_type)
            summary.setdefault("error_code", self.error.error_code)
            summary["retryable"] = self.error.retryable
            summary["terminal_error"] = self.error.terminal
        return summary

    def storage_result(self) -> dict[str, Any]:
        payload = {
            "handler_result": self.worker_result.to_dict(),
            "supervisor": self.to_dict(),
            **dict(self.worker_result.result),
        }
        if self.child_runner is not None:
            payload["child_runner"] = self.child_runner.to_dict()
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "supervisor_status": self.supervisor_status,
            "execution_mode": self.execution_mode,
            "worker_type": self.context.worker_type,
            "runtime_table": self.context.runtime_table,
            "request_id": self.context.request_id,
            "job_id": self.context.job_id,
            "handler_code": self.context.handler_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "heartbeat_count": self.heartbeat_count,
            "progress_stage": self.progress_stage,
            "progress_events": [event.to_dict() for event in self.progress_events],
            "failure_disposition": self.failure_disposition,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.child_runner is not None:
            payload["child_runner"] = self.child_runner.to_dict()
        return payload


class ExecutionSupervisor:
    def __init__(
        self,
        *,
        heartbeat_interval_seconds: float,
        callbacks: ExecutionSupervisorCallbacks | None = None,
    ) -> None:
        self.heartbeat_interval_seconds = max(float(heartbeat_interval_seconds or 0.0), 0.0)
        self.callbacks = callbacks or ExecutionSupervisorCallbacks()
        self._heartbeat_count = 0
        self._progress_events: list[ExecutionProgressEvent] = []
        self._lock = threading.Lock()

    def run(
        self,
        *,
        context: HandlerContext,
        dispatch: DispatchCallable,
        child_runner_config: ChildRunnerConfig | None = None,
    ) -> ExecutionSupervisorOutcome:
        started_at = time.time()
        runtime_context = self._bind_context(context)
        stop_event = threading.Event()
        heartbeat_thread = self._start_heartbeat_thread(stop_event)
        child_outcome: ChildRunnerEnvelope | None = None
        execution_mode = "inline"
        try:
            if child_runner_config is not None and child_runner_config.enabled:
                execution_mode = child_runner_config.mode
                child_outcome = ChildRunner(child_runner_config).run(
                    context=runtime_context,
                    dispatch=dispatch,
                    on_progress=self._report_child_progress,
                )
                worker_result = child_outcome.to_handler_result(runtime_context)
            else:
                worker_result = dispatch(runtime_context)
        except Exception as exc:  # noqa: BLE001
            worker_result = HandlerResult.failed(
                runtime_context,
                error=self._classify_exception(exc),
                summary={"supervisor_status": "exception"},
                result={},
            )
        finally:
            stop_event.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(self.heartbeat_interval_seconds, 0.1) + 0.05)

        finished_at = time.time()
        error = self._normalize_error(worker_result)
        return ExecutionSupervisorOutcome(
            context=runtime_context,
            worker_result=worker_result,
            supervisor_status=self._resolve_supervisor_status(child_outcome),
            started_at=started_at,
            finished_at=finished_at,
            heartbeat_count=self._heartbeat_count,
            execution_mode=execution_mode,
            progress_events=tuple(self._progress_events),
            error=error,
            child_runner=child_outcome,
        )

    def _bind_context(self, context: HandlerContext) -> HandlerContext:
        metadata = dict(context.metadata)
        metadata["progress_callback"] = self.report_progress
        if self.callbacks.heartbeat is not None:
            metadata["heartbeat_callback"] = self._emit_heartbeat
        metadata["supervisor_context"] = {
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "worker_type": context.worker_type,
            "runtime_table": context.runtime_table,
        }
        return replace(context, metadata=metadata)

    def _start_heartbeat_thread(self, stop_event: threading.Event) -> threading.Thread | None:
        if self.callbacks.heartbeat is None or self.heartbeat_interval_seconds <= 0.0:
            return None

        def _loop() -> None:
            while not stop_event.wait(self.heartbeat_interval_seconds):
                self._emit_heartbeat()

        thread = threading.Thread(
            target=_loop,
            name="execution-supervisor-heartbeat",
            daemon=True,
        )
        thread.start()
        return thread

    def _emit_heartbeat(self) -> None:
        if self.callbacks.heartbeat is None:
            return
        self.callbacks.heartbeat()
        with self._lock:
            self._heartbeat_count += 1

    def report_progress(
        self,
        progress_stage: str,
        *,
        message: str = "",
        percent: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> ExecutionProgressEvent:
        event = ExecutionProgressEvent(
            progress_stage=str(progress_stage or "").strip() or "in_progress",
            message=str(message or ""),
            percent=percent,
            details=dict(details or {}),
        )
        with self._lock:
            self._progress_events.append(event)
        if self.callbacks.on_progress is not None:
            self.callbacks.on_progress(event)
        return event

    def _report_child_progress(self, event: ChildRunnerProgressEvent) -> ExecutionProgressEvent:
        return self.report_progress(
            event.progress_stage,
            message=event.message,
            percent=event.percent,
            details=event.details,
        )

    def _resolve_supervisor_status(self, child_outcome: ChildRunnerEnvelope | None) -> str:
        if child_outcome is None:
            return "completed"
        if child_outcome.timed_out:
            return "timed_out"
        if child_outcome.status == "internal_error":
            return "child_process_error"
        return "completed"

    def _normalize_error(self, worker_result: HandlerResult) -> ExecutionSupervisorError | None:
        handler_error = worker_result.error
        if handler_error is None:
            if worker_result.status != "failed":
                return None
            handler_error = HandlerError(
                error_type="execution",
                error_code="handler_failed",
                message="Handler returned failed without a structured error payload.",
                retryable=True,
            )
        return ExecutionSupervisorError(
            error_type=handler_error.error_type,
            error_code=handler_error.error_code,
            message=handler_error.message,
            retryable=bool(handler_error.retryable),
            terminal=worker_result.status == "failed" and not bool(handler_error.retryable),
            details=dict(handler_error.details),
        )

    def _classify_exception(self, exc: Exception) -> HandlerError:
        error_type = "internal"
        error_code = "handler_unhandled_exception"
        retryable = True
        if isinstance(exc, (HandlerRegistryError, HandlerInvocationContractError, ValueError)):
            error_type = "contract"
            error_code = "handler_contract_error"
            retryable = False
        elif isinstance(exc, TimeoutError):
            error_type = "timeout"
            error_code = "handler_timeout"
        elif isinstance(exc, (ConnectionError, OSError)):
            error_type = "transport"
            error_code = "handler_transport_error"
        return HandlerError(
            error_type=error_type,
            error_code=error_code,
            message=str(exc),
            retryable=retryable,
            details={"exception_class": type(exc).__name__},
        )


def run_supervised_handler(
    *,
    context: HandlerContext,
    dispatch: DispatchCallable,
    heartbeat_interval_seconds: float,
    callbacks: ExecutionSupervisorCallbacks | None = None,
    child_runner_config: ChildRunnerConfig | None = None,
) -> ExecutionSupervisorOutcome:
    supervisor = ExecutionSupervisor(
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        callbacks=callbacks,
    )
    return supervisor.run(
        context=context,
        dispatch=dispatch,
        child_runner_config=child_runner_config,
    )
