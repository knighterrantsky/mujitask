from __future__ import annotations

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
DispatchCallable = Callable[[HandlerContext], HandlerResult]
ChildProgressCallback = Callable[["ChildRunnerProgressEvent"], None]


@dataclass(frozen=True, slots=True)
class ChildRunnerConfig:
    mode: ChildRunnerMode = "inline"
    timeout_seconds: float | None = None
    start_method: str | None = None
    poll_interval_seconds: float = 0.02
    terminate_grace_seconds: float = 0.2

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
        progress_events.append(event)
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
    ) -> ChildRunnerEnvelope:
        if not self.config.enabled:
            raise ValueError("ChildRunner.run() requires mode='child_process'.")

        started_at = time.time()
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
        child_pid: int | None = None
        progress_events: list[ChildRunnerProgressEvent] = []
        final_envelope: ChildRunnerEnvelope | None = None

        try:
            while True:
                if recv_conn.poll(self.config.poll_interval_seconds):
                    message = recv_conn.recv()
                    message_type = str(message.get("type") or "")
                    if message_type == "started":
                        child_pid = message.get("child_pid")
                        continue
                    if message_type == "progress":
                        event = ChildRunnerProgressEvent.from_payload(message.get("event") or {})
                        progress_events.append(event)
                        if on_progress is not None:
                            on_progress(event)
                        continue
                    if message_type == "result":
                        final_envelope = ChildRunnerEnvelope.from_payload(message.get("envelope") or {})
                        break

                if timeout_seconds is not None and timeout_seconds > 0 and (time.time() - started_at) >= timeout_seconds:
                    return self._terminate_for_timeout(
                        process=process,
                        started_at=started_at,
                        child_pid=child_pid or process.pid,
                        progress_events=tuple(progress_events),
                        start_method=start_method,
                    )

                if not process.is_alive():
                    if recv_conn.poll(0.0):
                        continue
                    break
        finally:
            recv_conn.close()

        process.join(timeout=max(self.config.terminate_grace_seconds, 0.05))
        exitcode = process.exitcode
        if final_envelope is not None:
            return replace(
                final_envelope,
                child_pid=final_envelope.child_pid or child_pid or process.pid,
                exitcode=exitcode,
                progress_events=tuple(progress_events) or final_envelope.progress_events,
                details={
                    **dict(final_envelope.details),
                    "start_method": start_method,
                    "dropped_metadata_keys": list(dropped_keys),
                },
            )

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
    ) -> ChildRunnerEnvelope:
        if process.is_alive():
            process.terminate()
            process.join(timeout=max(self.config.terminate_grace_seconds, 0.05))
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=max(self.config.terminate_grace_seconds, 0.05))

        finished_at = time.time()
        error = HandlerError(
            error_type="timeout",
            error_code="child_process_timeout",
            message="Handler execution exceeded the wall-clock timeout in child process mode.",
            retryable=True,
            details={
                "timeout_seconds": self.config.timeout_seconds,
                "child_pid": child_pid or process.pid,
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
            details={"start_method": start_method},
        )
