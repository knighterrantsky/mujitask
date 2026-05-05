from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeTaskRequestRecord:
    request_id: str
    project_code: str
    task_code: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    current_stage: str = ""
    progress_stage: str = ""
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
    error_type: str = ""
    error_code: str = ""
    dead_letter_reason: str = ""
    child_total_count: int = 0
    child_terminal_count: int = 0
    child_success_count: int = 0
    child_failed_count: int = 0
    child_skipped_count: int = 0
    worker_id: str = ""
    lease_until: float = 0.0
    heartbeat_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeTaskExecutionRecord:
    execution_id: str
    request_id: str
    item_code: str
    workflow_code: str
    business_key: str
    dedupe_key: str
    resource_code: str
    status: str
    queue_seq: int
    progress_stage: str = ""
    available_at: float = 0.0
    worker_id: str = ""
    worker_pid: int = 0
    attempt_count: int = 0
    max_attempts: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""
    error_type: str = ""
    error_code: str = ""
    dead_letter_reason: str = ""
    run_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    heartbeat_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    max_idle_seconds: float = 0.0
    heartbeat_timeout_seconds: float = 0.0
    progress_seq: int = 0
    progress_message: str = ""

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
    progress_stage: str = ""
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
    error_type: str = ""
    error_code: str = ""
    dead_letter_reason: str = ""
    sent_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResourceLeaseRecord:
    resource_code: str
    execution_id: str
    status: str
    lease_until: float
    heartbeat_at: float
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
