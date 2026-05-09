from __future__ import annotations

import time
from typing import Any, Mapping

from automation_business_scaffold.control_plane.watchdog.models import (
    DEFAULT_LIMIT_PER_RULE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    EXECUTION_TIMEOUT_RULE,
    FAIL_ACTION,
    FAIL_STATUS_BY_TABLE,
    LEASE_EXPIRED_RULE,
    OUTBOX_SENDING_TIMEOUT_RULE,
    REPAIR_ACTION,
    RETRY_ACTION,
    RETRY_STATUS_BY_TABLE,
    RULE_PRECEDENCE,
    RULE_SPECS,
    STALE_PROGRESS_RULE,
    WAITING_CHILDREN_RULE,
    WORKER_HEARTBEAT_TIMEOUT_RULE,
    WatchdogAction,
    WatchdogActionOutcome,
    WatchdogCandidate,
    WatchdogRuleSpec,
    WatchdogScanResult,
    WatchdogStoreProtocol,
)
from automation_business_scaffold.control_plane.watchdog.process_control import (
    kill_worker_process,
    looks_like_mujitask_worker,
)
from automation_business_scaffold.control_plane.watchdog.recovery_policy import (
    apply_watchdog_action,
    decide_watchdog_action,
)
from automation_business_scaffold.control_plane.watchdog.scan_queries import (
    build_watchdog_store,
    coerce_float,
    coerce_int,
    coerce_mapping,
    collect_watchdog_candidates,
    missing_watchdog_helpers,
    resolve_db_url,
)

_coerce_float = coerce_float
_coerce_int = coerce_int
_coerce_mapping = coerce_mapping
_resolve_db_url = resolve_db_url


