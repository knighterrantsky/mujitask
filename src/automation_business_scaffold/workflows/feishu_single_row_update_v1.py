from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_feishu_single_row_update_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="feishu_single_row_update_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="update_single_row",
                action=StepAction(type="update_single_row"),
                effects=["upload", "write"],
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "item", "items"],
                artifacts={"state_dump": True},
            )
        ],
    )
