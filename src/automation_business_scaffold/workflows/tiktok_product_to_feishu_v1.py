from __future__ import annotations

from automation_framework.runtime import StepAction, StepDefinition, WorkflowSpec


def build_tiktok_product_to_feishu_workflow(*, run_mode: str = "draft") -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="tiktok_product_to_feishu_v1",
        run_mode=run_mode,
        steps=[
            StepDefinition(
                step_id="fetch_tiktok_product",
                action=StepAction(type="fetch_tiktok_product"),
                postconditions=["result_data_exists:tiktok_product.title"],
                outputs=["tiktok_product"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="download_tiktok_product_image",
                action=StepAction(type="download_tiktok_product_image"),
                preconditions=["step_output_exists:fetch_tiktok_product.tiktok_product.title"],
                postconditions=["result_data_exists:tiktok_product_with_image.main_image_local_path"],
                outputs=["tiktok_product_with_image"],
                artifacts={"state_dump": True},
            ),
            StepDefinition(
                step_id="build_feishu_record",
                action=StepAction(type="build_feishu_record"),
                preconditions=[
                    "step_output_exists:download_tiktok_product_image.tiktok_product_with_image.main_image_local_path"
                ],
                postconditions=["result_data_exists:feishu_record.logical_fields.main_image_local_path"],
                outputs=["feishu_record"],
                artifacts={"state_dump": True},
            ),
        ],
    )
