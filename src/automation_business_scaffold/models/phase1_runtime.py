from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Phase1TaskRequestRecord:
    request_id: str
    project_code: str
    task_code: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    current_stage: str = ""
    trigger_mode: str = "manual"
    source_channel_code: str = ""
    source_session_id: str = ""
    reply_target: str = ""
    requested_by: str = ""
    idempotency_key: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    stage_cursor: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""
    child_total_count: int = 0
    child_terminal_count: int = 0
    child_success_count: int = 0
    child_failed_count: int = 0
    child_skipped_count: int = 0
    worker_id: str = ""
    lease_until: float = 0.0
    heartbeat_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class Phase1TaskExecutionRecord:
    execution_id: str
    request_id: str
    item_code: str
    workflow_code: str
    business_key: str
    dedupe_key: str
    resource_code: str
    status: str
    queue_seq: int
    available_at: float = 0.0
    worker_id: str = ""
    attempt_count: int = 0
    max_attempts: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""
    run_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    heartbeat_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NotificationOutboxRecord:
    outbox_id: str
    channel_code: str
    event_type: str
    ref_type: str
    ref_id: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    reply_target: str = ""
    dedupe_key: str = ""
    retry_count: int = 0
    max_retry_count: int = 10
    next_retry_at: float = 0.0
    worker_id: str = ""
    lease_until: float = 0.0
    heartbeat_at: float = 0.0
    last_error_text: str = ""
    sent_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
