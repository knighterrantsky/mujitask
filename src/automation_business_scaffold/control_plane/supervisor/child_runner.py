from __future__ import annotations

import json
import math
import multiprocessing
import os
import pickle
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Mapping

from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerNextAction,
    HandlerResult,
)

ChildRunnerMode = Literal["inline", "child_process"]
ChildStallStage = Literal["pre_kill", "post_kill"]
DispatchCallable = Callable[[HandlerContext], HandlerResult]
ChildProgressCallback = Callable[["ChildRunnerProgressEvent"], None]
ChildStallCallback = Callable[[ChildStallStage, Mapping[str, Any]], Mapping[str, Any] | None]

_SENSITIVE_LOG_KEY_MARKERS = (
    "access_key",
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "header",
    "password",
    "payload",
    "profile",
    "private_key",
    "secret",
    "session",
    "token",
    "url",
)
_MAX_LOG_COLLECTION_ITEMS = 20
_MAX_LOG_STRING_LENGTH = 512
_MAX_PROGRESS_EVENTS = 1_000
_PASSIVE_PROGRESS_STAGES = frozenset({"network_response_capture"})


@dataclass(frozen=True, slots=True)
class ChildRunnerConfig:
    mode: ChildRunnerMode = "inline"
    timeout_seconds: float | None = None
    start_method: str | None = None
    poll_interval_seconds: float = 0.02
    terminate_grace_seconds: float = 0.2
    idle_timeout_seconds: float | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "child_process"


@dataclass(frozen=True, slots=True)
class ChildRunnerProgressEvent:
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

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ChildRunnerProgressEvent:
        return cls(
            progress_stage=str(payload.get("progress_stage") or "").strip() or "in_progress",
            message=str(payload.get("message") or ""),
            percent=payload.get("percent"),
            details=dict(payload.get("details") or {}),
            reported_at=float(payload.get("reported_at") or time.time()),
        )


@dataclass(frozen=True, slots=True)
class ChildRunnerEnvelope:
    status: str
    execution_mode: ChildRunnerMode
    timed_out: bool
    started_at: float
    finished_at: float
    child_pid: int | None = None
    exitcode: int | None = None
    worker_result_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    progress_events: tuple[ChildRunnerProgressEvent, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "execution_mode": self.execution_mode,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "progress_events": [event.to_dict() for event in self.progress_events],
            "details": dict(self.details),
        }
        if self.child_pid is not None:
            payload["child_pid"] = self.child_pid
        if self.exitcode is not None:
            payload["exitcode"] = self.exitcode
        if self.worker_result_payload is not None:
            payload["worker_result"] = dict(self.worker_result_payload)
        if self.error_payload is not None:
            payload["error"] = dict(self.error_payload)
        return payload

    def storage_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        if self.worker_result_payload is not None:
            payload["worker_result"] = _compact_worker_result_payload(self.worker_result_payload)
        return payload

    def to_handler_result(self, context: HandlerContext) -> HandlerResult:
        if self.worker_result_payload is not None:
            return handler_result_from_payload(self.worker_result_payload, default_context=context)

        error = handler_error_from_payload(
            self.error_payload,
            default=HandlerError(
                error_type="internal",
                error_code="child_process_result_missing",
                message="Child runner finished without a structured result payload.",
                retryable=True,
            ),
        )
        summary = {
            "execution_mode": self.execution_mode,
            "child_runner_status": self.status,
        }
        if self.timed_out:
            summary["timeout"] = True
        return HandlerResult.failed(
            context,
            error=error,
            summary=summary,
            result={"child_runner": self.to_dict()},
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ChildRunnerEnvelope:
        return cls(
            status=str(payload.get("status") or "internal_error"),
            execution_mode=str(payload.get("execution_mode") or "child_process"),
            timed_out=bool(payload.get("timed_out")),
            started_at=float(payload.get("started_at") or time.time()),
            finished_at=float(payload.get("finished_at") or time.time()),
            child_pid=payload.get("child_pid"),
            exitcode=payload.get("exitcode"),
            worker_result_payload=dict(payload.get("worker_result") or {}) or None,
            error_payload=dict(payload.get("error") or {}) or None,
            progress_events=tuple(
                ChildRunnerProgressEvent.from_payload(item)
                for item in payload.get("progress_events") or ()
            ),
            details=dict(payload.get("details") or {}),
        )


def _compact_worker_result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("result", None)
    return result


def handler_error_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    default: HandlerError,
) -> HandlerError:
    if not payload:
        return default
    return HandlerError(
        error_type=str(payload.get("error_type") or default.error_type),
        error_code=str(payload.get("error_code") or default.error_code),
        message=str(payload.get("message") or default.message),
        retryable=bool(payload.get("retryable", default.retryable)),
        fallback_allowed=bool(payload.get("fallback_allowed", default.fallback_allowed)),
        fallback_reason=str(payload.get("fallback_reason") or default.fallback_reason),
        details=dict(payload.get("details") or {}),
    )


