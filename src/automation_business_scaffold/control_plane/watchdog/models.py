from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

LEASE_EXPIRED_RULE = "running_job_lease_expired"
WORKER_HEARTBEAT_TIMEOUT_RULE = "worker_heartbeat_timeout"
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
    WORKER_HEARTBEAT_TIMEOUT_RULE: 450,
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
        rule_code=WORKER_HEARTBEAT_TIMEOUT_RULE,
        helper_name="scan_worker_heartbeat_timeouts",
        target_status="running",
        description="Fail jobs whose owning worker heartbeat has stopped.",
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
    worker_pid: int = 0
    run_id: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    retry_count: int = 0
    max_retries: int = 0
    lease_until: float = 0.0
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    max_idle_seconds: float = 0.0
    heartbeat_timeout_seconds: float = 0.0
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
    error_code: str = ""
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
    db_connection_health: dict[str, Any] = field(default_factory=dict)

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
            "db_connection_health": dict(self.db_connection_health),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


@runtime_checkable
class WatchdogStoreProtocol(Protocol):
    def scan_expired_running_leases(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_stale_progress(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
        ...

    def scan_worker_heartbeat_timeouts(self, *, now: float, limit: int | None = None) -> Iterable[Mapping[str, Any]]:
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
