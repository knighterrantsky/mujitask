from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import automation_business_scaffold.control_plane.executor.request_dispatch as request_dispatch
import automation_business_scaffold.control_plane.executor.runner as runner
import automation_business_scaffold.control_plane.executor.worker_dispatch as worker_dispatch
from automation_business_scaffold.control_plane.watchdog.recovery_policy import decide_watchdog_action
from automation_business_scaffold.control_plane.watchdog.scan_queries import collect_watchdog_candidates

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE = REPO_ROOT / "src" / "automation_business_scaffold" / "control_plane"
RUNNER = CONTROL_PLANE / "executor" / "runner.py"
SCANNER = CONTROL_PLANE / "watchdog" / "scanner.py"


def _function_span(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return int(node.end_lineno or node.lineno) - node.lineno + 1
    raise AssertionError(f"{name} not found in {path}")


def _top_level_classes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def test_runner_dispatch_entrypoints_are_thin_facades() -> None:
    assert (CONTROL_PLANE / "executor" / "request_dispatch.py").is_file()
    assert (CONTROL_PLANE / "executor" / "worker_dispatch.py").is_file()
    assert (CONTROL_PLANE / "executor" / "request_aggregation.py").is_file()

    for function_name in ("execute_executor_once", "execute_api_worker_once", "execute_browser_once"):
        assert _function_span(RUNNER, function_name) <= 8

    for function_name in (
        "_persist_api_worker_outcome",
        "_persist_browser_execution_outcome",
        "_refresh_request_aggregate_counts",
        "_aggregate_request_children",
    ):
        assert _function_span(RUNNER, function_name) <= 20


def test_watchdog_scanner_keeps_scan_and_recovery_ownership_split() -> None:
    assert (CONTROL_PLANE / "watchdog" / "models.py").is_file()
    assert (CONTROL_PLANE / "watchdog" / "scan_queries.py").is_file()
    assert (CONTROL_PLANE / "watchdog" / "recovery_policy.py").is_file()
    assert (CONTROL_PLANE / "watchdog" / "process_control.py").is_file()

    scanner_classes = _top_level_classes(SCANNER)
    assert "WatchdogCandidate" not in scanner_classes
    assert "WatchdogAction" not in scanner_classes
    assert "WatchdogScanResult" not in scanner_classes
    assert _function_span(SCANNER, "execute_watchdog_scan_once") <= 70


def test_request_dispatch_waiting_path_updates_parent_request(monkeypatch) -> None:
    request = SimpleNamespace(
        request_id="req-1",
        task_code="search_keyword_competitor_products",
        current_stage="dispatch_products",
    )

    class FakeStore:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def claim_next_task_request(self, *, worker_id: str, lease_seconds: float) -> Any:
            return request

        def load_task_request(self, *, request_id: str) -> Any:
            return request

        def update_task_request(self, **payload: Any) -> None:
            self.updates.append(dict(payload))
            if "current_stage" in payload:
                request.current_stage = payload["current_stage"]

    class FakeRuntime:
        def advance_stage(self, **_: Any) -> dict[str, Any]:
            return {
                "action": "waiting",
                "current_stage": "dispatch_products",
                "message": "waiting on children",
                "details": {"child_jobs_created": 1},
            }

    store = FakeStore()
    monkeypatch.setattr(
        request_dispatch,
        "build_runtime_settings",
        lambda params: SimpleNamespace(worker_id="executor-test", lease_seconds=30.0),
    )
    monkeypatch.setattr(request_dispatch, "create_runtime_store", lambda settings: store)
    monkeypatch.setattr(request_dispatch, "refresh_request_aggregate_counts", lambda *_, **__: None)
    monkeypatch.setattr(
        request_dispatch,
        "get_workflow_definition",
        lambda task_code: SimpleNamespace(
            entry_stage_code="dispatch_products",
            summary_policy=SimpleNamespace(summary_stage_code="summary"),
        ),
    )
    monkeypatch.setattr(request_dispatch, "resolve_workflow_runtime", lambda task_code: FakeRuntime())
    monkeypatch.setattr(
        request_dispatch,
        "build_runtime_request_payload",
        lambda **kwargs: {
            "status": "ok",
            "request_id": kwargs["request_id"],
            "control_action": kwargs["control_action"],
            "message": kwargs["message"],
        },
    )

    payload = request_dispatch.execute_executor_once({})

    assert payload["daemon_status"] == "processed"
    assert payload["success_count"] == 1
    assert any(update.get("status") == "waiting" for update in store.updates)


def test_runner_public_api_and_browser_worker_facades_delegate(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        worker_dispatch,
        "execute_api_worker_once",
        lambda params: calls.append("api") or {"daemon_status": "processed", "worker": "api"},
    )
    monkeypatch.setattr(
        worker_dispatch,
        "execute_browser_once",
        lambda params: calls.append("browser") or {"daemon_status": "processed", "worker": "browser"},
    )

    assert runner.execute_api_worker_once({})["worker"] == "api"
    assert runner.execute_browser_once({})["worker"] == "browser"
    assert calls == ["api", "browser"]


def test_watchdog_scan_glue_and_recovery_policy_are_separate_paths() -> None:
    class FakeStore:
        def scan_expired_running_leases(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return [{"target_table": "api_worker_job", "job_id": "job-1", "attempt_count": 1, "max_attempts": 3}]

        def scan_stale_progress(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return []

        def scan_worker_heartbeat_timeouts(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return []

        def scan_execution_timeouts(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return []

        def scan_waiting_children_reconciliation(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return []

        def scan_expired_outbox_sending(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
            return []

        def apply_watchdog_action(self, *, action: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"applied": True, "action_type": action["action_type"]}

    candidates, missing = collect_watchdog_candidates(FakeStore(), now=100.0)
    action = decide_watchdog_action(candidates[0])

    assert missing == ()
    assert action.action_type == "retry"
    assert action.next_status == "pending"
