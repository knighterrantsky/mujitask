from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_sync_tk_influencer_pool_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "run",
) -> WorkflowSpec:
    del control_action
    return WorkflowSpec(
        workflow_id="sync_tk_influencer_pool_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="orchestrate_sync_tk_influencer_pool",
                action=StepAction(type="orchestrate_sync_tk_influencer_pool"),
                effects=["write", "upload"],
                postconditions=["result_data_exists:summary.total"],
                outputs=[
                    "summary",
                    "item",
                    "items",
                    "failed_items",
                    "processed_count",
                    "success_count",
                    "failed_count",
                    "daemon_status",
                    "parent_updates",
                    "worker_result",
                    "request_id",
                    "request_status",
                    "current_stage",
                    "child_total_count",
                    "child_terminal_count",
                    "result",
                    "outbox",
                ],
                artifacts={"state_dump": True},
            )
        ],
    )
