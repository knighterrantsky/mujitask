from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import automation_business_scaffold.infrastructure.browser.browser_bridge as browser_bridge
from automation_business_scaffold.control_plane.executor import looping
from automation_business_scaffold.control_plane.executor import worker_dispatch


ROOT = Path(__file__).resolve().parents[1]


def _api(name: str) -> Callable[..., Any]:
    value = getattr(browser_bridge, name, None)
    assert callable(value), f"browser_bridge must expose {name}(...)"
    return value


def test_caught_browser_error_marks_the_original_operation_suppressed() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def report(operation: str, *, details: Mapping[str, Any]) -> None:
        events.append((operation, dict(details)))

    with browser_bridge.browser_operation(report, "page_ready_wait") as operation_handle:
        try:
            raise TimeoutError("not persisted")
        except TimeoutError as exc:
            operation_handle.suppress(exc)

    assert [details["state"] for _, details in events] == [
        "started",
        "suppressed_error",
    ]
    assert events[0][1]["operation_id"] == events[1][1]["operation_id"]
    assert events[1][1]["error_class"] == "TimeoutError"


def test_browser_recovery_architecture_owners_are_explicit() -> None:
    contract = yaml.safe_load(
        (ROOT / "contracts" / "harness" / "architecture-ownership.yaml").read_text(
            encoding="utf-8"
        )
    )
    owners = contract["owners"]

    assert owners["browser_profile_health"]["paths"] == [
        "src/automation_business_scaffold/infrastructure/browser/browser_bridge.py"
    ]
    assert "worker_dispatch.py" in " ".join(
        owners["browser_execution_recovery"]["paths"]
    )
    assert any(
        "GCP VM" in rule for rule in owners["browser_profile_health"]["forbidden"]
    )

    recovery_contract = yaml.safe_load(
        (ROOT / "contracts" / "runtime" / "browser-execution-recovery.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert recovery_contract["scope"]["covered_handler_codes"] == [
        "amazon_product_browser_fetch",
        "tiktok_product_browser_fetch",
    ]


def test_unconfirmed_child_quarantine_blocks_future_browser_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "browser_runloop.quarantine.json"
    monkeypatch.setattr(worker_dispatch, "_BROWSER_RUNLOOP_QUARANTINE_PATH", marker)
    worker_dispatch._write_browser_runloop_quarantine(
        execution_id="execution-1",
        run_id="run-1",
        resource_code="browser:amazon:target-digest",
        child_pid=4321,
    )
    monkeypatch.setattr(
        worker_dispatch,
        "create_runtime_store",
        lambda settings: pytest.fail("quarantined runloop must not reach the claim boundary"),
    )

    payload = worker_dispatch.execute_browser_once({})

    assert payload["daemon_status"] == "quarantined"
    assert payload["error_code"] == "child_termination_failed"
    quarantine = payload["browser_runloop_quarantine"]
    assert quarantine["event"] == "child_exit_unconfirmed"
    assert quarantine["execution_id"] == "execution-1"
    assert quarantine["child_pid"] == 4321
    assert quarantine["child_pids"] == [4321]
    assert quarantine["created_at"] > 0
    persisted = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert persisted["resource_digest"] != "browser:amazon:target-digest"
    assert "resource_code" not in persisted
    assert marker.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("contents", ["", "{not-json", "[]", '{"child_pid": []}'])
def test_empty_or_corrupt_quarantine_marker_fails_closed_before_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
) -> None:
    marker = tmp_path / "browser_runloop.quarantine.json"
    marker.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(worker_dispatch, "_BROWSER_RUNLOOP_QUARANTINE_PATH", marker)
    monkeypatch.setattr(
        worker_dispatch,
        "create_runtime_store",
        lambda settings: pytest.fail("malformed quarantine must still block browser claim"),
    )

    payload = worker_dispatch.execute_browser_once({})

    assert payload["daemon_status"] == "quarantined"
    assert payload["error_code"] == "child_termination_failed"