def handler_result_from_payload(
    payload: Mapping[str, Any],
    *,
    default_context: HandlerContext,
) -> HandlerResult:
    error_payload = payload.get("error")
    next_action_payload = dict(payload.get("next_action") or {})
    error = (
        handler_error_from_payload(
            error_payload,
            default=HandlerError(
                error_type="internal",
                error_code="handler_result_error_missing",
                message="HandlerResult payload declared an error but did not include one.",
                retryable=True,
            ),
        )
        if error_payload is not None
        else None
    )
    return HandlerResult(
        status=str(payload.get("status") or "failed"),
        handler_code=str(payload.get("handler_code") or default_context.handler_code),
        request_id=str(payload.get("request_id") or default_context.request_id),
        job_id=str(payload.get("job_id") or default_context.job_id),
        summary=dict(payload.get("summary") or {}),
        result=dict(payload.get("result") or {}),
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        next_action=HandlerNextAction(
            type=str(next_action_payload.get("type") or "none"),
            payload=dict(next_action_payload.get("payload") or {}),
        ),
        error=error,
        contract_revision=str(payload.get("contract_revision") or "runtime_contract"),
    )


def _default_start_method() -> str:
    methods = tuple(multiprocessing.get_all_start_methods())
    if sys.platform == "darwin" and "spawn" in methods:
        return "spawn"
    if "spawn" in methods:
        return "spawn"
    if methods:
        return methods[0]
    return multiprocessing.get_start_method()


def _picklable_metadata(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    picklable: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in metadata.items():
        if key in {"progress_callback", "heartbeat_callback"}:
            dropped.append(key)
            continue
        try:
            pickle.dumps(value)
        except Exception:  # noqa: BLE001
            dropped.append(str(key))
            continue
        picklable[str(key)] = value
    return picklable, tuple(dropped)


def _sanitize_log_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    normalized_key = str(key).lower()
    if normalized_key and any(marker in normalized_key for marker in _SENSITIVE_LOG_KEY_MARKERS):
        return "[REDACTED]"
    if depth >= 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        if len(value) <= _MAX_LOG_STRING_LENGTH:
            return value
        return f"{value[:_MAX_LOG_STRING_LENGTH]}...[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_log_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:_MAX_LOG_COLLECTION_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_log_value(item, depth=depth + 1)
            for item in value[:_MAX_LOG_COLLECTION_ITEMS]
        ]
    return f"<{type(value).__name__}>"


def _emit_child_runner_log(event: str, **details: Any) -> None:
    payload = _sanitize_log_value(
        {
            "component": "child_runner",
            "event": str(event or "child_runner_event"),
            "reported_at": time.time(),
            **details,
        }
    )
    try:
        sys.stdout.write(
            json.dumps(payload, allow_nan=False, ensure_ascii=False, sort_keys=True) + "\n"
        )
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        return


def _append_progress_event(
    events: list[ChildRunnerProgressEvent],
    event: ChildRunnerProgressEvent,
) -> None:
    events.append(event)
    if len(events) > _MAX_PROGRESS_EVENTS:
        del events[: len(events) - _MAX_PROGRESS_EVENTS]


