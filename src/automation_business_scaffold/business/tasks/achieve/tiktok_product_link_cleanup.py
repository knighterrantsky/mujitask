from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import (
    build_cleanup_summary,
    delete_cleanup_duplicates,
    load_cleanup_records,
    normalize_cleanup_records,
    write_back_cleanup_records,
)
from automation_business_scaffold.business.workflows import build_tiktok_product_link_cleanup_workflow


class TikTokProductLinkCleanupTask(BaseWorkflowTask):
    name = "tiktok_product_link_cleanup"
    description = (
        "Normalize TikTok product links from Feishu, write the normalized URL back to 产品链接, "
        "and delete duplicate rows."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_tiktok_product_link_cleanup_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        trace_id = str(context.params.get("trace_id", self.name))

        if context.step.step_id == "load_records":
            payload = load_cleanup_records(context.params)
            return FrameworkResult.ok(
                message="Loaded Feishu rows for TikTok link cleanup.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "normalize_urls":
            records = context.get_step_output("load_records").get("records", [])
            payload = normalize_cleanup_records(records, context.params)
            return FrameworkResult.ok(
                message="Normalized TikTok product links and planned duplicate deletion.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "delete_duplicate_rows":
            items = context.get_step_output("normalize_urls").get("items", [])
            payload = delete_cleanup_duplicates(items, context.params)
            return FrameworkResult.ok(
                message="Computed or executed duplicate row deletions.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "write_back_normalized_urls":
            items = context.get_step_output("normalize_urls").get("items", [])
            deletion_results = context.get_step_output("delete_duplicate_rows").get("deletion_results", [])
            payload = write_back_cleanup_records(items, deletion_results, context.params)
            return FrameworkResult.ok(
                message="Computed or executed cleanup field updates.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "emit_summary":
            items = context.get_step_output("normalize_urls").get("items", [])
            deletion_results = context.get_step_output("delete_duplicate_rows").get("deletion_results", [])
            update_results = context.get_step_output("write_back_normalized_urls").get("update_results", [])
            payload = build_cleanup_summary(items, deletion_results, update_results, context.params)
            return FrameworkResult.ok(
                message="Built cleanup summary.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
