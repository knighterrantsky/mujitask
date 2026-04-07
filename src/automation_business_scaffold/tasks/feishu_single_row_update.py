from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_feishu_single_row_update
from automation_business_scaffold.workflows import build_feishu_single_row_update_workflow


class FeishuSingleRowUpdateTask(BaseWorkflowTask):
    name = "feishu_single_row_update"
    description = (
        "Update one Feishu competitor row by fetching TikTok fields plus FastMoss screenshot and sales metrics."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_feishu_single_row_update_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "update_single_row":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_feishu_single_row_update(context.params)
        return FrameworkResult.ok(
            message="Updated one Feishu competitor row.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