def _classify_child_exception(exc: BaseException) -> HandlerError:
    error_type = "internal"
    error_code = "child_process_execution_error"
    retryable = True
    if isinstance(exc, TimeoutError):
        error_type = "timeout"
        error_code = "child_process_timeout"
    elif isinstance(exc, (ConnectionError, OSError)):
        error_type = "transport"
        error_code = "child_process_transport_error"
    return HandlerError(
        error_type=error_type,
        error_code=error_code,
        message=str(exc) or type(exc).__name__,
        retryable=retryable,
        details={
            "exception_class": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        },
    )


def _child_process_main(
    send_conn: Any,
    context: HandlerContext,
    dispatch: DispatchCallable,
) -> None:
    started_at = time.time()
    child_pid = os.getpid()
    progress_events: list[ChildRunnerProgressEvent] = []

    def send_message(payload: dict[str, Any]) -> None:
        send_conn.send(payload)

    send_message(
        {
            "type": "started",
            "child_pid": child_pid,
            "started_at": started_at,
        }
    )

    def progress_proxy(
        progress_stage: str,
        *,
        message: str = "",
        percent: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        event = ChildRunnerProgressEvent(
            progress_stage=str(progress_stage or "").strip() or "in_progress",
            message=str(message or ""),
            percent=percent,
            details=dict(details or {}),
        )
        _append_progress_event(progress_events, event)
        send_message({"type": "progress", "event": event.to_dict()})

    child_context = replace(
        context,
        metadata={
            **dict(context.metadata),
            "progress_callback": progress_proxy,
            "heartbeat_callback": None,
        },
    )
    try:
        worker_result = dispatch(child_context)
        envelope = ChildRunnerEnvelope(
            status="returned",
            execution_mode="child_process",
            timed_out=False,
            started_at=started_at,
            finished_at=time.time(),
            child_pid=child_pid,
            worker_result_payload=worker_result.to_dict(),
            progress_events=tuple(progress_events),
        )
    except BaseException as exc:  # noqa: BLE001
        envelope = ChildRunnerEnvelope(
            status="internal_error",
            execution_mode="child_process",
            timed_out=False,
            started_at=started_at,
            finished_at=time.time(),
            child_pid=child_pid,
            error_payload=_classify_child_exception(exc).to_dict(),
            progress_events=tuple(progress_events),
        )
    send_message({"type": "result", "envelope": envelope.to_dict()})
    send_conn.close()


class ChildRunner:
    def __init__(self, config: ChildRunnerConfig | None = None) -> None:
        self.config = config or ChildRunnerConfig()

    def run(
        self,
        *,
        context: HandlerContext,
        dispatch: DispatchCallable,
        on_progress: ChildProgressCallback | None = None,
        on_stall: ChildStallCallback | None = None,
    ) -> ChildRunnerEnvelope:
        if not self.config.enabled:
            raise ValueError("ChildRunner.run() requires mode='child_process'.")

        started_at = time.time()
        started_monotonic = time.monotonic()
        sanitized_metadata, dropped_keys = _picklable_metadata(context.metadata)
        if dropped_keys:
            sanitized_metadata["child_runner_dropped_metadata_keys"] = list(dropped_keys)
        sanitized_context = replace(context, metadata=sanitized_metadata)
        start_method = str(self.config.start_method or _default_start_method())
        if sys.platform == "darwin" and start_method == "fork":
            raise RuntimeError(
                "macOS does not allow the default fork child runner path; use supervisor_mode=inline "
                "or an explicit non-fork child start method."
            )
        ctx = multiprocessing.get_context(start_method)
        recv_conn, send_conn = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=_child_process_main,
            args=(send_conn, sanitized_context, dispatch),
            name=f"handler-child-{context.handler_code}-{context.job_id}",
        )

        try:
            process.start()
        except Exception as exc:  # noqa: BLE001
            recv_conn.close()
            send_conn.close()
            error = _classify_child_exception(exc)
            return ChildRunnerEnvelope(
                status="internal_error",
                execution_mode="child_process",
                timed_out=False,
                started_at=started_at,
                finished_at=time.time(),
                error_payload=error.to_dict(),
                details={"start_method": start_method},
            )

        send_conn.close()
        timeout_seconds = self.config.timeout_seconds
        idle_timeout_seconds = self.config.idle_timeout_seconds
        child_pid: int | None = None
        progress_events: list[ChildRunnerProgressEvent] = []
        final_envelope: ChildRunnerEnvelope | None = None
        last_activity_at = started_at
        last_activity_monotonic = started_monotonic
        last_progress_stage = ""
        last_progress_state = "unknown"
        last_operation_id = ""
        active_operations: dict[str, dict[str, Any]] = {}
        child_started = False

        try:
            while True:
                if recv_conn.poll(self.config.poll_interval_seconds):
                    message = recv_conn.recv()
                    message_type = str(message.get("type") or "")
                    if message_type == "started":
                        child_pid = message.get("child_pid")
                        child_started = True
                        last_activity_at = time.time()
                        last_activity_monotonic = time.monotonic()
                        continue
                    if message_type == "progress":
                        event = ChildRunnerProgressEvent.from_payload(message.get("event") or {})
                        _append_progress_event(progress_events, event)
                        refreshes_idle_deadline = (
                            event.progress_stage not in _PASSIVE_PROGRESS_STAGES
                        )
                        event_state = str(event.details.get("state") or "unknown")
                        event_operation_id = str(event.details.get("operation_id") or "")
                        if refreshes_idle_deadline:
                            last_progress_stage = event.progress_stage
                            last_progress_state = event_state
                            last_operation_id = event_operation_id
                            if event_operation_id and event_state == "started":
                                active_operations.pop(event_operation_id, None)
                                active_operations[event_operation_id] = {
                                    "operation": event.progress_stage,
                                    "operation_id": event_operation_id,
                                    "state": "started",
                                }
                            elif event_operation_id and event_state in {
                                "completed",
                                "failed",
                                "suppressed_error",
                            }:
                                active_operations.pop(event_operation_id, None)
                            last_activity_at = time.time()
                            last_activity_monotonic = time.monotonic()
                        if on_progress is not None:
                            on_progress(event)
                            if refreshes_idle_deadline:
                                last_activity_at = time.time()
                                last_activity_monotonic = time.monotonic()
                    if message_type == "result":
                        last_activity_at = time.time()
                        last_activity_monotonic = time.monotonic()
                        final_envelope = ChildRunnerEnvelope.from_payload(message.get("envelope") or {})
                        break

                current_monotonic = time.monotonic()
                if (
                    timeout_seconds is not None
                    and timeout_seconds > 0
                    and (current_monotonic - started_monotonic) >= timeout_seconds
                ):
                    if on_stall is not None:
                        active_operation = (
                            next(reversed(active_operations.values()))
                            if active_operations
                            else {}
                        )
                        return self._terminate_for_stall(
                            process=process,
                            started_at=started_at,
                            child_pid=child_pid or process.pid,
                            progress_events=progress_events,
                            start_method=start_method,
                            dropped_keys=dropped_keys,
                            idle_seconds=current_monotonic - last_activity_monotonic,
                            last_activity_at=last_activity_at,
                            last_progress_stage=str(
                                active_operation.get("operation") or last_progress_stage
                            ),
                            on_progress=on_progress,
                            on_stall=on_stall,
                            last_progress_state=str(
                                active_operation.get("state") or last_progress_state
                            ),
                            last_operation_id=str(
                                active_operation.get("operation_id") or last_operation_id
                            ),
                            stall_trigger="handler_wall_timeout",
                            timed_out=True,
                        )
                    return self._terminate_for_timeout(
                        process=process,
                        started_at=started_at,
                        child_pid=child_pid or process.pid,
                        progress_events=tuple(progress_events),
                        start_method=start_method,
                        dropped_keys=dropped_keys,
                    )
                if (
                    idle_timeout_seconds is not None
                    and idle_timeout_seconds > 0
                    and child_started
                    and (current_monotonic - last_activity_monotonic) >= idle_timeout_seconds
                ):
                    active_operation = (
                        next(reversed(active_operations.values()))
                        if active_operations
                        else {}
                    )
                    return self._terminate_for_stall(
                        process=process,
                        started_at=started_at,
                        child_pid=child_pid or process.pid,
                        progress_events=progress_events,
                        start_method=start_method,
                        dropped_keys=dropped_keys,
                        idle_seconds=current_monotonic - last_activity_monotonic,
                        last_activity_at=last_activity_at,
                        last_progress_stage=str(
                            active_operation.get("operation") or last_progress_stage
                        ),
                        on_progress=on_progress,
                        on_stall=on_stall,
                        last_progress_state=str(
                            active_operation.get("state") or last_progress_state
                        ),
                        last_operation_id=str(
                            active_operation.get("operation_id") or last_operation_id
                        ),
                    )

                if not process.is_alive():
                    if recv_conn.poll(0.0):
                        continue
                    break
        except BaseException:  # noqa: BLE001
            self._terminate_process(process=process, reason="parent_runner_exception")
            raise
        finally:
            recv_conn.close()

        process.join(timeout=max(self.config.terminate_grace_seconds, 0.05))
        if final_envelope is not None:
            termination = self._terminate_process(
                process=process,
                reason="result_received_exit_confirmation",
            )
            if not termination["confirmed_exited"]:
                return self._termination_failed_envelope(
                    process=process,
                    started_at=started_at,
                    child_pid=final_envelope.child_pid or child_pid or process.pid,
                    progress_events=tuple(progress_events) or final_envelope.progress_events,
                    start_method=start_method,
                    dropped_keys=dropped_keys,
                    trigger="result_received_exit_confirmation",
                    timed_out=False,
                    termination=termination,
                )
            return replace(
                final_envelope,
                child_pid=final_envelope.child_pid or child_pid or process.pid,
                exitcode=termination.get("exitcode"),
                progress_events=tuple(progress_events) or final_envelope.progress_events,
                details={
                    **dict(final_envelope.details),
                    "start_method": start_method,
                    "dropped_metadata_keys": list(dropped_keys),
                    "termination": termination,
                },
            )

        exitcode = process.exitcode
        error = HandlerError(
            error_type="internal",
            error_code="child_process_result_missing",
            message="Child process exited without returning a structured result envelope.",
            retryable=True,
            details={"exitcode": exitcode},
        )
        return ChildRunnerEnvelope(
            status="internal_error",
            execution_mode="child_process",
            timed_out=False,
            started_at=started_at,
            finished_at=time.time(),
            child_pid=child_pid or process.pid,
            exitcode=exitcode,
            error_payload=error.to_dict(),
            progress_events=tuple(progress_events),
            details={
                "start_method": start_method,
                "dropped_metadata_keys": list(dropped_keys),
            },
        )

    def _terminate_for_timeout(
        self,
        *,
        process: multiprocessing.Process,
        started_at: float,
        child_pid: int | None,
        progress_events: tuple[ChildRunnerProgressEvent, ...],
        start_method: str,
        dropped_keys: tuple[str, ...] = (),
    ) -> ChildRunnerEnvelope:
        termination = self._terminate_process(process=process, reason="wall_timeout")
        if not termination["confirmed_exited"]:
            return self._termination_failed_envelope(
                process=process,
                started_at=started_at,
                child_pid=child_pid,
                progress_events=progress_events,
                start_method=start_method,
                dropped_keys=dropped_keys,
                trigger="wall_timeout",
                timed_out=True,
                termination=termination,
            )

        finished_at = time.time()
        error = HandlerError(
            error_type="timeout",
            error_code="child_process_timeout",
            message="Handler execution exceeded the wall-clock timeout in child process mode.",
            retryable=True,
            details={
                "timeout_seconds": self.config.timeout_seconds,
                "child_pid": child_pid or process.pid,
                "termination": termination,
            },
        )
        return ChildRunnerEnvelope(
            status="timed_out",
            execution_mode="child_process",
            timed_out=True,
            started_at=started_at,
            finished_at=finished_at,
            child_pid=child_pid or process.pid,
            exitcode=process.exitcode,
            error_payload=error.to_dict(),
            progress_events=progress_events,
            details={
                "start_method": start_method,
                "dropped_metadata_keys": list(dropped_keys),
                "termination": termination,
            },
        )

    def _terminate_for_stall(
        self,
        *,
        process: multiprocessing.Process,
        started_at: float,
        child_pid: int | None,
        progress_events: list[ChildRunnerProgressEvent],
        start_method: str,
        dropped_keys: tuple[str, ...],
        idle_seconds: float,
        last_activity_at: float,
        last_progress_stage: str,
        on_progress: ChildProgressCallback | None,
        on_stall: ChildStallCallback | None,
        last_progress_state: str = "unknown",
        last_operation_id: str = "",
        stall_trigger: str = "idle_stall",
        timed_out: bool = False,
    ) -> ChildRunnerEnvelope:
        stall_detected_at = time.time()
        stall_details = {
            "child_pid": child_pid or process.pid,
            "idle_timeout_seconds": self.config.idle_timeout_seconds,
            "idle_seconds": max(float(idle_seconds), 0.0),
            "last_activity_at": last_activity_at,
            "last_progress_stage": last_progress_stage,
            "last_progress_state": last_progress_state,
            "last_operation_id": last_operation_id,
            "stall_trigger": stall_trigger,
            "stall_detected_at": stall_detected_at,
        }
        stall_event = ChildRunnerProgressEvent(
            progress_stage="child_stall_detected",
            message="Child process stopped reporting activity.",
            details=dict(stall_details),
        )
        _append_progress_event(progress_events, stall_event)
        progress_delivery: dict[str, Any] = {"status": "not_configured"}
        if on_progress is not None:
            try:
                on_progress(stall_event)
                progress_delivery = {"status": "delivered"}
            except Exception as exc:  # noqa: BLE001
                progress_delivery = {
                    "status": "error",
                    "error_class": type(exc).__name__,
                }

        _emit_child_runner_log(
            "child_stall_detected",
            **stall_details,
            progress_delivery=progress_delivery,
        )
        pre_kill = self._invoke_stall_hook(
            on_stall=on_stall,
            stage="pre_kill",
            details=stall_details,
        )
        termination = self._terminate_process(process=process, reason=stall_trigger)
        if not termination["confirmed_exited"]:
            post_kill = {
                "stage": "post_kill",
                "status": "skipped",
                "reason": "child_exit_unconfirmed",
            }
            return self._termination_failed_envelope(
                process=process,
                started_at=started_at,
                child_pid=child_pid,
                progress_events=tuple(progress_events),
                start_method=start_method,
                dropped_keys=dropped_keys,
                trigger=stall_trigger,
                timed_out=timed_out,
                termination=termination,
                extra_details={
                    "stall": stall_details,
                    "progress_delivery": progress_delivery,
                    "stall_hooks": {
                        "pre_kill": pre_kill,
                        "post_kill": post_kill,
                    },
                },
            )

        post_kill = self._invoke_stall_hook(
            on_stall=on_stall,
            stage="post_kill",
            details={**stall_details, "termination": termination},
        )
        error = HandlerError(
            error_type="timeout",
            error_code="child_process_stalled",
            message="Child process stopped reporting activity before handler completion.",
            retryable=True,
            details={
                "idle_timeout_seconds": self.config.idle_timeout_seconds,
                "idle_seconds": stall_details["idle_seconds"],
                "stall_trigger": stall_trigger,
                "child_pid": child_pid or process.pid,
                "termination": termination,
            },
        )
        details = {
            "start_method": start_method,
            "dropped_metadata_keys": list(dropped_keys),
            "stall": stall_details,
            "progress_delivery": progress_delivery,
            "termination": termination,
            "stall_hooks": {
                "pre_kill": pre_kill,
                "post_kill": post_kill,
            },
        }
        _emit_child_runner_log(
            "child_stall_finished",
            child_pid=child_pid or process.pid,
            error_code="child_process_stalled",
            termination=termination,
            stall_hooks=details["stall_hooks"],
        )
        return ChildRunnerEnvelope(
            status="stalled",
            execution_mode="child_process",
            timed_out=timed_out,
            started_at=started_at,
            finished_at=time.time(),
            child_pid=child_pid or process.pid,
            exitcode=process.exitcode,
            error_payload=error.to_dict(),
            progress_events=tuple(progress_events),
            details=details,
        )

    def _invoke_stall_hook(
        self,
        *,
        on_stall: ChildStallCallback | None,
        stage: ChildStallStage,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        started_at = time.time()
        started_monotonic = time.monotonic()
        evidence: dict[str, Any] = {
            "stage": stage,
            "status": "not_configured",
        }
        if on_stall is not None:
            try:
                result = on_stall(stage, dict(details))
                if result is None:
                    evidence["status"] = "completed"
                    evidence["result"] = {}
                elif isinstance(result, Mapping):
                    evidence["status"] = "completed"
                    evidence["result"] = _sanitize_log_value(dict(result))
                else:
                    evidence["status"] = "invalid_result"
                    evidence["result_type"] = type(result).__name__
            except Exception as exc:  # noqa: BLE001
                evidence["status"] = "error"
                evidence["error_class"] = type(exc).__name__
        evidence["started_at"] = started_at
        evidence["finished_at"] = time.time()
        evidence["duration_seconds"] = max(time.monotonic() - started_monotonic, 0.0)
        _emit_child_runner_log("child_stall_hook_finished", **evidence)
        return evidence

    def _terminate_process(
        self,
        *,
        process: multiprocessing.Process,
        reason: str,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "reason": reason,
            "child_pid": process.pid,
            "terminate_sent": False,
            "kill_sent": False,
        }

        def is_alive(stage: str) -> bool:
            try:
                return bool(process.is_alive())
            except Exception as exc:  # noqa: BLE001
                evidence[f"{stage}_is_alive_error_class"] = type(exc).__name__
                return True

        def join(stage: str) -> None:
            try:
                process.join(timeout=max(self.config.terminate_grace_seconds, 0.05))
            except Exception as exc:  # noqa: BLE001
                evidence[f"{stage}_join_error_class"] = type(exc).__name__

        evidence["initially_alive"] = is_alive("initial")
        if evidence["initially_alive"]:
            try:
                process.terminate()
                evidence["terminate_sent"] = True
            except Exception as exc:  # noqa: BLE001
                evidence["terminate_error_class"] = type(exc).__name__
            join("terminate")

        if is_alive("post_terminate"):
            kill = getattr(process, "kill", None)
            if callable(kill):
                try:
                    kill()
                    evidence["kill_sent"] = True
                except Exception as exc:  # noqa: BLE001
                    evidence["kill_error_class"] = type(exc).__name__
                join("kill")
            else:
                evidence["kill_unavailable"] = True

        join("final")
        evidence["confirmed_exited"] = not is_alive("final")
        try:
            evidence["exitcode"] = process.exitcode
        except Exception as exc:  # noqa: BLE001
            evidence["exitcode_error_class"] = type(exc).__name__
            evidence["exitcode"] = None
        _emit_child_runner_log("child_process_termination", **evidence)
        return evidence

    def _termination_failed_envelope(
        self,
        *,
        process: multiprocessing.Process,
        started_at: float,
        child_pid: int | None,
        progress_events: tuple[ChildRunnerProgressEvent, ...],
        start_method: str,
        dropped_keys: tuple[str, ...],
        trigger: str,
        timed_out: bool,
        termination: Mapping[str, Any],
        extra_details: Mapping[str, Any] | None = None,
    ) -> ChildRunnerEnvelope:
        error = HandlerError(
            error_type="internal",
            error_code="child_termination_failed",
            message="Child process exit could not be confirmed after terminate and kill attempts.",
            retryable=True,
            details={
                "child_pid": child_pid or process.pid,
                "trigger": trigger,
                "termination": dict(termination),
            },
        )
        details = {
            "start_method": start_method,
            "dropped_metadata_keys": list(dropped_keys),
            "trigger": trigger,
            "termination": dict(termination),
            **dict(extra_details or {}),
        }
        _emit_child_runner_log(
            "child_termination_failed",
            child_pid=child_pid or process.pid,
            trigger=trigger,
            termination=termination,
        )
        return ChildRunnerEnvelope(
            status="termination_failed",
            execution_mode="child_process",
            timed_out=timed_out,
            started_at=started_at,
            finished_at=time.time(),
            child_pid=child_pid or process.pid,
            exitcode=termination.get("exitcode"),
            error_payload=error.to_dict(),
            progress_events=progress_events,
            details=details,
        )
