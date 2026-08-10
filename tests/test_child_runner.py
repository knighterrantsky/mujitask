from __future__ import annotations

import json
import multiprocessing
import sys
import time
from typing import Any, Mapping

import pytest

from automation_business_scaffold.control_plane.supervisor import child_runner as child_runner_module
from automation_business_scaffold.control_plane.supervisor.child_runner import (
    ChildRunner,
    ChildRunnerConfig,
    ChildRunnerEnvelope,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult


def _build_context() -> HandlerContext:
    return HandlerContext(
        request_id="req-child-1",
        job_id="job-child-1",
        handler_code="tiktok_product_request_fetch",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/123"},
        workflow_code="tiktok_fastmoss_product_ingest",
        stage_code="collect_product_data",
        job_code="tiktok_product_request_fetch",
        worker_id="pytest-child-runner",
    )


def _success_dispatch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("request_started", message="request path started")
        progress_callback("request_completed", details={"product_id": "123"})
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"product_id": "123", "source": "tiktok"},
    )


def _hanging_dispatch(context: HandlerContext) -> HandlerResult:
    time.sleep(0.3)
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"product_id": "123"},
    )


def _nested_operation_hanging_dispatch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(
            "page_ready_wait",
            details={"state": "started", "operation_id": "outer-page"},
        )
        progress_callback(
            "network_response_capture",
            details={"state": "started", "operation_id": "nested-response"},
        )
        progress_callback(
            "network_response_capture",
            details={"state": "completed", "operation_id": "nested-response"},
        )
    time.sleep(0.3)
    return HandlerResult.success(context)


def _passive_response_progress_hanging_dispatch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(
            "page_ready_wait",
            details={"state": "started", "operation_id": "outer-page"},
        )
        deadline = time.monotonic() + 0.3
        sequence = 0
        while time.monotonic() < deadline:
            sequence += 1
            operation_id = f"response-{sequence}"
            progress_callback(
                "network_response_capture",
                details={"state": "started", "operation_id": operation_id},
            )
            progress_callback(
                "network_response_capture",
                details={"state": "suppressed_error", "operation_id": operation_id},
            )
    return HandlerResult.success(context)


def _portable_child_start_method() -> str:
    methods = multiprocessing.get_all_start_methods()
    if sys.platform != "darwin" and "fork" in methods:
        return "fork"
    return "spawn"


def test_child_runner_returns_handler_result_envelope() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS forbids fork child runner.")
    progress_events: list[str] = []
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=1.0,
            start_method="fork",
            poll_interval_seconds=0.01,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_success_dispatch,
        on_progress=lambda event: progress_events.append(event.progress_stage),
    )

    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "returned"
    assert envelope.execution_mode == "child_process"
    assert envelope.timed_out is False
    assert result.status == "success"
    assert result.result["product_id"] == "123"
    assert progress_events == ["request_started", "request_completed"]
    assert envelope.to_dict()["details"]["start_method"]


def test_child_runner_returns_structured_timeout_error() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS forbids fork child runner.")
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=0.05,
            start_method="fork",
            poll_interval_seconds=0.01,
            terminate_grace_seconds=0.05,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_hanging_dispatch,
    )
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "timed_out"
    assert envelope.timed_out is True
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "timeout"
    assert result.error.error_code == "child_process_timeout"
    assert result.summary["child_runner_status"] == "timed_out"
    assert envelope.details["termination"]["confirmed_exited"] is True


def test_child_runner_detects_idle_stall_and_records_probe_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stall_stages: list[str] = []
    progress_stages: list[str] = []

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        stall_stages.append(stage)
        assert details["idle_seconds"] >= 0.04
        return {
            "probe_status": "healthy" if stage == "pre_kill" else "unhealthy",
            "access_token": "must-not-be-logged",
        }

    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=2.0,
            idle_timeout_seconds=0.05,
            start_method=_portable_child_start_method(),
            poll_interval_seconds=0.005,
            terminate_grace_seconds=0.2,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_hanging_dispatch,
        on_progress=lambda event: progress_stages.append(event.progress_stage),
        on_stall=on_stall,
    )
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "stalled"
    assert envelope.timed_out is False
    assert result.error is not None
    assert result.error.error_code == "child_process_stalled"
    assert stall_stages == ["pre_kill", "post_kill"]
    assert progress_stages == ["child_stall_detected"]
    assert envelope.details["termination"]["confirmed_exited"] is True
    assert envelope.details["stall_hooks"]["pre_kill"]["status"] == "completed"
    assert (
        envelope.details["stall_hooks"]["pre_kill"]["result"]["access_token"]
        == "[REDACTED]"
    )

    log_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    logs = [json.loads(line) for line in log_lines]
    assert any(log["event"] == "child_stall_detected" for log in logs)
    assert any(log["event"] == "child_process_termination" for log in logs)
    assert "must-not-be-logged" not in "\n".join(log_lines)


def test_child_runner_routes_handler_wall_timeout_through_stall_diagnosis() -> None:
    stall_stages: list[str] = []

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        stall_stages.append(stage)
        assert details["stall_trigger"] == "handler_wall_timeout"
        return {}

    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=0.05,
            idle_timeout_seconds=1.0,
            start_method=_portable_child_start_method(),
            poll_interval_seconds=0.005,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_hanging_dispatch,
        on_stall=on_stall,
    )

    assert envelope.status == "stalled"
    assert envelope.timed_out is True
    assert envelope.details["stall"]["stall_trigger"] == "handler_wall_timeout"
    assert stall_stages == ["pre_kill", "post_kill"]