def test_unreadable_quarantine_marker_fails_closed_before_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "browser_runloop.quarantine.json"
    marker.write_text("{}", encoding="utf-8")
    original_lstat = Path.lstat

    def denied_lstat(path: Path) -> Any:
        if path == marker:
            raise PermissionError("denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    monkeypatch.setattr(worker_dispatch, "_BROWSER_RUNLOOP_QUARANTINE_PATH", marker)
    monkeypatch.setattr(
        worker_dispatch,
        "create_runtime_store",
        lambda settings: pytest.fail("unreadable quarantine must block browser claim"),
    )

    payload = worker_dispatch.execute_browser_once({})

    assert payload["daemon_status"] == "quarantined"
    assert payload["browser_runloop_quarantine"]["error_class"] == "PermissionError"


def test_dangling_quarantine_marker_symlink_also_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "browser_runloop.quarantine.json"
    marker.symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(worker_dispatch, "_BROWSER_RUNLOOP_QUARANTINE_PATH", marker)
    monkeypatch.setattr(
        worker_dispatch,
        "create_runtime_store",
        lambda settings: pytest.fail("any quarantine directory entry must block claim"),
    )

    assert worker_dispatch.execute_browser_once({})["daemon_status"] == "quarantined"


def test_termination_failure_writes_quarantine_before_terminal_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "browser_runloop.quarantine.json"
    monkeypatch.setattr(worker_dispatch, "_BROWSER_RUNLOOP_QUARANTINE_PATH", marker)
    execution = SimpleNamespace(
        payload={},
        run_id="run-1",
        request_id="request-1",
        execution_id="execution-1",
        item_code="tiktok_product_browser_fetch",
        workflow_code="workflow-1",
        business_key="business-1",
        dedupe_key="dedupe-1",
        resource_code="browser:tiktok:target-digest",
        attempt_count=1,
        max_attempts=1,
        max_execution_seconds=300.0,
    )

    class Store:
        def claim_next_browser_execution(self, **kwargs: Any) -> Any:
            del kwargs
            return execution

        def update_task_execution_progress(self, **kwargs: Any) -> None:
            del kwargs

    settings = SimpleNamespace(
        worker_id="worker-1",
        lease_seconds=30.0,
        heartbeat_interval_seconds=5.0,
        retry_delay_seconds=5.0,
    )
    monkeypatch.setattr(worker_dispatch, "build_runtime_settings", lambda params: settings)
    monkeypatch.setattr(worker_dispatch, "create_runtime_store", lambda current: Store())
    monkeypatch.setattr(worker_dispatch, "run_supervised_handler", lambda **kwargs: object())
    terminated = SimpleNamespace(status="termination_failed", child_pid=4321)
    outcome = SimpleNamespace(
        child_runner=terminated,
        error=None,
        worker_result=SimpleNamespace(
            result={
                "browser_diagnosis": {
                    "probe_before_kill": {
                        "status": "inconclusive",
                        "probe_exit_confirmed": False,
                        "probe_pid": 9876,
                    }
                }
            }
        ),
    )
    monkeypatch.setattr(worker_dispatch, "_attach_browser_stall_diagnosis", lambda raw: outcome)

    def stop_at_persistence(**kwargs: Any) -> Any:
        del kwargs
        assert marker.exists()
        raise RuntimeError("persistence boundary reached")

    monkeypatch.setattr(worker_dispatch, "persist_browser_execution_outcome", stop_at_persistence)

    with pytest.raises(RuntimeError, match="persistence boundary reached"):
        worker_dispatch.execute_browser_once({})

    persisted = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert persisted["execution_id"] == "execution-1"
    assert persisted["child_pid"] == 9876
    assert persisted["child_pids"] == [9876, 4321]


def test_quarantine_write_failure_holds_the_current_execution_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Store:
        def update_task_execution_progress(self, **kwargs: Any) -> None:
            calls.append(("progress", dict(kwargs)))

        def heartbeat_browser_execution(self, **kwargs: Any) -> None:
            calls.append(("heartbeat", dict(kwargs)))

    monkeypatch.setattr(
        worker_dispatch,
        "_write_browser_runloop_quarantine",
        lambda **kwargs: (_ for _ in ()).throw(OSError("marker unavailable")),
    )
    monkeypatch.setattr(
        worker_dispatch.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(RuntimeError(f"held:{seconds}")),
    )

    with pytest.raises(RuntimeError, match="held:0.2"):
        worker_dispatch._hold_browser_runloop_fail_closed(
            store=Store(),
            execution_id="execution-1",
            run_id="run-1",
            resource_code="browser:amazon:target",
            child_pid=4321,
            lease_seconds=30.0,
            heartbeat_interval_seconds=0.1,
            error_class="PermissionError",
        )

    assert [name for name, _ in calls] == ["progress", "heartbeat"]
    assert calls[0][1]["progress_stage"] == "browser_runloop_quarantine_write_failed"


def test_any_quarantine_write_or_fsync_error_enters_fail_closed_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker_dispatch,
        "_write_browser_runloop_quarantine",
        lambda **kwargs: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    def hold(**kwargs: Any) -> None:
        observed.append(dict(kwargs))
        raise RuntimeError("held fail closed")

    monkeypatch.setattr(worker_dispatch, "_hold_browser_runloop_fail_closed", hold)

    with pytest.raises(RuntimeError, match="held fail closed"):
        worker_dispatch._quarantine_unconfirmed_browser_child(
            store=SimpleNamespace(),
            execution_id="execution-1",
            run_id="run-1",
            resource_code="browser:amazon:target",
            child_pid=4321,
            lease_seconds=30.0,
            heartbeat_interval_seconds=5.0,
        )

    assert observed[0]["error_class"] == "OSError"
    assert observed[0]["execution_id"] == "execution-1"


def test_quarantine_write_failure_persists_hold_and_retries_until_marker_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    write_calls: list[dict[str, Any]] = []

    class Store:
        def update_task_execution_progress(self, **kwargs: Any) -> None:
            assert kwargs["progress_stage"] == "browser_runloop_quarantine_write_failed"
            events.append("progress")

        def heartbeat_browser_execution(self, **kwargs: Any) -> None:
            del kwargs
            events.append("heartbeat")

    def write_marker(**kwargs: Any) -> None:
        write_calls.append(dict(kwargs))
        events.append(f"write_{len(write_calls)}")
        if len(write_calls) < 3:
            raise OSError("transient marker failure")

    monkeypatch.setattr(worker_dispatch, "_write_browser_runloop_quarantine", write_marker)
    monkeypatch.setattr(
        worker_dispatch.time,
        "sleep",
        lambda seconds: events.append(f"sleep_{seconds}"),
    )

    worker_dispatch._quarantine_unconfirmed_browser_child(
        store=Store(),
        execution_id="execution-1",
        run_id="run-1",
        resource_code="browser:amazon:target",
        child_pids=[9876, 4321],
        lease_seconds=30.0,
        heartbeat_interval_seconds=0.1,
    )

    assert events == [
        "write_1",
        "progress",
        "heartbeat",
        "write_2",
        "sleep_0.2",
        "progress",
        "heartbeat",
        "write_3",
    ]
    assert len(write_calls) == 3
    assert all(call["child_pids"] == [9876, 4321] for call in write_calls)


def test_quarantined_runloop_sleeps_without_counting_processed_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        stop_when_idle=False,
        max_idle_cycles=1,
        max_iterations=2,
        poll_interval_seconds=0.25,
    )
    sleeps: list[float] = []
    calls = 0

    def quarantined_once(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        del params
        calls += 1
        return {
            "daemon_status": "quarantined",
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
        }

    monkeypatch.setattr(looping, "build_runtime_settings", lambda params: settings)
    monkeypatch.setattr(looping.time, "sleep", lambda seconds: sleeps.append(seconds))

    payload = looping.run_control_loop(
        params={},
        actor="daemon",
        once_func=quarantined_once,
        idle_status_key="daemon_status",
    )

    assert calls == 2
    assert sleeps == [0.25]
    assert payload["processed_count"] == 0


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        assert isinstance(payload, Mapping)
        return dict(payload)
    raise AssertionError(f"browser health API returned unsupported result: {type(value).__name__}")


class _RawProbePage:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        assert url == "about:blank"
        self._events.append("about_blank")

    def evaluate(self, expression: str) -> str:
        assert expression == "document.readyState"
        self._events.append("evaluate")
        return "complete"

    def close(self) -> None:
        self._events.append("page_close")


class _AutomationProbePage:
    humanize = False

    def __init__(self, raw_page: _RawProbePage) -> None:
        self.raw_page = raw_page


class _ProbeSession:
    session_ref = "probe-session"

    def __init__(self, events: list[str], *, detach_error: Exception | None = None) -> None:
        self._events = events
        self._detach_error = detach_error
        self.raw_page = _RawProbePage(events)
        self.close_calls = 0

    def get_or_create_automation_page(self) -> _AutomationProbePage:
        self._events.append("new_page")
        return _AutomationProbePage(self.raw_page)

    def detach(self) -> None:
        self._events.append("detach")
        if self._detach_error is not None:
            raise self._detach_error

    def close(self) -> None:
        self.close_calls += 1
        raise AssertionError("functional probe must never call session.close() on shared CDP")


class _ProbeProvider:
    provider_name = "chrome_cdp"

    def __init__(self, events: list[str], session: _ProbeSession) -> None:
        self._events = events
        self._session = session
        self.requests: list[Any] = []

    def open_session(self, request: Any) -> _ProbeSession:
        self._events.append("open_session")
        self.requests.append(request)
        return self._session


def _install_probe_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detach_error: Exception | None = None,
    session_recovery: Mapping[str, Any] | None = None,
) -> tuple[list[str], _ProbeSession, _ProbeProvider]:
    events: list[str] = []
    session = _ProbeSession(events, detach_error=detach_error)
    provider = _ProbeProvider(events, session)
    target = SimpleNamespace(
        provider="chrome_cdp",
        profile_id="chrome-gcp",
        workspace_id=None,
        profile_ref="chrome-gcp",
        metadata={
            "debug_http": "http://127.0.0.1:9222",
            "auto_start": True,
            "session_recovery": dict(session_recovery or {"enabled": False}),
        },
    )
    monkeypatch.setattr(browser_bridge, "resolve_browser_target", lambda **kwargs: target)
    monkeypatch.setattr(browser_bridge, "build_browser_provider", lambda provider_name: provider)
    monkeypatch.setattr(browser_bridge, "build_target_key", lambda resolved: "chrome-gcp-target")
    return events, session, provider


