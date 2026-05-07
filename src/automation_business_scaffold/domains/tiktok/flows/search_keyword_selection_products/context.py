from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class StageContext:
    request_id: str
    task_code: str
    workflow_code: str
    stage_code: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageDecision:
    action: str
    stage_code: str
    next_stage: str = ""
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        if self.action == "waiting":
            return {
                "action": "waiting",
                "current_stage": self.stage_code,
                "message": self.message,
                "details": dict(self.details),
            }
        result: dict[str, Any] = {"action": self.action}
        if self.next_stage:
            result["next_stage"] = self.next_stage
        if self.details:
            result["details"] = dict(self.details)
        return result


def workflow_stage_context(*, request: Any, workflow: Any, stage_code: str) -> StageContext:
    return StageContext(
        request_id=str(request.request_id),
        task_code=str(request.task_code),
        workflow_code=str(workflow.workflow_code),
        stage_code=stage_code,
        payload=dict(request.payload or {}),
    )
