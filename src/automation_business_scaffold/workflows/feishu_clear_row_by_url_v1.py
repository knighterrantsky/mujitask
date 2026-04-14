from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_feishu_clear_row_by_url_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="feishu_clear_row_by_url_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="clear_row_by_url",
                action=StepAction(type="clear_row_by_url"),
                effects=["write"],
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "item", "items"],
                artifacts={"state_dump": True},
            )
        ],
    )