def _healthy_probe() -> dict[str, Any]:
    return {
        "status": "healthy",
        "failed_phase": "",
        "cleanup_status": "completed",
        "error_class": "",
        "timed_out": False,
    }


def _unhealthy_probe(*, failed_phase: str = "connect_cdp") -> dict[str, Any]:
    return {
        "status": "unhealthy",
        "failed_phase": failed_phase,
        "cleanup_status": "not_started",
        "error_class": "TimeoutError",
        "timed_out": True,
    }


def _slow_probe_callable(**kwargs: Any) -> dict[str, Any]:
    del kwargs
    time.sleep(5.0)
    return _healthy_probe()


def _slow_new_page_probe_callable(
    *,
    _phase_callback: Callable[[str], None],
    **kwargs: Any,
) -> dict[str, Any]:
    del kwargs
    _phase_callback("connect_cdp")
    _phase_callback("create_new_page")
    interrupted = False
    try:
        time.sleep(5.0)
    except BaseException:  # noqa: BLE001 - SIGTERM must unwind the probe cleanup boundary.
        interrupted = True
    finally:
        marker_path = os.environ.get("MUJITASK_TEST_PROBE_CLEANUP_MARKER", "")
        if marker_path:
            Path(marker_path).write_text("cleanup_completed", encoding="utf-8")
    if not interrupted:
        return _healthy_probe()
    return {
        "status": "unhealthy",
        "healthy": False,
        "failed_phase": "create_new_page",
        "cleanup_status": "completed",
        "error_class": "TimeoutError",
        "timed_out": True,
    }


