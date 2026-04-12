from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskRequestRecord:
    request_id: str
    task_name: str
    resource_code: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    requested_by: str = ""
    idempotency_key: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskExecutionRecord:
    execution_id: str
    request_id: str
    task_name: str
    resource_code: str
    status: str
    queue_seq: int
    worker_id: str = ""
    run_id: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_text: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    heartbeat_at: float = 0.0

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


@dataclass(frozen=True, slots=True)
class ControlledExecutionSnapshot:
    request: TaskRequestRecord
    execution: TaskExecutionRecord
    lease: ResourceLeaseRecord | None = None
    queue_position: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "execution": self.execution.to_dict(),
            "lease": self.lease.to_dict() if self.lease is not None else {},
            "queue_position": self.queue_position,
        }
