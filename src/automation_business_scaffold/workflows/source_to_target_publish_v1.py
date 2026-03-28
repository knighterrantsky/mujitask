from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_source_to_target_publish_workflow(
    *,
    run_mode: str = "draft",
    include_submit: bool = False,
) -> WorkflowSpec:
    steps = [
        StepDefinition(
            step_id="extract_source_item",
            action=StepAction(type="extract_source_item"),
            postconditions=["result_data_exists:source_item.title"],
            outputs=["source_item"],
            artifacts={"state_dump": True},
        ),
        StepDefinition(
            step_id="map_publish_payload",
            action=StepAction(type="map_publish_payload"),
            preconditions=["step_output_exists:extract_source_item.source_item.title"],
            postconditions=["result_data_exists:publish_payload.title"],
            outputs=["publish_payload"],
            artifacts={"state_dump": True},
        ),
        StepDefinition(
            step_id="fill_target_form",
            action=StepAction(type="fill_target_form"),
            preconditions=["step_output_exists:map_publish_payload.publish_payload.title"],
            postconditions=["result_data_exists:draft_form.title"],
            outputs=["draft_form"],
            effects=["write"],
            artifacts={"state_dump": True, "html_snapshot": True},
        ),
    ]

    if include_submit:
        steps.append(
            StepDefinition(
                step_id="submit_target_publish",
                action=StepAction(type="submit_target_publish"),
                preconditions=["step_output_exists:fill_target_form.draft_form.title"],
                postconditions=["result_data_exists:publish_result.status"],
                outputs=["publish_result"],
                effects=["submit"],
                artifacts={"state_dump": True, "html_snapshot": True},
            )
        )
    else:
        steps.append(
            StepDefinition(
                step_id="save_target_draft",
                action=StepAction(type="save_target_draft"),
                preconditions=["step_output_exists:fill_target_form.draft_form.title"],
                postconditions=["result_data_exists:draft_result.status"],
                outputs=["draft_result"],
                effects=["draft"],
                artifacts={"state_dump": True},
            )
        )

    return WorkflowSpec(
        workflow_id="source_to_target_publish_demo_v1",
        run_mode=run_mode,
        steps=steps,
    )

