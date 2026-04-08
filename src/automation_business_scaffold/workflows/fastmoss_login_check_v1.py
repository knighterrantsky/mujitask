from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_fastmoss_login_check_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="fastmoss_login_check_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="validate_fastmoss_login",
                action=StepAction(type="validate_fastmoss_login"),
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "item", "items"],
                artifacts={"state_dump": True},
            )
        ],
    )
