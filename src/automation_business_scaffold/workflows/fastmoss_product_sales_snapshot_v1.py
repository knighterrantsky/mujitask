from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_fastmoss_product_sales_snapshot_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="fastmoss_product_sales_snapshot_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="fetch_fastmoss_sales_snapshot",
                action=StepAction(type="fetch_fastmoss_sales_snapshot"),
                postconditions=["result_data_exists:fastmoss_snapshot.product_id"],
                outputs=["fastmoss_snapshot"],
                artifacts={"state_dump": True},
            )
        ],
    )
