from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_feishu_batch_sync_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="tiktok_feishu_batch_sync_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="load_records",
                action=StepAction(type="load_records"),
                postconditions=["result_data_exists:records"],
                outputs=["records"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="filter_target_rows",
                action=StepAction(type="filter_target_rows"),
                preconditions=["step_output_exists:load_records.records"],
                postconditions=["result_data_exists:target_rows"],
                outputs=["items", "target_rows"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="process_target_rows",
                action=StepAction(type="process_target_rows"),
                effects=["upload", "write"],
                preconditions=["step_output_exists:filter_target_rows.target_rows"],
                postconditions=["result_data_exists:items"],
                outputs=["items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="emit_summary",
                action=StepAction(type="emit_summary"),
                preconditions=[
                    "step_output_exists:filter_target_rows.items",
                    "step_output_exists:process_target_rows.items",
                ],
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "items", "settings"],
                artifacts={"state_dump": True},
            ),
        ],
    )
