from __future__ import annotations

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


__all__ = [
    "FORMAL_TASK_WORKFLOW_ACTION_TYPE",
    "FORMAL_TASK_WORKFLOW_OUTPUTS",
    "FORMAL_TASK_WORKFLOW_STEP_ID",
    "build_formal_task_workflow",
]
