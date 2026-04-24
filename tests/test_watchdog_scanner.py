from __future__ import annotations

import json
from typing import Any, Mapping

from automation_business_scaffold import watchdog_scanner as watchdog_cli
from automation_business_scaffold.business.flows.watchdog_scanner import (
    EXECUTION_TIMEOUT_RULE,
    LEASE_EXPIRED_RULE,
    OUTBOX_SENDING_TIMEOUT_RULE,
    STALE_PROGRESS_RULE,
    WAITING_CHILDREN_RULE,
    collect_watchdog_candidates,
    decide_watchdog_action,
    execute_watchdog_scan_once,
    run_watchdog_scanner,
)


class FakeWatchdogStore:
    def __init__(self) -> None:
        self.rows_by_helper: dict[str, list[dict[str, Any]]] = {
            "scan_expired_running_leases": [],
            "scan_stale_progress": [],
            "scan_execution_timeouts": [],
            "scan_waiting_children_reconciliation": [],
            "scan_expired_outbox_sending": [],
        }
        self.applied_actions: list[dict[str, Any]] = []

    def scan_expired_running_leases(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
        return self.rows_by_helper["scan_expired_running_leases"][: limit or None]

    def scan_stale_progress(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
        return self.rows_by_helper["scan_stale_progress"][: limit or None]

    def scan_execution_timeouts(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
        return self.rows_by_helper["scan_execution_timeouts"][: limit or None]

    def scan_waiting_children_reconciliation(
        self,
        *,
        now: float,
        limit: int | None = None,
    ) -> list[Mapping[str, Any]]:
        return self.rows_by_helper["scan_waiting_children_reconciliation"][: limit or None]

    def scan_expired_outbox_sending(self, *, now: float, limit: int | None = None) -> list[Mapping[str, Any]]:
        return self.rows_by_helper["scan_expired_outbox_sending"][: limit or None]

    def apply_watchdog_action(self, *, action: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = dict(action)
        self.applied_actions.append(payload)
        return {
            "target_table": payload.get("target_table", ""),
            "target_id": payload.get("target_id", ""),
            "action_type": payload.get("action_type", ""),
            "applied": True,
        }


def test_watchdog_scan_decides_retry_for_lease_expired_job() -> None:
    store = FakeWatchdogStore()
    store.rows_by_helper["scan_expired_running_leases"].append(
        {
            "target_table": "api_worker_job",
            "job_id": "job-1",
            "request_id": "req-1",
            "status": "running",
            "attempt_count": 1,
            "max_attempts": 3,
        }
    )

    payload = execute_watchdog_scan_once({"now": 100.0}, store=store)

    assert payload["status"] == "ok"
    assert payload["counts_by_action"]["retry"] == 1
    assert payload["outcomes"][0]["action"]["rule_code"] == LEASE_EXPIRED_RULE
    assert payload["outcomes"][0]["action"]["action_type"] == "retry"
    assert payload["outcomes"][0]["action"]["next_status"] == "retry_wait"
    assert payload["outcomes"][0]["action"]["error_type"] == "lease_expired"
    assert store.applied_actions[0]["target_id"] == "job-1"


def test_watchdog_scan_decides_fail_when_stale_progress_budget_is_exhausted() -> None:
    store = FakeWatchdogStore()
    store.rows_by_helper["scan_stale_progress"].append(
        {
            "target_table": "api_worker_job",
            "job_id": "job-2",
            "request_id": "req-2",
            "status": "running",
            "attempt_count": 3,
            "max_attempts": 3,
            "progress_stage": "fastmoss_creator_fetch",
        }
    )

    payload = execute_watchdog_scan_once({"now": 100.0}, store=store)

    assert payload["counts_by_action"]["fail"] == 1
    outcome = payload["outcomes"][0]
    assert outcome["action"]["rule_code"] == STALE_PROGRESS_RULE
    assert outcome["action"]["action_type"] == "fail"
    assert outcome["action"]["next_status"] == "failed"
    assert outcome["action"]["error_type"] == "stale_progress"


def test_watchdog_scan_decides_repair_for_waiting_children_parent() -> None:
    store = FakeWatchdogStore()
    store.rows_by_helper["scan_waiting_children_reconciliation"].append(
        {
            "target_table": "task_request",
            "request_id": "req-3",
            "status": "waiting_children",
            "progress_stage": "product_collection",
        }
    )

    payload = execute_watchdog_scan_once({"now": 100.0}, store=store)

    assert payload["counts_by_action"]["repair"] == 1
    outcome = payload["outcomes"][0]
    assert outcome["action"]["rule_code"] == WAITING_CHILDREN_RULE
    assert outcome["action"]["repair_operation"] == "reconcile_parent_waiting_children"
    assert outcome["action"]["next_status"] == "ready_for_summary"
    assert outcome["action"]["error_type"] == "waiting_children_unreconciled"


def test_watchdog_scan_decides_fail_for_timed_out_outbox_when_retries_exhausted() -> None:
    store = FakeWatchdogStore()
    store.rows_by_helper["scan_expired_outbox_sending"].append(
        {
            "target_table": "notification_outbox",
            "outbox_id": "outbox-1",
            "status": "sending",
            "retry_count": 2,
            "max_retries": 2,
        }
    )

    payload = execute_watchdog_scan_once({"now": 100.0}, store=store)

    assert payload["counts_by_action"]["fail"] == 1
    outcome = payload["outcomes"][0]
    assert outcome["action"]["rule_code"] == OUTBOX_SENDING_TIMEOUT_RULE
    assert outcome["action"]["action_type"] == "fail"
    assert outcome["action"]["next_status"] == "failed"
    assert outcome["action"]["error_type"] == "outbox_sending_timeout"


def test_watchdog_collect_prefers_timeout_over_lease_for_same_target() -> None:
    store = FakeWatchdogStore()
    duplicated = {
        "target_table": "task_execution",
        "execution_id": "exec-1",
        "request_id": "req-4",
        "status": "running",
        "attempt_count": 1,
        "max_attempts": 5,
    }
    store.rows_by_helper["scan_expired_running_leases"].append(dict(duplicated))
    store.rows_by_helper["scan_execution_timeouts"].append(dict(duplicated))

    candidates, missing_helpers = collect_watchdog_candidates(store, now=100.0)

    assert missing_helpers == ()
    assert len(candidates) == 1
    assert candidates[0].rule_code == EXECUTION_TIMEOUT_RULE


def test_decide_watchdog_action_keeps_retry_status_for_execution_timeout() -> None:
    candidates, _ = collect_watchdog_candidates(
        _seed_store(
            "scan_execution_timeouts",
            {
                "target_table": "task_execution",
                "execution_id": "exec-2",
                "request_id": "req-5",
                "status": "running",
                "attempt_count": 1,
                "max_attempts": 4,
            },
        ),
        now=100.0,
    )
    action = decide_watchdog_action(candidates[0])

    assert action.rule_code == EXECUTION_TIMEOUT_RULE
    assert action.action_type == "retry"
    assert action.next_status == "retry_wait"
    assert action.error_type == "timeout"


def test_run_watchdog_scanner_stops_after_idle_cycle() -> None:
    store = FakeWatchdogStore()

    payload = run_watchdog_scanner(
        {
            "execution_control_stop_when_idle": True,
            "execution_control_max_idle_cycles": 1,
            "execution_control_poll_interval_seconds": 0.0,
            "max_iterations": 3,
        },
        store=store,
    )

    assert payload["status"] == "idle"
    assert payload["cycle_count"] == 1
    assert payload["action_count"] == 0


def test_watchdog_entrypoint_once_uses_single_scan_payload(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        watchdog_cli,
        "execute_watchdog_scan_once",
        lambda params: {
            "status": "ok",
            "action_count": 1,
            "counts_by_action": {"retry": 1},
            "outcomes": [],
        },
    )

    exit_code = watchdog_cli.main(["--once", "--dry-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["action_count"] == 1


def _seed_store(helper_name: str, row: Mapping[str, Any]) -> FakeWatchdogStore:
    store = FakeWatchdogStore()
    store.rows_by_helper[helper_name].append(dict(row))
    return store
