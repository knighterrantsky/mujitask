from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_fastmoss_keyword_candidate_discovery_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="fastmoss_keyword_candidate_discovery_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="discover_keyword_candidates",
                action=StepAction(type="discover_keyword_candidates"),
                postconditions=["result_data_exists:summary.total"],
                outputs=["summary", "items", "target_items", "settings"],
                artifacts={"state_dump": True},
            )
        ],
    )
