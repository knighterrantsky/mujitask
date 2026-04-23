from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import run_feishu_single_row_update
from automation_business_scaffold.business.tasks.workflow_specs import build_single_step_workflow


class FeishuSingleRowUpdateTask(BaseWorkflowTask):
    name = "feishu_single_row_update"
    description = (
        "Update one Feishu competitor row by fetching TikTok fields plus FastMoss screenshot and sales metrics."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_single_step_workflow(
            workflow_id="feishu_single_row_update_v1",
            run_mode=run_mode,
            step_id="update_single_row",
            action_type="update_single_row",
            effects=["upload", "write"],
            postconditions=["result_data_exists:summary.total"],
            outputs=["summary", "item", "items"],
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "update_single_row":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_feishu_single_row_update(context.params)
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Updated one Feishu competitor row."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
