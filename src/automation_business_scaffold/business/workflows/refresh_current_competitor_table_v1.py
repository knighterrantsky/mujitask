from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_refresh_current_competitor_table_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "run",
) -> WorkflowSpec:
    normalized_action = str(control_action or "run").strip().lower()
    if normalized_action and normalized_action != "run":
        return WorkflowSpec(
            workflow_id="refresh_current_competitor_table_v1",
            run_mode=run_mode,
            steps=[
                StepDefinition(
                    step_id="orchestrate_refresh_current_competitor_table",
                    action=StepAction(type="orchestrate_refresh_current_competitor_table"),
                    effects=["write", "upload"],
                    postconditions=["result_data_exists:summary.total"],
                    outputs=[
                        "summary",
                        "item",
                        "items",
                        "request_id",
                        "request_status",
                        "current_stage",
                        "outbox",
                    ],
                    artifacts={"state_dump": True},
                )
            ],
        )

    return WorkflowSpec(
        workflow_id="refresh_current_competitor_table_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="submit_refresh_request",
                action=StepAction(type="submit_refresh_request"),
                effects=["write"],
                postconditions=["result_data_exists:request_id"],
                outputs=["request_id", "request_status", "summary"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="plan_refresh_work",
                action=StepAction(type="plan_refresh_work"),
                effects=["write"],
                preconditions=["step_output_exists:submit_refresh_request.request_id"],
                postconditions=["result_data_exists:request_id"],
                outputs=[
                    "summary",
                    "request_id",
                    "request_status",
                    "current_stage",
                    "executions",
                    "child_total_count",
                ],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="run_refresh_browser_updates",
                action=StepAction(type="run_refresh_browser_updates"),
                effects=["write", "upload"],
                preconditions=["step_output_exists:submit_refresh_request.request_id"],
                postconditions=["result_data_exists:processed_count"],
                outputs=["processed_count", "success_count", "failed_count", "items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="finalize_refresh_summary",
                action=StepAction(type="finalize_refresh_summary"),
                effects=["write"],
                preconditions=["step_output_exists:submit_refresh_request.request_id"],
                postconditions=["result_data_exists:request_status"],
                outputs=["summary", "request_id", "request_status", "current_stage", "outbox"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="dispatch_refresh_outbox",
                action=StepAction(type="dispatch_refresh_outbox"),
                effects=["write"],
                preconditions=["step_output_exists:submit_refresh_request.request_id"],
                postconditions=["result_data_exists:processed_count"],
                outputs=["processed_count", "success_count", "failed_count", "items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="load_refresh_result",
                action=StepAction(type="load_refresh_result"),
                preconditions=["step_output_exists:submit_refresh_request.request_id"],
                postconditions=["result_data_exists:request_status"],
                outputs=["summary", "item", "items", "request_id", "request_status", "current_stage", "outbox"],
                artifacts={"state_dump": True},
            ),
        ],
    )
