from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_feishu_single_sync_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="tiktok_feishu_single_sync_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="sync_single_url",
                action=StepAction(type="sync_single_url"),
                postconditions=["result_data_exists:status"],
                outputs=[
                    "status",
                    "record_id",
                    "product_url",
                    "product_id",
                    "fields",
                    "duplicate_reason",
                    "existing_record_id",
                ],
                artifacts={"state_dump": True},
            ),
        ],
    )
