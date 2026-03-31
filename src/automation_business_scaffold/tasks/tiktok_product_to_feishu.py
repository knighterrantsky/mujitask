from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import (
    build_feishu_bitable_record,
    download_tiktok_product_main_image,
    fetch_tiktok_product_record,
)
from automation_business_scaffold.models import TikTokProductRecord
from automation_business_scaffold.validators import (
    validate_tiktok_product_record,
    validate_tiktok_product_url,
)
from automation_business_scaffold.workflows import build_tiktok_product_to_feishu_workflow


class TikTokProductToFeishuTask(BaseWorkflowTask):
    name = "tiktok_product_to_feishu"
    description = (
        "Fetch a TikTok Shop product page and prepare Feishu Bitable fields for the item."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_tiktok_product_to_feishu_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        trace_id = str(context.params.get("trace_id", self.name))
        product_url = str(context.params.get("product_url") or context.params.get("url") or "").strip()

        if context.step.step_id == "fetch_tiktok_product":
            validate_tiktok_product_url(product_url)
            product = fetch_tiktok_product_record(product_url)
            validate_tiktok_product_record(product)
            product_data = product.to_dict()

            return FrameworkResult.ok(
                message="Fetched TikTok Shop product data.",
                data={"tiktok_product": product_data},
                metadata={
                    "artifacts_payload": {
                        "state_dump": {
                            "trace_id": trace_id,
                            "step": context.step.step_id,
                            "tiktok_product": product_data,
                        }
                    }
                },
            )

        if context.step.step_id == "download_tiktok_product_image":
            previous = context.get_step_output("fetch_tiktok_product").get("tiktok_product", {})
            product = TikTokProductRecord.from_dict(previous)
            validate_tiktok_product_record(product)
            product_with_image = download_tiktok_product_main_image(product)
            validate_tiktok_product_record(product_with_image, require_local_image=True)
            product_data = product_with_image.to_dict()

            return FrameworkResult.ok(
                message="Downloaded TikTok product main image to local file.",
                data={"tiktok_product_with_image": product_data},
                metadata={
                    "artifacts_payload": {
                        "state_dump": {
                            "trace_id": trace_id,
                            "step": context.step.step_id,
                            "tiktok_product_with_image": product_data,
                        }
                    }
                },
            )

        if context.step.step_id == "build_feishu_record":
            previous = (
                context.get_step_output("download_tiktok_product_image").get("tiktok_product_with_image", {})
            )
            product = TikTokProductRecord.from_dict(previous)
            validate_tiktok_product_record(product, require_local_image=True)

            raw_mapping = context.params.get("field_mapping")
            if raw_mapping is not None and not isinstance(raw_mapping, dict):
                raise ValueError("field_mapping must be an object when provided")
            field_mapping = (
                {str(key): str(value) for key, value in raw_mapping.items()}
                if isinstance(raw_mapping, dict)
                else None
            )

            feishu_record = build_feishu_bitable_record(product, field_mapping=field_mapping)
            return FrameworkResult.ok(
                message="Prepared Feishu Bitable fields from TikTok product data.",
                data={"feishu_record": feishu_record},
                metadata={
                    "artifacts_payload": {
                        "state_dump": {
                            "trace_id": trace_id,
                            "step": context.step.step_id,
                            "feishu_record": feishu_record,
                        }
                    }
                },
            )

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
