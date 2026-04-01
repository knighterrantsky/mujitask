from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import (
    build_batch_sync_summary,
    filter_batch_sync_rows,
    load_batch_sync_records,
    process_batch_sync_rows,
)
from automation_business_scaffold.workflows import build_tiktok_feishu_batch_sync_workflow


class TikTokFeishuBatchSyncTask(BaseWorkflowTask):
    name = "tiktok_feishu_batch_sync"
    description = (
        "Read pre-cleaned TikTok product rows from Feishu, skip rows whose stage-1 fields are already filled, "
        "and process each incomplete row one-by-one via browser collection, attachment upload, and direct write-back."
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

        if context.step.step_id == "process_target_rows":
            rows = context.get_step_output("filter_target_rows").get("target_rows", [])
            payload = process_batch_sync_rows(rows, context.params)
            return FrameworkResult.ok(
                message="Processed TikTok stage-1 rows one-by-one.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        if context.step.step_id == "emit_summary":
            filtered_items = context.get_step_output("filter_target_rows").get("items", [])
            written_items = context.get_step_output("process_target_rows").get("items", [])
            payload = build_batch_sync_summary(filtered_items, written_items, context.params)
            return FrameworkResult.ok(
                message="Built TikTok batch sync summary.",
                data=payload,
                metadata={"artifacts_payload": {"state_dump": {"trace_id": trace_id, **payload}}},
            )

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
