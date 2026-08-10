from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from automation_framework.browser import (
    BlockedHandlingConfig,
    BlockerRulesConfig,
    BrowserSessionRequest,
    build_browser_provider,
    build_target_key,
    resolve_browser_target,
)


@dataclass(slots=True)
class BrowserPageSession:
    provider_name: str
    target_key: str
    profile_ref: str
    session_ref: str
    humanize: bool
    page: Any
    raw_page: Any


BrowserProgressCallback = Callable[..., Any]
BrowserRecoveryCallback = Callable[[], bool]

_PROBE_HARD_TIMEOUT_SECONDS = 45.0
_RECOVERY_STOP_TIMEOUT_CAP_SECONDS = 75.0
_RECOVERY_TOTAL_TIMEOUT_SECONDS = 120.0
_BROWSER_PROBE_PHASES = frozenset(
    {
        "connect_cdp",
        "create_new_page",
        "navigate_about_blank",
        "evaluate_document_ready_state",
    }
)
_NON_BROWSER_STALL_OPERATIONS = frozenset(
    {
        "artifact_upload",
        "artifact_verify",
        "browser_provider_build",
        "browser_target_resolve",
        "main_image_download",
        "product_parse",
    }
)


class _ProbeTerminationRequested(Exception):
    """Interrupt a timed-out probe so its page/session cleanup can run."""


@dataclass(slots=True)
class BrowserOperationHandle:
    operation_id: str
    terminal_state: str = "completed"
    error_class: str = ""

    def suppress(self, exc: BaseException) -> None:
        self.terminal_state = "suppressed_error"
        if not self.error_class:
            self.error_class = type(exc).__name__


_CURRENT_BROWSER_OPERATION: ContextVar[BrowserOperationHandle | None] = ContextVar(
    "current_browser_operation",
    default=None,
)


def suppress_browser_operation(
    exc: BaseException,
    *,
    operation_handle: BrowserOperationHandle | None = None,
) -> None:
    handle = operation_handle or _CURRENT_BROWSER_OPERATION.get()
    if handle is not None:
        handle.suppress(exc)


