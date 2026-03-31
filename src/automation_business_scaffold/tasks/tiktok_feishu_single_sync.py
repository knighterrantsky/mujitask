from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_tiktok_feishu_single_sync
from automation_business_scaffold.workflows import build_tiktok_feishu_single_sync_workflow


class TikTokFeishuSingleSyncTask(BaseWorkflowTask):
    name = "tiktok_feishu_single_sync"
    description = (
        "Fetch one TikTok Shop product URL and insert one Feishu Bitable row; skip if "
        "the URL or SKU already exists."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_tiktok_feishu_single_sync_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "sync_single_url":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")

        payload = run_tiktok_feishu_single_sync(context.params)
        trace_id = str(context.params.get("trace_id", self.name))

        return FrameworkResult.ok(
            message="Processed one TikTok product URL and synchronized it to Feishu.",
            data=payload,
            metadata={
                "artifacts_payload": {
                    "state_dump": {
                        "trace_id": trace_id,
                        "step": context.step.step_id,
                        **payload,
                    }
                }
            },
        )
