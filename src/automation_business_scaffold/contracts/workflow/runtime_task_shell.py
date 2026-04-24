from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult
from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec

FORMAL_TASK_WORKFLOW_STEP_ID = "dispatch_task_request"
FORMAL_TASK_WORKFLOW_ACTION_TYPE = "dispatch_task_request"
FORMAL_TASK_WORKFLOW_OUTPUTS = [
    "request_id",
    "task_code",
    "request_status",
    "current_stage",
    "summary",
    "result",
    "error",
    "child_total_count",
    "child_terminal_count",
    "child_success_count",
    "child_failed_count",
    "child_skipped_count",
    "task_request",
    "executions",
    "api_worker_jobs",
    "api_worker_job_summary",
    "outbox",
    "item",
    "items",
    "processed_count",
    "success_count",
    "failed_count",
    "daemon_status",
    "dispatcher_status",
    "message",
]


def build_formal_task_workflow(*, workflow_code: str, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_code,
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id=FORMAL_TASK_WORKFLOW_STEP_ID,
                action=StepAction(type=FORMAL_TASK_WORKFLOW_ACTION_TYPE),
                effects=["submit", "write"],
                postconditions=["result_data_exists:request_status"],
                outputs=list(FORMAL_TASK_WORKFLOW_OUTPUTS),
                artifacts={"state_dump": True},
            )
        ],
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
    "FORMAL_TASK_WORKFLOW_ACTION_TYPE",
    "FORMAL_TASK_WORKFLOW_OUTPUTS",
    "FORMAL_TASK_WORKFLOW_STEP_ID",
    "RuntimeTaskShell",
    "build_formal_task_workflow",
    "ok_result",
]