def test_child_runner_preserves_the_outer_active_operation_for_stall_diagnosis() -> None:
    observed: list[dict[str, Any]] = []

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        if stage == "pre_kill":
            observed.append(dict(details))
        return {}

    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=2.0,
            idle_timeout_seconds=0.05,
            start_method=_portable_child_start_method(),
            poll_interval_seconds=0.005,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_nested_operation_hanging_dispatch,
        on_stall=on_stall,
    )

    assert envelope.status == "stalled"
    assert observed[0]["last_progress_stage"] == "page_ready_wait"
    assert observed[0]["last_progress_state"] == "started"
    assert observed[0]["last_operation_id"] == "outer-page"


def test_passive_network_response_events_do_not_mask_an_outer_page_stall() -> None:
    observed: list[dict[str, Any]] = []

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        if stage == "pre_kill":
            observed.append(dict(details))
        return {}

    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=2.0,
            idle_timeout_seconds=0.05,
            start_method=_portable_child_start_method(),
            poll_interval_seconds=0.005,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_passive_response_progress_hanging_dispatch,
        on_stall=on_stall,
    )

    assert envelope.status == "stalled"
    assert observed[0]["last_progress_stage"] == "page_ready_wait"
    assert observed[0]["last_operation_id"] == "outer-page"


class _UnkillableProcess:
    pid = 4321
    exitcode = None

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def join(self, timeout: float) -> None:
        del timeout


class _TerminableResultProcess(_UnkillableProcess):
    def __init__(self) -> None:
        super().__init__()
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.alive = False
        self.exitcode = -15


class _ResultConnection:
    def __init__(self, message: Mapping[str, Any] | None = None) -> None:
        self.message = message
        self.received = False

    def poll(self, timeout: float = 0.0) -> bool:
        del timeout
        return self.message is not None and not self.received

    def recv(self) -> Mapping[str, Any]:
        self.received = True
        assert self.message is not None
        return self.message

    def close(self) -> None:
        return None


def _install_result_process_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    process: _UnkillableProcess,
) -> None:
    now = time.time()
    returned = ChildRunnerEnvelope(
        status="returned",
        execution_mode="child_process",
        timed_out=False,
        started_at=now,
        finished_at=now,
        child_pid=process.pid,
        worker_result_payload=_success_dispatch(_build_context()).to_dict(),
    )

    class ResultContext:
        def Pipe(self, *, duplex: bool) -> tuple[_ResultConnection, _ResultConnection]:
            assert duplex is False
            return (
                _ResultConnection({"type": "result", "envelope": returned.to_dict()}),
                _ResultConnection(),
            )

        def Process(self, **kwargs: Any) -> _UnkillableProcess:
            assert kwargs["name"].startswith("handler-child-")
            return process

    monkeypatch.setattr(
        child_runner_module.multiprocessing,
        "get_context",
        lambda start_method: ResultContext(),
    )


def test_child_runner_confirms_result_process_exit_and_preserves_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _TerminableResultProcess()
    _install_result_process_context(monkeypatch, process=process)
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=1.0,
            start_method="spawn",
            terminate_grace_seconds=0.01,
        )
    )

    envelope = runner.run(context=_build_context(), dispatch=_success_dispatch)
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "returned"
    assert result.status == "success"
    assert envelope.exitcode == -15
    assert envelope.details["termination"]["confirmed_exited"] is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_child_runner_result_with_unconfirmed_exit_returns_termination_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _UnkillableProcess()
    _install_result_process_context(monkeypatch, process=process)
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=1.0,
            start_method="spawn",
            terminate_grace_seconds=0.01,
        )
    )

    envelope = runner.run(context=_build_context(), dispatch=_success_dispatch)
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "termination_failed"
    assert result.error is not None
    assert result.error.error_code == "child_termination_failed"
    assert envelope.details["termination"]["confirmed_exited"] is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_child_runner_returns_termination_failure_when_exit_is_not_confirmed() -> None:
    process = _UnkillableProcess()
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=0.05,
            terminate_grace_seconds=0.01,
        )
    )

    envelope = runner._terminate_for_timeout(
        process=process,  # type: ignore[arg-type]
        started_at=time.time(),
        child_pid=process.pid,
        progress_events=(),
        start_method="spawn",
    )
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "termination_failed"
    assert result.error is not None
    assert result.error.error_code == "child_termination_failed"
    assert envelope.details["termination"]["confirmed_exited"] is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_child_runner_skips_post_kill_hook_when_exit_is_not_confirmed() -> None:
    process = _UnkillableProcess()
    stall_stages: list[str] = []

    def on_stall(stage: str, details: Mapping[str, Any]) -> Mapping[str, Any]:
        stall_stages.append(stage)
        return {"detail_count": len(details)}

    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            idle_timeout_seconds=0.05,
            terminate_grace_seconds=0.01,
        )
    )

    envelope = runner._terminate_for_stall(
        process=process,  # type: ignore[arg-type]
        started_at=time.time(),
        child_pid=process.pid,
        progress_events=[],
        start_method="spawn",
        dropped_keys=(),
        idle_seconds=0.05,
        last_activity_at=time.time() - 0.05,
        last_progress_stage="navigation",
        on_progress=None,
        on_stall=on_stall,
    )

    assert envelope.status == "termination_failed"
    assert stall_stages == ["pre_kill"]
    assert envelope.details["stall_hooks"]["post_kill"] == {
        "stage": "post_kill",
        "status": "skipped",
        "reason": "child_exit_unconfirmed",
    }


def test_child_runner_rejects_fork_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "automation_business_scaffold.control_plane.supervisor.child_runner.sys.platform",
        "darwin",
    )
    runner = ChildRunner(ChildRunnerConfig(mode="child_process", start_method="fork"))

    with pytest.raises(RuntimeError, match="macOS"):
        runner.run(context=_build_context(), dispatch=_success_dispatch)