def execute_watchdog_scan_once(
    params: Mapping[str, Any] | None = None,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    normalized = dict(params or {})
    current_time = coerce_float(normalized.get("now")) or time.time()
    limit_per_rule = coerce_int(normalized.get("limit_per_rule")) or DEFAULT_LIMIT_PER_RULE
    apply_actions = bool(normalized.get("apply_actions", True))

    resolved_store = store if store is not None else build_watchdog_store(normalized)
    db_connection_health = collect_db_connection_health(
        resolved_store,
        max_connection_ratio=coerce_float(normalized.get("db_health_max_connection_ratio")) or 0.8,
        max_idle_in_transaction=(
            coerce_int(normalized.get("db_health_max_idle_in_transaction"))
            if normalized.get("db_health_max_idle_in_transaction") not in (None, "")
            else -1
        ),
    )
    candidates, missing_helpers = collect_watchdog_candidates(
        resolved_store,
        now=current_time,
        limit_per_rule=limit_per_rule,
    )
    outcomes: list[WatchdogActionOutcome] = []
    counts_by_rule: dict[str, int] = {}
    counts_by_action: dict[str, int] = {}
    applied_count = 0
    for candidate in candidates:
        action = decide_watchdog_action(candidate)
        counts_by_rule[candidate.rule_code] = counts_by_rule.get(candidate.rule_code, 0) + 1
        counts_by_action[action.action_type] = counts_by_action.get(action.action_type, 0) + 1
        store_result: dict[str, Any] = {}
        applied = False
        if apply_actions and callable(getattr(resolved_store, "apply_watchdog_action", None)):
            store_result = apply_watchdog_action(resolved_store, action)
            applied = bool(store_result.get("applied", True))
            if applied:
                kill_result = _maybe_kill_timed_out_worker(action, store_result)
                if kill_result:
                    store_result["kill_result"] = kill_result
                applied_count += 1
        outcomes.append(
            WatchdogActionOutcome(
                candidate=candidate,
                action=action,
                applied=applied,
                store_result=store_result,
            )
        )
    status = "idle" if not outcomes else "ok"
    result = WatchdogScanResult(
        now=current_time,
        status=status,
        scanned_count=len(candidates),
        action_count=len(outcomes),
        applied_count=applied_count,
        idle=not outcomes,
        missing_helpers=missing_helpers,
        outcomes=tuple(outcomes),
        counts_by_rule=counts_by_rule,
        counts_by_action=counts_by_action,
        db_connection_health=db_connection_health,
    )
    return result.to_dict()


def collect_db_connection_health(
    store: Any,
    *,
    max_connection_ratio: float = 0.8,
    max_idle_in_transaction: int = -1,
) -> dict[str, Any]:
    helper = getattr(store, "collect_db_connection_health", None)
    if not callable(helper):
        return {"status": "unavailable", "healthy": True, "reason": "store_missing_db_health_helper"}
    return dict(
        helper(
            max_connection_ratio=max_connection_ratio,
            max_idle_in_transaction=max_idle_in_transaction,
        )
        or {}
    )


def run_watchdog_scanner(
    params: Mapping[str, Any] | None = None,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    normalized = dict(params or {})
    max_iterations = max(coerce_int(normalized.get("max_iterations")), 0)
    stop_when_idle = bool(normalized.get("execution_control_stop_when_idle", True))
    max_idle_cycles = max(coerce_int(normalized.get("execution_control_max_idle_cycles")) or 1, 1)
    poll_interval_seconds = coerce_float(normalized.get("execution_control_poll_interval_seconds"))
    if poll_interval_seconds <= 0:
        poll_interval_seconds = DEFAULT_POLL_INTERVAL_SECONDS

    resolved_store = store if store is not None else build_watchdog_store(normalized)
    cycle_count = 0
    idle_cycles = 0
    total_scanned = 0
    total_actions = 0
    total_applied = 0
    counts_by_rule: dict[str, int] = {}
    counts_by_action: dict[str, int] = {}
    last_payload: dict[str, Any] = {
        "status": "idle",
        "idle": True,
        "missing_helpers": list(missing_watchdog_helpers(resolved_store)),
        "outcomes": [],
    }

    while True:
        cycle_count += 1
        last_payload = execute_watchdog_scan_once(normalized, store=resolved_store)
        total_scanned += int(last_payload.get("scanned_count", 0) or 0)
        total_actions += int(last_payload.get("action_count", 0) or 0)
        total_applied += int(last_payload.get("applied_count", 0) or 0)
        for rule_code, count in dict(last_payload.get("counts_by_rule", {})).items():
            counts_by_rule[str(rule_code)] = counts_by_rule.get(str(rule_code), 0) + coerce_int(count)
        for action_code, count in dict(last_payload.get("counts_by_action", {})).items():
            counts_by_action[str(action_code)] = counts_by_action.get(str(action_code), 0) + coerce_int(count)
        if bool(last_payload.get("idle", False)):
            idle_cycles += 1
        else:
            idle_cycles = 0

        if max_iterations and cycle_count >= max_iterations:
            break
        if stop_when_idle and idle_cycles >= max_idle_cycles:
            break
        time.sleep(poll_interval_seconds)

    return {
        "status": "idle" if total_actions == 0 else "ok",
        "cycle_count": cycle_count,
        "idle_cycles": idle_cycles,
        "scanned_count": total_scanned,
        "action_count": total_actions,
        "applied_count": total_applied,
        "counts_by_rule": counts_by_rule,
        "counts_by_action": counts_by_action,
        "missing_helpers": last_payload.get("missing_helpers", []),
        "last_cycle": last_payload,
    }


def _maybe_kill_timed_out_worker(action: WatchdogAction, store_result: Mapping[str, Any]) -> dict[str, Any]:
    if action.action_type != FAIL_ACTION:
        return {}
    if action.target_table not in {"api_worker_job", "task_execution"}:
        return {}
    metadata = dict(action.metadata or {})
    worker_pid = store_result.get("worker_pid") or metadata.get("observed_worker_pid")
    worker_id = str(store_result.get("worker_id") or metadata.get("observed_worker_id") or "")
    return kill_worker_process(worker_pid, expected_worker_id=worker_id)
