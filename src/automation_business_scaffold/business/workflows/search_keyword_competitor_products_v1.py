from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_search_keyword_competitor_products_workflow(
    *,
    run_mode: str = "draft",
    control_action: str = "run",
) -> WorkflowSpec:
    normalized_action = str(control_action or "run").strip().lower()
    if normalized_action and normalized_action != "run":
        return WorkflowSpec(
            workflow_id="search_keyword_competitor_products_v1",
            run_mode=run_mode,
            steps=[
                StepDefinition(
                    step_id="orchestrate_search_keyword_competitor_products",
                    action=StepAction(type="orchestrate_search_keyword_competitor_products"),
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
        workflow_id="search_keyword_competitor_products_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="submit_keyword_request",
                action=StepAction(type="submit_keyword_request"),
                effects=["write"],
                postconditions=["result_data_exists:request_id"],
                outputs=["request_id", "request_status", "summary"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="enqueue_keyword_discovery",
                action=StepAction(type="enqueue_keyword_discovery"),
                effects=["write"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
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
                step_id="run_keyword_discovery_browser",
                action=StepAction(type="run_keyword_discovery_browser"),
                effects=["write", "upload"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:processed_count"],
                outputs=["processed_count", "success_count", "failed_count", "items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="process_keyword_candidates",
                action=StepAction(type="process_keyword_candidates"),
                effects=["write"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:request_id"],
                outputs=["summary", "request_id", "request_status", "current_stage", "result", "executions"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="run_keyword_detail_updates",
                action=StepAction(type="run_keyword_detail_updates"),
                effects=["write", "upload"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:processed_count"],
                outputs=["processed_count", "success_count", "failed_count", "items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="finalize_keyword_summary",
                action=StepAction(type="finalize_keyword_summary"),
                effects=["write"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:request_status"],
                outputs=["summary", "request_id", "request_status", "current_stage", "outbox"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="dispatch_keyword_outbox",
                action=StepAction(type="dispatch_keyword_outbox"),
                effects=["write"],
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:processed_count"],
                outputs=["processed_count", "success_count", "failed_count", "items"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="load_keyword_result",
                action=StepAction(type="load_keyword_result"),
                preconditions=["step_output_exists:submit_keyword_request.request_id"],
                postconditions=["result_data_exists:request_status"],
                outputs=["summary", "item", "items", "request_id", "request_status", "current_stage", "outbox"],
                artifacts={"state_dump": True},
            ),
        ],
    )
