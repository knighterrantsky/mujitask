from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_search_keyword_competitor_products_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
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