def _emit_browser_health_log(event: str, **details: Any) -> None:
    safe_details = {
        str(key): value
        for key, value in details.items()
        if key
        in {
            "cleanup_status",
            "duration_ms",
            "error_class",
            "failure_reason",
            "failed_phase",
            "healthy",
            "restart_count",
            "status",
            "timed_out",
        }
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    try:
        print(
            json.dumps(
                {
                    "component": "browser_profile_health",
                    "event": str(event or "browser_health_event"),
                    "reported_at": time.time(),
                    **safe_details,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
    except Exception:
        return


def _report_browser_progress(
    callback: BrowserProgressCallback | None,
    operation: str,
    *,
    state: str,
    started_at: float,
    operation_id: str,
    error_class: str = "",
) -> None:
    if not callable(callback):
        return
    details: dict[str, Any] = {
        "state": state,
        "elapsed_ms": round(max(time.perf_counter() - started_at, 0.0) * 1_000.0, 3),
        "operation_id": operation_id,
    }
    if error_class:
        details["error_class"] = error_class
    try:
        callback(operation, details=details)
    except Exception:
        # Browser progress is diagnostic only and must not change business execution.
        return


def report_browser_operation(
    callback: BrowserProgressCallback | None,
    operation: str,
    *,
    state: str,
    started_at: float,
    operation_id: str = "",
    error_class: str = "",
    **details: Any,
) -> None:
    if not callable(callback):
        return
    safe_details: dict[str, Any] = {
        "state": state,
        "elapsed_ms": round(max(time.monotonic() - started_at, 0.0) * 1_000.0, 3),
        "operation_id": operation_id or str(time.monotonic_ns()),
    }
    if error_class:
        safe_details["error_class"] = error_class
    safe_details.update(
        {
            str(key): value
            for key, value in details.items()
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 160
        }
    )
    try:
        callback(operation, details=safe_details)
    except Exception:
        return


@contextmanager
def browser_operation(
    callback: BrowserProgressCallback | None,
    operation: str,
    *,
    error_state: str = "failed",
    **details: Any,
) -> Iterator[BrowserOperationHandle]:
    started_at = time.monotonic()
    handle = BrowserOperationHandle(operation_id=str(time.monotonic_ns()))
    report_browser_operation(
        callback,
        operation,
        state="started",
        started_at=started_at,
        operation_id=handle.operation_id,
        **details,
    )
    current_token = _CURRENT_BROWSER_OPERATION.set(handle)
    try:
        try:
            yield handle
        except Exception as exc:
            report_browser_operation(
                callback,
                operation,
                state=error_state,
                started_at=started_at,
                operation_id=handle.operation_id,
                error_class=handle.error_class or type(exc).__name__,
                **details,
            )
            raise
        report_browser_operation(
            callback,
            operation,
            state=handle.terminal_state,
            started_at=started_at,
            operation_id=handle.operation_id,
            error_class=handle.error_class,
            **details,
        )
    finally:
        _CURRENT_BROWSER_OPERATION.reset(current_token)


def resolve_automation_browser_target_digest(*, profile_ref: str) -> str:
    target = resolve_browser_target(profile_ref=profile_ref)
    return hashlib.sha256(build_target_key(target).encode("utf-8")).hexdigest()


def _notify_probe_phase(callback: Callable[[str], Any] | None, phase: str) -> None:
    if not callable(callback):
        return
    try:
        callback(phase)
    except Exception:
        return


def _probe_exception_status(*, phase: str, exc: BaseException) -> str:
    if phase not in _BROWSER_PROBE_PHASES:
        return "inconclusive"
    error_class = type(exc).__name__.lower()
    error_text = str(exc).lower()
    if error_class in {
        "chromecdpsessionconnecterror",
        "chromecdpstartuperror",
        "chromecdpunavailableerror",
    }:
        return "unhealthy"
    if "timeout" in error_class or isinstance(exc, TimeoutError):
        return "unhealthy"
    browser_failure_signals = (
        "browser closed",
        "browser has been closed",
        "connection closed",
        "disconnected",
        "target closed",
        "target page, context or browser has been closed",
        "websocket",
    )
    if any(signal in error_text for signal in browser_failure_signals):
        return "unhealthy"
    return "inconclusive"


def _probe_browser_health_in_process(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    connect_timeout_seconds: float = 30.0,
    new_page_timeout_seconds: float = 5.0,
    evaluate_timeout_seconds: float = 5.0,
    allow_auto_start: bool = False,
    _phase_callback: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    failed_phase = "resolve_target"
    error_class = ""
    failure_reason = ""
    status = "inconclusive"
    timed_out = False
    cleanup_status = "not_started"
    target_digest = ""
    session: Any | None = None
    raw_page: Any | None = None

    try:
        _notify_probe_phase(_phase_callback, failed_phase)
        target = resolve_browser_target(
            profile_ref=profile_ref,
            workspace_id=workspace_id,
            profile_id=profile_id,
            provider_name=provider_name,
        )
        target_digest = hashlib.sha256(build_target_key(target).encode("utf-8")).hexdigest()
        provider = build_browser_provider(target.provider)
        metadata = {
            **dict(target.metadata or {}),
            "profile_ref": target.profile_ref,
        }
        if target.provider == "chrome_cdp":
            metadata["auto_start"] = bool(metadata.get("auto_start")) and bool(
                allow_auto_start
            )
            metadata["connect_timeout_seconds"] = max(float(connect_timeout_seconds), 0.01)
            recovery = metadata.get("session_recovery")
            if isinstance(recovery, Mapping):
                metadata["session_recovery"] = {**dict(recovery), "enabled": False}
        request = BrowserSessionRequest(
            profile_id=target.profile_id,
            workspace_id=target.workspace_id,
            headless=False,
            force_open=False,
            humanize=False,
            blocked_handling=BlockedHandlingConfig(),
            blocker_rules=BlockerRulesConfig(),
            metadata=metadata,
        )

        failed_phase = "connect_cdp"
        _notify_probe_phase(_phase_callback, failed_phase)
        session = provider.open_session(request)
        failed_phase = "create_new_page"
        _notify_probe_phase(_phase_callback, failed_phase)
        if hasattr(session, "get_or_create_automation_page"):
            page = session.get_or_create_automation_page()
        else:
            page = session.get_or_create_page()
        raw_page = getattr(page, "raw_page", page)
        set_default_timeout = getattr(raw_page, "set_default_timeout", None)
        if callable(set_default_timeout):
            set_default_timeout(max(float(evaluate_timeout_seconds), 0.01) * 1_000.0)

        failed_phase = "navigate_about_blank"
        _notify_probe_phase(_phase_callback, failed_phase)
        raw_page.goto(
            "about:blank",
            wait_until="commit",
            timeout=max(float(new_page_timeout_seconds), 0.01) * 1_000.0,
        )
        failed_phase = "evaluate_document_ready_state"
        _notify_probe_phase(_phase_callback, failed_phase)
        ready_state = raw_page.evaluate("document.readyState")
        if str(ready_state or "") not in {"loading", "interactive", "complete"}:
            raise RuntimeError("browser health probe returned an invalid ready state")
        failed_phase = ""
        status = "healthy"
    except _ProbeTerminationRequested:
        error_class = "TimeoutError"
        timed_out = True
        status = "unhealthy" if failed_phase in _BROWSER_PROBE_PHASES else "inconclusive"
    except Exception as exc:
        error_class = type(exc).__name__
        raw_reason = str(getattr(exc, "reason", "") or "").strip()
        if raw_reason and raw_reason.replace("_", "").isalnum() and len(raw_reason) <= 80:
            failure_reason = raw_reason
        elif error_class == "ChromeCdpUnavailableError":
            failure_reason = "endpoint_unavailable"
        timed_out = isinstance(exc, TimeoutError) or "timeout" in error_class.lower()
        status = _probe_exception_status(phase=failed_phase, exc=exc)
    finally:
        cleanup_error_class = ""
        if raw_page is not None:
            cleanup_status = "started"
            try:
                failed_phase = failed_phase or "close_probe_page"
                _notify_probe_phase(_phase_callback, "close_probe_page")
                raw_page.close()
            except Exception as exc:
                cleanup_error_class = type(exc).__name__
                failed_phase = "close_probe_page"
        if session is not None:
            cleanup_status = "started"
            detach = getattr(session, "detach", None)
            if not callable(detach):
                detach = getattr(session, "disconnect", None)
            try:
                if not callable(detach):
                    raise RuntimeError("browser session detach is unavailable")
                _notify_probe_phase(_phase_callback, "session_detach")
                detach()
            except Exception as exc:
                cleanup_error_class = type(exc).__name__
                failed_phase = "session_detach"
        if cleanup_error_class:
            status = "inconclusive"
            cleanup_status = "failed"
            error_class = cleanup_error_class
        elif raw_page is not None or session is not None:
            cleanup_status = "completed"
        else:
            cleanup_status = "not_required"
        if status == "healthy" and cleanup_status != "completed":
            status = "inconclusive"
            failed_phase = failed_phase or "session_detach"
            error_class = error_class or "RuntimeError"
        elif status == "healthy":
            failed_phase = ""

    result = {
        "status": status,
        "healthy": status == "healthy",
        "failed_phase": failed_phase,
        "cleanup_status": cleanup_status,
        "error_class": error_class,
        "failure_reason": failure_reason,
        "timed_out": timed_out,
        "duration_ms": round(max(time.monotonic() - started_at, 0.0) * 1_000.0, 3),
        "target_digest": target_digest,
    }
    _emit_browser_health_log("functional_probe_finished", **result)
    return result


def _probe_callable_process_main(
    send_conn: Any,
    probe_callable: Callable[..., Any],
    probe_kwargs: Mapping[str, Any],
) -> None:
    previous_sigterm_handler: Any | None = None

    def request_cleanup(signum: int, frame: Any) -> None:
        del signum, frame
        raise _ProbeTerminationRequested("probe deadline reached")

    def report_phase(phase: str) -> None:
        send_conn.send({"type": "phase", "phase": str(phase or "")})

    try:
        if hasattr(signal, "SIGTERM"):
            previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, request_cleanup)
        result = probe_callable(**dict(probe_kwargs), _phase_callback=report_phase)
        payload = dict(result) if isinstance(result, Mapping) else result.to_dict()
        send_conn.send({"type": "result", "result": payload})
    except BaseException as exc:  # noqa: BLE001
        try:
            send_conn.send(
                {
                    "type": "error",
                    "error_class": type(exc).__name__,
                }
            )
        except Exception:
            pass
    finally:
        if previous_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, previous_sigterm_handler)
            except Exception:
                pass
        send_conn.close()


def _terminate_probe_process(
    process: multiprocessing.Process,
    *,
    recv_conn: Any | None = None,
) -> tuple[bool, Mapping[str, Any] | None, bool]:
    response: Mapping[str, Any] | None = None
    cleanup_unwind_completed = False
    if process.is_alive():
        process.terminate()
        cleanup_deadline = time.monotonic() + 0.5
        while process.is_alive() and time.monotonic() < cleanup_deadline:
            if recv_conn is not None and recv_conn.poll(0.05):
                try:
                    candidate = recv_conn.recv()
                except EOFError:
                    break
                if candidate.get("type") in {"result", "error"}:
                    response = candidate
            process.join(timeout=0.05)
        cleanup_unwind_completed = not process.is_alive()
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=0.5)
    return not process.is_alive(), response, cleanup_unwind_completed


def probe_browser_health(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    timeout_seconds: float = _PROBE_HARD_TIMEOUT_SECONDS,
    connect_timeout_seconds: float = 30.0,
    new_page_timeout_seconds: float = 5.0,
    evaluate_timeout_seconds: float = 5.0,
    allow_auto_start: bool = False,
    probe_callable: Callable[..., Any] = _probe_browser_health_in_process,
) -> dict[str, Any]:
    probe_kwargs = {
        "profile_ref": profile_ref,
        "workspace_id": workspace_id,
        "profile_id": profile_id,
        "provider_name": provider_name,
        "connect_timeout_seconds": connect_timeout_seconds,
        "new_page_timeout_seconds": new_page_timeout_seconds,
        "evaluate_timeout_seconds": evaluate_timeout_seconds,
        "allow_auto_start": allow_auto_start,
    }
    started_at = time.monotonic()
    overall_deadline = started_at + max(float(timeout_seconds), 0.01)
    start_method = "spawn" if "spawn" in multiprocessing.get_all_start_methods() else None
    ctx = multiprocessing.get_context(start_method)
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_probe_callable_process_main,
        args=(send_conn, probe_callable, probe_kwargs),
        name="browser-functional-health-probe",
    )
    _emit_browser_health_log("probe_process_starting", status="starting")
    try:
        process.start()
    except Exception as exc:
        recv_conn.close()
        send_conn.close()
        result = {
            "status": "inconclusive",
            "healthy": False,
            "failed_phase": "probe_process",
            "cleanup_status": "not_started",
            "error_class": type(exc).__name__,
            "timed_out": False,
            "duration_ms": round(max(time.monotonic() - started_at, 0.0) * 1_000.0, 3),
        }
        _emit_browser_health_log("bounded_probe_finished", **result)
        return result
    send_conn.close()

    message: Mapping[str, Any] | None = None
    phase_deadline = overall_deadline
    current_phase = ""
    deadline_expired = False
    phase_timeouts = {
        "connect_cdp": max(float(connect_timeout_seconds), 0.01),
        "create_new_page": max(float(new_page_timeout_seconds), 0.01),
        "navigate_about_blank": max(float(new_page_timeout_seconds), 0.01),
        "evaluate_document_ready_state": max(float(evaluate_timeout_seconds), 0.01),
    }
    while True:
        effective_deadline = min(overall_deadline, phase_deadline)
        if time.monotonic() >= effective_deadline:
            deadline_expired = True
            break
        remaining = max(effective_deadline - time.monotonic(), 0.0)
        if recv_conn.poll(min(remaining, 0.05)):
            try:
                incoming = recv_conn.recv()
            except EOFError:
                message = None
                break
            if incoming.get("type") == "phase":
                current_phase = str(incoming.get("phase") or "")
                phase_timeout = phase_timeouts.get(current_phase)
                phase_deadline = (
                    min(overall_deadline, time.monotonic() + phase_timeout)
                    if phase_timeout is not None
                    else overall_deadline
                )
                continue
            message = incoming
            break
        if not process.is_alive():
            break
    if message is not None and message.get("type") == "result":
        process.join(timeout=0.5)
        exit_confirmed = not process.is_alive()
        if process.is_alive():
            exit_confirmed, _, _ = _terminate_probe_process(process, recv_conn=recv_conn)
        result = dict(message.get("result") or {})
        result["probe_pid"] = int(process.pid or 0)
        result["probe_exit_confirmed"] = exit_confirmed
        if not exit_confirmed:
            result.update(
                {
                    "status": "inconclusive",
                    "healthy": False,
                    "failed_phase": "probe_process_exit",
                    "cleanup_status": "unconfirmed",
                    "error_class": "ProbeTerminationError",
                }
            )
        result.setdefault(
            "duration_ms",
            round(max(time.monotonic() - started_at, 0.0) * 1_000.0, 3),
        )
        recv_conn.close()
        return result

    timed_out = deadline_expired and process.is_alive()
    terminated, termination_response, cleanup_unwind_completed = _terminate_probe_process(
        process,
        recv_conn=recv_conn,
    )
    recv_conn.close()
    cleanup_status = "unconfirmed"
    if termination_response is not None and termination_response.get("type") == "result":
        cleanup_status = str(
            (termination_response.get("result") or {}).get("cleanup_status") or "unconfirmed"
        )
    cleanup_confirmed = cleanup_unwind_completed and cleanup_status in {
        "completed",
        "not_required",
    }
    error_class = "TimeoutError" if timed_out else str(
        (message or termination_response or {}).get("error_class")
        or "BrowserProbeProcessError"
    )
    result = {
        "status": (
            "unhealthy"
            if terminated
            and cleanup_confirmed
            and timed_out
            and current_phase in _BROWSER_PROBE_PHASES
            else "inconclusive"
        ),
        "healthy": False,
        "failed_phase": current_phase or ("probe_deadline" if timed_out else "probe_process"),
        "cleanup_status": cleanup_status,
        "error_class": "ProbeTerminationError" if not terminated else error_class,
        "timed_out": timed_out,
        "probe_exit_confirmed": terminated,
        "probe_pid": int(process.pid or 0),
        "duration_ms": round(max(time.monotonic() - started_at, 0.0) * 1_000.0, 3),
    }
    _emit_browser_health_log("bounded_probe_finished", **result)
    return result


def _validated_recovery_command(target: Any) -> tuple[list[str], float] | None:
    recovery = (target.metadata or {}).get("session_recovery")
    if not isinstance(recovery, Mapping) or recovery.get("enabled") is not True:
        return None
    if target.provider != "chrome_cdp" or target.profile_ref in {"", "direct"}:
        return None
    command = recovery.get("stop_command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        return None
    executable = Path(command[0])
    if not executable.is_absolute():
        return None
    try:
        file_stat = executable.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid not in {0, os.getuid()}
        or file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(executable, os.X_OK)
    ):
        return None
    try:
        timeout_seconds = max(float(recovery.get("stop_timeout_seconds") or 30.0), 0.01)
    except (TypeError, ValueError):
        timeout_seconds = 30.0
    return list(command), min(timeout_seconds, _RECOVERY_STOP_TIMEOUT_CAP_SECONDS)


def ensure_browser_healthy(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    max_restarts: int = 1,
    initial_probe: Mapping[str, Any] | None = None,
    timeout_seconds: float = _PROBE_HARD_TIMEOUT_SECONDS,
    before_restart: BrowserRecoveryCallback | None = None,
) -> dict[str, Any]:
    target_kwargs = {
        "profile_ref": profile_ref,
        "workspace_id": workspace_id,
        "profile_id": profile_id,
        "provider_name": provider_name,
    }
    probe = dict(initial_probe or probe_browser_health(**target_kwargs, timeout_seconds=timeout_seconds))
    if probe.get("status") == "healthy" or probe.get("healthy") is True:
        return {
            "status": "healthy",
            "healthy": True,
            "recovery_authorized": False,
            "restart_count": 0,
            "initial_probe": probe,
            "probe_after_restart": None,
        }
    if (
        probe.get("status") != "unhealthy"
        or probe.get("probe_exit_confirmed") is False
    ):
        return {
            "status": "inconclusive",
            "healthy": False,
            "recovery_authorized": False,
            "restart_count": 0,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_probe_inconclusive",
        }

    try:
        target = resolve_browser_target(**target_kwargs)
    except Exception as exc:
        return {
            "status": "unhealthy",
            "healthy": False,
            "recovery_authorized": False,
            "restart_count": 0,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_recovery_unavailable",
            "error_class": type(exc).__name__,
        }
    validated = _validated_recovery_command(target)
    recovery_authorized = validated is not None and min(max(int(max_restarts), 0), 1) == 1
    if not recovery_authorized:
        return {
            "status": "unhealthy",
            "healthy": False,
            "recovery_authorized": False,
            "restart_count": 0,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_recovery_unavailable",
        }

    recovery_deadline = time.monotonic() + _RECOVERY_TOTAL_TIMEOUT_SECONDS
    if before_restart is not None:
        try:
            restart_allowed = before_restart() is True
        except Exception:
            restart_allowed = False
        if not restart_allowed:
            return {
                "status": "unavailable",
                "healthy": False,
                "recovery_authorized": True,
                "restart_count": 0,
                "initial_probe": probe,
                "probe_after_restart": None,
                "error_code": "browser_recovery_unavailable",
            }

    command, stop_timeout_seconds = validated
    remaining_seconds = recovery_deadline - time.monotonic()
    if remaining_seconds <= 0:
        result = {
            "status": "unhealthy",
            "healthy": False,
            "recovery_authorized": True,
            "restart_count": 0,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_recovery_failed",
            "error_class": "TimeoutError",
        }
        _emit_browser_health_log("browser_recovery_finished", **result)
        return result

    restart_count = 1
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=False,
            timeout=min(stop_timeout_seconds, remaining_seconds),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            raise RuntimeError("browser recovery stop command failed")
    except Exception as exc:
        result = {
            "status": "unhealthy",
            "healthy": False,
            "recovery_authorized": True,
            "restart_count": restart_count,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_recovery_failed",
            "error_class": type(exc).__name__,
        }
        _emit_browser_health_log("browser_recovery_finished", **result)
        return result

    remaining_seconds = recovery_deadline - time.monotonic()
    if remaining_seconds <= 0:
        result = {
            "status": "unhealthy",
            "healthy": False,
            "recovery_authorized": True,
            "restart_count": restart_count,
            "initial_probe": probe,
            "probe_after_restart": None,
            "error_code": "browser_recovery_failed",
            "error_class": "TimeoutError",
        }
        _emit_browser_health_log("browser_recovery_finished", **result)
        return result

    probe_after_restart = probe_browser_health(
        **target_kwargs,
        timeout_seconds=min(max(float(timeout_seconds), 0.01), remaining_seconds),
        allow_auto_start=True,
    )
    recovery_deadline_expired = time.monotonic() >= recovery_deadline
    healthy = not recovery_deadline_expired and (
        probe_after_restart.get("status") == "healthy"
        or probe_after_restart.get("healthy") is True
    )
    result = {
        "status": "healthy" if healthy else "unhealthy",
        "healthy": healthy,
        "recovery_authorized": True,
        "restart_count": restart_count,
        "initial_probe": probe,
        "probe_after_restart": probe_after_restart,
    }
    if not healthy:
        result["error_code"] = "browser_recovery_failed"
    if recovery_deadline_expired:
        result["error_class"] = "TimeoutError"
    _emit_browser_health_log("browser_recovery_finished", **result)
    return result


def _probe_is_healthy(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    return value.get("healthy") is True or value.get("status") == "healthy"


def classify_browser_stall(
    *,
    last_operation: str,
    last_operation_state: str = "started",
    probe_before_kill: Mapping[str, Any] | None,
    probe_after_kill: Mapping[str, Any] | None,
    probe_after_restart: Mapping[str, Any] | None,
    child_exit_confirmed: bool,
    restart_count: int,
    recovery_status: str = "",
    external_host_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    external = dict(external_host_evidence or {})
    external_status = str(external.get("status") or "")
    external_source = str(external.get("source") or "")
    external_is_valid = (
        external_source == "gcp_instance_status_api"
        and external_status in {"gcp_instance_stopped", "gcp_instance_terminated"}
    ) or (
        external_source in {"independent_host_heartbeat", "independent_network_probe"}
        and external_status == "vm_unreachable_from_independent_observer"
    )
    before_status = str((probe_before_kill or {}).get("status") or "")
    after_status = str((probe_after_kill or {}).get("status") or "")
    restart_status = str((probe_after_restart or {}).get("status") or "")
    if external_is_valid:
        failure_scope = "gcp_instance_unreachable"
        diagnosis_code = "gcp_vm_unreachable"
        confidence = "high"
    elif not child_exit_confirmed:
        failure_scope = "unknown"
        diagnosis_code = "child_exit_unconfirmed"
        confidence = "low"
    elif _probe_is_healthy(probe_before_kill):
        if str(last_operation_state or "unknown") != "started":
            failure_scope = "unknown"
            diagnosis_code = "no_active_operation_at_stall"
        elif last_operation in _NON_BROWSER_STALL_OPERATIONS:
            failure_scope = "non_browser_handler"
            diagnosis_code = "non_browser_operation_stall"
        elif last_operation in {"page_navigation", "page_ready_wait"}:
            failure_scope = "page_or_site"
            diagnosis_code = "current_page_or_site_stall"
        else:
            failure_scope = "target_or_session"
            diagnosis_code = "current_target_or_collection_operation_stall"
        confidence = "low" if diagnosis_code == "no_active_operation_at_stall" else "medium"
    elif before_status in {"inconclusive", "unavailable", ""}:
        failure_scope = "unknown"
        diagnosis_code = "browser_probe_inconclusive"
        confidence = "low"
    elif before_status == "unhealthy" and _probe_is_healthy(probe_after_kill):
        failure_scope = "target_or_session"
        diagnosis_code = "child_session_or_transient_cdp_contention"
        confidence = "medium"
    elif after_status in {"inconclusive", "unavailable", ""}:
        failure_scope = "unknown"
        diagnosis_code = "browser_probe_inconclusive"
        confidence = "low"
    elif (
        before_status == "unhealthy"
        and after_status == "unhealthy"
        and _probe_is_healthy(probe_after_restart)
    ):
        failure_scope = "browser_instance"
        diagnosis_code = "shared_chrome_cdp_recovered_after_restart"
        confidence = "high"
    elif restart_status in {"inconclusive", "unavailable"}:
        failure_scope = "unknown"
        diagnosis_code = "browser_probe_inconclusive"
        confidence = "low"
    elif (
        before_status == "unhealthy"
        and after_status == "unhealthy"
        and restart_status == "unhealthy"
        and int(restart_count or 0) > 0
    ):
        failure_scope = "host_runtime_suspected"
        diagnosis_code = "browser_and_local_host_runtime_unresolved"
        confidence = "low"
    elif before_status == "unhealthy" and after_status == "unhealthy":
        failure_scope = "browser_instance"
        diagnosis_code = "shared_chrome_cdp_unhealthy_recovery_unavailable"
        confidence = "high"
    else:
        failure_scope = "unknown"
        diagnosis_code = "browser_probe_unavailable"
        confidence = "low"

    final_error_code = "child_process_stalled"
    if (
        diagnosis_code in {
            "browser_and_local_host_runtime_unresolved",
            "shared_chrome_cdp_unhealthy_recovery_unavailable",
        }
        or recovery_status in {"unhealthy", "failed"}
    ):
        final_error_code = "browser_recovery_failed"
    if diagnosis_code == "child_exit_unconfirmed":
        final_error_code = "child_termination_failed"

    return {
        "last_operation": str(last_operation or ""),
        "last_operation_state": str(last_operation_state or "started"),
        "probe_before_kill": dict(probe_before_kill or {}),
        "probe_after_kill": dict(probe_after_kill or {}),
        "probe_after_restart": dict(probe_after_restart or {}),
        "child_exit_confirmed": bool(child_exit_confirmed),
        "restart_count": min(max(int(restart_count or 0), 0), 1),
        "failure_scope": failure_scope,
        "diagnosis_code": diagnosis_code,
        "diagnosis_confidence": confidence,
        "root_cause_confirmed": False,
        "external_host_evidence": external if external else "absent",
        "final_error_code": final_error_code,
    }


@contextmanager
def open_automation_page(
    *,
    profile_ref: str | None = None,
    workspace_id: int | None = None,
    profile_id: str | None = None,
    provider_name: str | None = None,
    headless: bool = False,
    force_open: bool = False,
    blocked_handling: BlockedHandlingConfig | None = None,
    blocker_rules: BlockerRulesConfig | None = None,
    progress_callback: BrowserProgressCallback | None = None,
) -> Iterator[BrowserPageSession]:
    target_started_at = time.perf_counter()
    target_operation_id = str(time.monotonic_ns())
    _report_browser_progress(
        progress_callback,
        "browser_target_resolve",
        state="started",
        started_at=target_started_at,
        operation_id=target_operation_id,
    )
    try:
        target = resolve_browser_target(
            profile_ref=profile_ref,
            workspace_id=workspace_id,
            profile_id=profile_id,
            provider_name=provider_name,
        )
    except Exception as exc:
        _report_browser_progress(
            progress_callback,
            "browser_target_resolve",
            state="failed",
            started_at=target_started_at,
            operation_id=target_operation_id,
            error_class=type(exc).__name__,
        )
        raise
    _report_browser_progress(
        progress_callback,
        "browser_target_resolve",
        state="completed",
        started_at=target_started_at,
        operation_id=target_operation_id,
    )

    provider_started_at = time.perf_counter()
    provider_operation_id = str(time.monotonic_ns())
    _report_browser_progress(
        progress_callback,
        "browser_provider_build",
        state="started",
        started_at=provider_started_at,
        operation_id=provider_operation_id,
    )
    try:
        provider = build_browser_provider(target.provider)
    except Exception as exc:
        _report_browser_progress(
            progress_callback,
            "browser_provider_build",
            state="failed",
            started_at=provider_started_at,
            operation_id=provider_operation_id,
            error_class=type(exc).__name__,
        )
        raise
    _report_browser_progress(
        progress_callback,
        "browser_provider_build",
        state="completed",
        started_at=provider_started_at,
        operation_id=provider_operation_id,
    )
    request_metadata = {
        "profile_ref": target.profile_ref,
        **target.metadata,
    }
    if target.provider == "chrome_cdp" and isinstance(
        request_metadata.get("session_recovery"), Mapping
    ):
        request_metadata["session_recovery"] = {
            **dict(request_metadata["session_recovery"]),
            "enabled": False,
        }
    request = BrowserSessionRequest(
        profile_id=target.profile_id,
        workspace_id=target.workspace_id,
        headless=headless,
        force_open=force_open,
        blocked_handling=blocked_handling or BlockedHandlingConfig(),
        blocker_rules=blocker_rules or BlockerRulesConfig(),
        metadata=request_metadata,
    )
    session_started_at = time.perf_counter()
    session_operation_id = str(time.monotonic_ns())
    _report_browser_progress(
        progress_callback,
        "browser_session_open",
        state="started",
        started_at=session_started_at,
        operation_id=session_operation_id,
    )
    try:
        session = provider.open_session(request)
    except Exception as exc:
        _report_browser_progress(
            progress_callback,
            "browser_session_open",
            state="failed",
            started_at=session_started_at,
            operation_id=session_operation_id,
            error_class=type(exc).__name__,
        )
        raise
    _report_browser_progress(
        progress_callback,
        "browser_session_open",
        state="completed",
        started_at=session_started_at,
        operation_id=session_operation_id,
    )
    try:
        page_started_at = time.perf_counter()
        page_operation_id = str(time.monotonic_ns())
        _report_browser_progress(
            progress_callback,
            "browser_page_create",
            state="started",
            started_at=page_started_at,
            operation_id=page_operation_id,
        )
        if hasattr(session, "get_or_create_automation_page"):
            try:
                page = session.get_or_create_automation_page()
            except Exception as exc:
                _report_browser_progress(
                    progress_callback,
                    "browser_page_create",
                    state="failed",
                    started_at=page_started_at,
                    operation_id=page_operation_id,
                    error_class=type(exc).__name__,
                )
                raise
            raw_page = getattr(page, "raw_page", page)
            _report_browser_progress(
                progress_callback,
                "browser_page_create",
                state="completed",
                started_at=page_started_at,
                operation_id=page_operation_id,
            )
            yield BrowserPageSession(
                provider_name=provider.provider_name,
                target_key=build_target_key(target),
                profile_ref=target.profile_ref,
                session_ref=session.session_ref,
                humanize=bool(getattr(page, "humanize", False)),
                page=page,
                raw_page=raw_page,
            )
        else:
            try:
                page = session.get_or_create_page()
            except Exception as exc:
                _report_browser_progress(
                    progress_callback,
                    "browser_page_create",
                    state="failed",
                    started_at=page_started_at,
                    operation_id=page_operation_id,
                    error_class=type(exc).__name__,
                )
                raise
            raw_page = getattr(page, "raw_page", page)
            _report_browser_progress(
                progress_callback,
                "browser_page_create",
                state="completed",
                started_at=page_started_at,
                operation_id=page_operation_id,
            )
            yield BrowserPageSession(
                provider_name=provider.provider_name,
                target_key=build_target_key(target),
                profile_ref=target.profile_ref,
                session_ref=session.session_ref,
                humanize=bool(getattr(page, "humanize", False)),
                page=page,
                raw_page=raw_page,
            )
    finally:
        close_started_at = time.perf_counter()
        close_operation_id = str(time.monotonic_ns())
        _report_browser_progress(
            progress_callback,
            "browser_session_close",
            state="started",
            started_at=close_started_at,
            operation_id=close_operation_id,
        )
        try:
            session.close()
        except Exception as exc:
            _report_browser_progress(
                progress_callback,
                "browser_session_close",
                state="failed",
                started_at=close_started_at,
                operation_id=close_operation_id,
                error_class=type(exc).__name__,
            )
            raise
        _report_browser_progress(
            progress_callback,
            "browser_session_close",
            state="completed",
            started_at=close_started_at,
            operation_id=close_operation_id,
        )
