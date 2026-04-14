from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_feishu_clear_row_by_url
from automation_business_scaffold.workflows import build_feishu_clear_row_by_url_workflow


class FeishuClearRowByUrlTask(BaseWorkflowTask):
    name = "feishu_clear_row_by_url"
    description = (
        "Find one Feishu competitor row by 产品链接 and clear every other field for testing reset flows."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_feishu_clear_row_by_url_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "clear_row_by_url":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_feishu_clear_row_by_url(context.params)
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Cleared one Feishu competitor row by URL."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