def _slow_connect_probe_callable(
    *,
    _phase_callback: Callable[[str], None],
    **kwargs: Any,
) -> dict[str, Any]:
    del kwargs
    _phase_callback("connect_cdp")
    interrupted = False
    try:
        time.sleep(5.0)
    except BaseException:  # noqa: BLE001 - SIGTERM is the probe deadline mechanism.
        interrupted = True
    if not interrupted:
        return _healthy_probe()
    return {
        "status": "unhealthy",
        "healthy": False,
        "failed_phase": "connect_cdp",
        "cleanup_status": "not_required",
        "error_class": "TimeoutError",
        "timed_out": True,
    }


def _sigterm_ignoring_probe_callable(
    *,
    _phase_callback: Callable[[str], None],
    **kwargs: Any,
) -> dict[str, Any]:
    del kwargs
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _phase_callback("connect_cdp")
    _phase_callback("create_new_page")
    time.sleep(5.0)
    return _healthy_probe()


def test_in_process_probe_uses_non_destructive_shared_cdp_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, session, provider = _install_probe_fakes(monkeypatch)

    result = _api("_probe_browser_health_in_process")(
        profile_ref="chrome-gcp",
        connect_timeout_seconds=30.0,
        new_page_timeout_seconds=5.0,
        evaluate_timeout_seconds=5.0,
    )

    payload = _payload(result)
    assert payload["status"] == "healthy"
    assert payload["cleanup_status"] == "completed"
    assert events == [
        "open_session",
        "new_page",
        "about_blank",
        "evaluate",
        "page_close",
        "detach",
    ]
    assert session.close_calls == 0
    assert len(provider.requests) == 1
    assert provider.requests[0].metadata["profile_ref"] == "chrome-gcp"
    assert provider.requests[0].metadata["auto_start"] is False
    assert provider.requests[0].metadata["session_recovery"]["enabled"] is False


def test_probe_cleanup_failure_is_inconclusive_and_never_closes_shared_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, session, _ = _install_probe_fakes(
        monkeypatch,
        detach_error=RuntimeError("detach unavailable"),
    )

    result = _api("_probe_browser_health_in_process")(
        profile_ref="chrome-gcp",
        connect_timeout_seconds=30.0,
        new_page_timeout_seconds=5.0,
        evaluate_timeout_seconds=5.0,
    )

    payload = _payload(result)
    assert payload["status"] == "inconclusive"
    assert payload["failed_phase"] == "session_detach"
    assert payload["cleanup_status"] == "failed"
    assert payload["error_class"] == "RuntimeError"
    assert events[-2:] == ["page_close", "detach"]
    assert session.close_calls == 0


