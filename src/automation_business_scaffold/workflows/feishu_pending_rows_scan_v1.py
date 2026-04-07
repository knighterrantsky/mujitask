from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_feishu_pending_rows_scan_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="feishu_pending_rows_scan_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="scan_pending_rows",
                action=StepAction(type="scan_pending_rows"),
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "items", "target_rows", "settings"],
                artifacts={"state_dump": True},
            )
        ],
    )
