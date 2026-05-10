from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.control_plane.watchdog.models import (
    EXECUTION_TIMEOUT_RULE,
    FAIL_ACTION,
    FAIL_STATUS_BY_TABLE,
    LEASE_EXPIRED_RULE,
    OUTBOX_SENDING_TIMEOUT_RULE,
    REPAIR_ACTION,
    RETRY_ACTION,
    RETRY_STATUS_BY_TABLE,
    STALE_PROGRESS_RULE,
    WAITING_CHILDREN_RULE,
    WORKER_HEARTBEAT_TIMEOUT_RULE,
    WatchdogAction,
    WatchdogCandidate,
)


def decide_watchdog_action(candidate: WatchdogCandidate) -> WatchdogAction:
    target_table = candidate.target_table
    fail_status = str(candidate.metadata.get("fail_status") or FAIL_STATUS_BY_TABLE.get(target_table) or "failed")
    retry_status = str(candidate.metadata.get("retry_status") or RETRY_STATUS_BY_TABLE.get(target_table) or "pending")

    if candidate.rule_code == WAITING_CHILDREN_RULE:
        return _repair_waiting_children(candidate, target_table=target_table)
    if candidate.rule_code == OUTBOX_SENDING_TIMEOUT_RULE:
        return _recover_outbox_sending(candidate, target_table=target_table, fail_status=fail_status, retry_status=retry_status)
    if candidate.rule_code == EXECUTION_TIMEOUT_RULE:
        return _recover_execution_timeout(candidate, target_table=target_table, fail_status=fail_status, retry_status=retry_status)
    if candidate.rule_code == WORKER_HEARTBEAT_TIMEOUT_RULE:
        return _fail_worker_heartbeat_timeout(candidate, target_table=target_table, fail_status=fail_status)
    if candidate.rule_code == STALE_PROGRESS_RULE:
        return _recover_stale_progress(candidate, target_table=target_table, fail_status=fail_status, retry_status=retry_status)
    if candidate.rule_code == LEASE_EXPIRED_RULE:
        return _recover_expired_lease(candidate, target_table=target_table, fail_status=fail_status, retry_status=retry_status)
    return _recover_expired_lease(candidate, target_table=target_table, fail_status=fail_status, retry_status=retry_status)


def apply_watchdog_action(store: Any, action: WatchdogAction) -> dict[str, Any]:
    helper = getattr(store, "apply_watchdog_action", None)
    if not callable(helper):
        raise RuntimeError("Watchdog store is missing apply_watchdog_action().")
    payload = helper(action=action.to_dict())
    return dict(payload or {})


def _repair_waiting_children(candidate: WatchdogCandidate, *, target_table: str) -> WatchdogAction:
    next_status = str(candidate.metadata.get("repair_status") or "pending")
    reason = candidate.reason or "Parent request is stuck in waiting after all children became terminal."
    return WatchdogAction(
        action_type=REPAIR_ACTION,
        rule_code=candidate.rule_code,
        target_table=target_table,
        target_id=candidate.target_id,
        target_status=candidate.target_status,
        request_id=candidate.request_id,
        next_status=next_status,
        error_type="waiting_unreconciled",
        reason=reason,
        repair_operation="reconcile_parent_waiting_children",
        metadata=_action_metadata(
            candidate,
            progress_stage=candidate.progress_stage,
            parent_request_id=candidate.parent_request_id,
        ),
    )


def _recover_outbox_sending(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
    retry_status: str,
) -> WatchdogAction:
    retry_budget = candidate.retry_budget
    exhausted = candidate.retry_count + 1 >= retry_budget if retry_budget > 0 else False
    action_type = FAIL_ACTION if exhausted else RETRY_ACTION
    next_status = fail_status if exhausted else retry_status
    reason = candidate.reason or "Outbox record stayed in sending beyond its watchdog timeout window."
    return WatchdogAction(
        action_type=action_type,
        rule_code=candidate.rule_code,
        target_table=target_table,
        target_id=candidate.target_id,
        target_status=candidate.target_status,
        request_id=candidate.request_id,
        next_status=next_status,
        error_type="outbox_sending_timeout",
        reason=reason,
        metadata=_action_metadata(candidate, retry_budget_exhausted=exhausted),
    )


