from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_single_step_workflow(
    *,
    workflow_id: str,
    run_mode: str,
    step_id: str,
    action_type: str,
    effects: list[str] | None = None,
    postconditions: list[str] | None = None,
    outputs: list[str] | None = None,
) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id=step_id,
                action=StepAction(type=action_type),
                effects=effects or [],
                postconditions=postconditions or [],
                outputs=outputs or [],
                artifacts={"state_dump": True},
            )
        ],
    )