def test_regular_browser_page_disables_provider_owned_session_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, session, provider = _install_probe_fakes(
        monkeypatch,
        session_recovery={
            "enabled": True,
            "stop_command": ["/usr/bin/true", "chrome-gcp"],
        },
    )
    session.close = lambda: events.append("session_close")  # type: ignore[method-assign]

    with browser_bridge.open_automation_page(profile_ref="chrome-gcp"):
        pass

    assert provider.requests[0].metadata["auto_start"] is True
    assert provider.requests[0].metadata["session_recovery"]["enabled"] is False
    assert events[-1] == "session_close"


def test_bounded_probe_hard_timeout_returns_without_waiting_for_hung_probe() -> None:
    started_at = time.monotonic()

    result = _api("probe_browser_health")(
        profile_ref="chrome-gcp",
        timeout_seconds=0.05,
        probe_callable=_slow_probe_callable,
    )

    elapsed = time.monotonic() - started_at
    payload = _payload(result)
    assert elapsed < 1.5, "hard-deadline wrapper must terminate its hung probe boundary"
    assert payload["status"] == "inconclusive"
    assert payload["failed_phase"] == "probe_deadline"
    assert payload["error_class"] == "TimeoutError"
    assert payload["timed_out"] is True


def test_probe_process_start_time_consumes_the_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    poll_calls: list[float] = []

    class FakeConnection:
        def close(self) -> None:
            return None

        def poll(self, timeout: float) -> bool:
            poll_calls.append(timeout)
            now[0] += timeout
            return False

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.alive = False

        def start(self) -> None:
            now[0] = 2.0
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.alive = False

        def kill(self) -> None:
            self.alive = False

        def join(self, timeout: float | None = None) -> None:
            del timeout

    fake_process = FakeProcess()

    class FakeContext:
        def Pipe(self, *, duplex: bool) -> tuple[FakeConnection, FakeConnection]:
            assert duplex is False
            return FakeConnection(), FakeConnection()

        def Process(self, **kwargs: Any) -> FakeProcess:
            assert kwargs["name"] == "browser-functional-health-probe"
            return fake_process

    monkeypatch.setattr(browser_bridge.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        browser_bridge.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result = _api("probe_browser_health")(
        profile_ref="chrome-gcp",
        timeout_seconds=1.0,
    )

    payload = _payload(result)
    assert payload["status"] == "inconclusive"
    assert payload["failed_phase"] == "probe_deadline"
    assert payload["timed_out"] is True
    assert payload["probe_exit_confirmed"] is True
    assert poll_calls == []


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM cleanup semantics require POSIX")
def test_phase_timeout_sigterm_runs_probe_cleanup_before_unhealthy_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_marker = tmp_path / "probe-cleanup.marker"
    monkeypatch.setenv("MUJITASK_TEST_PROBE_CLEANUP_MARKER", str(cleanup_marker))
    started_at = time.monotonic()

    result = _api("probe_browser_health")(
        profile_ref="chrome-gcp",
        timeout_seconds=1.0,
        connect_timeout_seconds=0.5,
        new_page_timeout_seconds=0.05,
        evaluate_timeout_seconds=0.5,
        probe_callable=_slow_new_page_probe_callable,
    )

    payload = _payload(result)
    assert time.monotonic() - started_at < 1.0
    assert payload["status"] == "unhealthy"
    assert payload["failed_phase"] == "create_new_page"
    assert payload["cleanup_status"] == "completed"
    assert payload["timed_out"] is True
    assert cleanup_marker.read_text(encoding="utf-8") == "cleanup_completed"


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM cleanup semantics require POSIX")
def test_connect_timeout_without_owned_page_or_session_is_unhealthy() -> None:
    result = _api("probe_browser_health")(
        profile_ref="chrome-gcp",
        timeout_seconds=1.0,
        connect_timeout_seconds=0.05,
        new_page_timeout_seconds=0.5,
        evaluate_timeout_seconds=0.5,
        probe_callable=_slow_connect_probe_callable,
    )

    payload = _payload(result)
    assert payload["status"] == "unhealthy"
    assert payload["failed_phase"] == "connect_cdp"
    assert payload["cleanup_status"] == "not_required"
    assert payload["probe_exit_confirmed"] is True


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL fallback semantics require POSIX")
def test_forced_probe_kill_is_inconclusive_and_never_restarts_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _api("probe_browser_health")(
        profile_ref="chrome-gcp",
        timeout_seconds=1.0,
        connect_timeout_seconds=0.5,
        new_page_timeout_seconds=0.05,
        evaluate_timeout_seconds=0.5,
        probe_callable=_sigterm_ignoring_probe_callable,
    )

    payload = _payload(result)
    assert payload["status"] == "inconclusive"
    assert payload["healthy"] is False
    assert payload["failed_phase"] == "create_new_page"
    assert payload["cleanup_status"] == "unconfirmed"
    assert payload["timed_out"] is True
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unconfirmed probe cleanup must not restart browser"),
    )

    recovery = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=payload,
    )

    recovery_payload = _payload(recovery)
    assert recovery_payload["status"] == "inconclusive"
    assert recovery_payload["restart_count"] == 0
    assert recovery_payload["recovery_authorized"] is False