def _recover_execution_timeout(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
    retry_status: str,
) -> WatchdogAction:
    reason = candidate.reason or "Execution exceeded max_execution_seconds."
    if target_table in {"api_worker_job", "task_execution"}:
        return WatchdogAction(
            action_type=FAIL_ACTION,
            rule_code=candidate.rule_code,
            target_table=target_table,
            target_id=candidate.target_id,
            target_status=candidate.target_status,
            request_id=candidate.request_id,
            next_status=fail_status,
            error_type="timeout",
            error_code="job_total_timeout",
            reason=reason,
            metadata=_action_metadata(candidate, retry_budget_exhausted=True),
        )
    return _retry_or_fail_action(
        candidate,
        target_table=target_table,
        fail_status=fail_status,
        retry_status=retry_status,
        error_type="timeout",
        error_code="job_total_timeout",
        reason=reason,
    )


def _fail_worker_heartbeat_timeout(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
) -> WatchdogAction:
    reason = candidate.reason or "Worker heartbeat exceeded heartbeat_timeout_seconds."
    return WatchdogAction(
        action_type=FAIL_ACTION,
        rule_code=candidate.rule_code,
        target_table=target_table,
        target_id=candidate.target_id,
        target_status=candidate.target_status,
        request_id=candidate.request_id,
        next_status=fail_status,
        error_type="timeout",
        error_code="worker_heartbeat_timeout",
        reason=reason,
        metadata=_action_metadata(candidate, retry_budget_exhausted=True),
    )


def _recover_stale_progress(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
    retry_status: str,
) -> WatchdogAction:
    reason = candidate.reason or "Heartbeat is still moving, but last_progress_at has gone stale."
    if target_table in {"api_worker_job", "task_execution"}:
        return WatchdogAction(
            action_type=FAIL_ACTION,
            rule_code=candidate.rule_code,
            target_table=target_table,
            target_id=candidate.target_id,
            target_status=candidate.target_status,
            request_id=candidate.request_id,
            next_status=fail_status,
            error_type="stale_progress",
            error_code="job_no_progress_timeout",
            reason=reason,
            metadata=_action_metadata(candidate, retry_budget_exhausted=True),
        )
    return _retry_or_fail_action(
        candidate,
        target_table=target_table,
        fail_status=fail_status,
        retry_status=retry_status,
        error_type="stale_progress",
        error_code="job_no_progress_timeout",
        reason=reason,
    )


def _recover_expired_lease(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
    retry_status: str,
) -> WatchdogAction:
    reason = candidate.reason or "Lease expired before the worker completed its running job."
    return _retry_or_fail_action(
        candidate,
        target_table=target_table,
        fail_status=fail_status,
        retry_status=retry_status,
        error_type="lease_expired",
        error_code="",
        reason=reason,
    )


def _retry_or_fail_action(
    candidate: WatchdogCandidate,
    *,
    target_table: str,
    fail_status: str,
    retry_status: str,
    error_type: str,
    error_code: str,
    reason: str,
) -> WatchdogAction:
    exhausted = candidate.retry_budget_exhausted
    action_type = FAIL_ACTION if exhausted else RETRY_ACTION
    next_status = fail_status if exhausted else retry_status
    return WatchdogAction(
        action_type=action_type,
        rule_code=candidate.rule_code,
        target_table=target_table,
        target_id=candidate.target_id,
        target_status=candidate.target_status,
        request_id=candidate.request_id,
        next_status=next_status,
        error_type=error_type,
        error_code=error_code,
        reason=reason,
        metadata=_action_metadata(candidate, retry_budget_exhausted=exhausted),
    )


def _action_metadata(candidate: WatchdogCandidate, **extra: Any) -> dict[str, Any]:
    metadata = dict(candidate.metadata)
    metadata.update(
        {
            "observed_attempt_count": candidate.attempt_count,
            "observed_retry_count": candidate.retry_count,
            "observed_lease_until": candidate.lease_until,
            "observed_started_at": candidate.started_at,
            "observed_last_progress_at": candidate.last_progress_at,
            "observed_max_execution_seconds": candidate.max_execution_seconds,
            "observed_run_id": candidate.run_id,
            "observed_worker_id": candidate.worker_id,
            "observed_worker_pid": candidate.worker_pid,
            "observed_heartbeat_at": candidate.heartbeat_at,
            "observed_max_idle_seconds": candidate.max_idle_seconds,
            "observed_heartbeat_timeout_seconds": candidate.heartbeat_timeout_seconds,
        }
    )
    metadata.update(extra)
    return metadata
