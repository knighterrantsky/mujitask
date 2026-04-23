from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_product_link_cleanup_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="tiktok_product_link_cleanup_v1",
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
                step_id="normalize_urls",
                action=StepAction(type="normalize_urls"),
                preconditions=["step_output_exists:load_records.records"],
                postconditions=["result_data_exists:items"],
                outputs=["items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="delete_duplicate_rows",
                action=StepAction(type="delete_duplicate_rows"),
                effects=["write"],
                preconditions=["step_output_exists:normalize_urls.items"],
                postconditions=["result_data_exists:deletion_results"],
                outputs=["deletion_results"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="write_back_normalized_urls",
                action=StepAction(type="write_back_normalized_urls"),
                effects=["write"],
                preconditions=["step_output_exists:normalize_urls.items"],
                postconditions=["result_data_exists:update_results"],
                outputs=["update_results"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="emit_summary",
                action=StepAction(type="emit_summary"),
                preconditions=[
                    "step_output_exists:normalize_urls.items",
                    "step_output_exists:delete_duplicate_rows.deletion_results",
                    "step_output_exists:write_back_normalized_urls.update_results",
                ],
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "items", "settings"],
                artifacts={"state_dump": True},
            ),
        ],
    )