def test_unconfirmed_probe_exit_becomes_terminal_quarantine_evidence() -> None:
    diagnosis = worker_dispatch._apply_unconfirmed_probe_failure(
        {
            "failure_scope": "unknown",
            "diagnosis_code": "browser_probe_inconclusive",
            "diagnosis_confidence": "low",
            "root_cause_confirmed": False,
            "probe_before_kill": {
                "status": "inconclusive",
                "probe_exit_confirmed": False,
                "probe_pid": 9876,
            },
        }
    )

    assert diagnosis["diagnosis_code"] == "browser_probe_exit_unconfirmed"
    assert diagnosis["final_error_code"] == "browser_probe_termination_failed"
    assert worker_dispatch._unconfirmed_probe_pid(diagnosis) == 9876


def test_probe_configuration_failure_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_bridge,
        "resolve_browser_target",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("invalid profile")),
    )

    result = _api("_probe_browser_health_in_process")(profile_ref="missing-profile")

    payload = _payload(result)
    assert payload["status"] == "inconclusive"
    assert payload["failed_phase"] == "resolve_target"


def test_known_chrome_cdp_unavailable_error_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable_error = type("ChromeCdpUnavailableError", (RuntimeError,), {})
    events, _session, provider = _install_probe_fakes(monkeypatch)
    del events
    monkeypatch.setattr(
        provider,
        "open_session",
        lambda request: (_ for _ in ()).throw(unavailable_error("endpoint unavailable")),
    )

    result = _api("_probe_browser_health_in_process")(profile_ref="chrome-gcp")

    payload = _payload(result)
    assert payload["status"] == "unhealthy"
    assert payload["failure_reason"] == "endpoint_unavailable"


def test_ensure_browser_healthy_does_not_recover_without_explicit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe_fakes(
        monkeypatch,
        session_recovery={
            "enabled": False,
            "stop_command": ["/usr/bin/true", "chrome-gcp"],
        },
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("disabled session_recovery must not run stop_command"),
    )
    monkeypatch.setattr(
        browser_bridge,
        "probe_browser_health",
        lambda **kwargs: pytest.fail("disabled recovery must not run a post-restart probe"),
        raising=False,
    )

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=_unhealthy_probe(),
    )

    payload = _payload(result)
    assert payload["status"] == "unhealthy"
    assert payload["recovery_authorized"] is False
    assert payload["restart_count"] == 0


def test_ensure_browser_healthy_never_restarts_from_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            f"unavailable evidence must not restart: {args}, {kwargs}"
        ),
    )

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe={"status": "unavailable", "healthy": False},
    )

    payload = _payload(result)
    assert payload["status"] == "inconclusive"
    assert payload["restart_count"] == 0
    assert payload["recovery_authorized"] is False


def test_ensure_browser_healthy_runs_exact_authorized_stop_command_once_and_reprobes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_command = [str(Path("/usr/bin/true")), "--profile-ref", "chrome-gcp"]
    _install_probe_fakes(
        monkeypatch,
        session_recovery={
            "enabled": True,
            "stop_command": stop_command,
            "stop_timeout_seconds": 10,
        },
    )
    command_calls: list[tuple[list[str], dict[str, Any]]] = []
    probe_calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        command_calls.append((list(command), dict(kwargs)))
        return SimpleNamespace(returncode=0)

    def fake_probe(**kwargs: Any) -> dict[str, Any]:
        probe_calls.append(dict(kwargs))
        return _healthy_probe()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(browser_bridge, "probe_browser_health", fake_probe, raising=False)

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=_unhealthy_probe(),
        before_restart=lambda: command_calls.append(([], {"before_restart": True})) or True,
    )

    payload = _payload(result)
    assert payload["status"] == "healthy"
    assert payload["recovery_authorized"] is True
    assert payload["restart_count"] == 1
    assert len(command_calls) == 2
    assert command_calls[0] == ([], {"before_restart": True})
    assert command_calls[1][0] == stop_command
    assert command_calls[1][1]["shell"] is False
    assert len(probe_calls) == 1
    assert probe_calls[0]["profile_ref"] == "chrome-gcp"
    assert probe_calls[0]["allow_auto_start"] is True


