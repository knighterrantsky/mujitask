from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

LEASE_EXPIRED_RULE = "running_job_lease_expired"
STALE_PROGRESS_RULE = "stale_progress"
EXECUTION_TIMEOUT_RULE = "execution_timeout"
WAITING_CHILDREN_RULE = "parent_waiting_children_unreconciled"
OUTBOX_SENDING_TIMEOUT_RULE = "outbox_sending_timeout"

RETRY_ACTION = "retry"
FAIL_ACTION = "fail"
REPAIR_ACTION = "repair"

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_LIMIT_PER_RULE = 200

RULE_PRECEDENCE = {
    EXECUTION_TIMEOUT_RULE: 500,
    STALE_PROGRESS_RULE: 400,
    LEASE_EXPIRED_RULE: 300,
    OUTBOX_SENDING_TIMEOUT_RULE: 200,
    WAITING_CHILDREN_RULE: 100,
}

RETRY_STATUS_BY_TABLE = {
    "api_worker_job": "retry_wait",
    "task_execution": "retry_wait",
    "notification_outbox": "retry_wait",
    "task_request": "pending",
}

FAIL_STATUS_BY_TABLE = {
    "api_worker_job": "failed",
    "task_execution": "failed",
    "notification_outbox": "failed",
    "task_request": "failed",
}


@dataclass(frozen=True, slots=True)
class WatchdogRuleSpec:
    rule_code: str
    helper_name: str
    target_status: str
    description: str


RULE_SPECS = (
    WatchdogRuleSpec(
        rule_code=LEASE_EXPIRED_RULE,
        helper_name="scan_expired_running_leases",
        target_status="running",
        description="Recover running jobs whose lease has expired.",
    ),
    WatchdogRuleSpec(
        rule_code=STALE_PROGRESS_RULE,
        helper_name="scan_stale_progress",
        target_status="running",
        description="Retry or fail jobs that keep heartbeating without real progress.",
    ),
    WatchdogRuleSpec(
        rule_code=EXECUTION_TIMEOUT_RULE,
        helper_name="scan_execution_timeouts",
        target_status="running",
        description="Retry or fail jobs whose execution timeout budget has been exceeded.",
    ),
    WatchdogRuleSpec(
        rule_code=WAITING_CHILDREN_RULE,
        helper_name="scan_waiting_children_reconciliation",
        target_status="waiting_children",
        description="Repair parent requests whose children are terminal but summary has not advanced.",
    ),
    WatchdogRuleSpec(
        rule_code=OUTBOX_SENDING_TIMEOUT_RULE,
        helper_name="scan_expired_outbox_sending",
        target_status="sending",
        description="Recover outbox records that remain stuck in sending.",
    ),
)


@dataclass(frozen=True, slots=True)
class WatchdogCandidate:
    rule_code: str
    target_table: str
    target_id: str
    target_status: str
    target_kind: str = ""
    request_id: str = ""
    parent_request_id: str = ""
    worker_id: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    retry_count: int = 0
    max_retries: int = 0
    lease_until: float = 0.0
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    progress_stage: str = ""
    next_retry_at: float = 0.0
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.target_table, self.target_id)

    @property
    def retry_budget(self) -> int:
        if self.max_attempts > 0:
            return self.max_attempts
        return self.max_retries

    @property
    def retry_ordinal(self) -> int:
        if self.max_attempts > 0:
            return self.attempt_count
        return self.retry_count

    @property
    def retry_budget_exhausted(self) -> bool:
        budget = self.retry_budget
        return budget > 0 and self.retry_ordinal >= budget

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WatchdogAction:
    action_type: str
    rule_code: str
    target_table: str
    target_id: str
    target_status: str
    request_id: str = ""
    next_status: str = ""
    error_type: str = ""
    reason: str = ""
    repair_operation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WatchdogActionOutcome:
    candidate: WatchdogCandidate
    action: WatchdogAction
    applied: bool = False
    store_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "action": self.action.to_dict(),
            "applied": self.applied,
            "store_result": dict(self.store_result),
        }


@dataclass(frozen=True, slots=True)
class WatchdogScanResult:
    now: float
    status: str
    scanned_count: int
    action_count: int
    applied_count: int
    idle: bool
    missing_helpers: tuple[str, ...] = ()
    outcomes: tuple[WatchdogActionOutcome, ...] = ()
    counts_by_rule: dict[str, int] = field(default_factory=dict)
    counts_by_action: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "now": self.now,
            "idle": self.idle,
            "scanned_count": self.scanned_count,
            "action_count": self.action_count,
            "applied_count": self.applied_count,
            "missing_helpers": list(self.missing_helpers),
            "counts_by_rule": dict(self.counts_by_rule),
            "counts_by_action": dict(self.counts_by_action),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


