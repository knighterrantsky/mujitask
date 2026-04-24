from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult
from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.business.workflows.runtime_workflow_shell import (
    FORMAL_TASK_WORKFLOW_STEP_ID,
)


def ok_result(payload: dict[str, Any], *, default_message: str) -> FrameworkResult:
    return FrameworkResult.ok(
        message=str(payload.get("message", "") or default_message),
        data=payload,
        metadata={"artifacts_payload": {"state_dump": payload}},
    )


class RuntimeTaskShell(BaseWorkflowTask):
    success_message = "Processed the runtime task request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        raise NotImplementedError

    def run_runtime_request(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def build_workflow(self, params: dict[str, Any]) -> WorkflowSpec:
        run_mode = str(params.get("run_mode", "full_auto") or "full_auto")
        control_action = str(params.get("control_action", "submit") or "submit")
        return self.build_runtime_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != FORMAL_TASK_WORKFLOW_STEP_ID:
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = self.run_runtime_request(dict(context.params))
        return ok_result(payload, default_message=self.success_message)


__all__ = [
    "RuntimeTaskShell",
    "ok_result",
]