def test_ensure_browser_healthy_never_exceeds_one_restart_when_reprobe_stays_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_command = ["/usr/bin/true", "--profile-ref", "chrome-gcp"]
    _install_probe_fakes(
        monkeypatch,
        session_recovery={"enabled": True, "stop_command": stop_command},
    )
    command_calls: list[list[str]] = []
    probe_calls: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        del kwargs
        command_calls.append(list(command))
        return SimpleNamespace(returncode=0)

    def fake_probe(**kwargs: Any) -> dict[str, Any]:
        probe_calls.append(str(kwargs["profile_ref"]))
        return _unhealthy_probe(failed_phase="evaluate_document_ready_state")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(browser_bridge, "probe_browser_health", fake_probe, raising=False)

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=_unhealthy_probe(),
    )

    payload = _payload(result)
    assert payload["status"] == "unhealthy"
    assert payload["restart_count"] == 1
    assert payload["error_code"] == "browser_recovery_failed"
    assert command_calls == [stop_command]
    assert probe_calls == ["chrome-gcp"]


def test_browser_recovery_stop_and_reprobe_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_command = ["/usr/bin/true", "--profile-ref", "chrome-gcp"]
    _install_probe_fakes(
        monkeypatch,
        session_recovery={
            "enabled": True,
            "stop_command": stop_command,
            "stop_timeout_seconds": 75,
        },
    )
    now = [0.0]
    stop_timeouts: list[float] = []
    probe_timeouts: list[float] = []
    monkeypatch.setattr(browser_bridge.time, "monotonic", lambda: now[0])

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert command == stop_command
        stop_timeouts.append(float(kwargs["timeout"]))
        now[0] = 119.0
        return SimpleNamespace(returncode=0)

    def fake_probe(**kwargs: Any) -> dict[str, Any]:
        probe_timeouts.append(float(kwargs["timeout_seconds"]))
        return _healthy_probe()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(browser_bridge, "probe_browser_health", fake_probe)

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=_unhealthy_probe(),
    )

    assert _payload(result)["status"] == "healthy"
    assert stop_timeouts == [75.0]
    assert probe_timeouts == [1.0]


