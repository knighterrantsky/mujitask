from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_feishu_batch_sync_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="tiktok_feishu_batch_sync_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="sync_batch_urls",
                action=StepAction(type="sync_batch_urls"),
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "items", "settings"],
                artifacts={"state_dump": True},
            ),
        ],
    )
