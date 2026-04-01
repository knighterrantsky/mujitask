from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import (
    build_batch_sync_summary,
    collect_batch_sync_products,
    filter_batch_sync_rows,
    load_batch_sync_records,
    upload_batch_sync_artifacts,
    write_back_batch_sync_rows,
)
from automation_business_scaffold.workflows import build_tiktok_feishu_batch_sync_workflow


class TikTokFeishuBatchSyncTask(BaseWorkflowTask):
    name = "tiktok_feishu_batch_sync"
    description = (
        "Read TikTok product rows from Feishu, collect stage-1 product data via browser, "
        "and write results back to the same rows."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_tiktok_feishu_batch_sync_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        trace_id = str(context.params.get("trace_id", self.name))

        if context.step.step_id == "load_records":
            payload = load_batch_sync_records(context.params)
            return FrameworkResult.ok(
                message="Loaded Feishu rows for TikTok batch sync.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "filter_target_rows":
            records = context.get_step_output("load_records").get("records", [])
            payload = filter_batch_sync_rows(records, context.params)
            return FrameworkResult.ok(
                message="Selected target rows for TikTok stage-1 sync.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "collect_tiktok_stage1":
            rows = context.get_step_output("filter_target_rows").get("target_rows", [])
            payload = collect_batch_sync_products(rows, context.params)
            return FrameworkResult.ok(
                message="Collected TikTok stage-1 product data via browser.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "upload_artifacts":
            items = context.get_step_output("collect_tiktok_stage1").get("items", [])
            payload = upload_batch_sync_artifacts(items, context.params)
            return FrameworkResult.ok(
                message="Prepared or uploaded TikTok sync artifacts.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "write_back_rows":
            items = context.get_step_output("upload_artifacts").get("items", [])
            payload = write_back_batch_sync_rows(items, context.params)
            return FrameworkResult.ok(
                message="Prepared or executed Feishu row updates.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "emit_summary":
            filtered_items = context.get_step_output("filter_target_rows").get("items", [])
            written_items = context.get_step_output("write_back_rows").get("items", [])
            payload = build_batch_sync_summary(filtered_items, written_items, context.params)
            return FrameworkResult.ok(
                message="Built TikTok batch sync summary.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