def test_browser_recovery_rejects_healthy_probe_returned_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_command = ["/usr/bin/true", "--profile-ref", "chrome-gcp"]
    _install_probe_fakes(
        monkeypatch,
        session_recovery={"enabled": True, "stop_command": stop_command},
    )
    now = [0.0]
    monkeypatch.setattr(browser_bridge.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0),
    )

    def fake_probe(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["timeout_seconds"] == 45.0
        now[0] = 121.0
        return _healthy_probe()

    monkeypatch.setattr(browser_bridge, "probe_browser_health", fake_probe)

    result = _api("ensure_browser_healthy")(
        profile_ref="chrome-gcp",
        max_restarts=1,
        initial_probe=_unhealthy_probe(),
    )

    payload = _payload(result)
    assert payload["status"] == "unhealthy"
    assert payload["healthy"] is False
    assert payload["restart_count"] == 1
    assert payload["error_code"] == "browser_recovery_failed"
    assert payload["error_class"] == "TimeoutError"
    assert payload["probe_after_restart"]["status"] == "healthy"


@pytest.mark.parametrize(
    ("last_operation", "before", "after", "after_restart", "expected_scope", "expected_code"),
    [
        (
            "page_ready_wait",
            _healthy_probe(),
            None,
            None,
            "page_or_site",
            "current_page_or_site_stall",
        ),
        (
            "page_content_read",
            _unhealthy_probe(),
            _healthy_probe(),
            None,
            "target_or_session",
            "child_session_or_transient_cdp_contention",
        ),
        (
            "collection_evaluate",
            _unhealthy_probe(),
            _unhealthy_probe(),
            _healthy_probe(),
            "browser_instance",
            "shared_chrome_cdp_recovered_after_restart",
        ),
        (
            "collection_evaluate",
            _unhealthy_probe(),
            _unhealthy_probe(),
            _unhealthy_probe(),
            "host_runtime_suspected",
            "browser_and_local_host_runtime_unresolved",
        ),
    ],
)
def test_classify_browser_stall_uses_the_governed_evidence_matrix(
    last_operation: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    after_restart: Mapping[str, Any] | None,
    expected_scope: str,
    expected_code: str,
) -> None:
    result = _api("classify_browser_stall")(
        last_operation=last_operation,
        last_operation_state="started",
        probe_before_kill=before,
        probe_after_kill=after,
        probe_after_restart=after_restart,
        child_exit_confirmed=True,
        restart_count=1 if after_restart is not None else 0,
        external_host_evidence=None,
    )

    payload = _payload(result)
    assert payload["failure_scope"] == expected_scope
    assert payload["diagnosis_code"] == expected_code
    assert payload["diagnosis_confidence"] in {"high", "medium", "low"}
    assert payload["root_cause_confirmed"] is False


def test_classification_forbids_gcp_instance_scope_without_external_evidence() -> None:
    result = _api("classify_browser_stall")(
        last_operation="browser_session_open",
        last_operation_state="started",
        probe_before_kill=_unhealthy_probe(),
        probe_after_kill=_unhealthy_probe(),
        probe_after_restart=_unhealthy_probe(),
        child_exit_confirmed=True,
        restart_count=1,
        external_host_evidence=None,
    )

    payload = _payload(result)
    assert payload["failure_scope"] == "host_runtime_suspected"
    assert payload["failure_scope"] != "gcp_instance_unreachable"
    assert payload["external_host_evidence"] in (None, {}, "", "absent")
    assert payload["root_cause_confirmed"] is False


def test_classification_keeps_non_browser_operation_and_unavailable_probe_unknown() -> None:
    non_browser = _api("classify_browser_stall")(
        last_operation="artifact_upload",
        last_operation_state="started",
        probe_before_kill=_healthy_probe(),
        probe_after_kill=_healthy_probe(),
        probe_after_restart=None,
        child_exit_confirmed=True,
        restart_count=0,
    )
    unavailable = _api("classify_browser_stall")(
        last_operation="page_ready_wait",
        last_operation_state="started",
        probe_before_kill=_unhealthy_probe(),
        probe_after_kill={"status": "unavailable", "healthy": False},
        probe_after_restart=None,
        child_exit_confirmed=True,
        restart_count=0,
    )

    assert _payload(non_browser)["failure_scope"] == "non_browser_handler"
    assert _payload(unavailable)["failure_scope"] == "unknown"


def test_classification_rejects_unattributed_external_gcp_status() -> None:
    result = _api("classify_browser_stall")(
        last_operation="browser_session_open",
        last_operation_state="started",
        probe_before_kill=_unhealthy_probe(),
        probe_after_kill=_unhealthy_probe(),
        probe_after_restart=_unhealthy_probe(),
        child_exit_confirmed=True,
        restart_count=1,
        external_host_evidence={"status": "gcp_instance_stopped"},
    )

    assert _payload(result)["failure_scope"] == "host_runtime_suspected"


def test_classification_does_not_treat_a_completed_operation_as_the_stall() -> None:
    result = _api("classify_browser_stall")(
        last_operation="page_navigation",
        last_operation_state="completed",
        probe_before_kill=_healthy_probe(),
        probe_after_kill=_healthy_probe(),
        probe_after_restart=None,
        child_exit_confirmed=True,
        restart_count=0,
    )

    payload = _payload(result)
    assert payload["failure_scope"] == "unknown"
    assert payload["diagnosis_code"] == "no_active_operation_at_stall"


def test_unhealthy_probe_chain_takes_precedence_over_completed_operation_state() -> None:
    result = _api("classify_browser_stall")(
        last_operation="page_navigation",
        last_operation_state="completed",
        probe_before_kill=_unhealthy_probe(),
        probe_after_kill=_unhealthy_probe(),
        probe_after_restart=_unhealthy_probe(),
        child_exit_confirmed=True,
        restart_count=1,
    )

    payload = _payload(result)
    assert payload["failure_scope"] == "host_runtime_suspected"
    assert payload["diagnosis_code"] == "browser_and_local_host_runtime_unresolved"


def test_classification_allows_gcp_unreachable_only_with_independent_evidence() -> None:
    external_evidence = {
        "source": "independent_host_heartbeat",
        "status": "vm_unreachable_from_independent_observer",
    }

    result = _api("classify_browser_stall")(
        last_operation="browser_session_open",
        last_operation_state="started",
        probe_before_kill=_unhealthy_probe(),
        probe_after_kill=_unhealthy_probe(),
        probe_after_restart=_unhealthy_probe(),
        child_exit_confirmed=True,
        restart_count=1,
        external_host_evidence=external_evidence,
    )

    payload = _payload(result)
    assert payload["failure_scope"] == "gcp_instance_unreachable"
    assert payload["diagnosis_code"] == "gcp_vm_unreachable"
    assert payload["external_host_evidence"] == external_evidence
    assert payload["root_cause_confirmed"] is False