@runtime_checkable
class WatchdogStoreProtocol(Protocol):
    def scan_expired_running_leases(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_stale_progress(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_execution_timeouts(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_waiting_children_reconciliation(
        self,
        *,
        now: float,
        limit: int | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_expired_outbox_sending(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def apply_watchdog_action(self, *, action: Mapping[str, Any]) -> Mapping[str, Any] | None:
        ...


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _resolve_db_url(params: Mapping[str, Any] | None = None) -> str:
    normalized = dict(params or {})
    db_url = str(
        normalized.get("execution_control_db_url")
        or normalized.get("db_url")
        or os.getenv("BUSINESS_EXECUTION_CONTROL_DB_URL")
        or os.getenv("EXECUTION_CONTROL_DB_URL")
        or ""
    ).strip()
    return db_url


def build_watchdog_store(params: Mapping[str, Any] | None = None) -> Any:
    from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

    return RuntimeStore(db_url=_resolve_db_url(params))


def missing_watchdog_helpers(store: Any) -> tuple[str, ...]:
    missing: list[str] = []
    for spec in RULE_SPECS:
        helper = getattr(store, spec.helper_name, None)
        if not callable(helper):
            missing.append(spec.helper_name)
    if not callable(getattr(store, "apply_watchdog_action", None)):
        missing.append("apply_watchdog_action")
    return tuple(missing)


def _candidate_from_mapping(rule_code: str, payload: Mapping[str, Any], spec: WatchdogRuleSpec) -> WatchdogCandidate:
    metadata = _coerce_mapping(payload.get("metadata"))
    nested_payload = _coerce_mapping(payload.get("payload"))
    target_table = str(
        payload.get("target_table")
        or payload.get("runtime_table")
        or metadata.get("target_table")
        or ""
    ).strip()
    target_id = str(
        payload.get("target_id")
        or payload.get("job_id")
        or payload.get("execution_id")
        or payload.get("request_id")
        or payload.get("outbox_id")
        or metadata.get("target_id")
        or ""
    ).strip()
    request_id = str(payload.get("request_id") or metadata.get("request_id") or "").strip()
    if target_table == "task_request" and not request_id:
        request_id = target_id
    return WatchdogCandidate(
        rule_code=rule_code,
        target_table=target_table,
        target_id=target_id,
        target_status=str(payload.get("status") or spec.target_status or "").strip(),
        target_kind=str(payload.get("target_kind") or payload.get("job_code") or payload.get("item_code") or "").strip(),
        request_id=request_id,
        parent_request_id=str(payload.get("parent_request_id") or metadata.get("parent_request_id") or "").strip(),
        worker_id=str(payload.get("worker_id") or "").strip(),
        attempt_count=_coerce_int(payload.get("attempt_count")),
        max_attempts=_coerce_int(payload.get("max_attempts")),
        retry_count=_coerce_int(payload.get("retry_count")),
        max_retries=_coerce_int(payload.get("max_retries") or payload.get("max_retry_count")),
        lease_until=_coerce_float(payload.get("lease_until")),
        started_at=_coerce_float(payload.get("started_at")),
        heartbeat_at=_coerce_float(payload.get("heartbeat_at")),
        last_progress_at=_coerce_float(payload.get("last_progress_at")),
        max_execution_seconds=_coerce_float(payload.get("max_execution_seconds")),
        progress_stage=str(payload.get("progress_stage") or "").strip(),
        next_retry_at=_coerce_float(payload.get("next_retry_at")),
        reason=str(payload.get("reason") or metadata.get("reason") or "").strip(),
        payload=nested_payload,
        metadata=metadata,
    )


def _dedupe_candidates(candidates: Iterable[WatchdogCandidate]) -> tuple[WatchdogCandidate, ...]:
    selected: dict[tuple[str, str], WatchdogCandidate] = {}
    for candidate in candidates:
        if not candidate.target_table or not candidate.target_id:
            continue
        key = candidate.dedupe_key
        existing = selected.get(key)
        if existing is None:
            selected[key] = candidate
            continue
        if RULE_PRECEDENCE.get(candidate.rule_code, 0) > RULE_PRECEDENCE.get(existing.rule_code, 0):
            selected[key] = candidate
    ordered = sorted(
        selected.values(),
        key=lambda item: (-RULE_PRECEDENCE.get(item.rule_code, 0), item.target_table, item.target_id),
    )
    return tuple(ordered)


def collect_watchdog_candidates(
    store: Any,
    *,
    now: float | None = None,
    limit_per_rule: int | None = DEFAULT_LIMIT_PER_RULE,
) -> tuple[tuple[WatchdogCandidate, ...], tuple[str, ...]]:
    current_time = _coerce_float(now) or time.time()
    missing_helpers = missing_watchdog_helpers(store)
    candidates: list[WatchdogCandidate] = []
    for spec in RULE_SPECS:
        helper = getattr(store, spec.helper_name, None)
        if not callable(helper):
            continue
        rows = helper(now=current_time, limit=limit_per_rule)
        for row in rows or ():
            candidates.append(_candidate_from_mapping(spec.rule_code, row, spec))
    return _dedupe_candidates(candidates), missing_helpers


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
        }
    )
    metadata.update(extra)
    return metadata


def decide_watchdog_action(candidate: WatchdogCandidate) -> WatchdogAction:
    target_table = candidate.target_table
    fail_status = str(candidate.metadata.get("fail_status") or FAIL_STATUS_BY_TABLE.get(target_table) or "failed")
    retry_status = str(candidate.metadata.get("retry_status") or RETRY_STATUS_BY_TABLE.get(target_table) or "retry_wait")

    if candidate.rule_code == WAITING_CHILDREN_RULE:
        next_status = str(candidate.metadata.get("repair_status") or "ready_for_summary")
        reason = candidate.reason or "Parent request is stuck in waiting_children after all children became terminal."
        return WatchdogAction(
            action_type=REPAIR_ACTION,
            rule_code=candidate.rule_code,
            target_table=target_table,
            target_id=candidate.target_id,
            target_status=candidate.target_status,
            request_id=candidate.request_id,
            next_status=next_status,
            error_type="waiting_children_unreconciled",
            reason=reason,
            repair_operation="reconcile_parent_waiting_children",
            metadata=_action_metadata(
                candidate,
                progress_stage=candidate.progress_stage,
                parent_request_id=candidate.parent_request_id,
            ),
        )

    if candidate.rule_code == OUTBOX_SENDING_TIMEOUT_RULE:
        retry_budget = candidate.retry_budget
        if retry_budget > 0:
            exhausted = candidate.retry_count + 1 >= retry_budget
        else:
            exhausted = False
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

    if candidate.rule_code == EXECUTION_TIMEOUT_RULE:
        exhausted = candidate.retry_budget_exhausted
        action_type = FAIL_ACTION if exhausted else RETRY_ACTION
        next_status = fail_status if exhausted else retry_status
        reason = candidate.reason or "Execution exceeded max_execution_seconds."
        return WatchdogAction(
            action_type=action_type,
            rule_code=candidate.rule_code,
            target_table=target_table,
            target_id=candidate.target_id,
            target_status=candidate.target_status,
            request_id=candidate.request_id,
            next_status=next_status,
            error_type="timeout",
            reason=reason,
            metadata=_action_metadata(candidate, retry_budget_exhausted=exhausted),
        )

    if candidate.rule_code == STALE_PROGRESS_RULE:
        exhausted = candidate.retry_budget_exhausted
        action_type = FAIL_ACTION if exhausted else RETRY_ACTION
        next_status = fail_status if exhausted else retry_status
        reason = candidate.reason or "Heartbeat is still moving, but last_progress_at has gone stale."
        return WatchdogAction(
            action_type=action_type,
            rule_code=candidate.rule_code,
            target_table=target_table,
            target_id=candidate.target_id,
            target_status=candidate.target_status,
            request_id=candidate.request_id,
            next_status=next_status,
            error_type="stale_progress",
            reason=reason,
            metadata=_action_metadata(candidate, retry_budget_exhausted=exhausted),
        )

    exhausted = candidate.retry_budget_exhausted
    action_type = FAIL_ACTION if exhausted else RETRY_ACTION
    next_status = fail_status if exhausted else retry_status
    reason = candidate.reason or "Lease expired before the worker completed its running job."
    return WatchdogAction(
        action_type=action_type,
        rule_code=candidate.rule_code,
        target_table=target_table,
        target_id=candidate.target_id,
        target_status=candidate.target_status,
        request_id=candidate.request_id,
        next_status=next_status,
        error_type="lease_expired",
        reason=reason,
        metadata=_action_metadata(candidate, retry_budget_exhausted=exhausted),
    )


def apply_watchdog_action(store: Any, action: WatchdogAction) -> dict[str, Any]:
    helper = getattr(store, "apply_watchdog_action", None)
    if not callable(helper):
        raise RuntimeError("Watchdog store is missing apply_watchdog_action().")
    payload = helper(action=action.to_dict())
    return dict(payload or {})


def execute_watchdog_scan_once(
    params: Mapping[str, Any] | None = None,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    normalized = dict(params or {})
    current_time = _coerce_float(normalized.get("now")) or time.time()
    limit_per_rule = _coerce_int(normalized.get("limit_per_rule")) or DEFAULT_LIMIT_PER_RULE
    apply_actions = bool(normalized.get("apply_actions", True))

    resolved_store = store if store is not None else build_watchdog_store(normalized)
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
    )
    return result.to_dict()


def run_watchdog_scanner(
    params: Mapping[str, Any] | None = None,
    *,
    store: Any | None = None,
) -> dict[str, Any]:
    normalized = dict(params or {})
    max_iterations = max(_coerce_int(normalized.get("max_iterations")), 0)
    stop_when_idle = bool(normalized.get("execution_control_stop_when_idle", True))
    max_idle_cycles = max(_coerce_int(normalized.get("execution_control_max_idle_cycles")) or 1, 1)
    poll_interval_seconds = _coerce_float(normalized.get("execution_control_poll_interval_seconds"))
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
            counts_by_rule[str(rule_code)] = counts_by_rule.get(str(rule_code), 0) + _coerce_int(count)
        for action_code, count in dict(last_payload.get("counts_by_action", {})).items():
            counts_by_action[str(action_code)] = counts_by_action.get(str(action_code), 0) + _coerce_int(count)
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
