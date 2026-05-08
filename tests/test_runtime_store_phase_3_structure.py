from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from automation_business_scaffold.infrastructure.runtime.request_lifecycle import RuntimeRequestLifecycle
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.infrastructure.runtime.watchdog_recovery import WatchdogRecoveryCoordinator


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "infrastructure" / "runtime"
RUNTIME_STORE = RUNTIME_ROOT / "runtime_store.py"


class _FakeResult:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> dict[str, Any]:
        return self._row


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(self._rows.pop(0))


class _FakeSqlStore:
    @staticmethod
    def _text(sql: str) -> str:
        return sql


def test_request_lifecycle_aggregates_task_and_api_children() -> None:
    coordinator = RuntimeRequestLifecycle(_FakeSqlStore())
    connection = _FakeConnection(
        [
            {
                "total_count": 2,
                "terminal_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "active_count": 1,
            },
            {
                "total_count": 3,
                "terminal_count": 3,
                "success_count": 2,
                "failed_count": 1,
                "skipped_count": 0,
                "active_count": 0,
            },
        ]
    )

    counts = coordinator.aggregate_children(connection, request_id="req-1")

    assert counts == {
        "total_count": 5,
        "terminal_count": 4,
        "success_count": 3,
        "failed_count": 1,
        "skipped_count": 0,
        "active_count": 1,
    }


def test_outbox_claim_and_retry_paths_delegate_to_repository() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeOutboxRepo:
        def claim_next_outbox(self, **kwargs: Any) -> str:
            calls.append(("claim", kwargs))
            return "claimed"

        def mark_outbox_retry_or_failed(self, **kwargs: Any) -> str:
            calls.append(("retry", kwargs))
            return "retry_wait"

    store = RuntimeStore.__new__(RuntimeStore)
    store._notification_outbox_repo = FakeOutboxRepo()
    store._ensure_runtime_schema_ready = lambda: calls.append(("schema", {}))

    claimed = RuntimeStore.claim_next_outbox(store, worker_id="worker-1", lease_seconds=30)
    retried = RuntimeStore.mark_outbox_retry_or_failed(
        store,
        outbox_id="outbox-1",
        error_text="temporary",
        retry_delay_seconds=5,
        retryable=True,
        error_type="timeout",
        error_code="outbox_timeout",
    )

    assert claimed == "claimed"
    assert retried == "retry_wait"
    assert calls == [
        ("schema", {}),
        ("claim", {"worker_id": "worker-1", "lease_seconds": 30}),
        (
            "retry",
            {
                "outbox_id": "outbox-1",
                "error_text": "temporary",
                "retry_delay_seconds": 5,
                "retryable": True,
                "error_type": "timeout",
                "error_code": "outbox_timeout",
                "dead_letter_reason": "",
            },
        ),
    ]


def test_watchdog_recovery_repair_delegates_request_reconciliation() -> None:
    class Request:
        status = "ready_for_summary"

    class FakeStore:
        def __init__(self) -> None:
            self.reconciled: list[str] = []

        def reconcile_request_waiting_children(self, *, request_id: str) -> dict[str, Any]:
            self.reconciled.append(request_id)
            return {"request": Request(), "transitioned": True}

    store = FakeStore()

    result = WatchdogRecoveryCoordinator(store).apply_watchdog_action(
        action={"target_table": "task_request", "target_id": "req-1", "action_type": "repair"}
    )

    assert result["applied"] is True
    assert result["status"] == "ready_for_summary"
    assert store.reconciled == ["req-1"]


def test_runtime_store_phase_3_coordination_does_not_reflow_into_facade() -> None:
    source = RUNTIME_STORE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    method_lengths = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }

    assert method_lengths["apply_watchdog_action"] <= 3
    assert method_lengths["scan_waiting_children_reconciliation"] <= 3
    assert method_lengths["scan_expired_outbox_sending"] <= 3
    assert method_lengths["reconcile_request_waiting_children"] <= 3
    assert method_lengths["_aggregate_runtime_request_children"] <= 3
    assert method_lengths["_refresh_request_child_counts"] <= 3
    assert "WatchdogRecoveryCoordinator" in source
    assert "RuntimeRequestLifecycle" in source
    assert (RUNTIME_ROOT / "request_lifecycle.py").is_file()
    assert (RUNTIME_ROOT / "watchdog_recovery.py").is_file()
