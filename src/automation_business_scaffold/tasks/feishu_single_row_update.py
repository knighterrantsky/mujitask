from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import (
    run_controlled_feishu_single_row_update,
    run_feishu_single_row_update,
)
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
        payload = (
            run_controlled_feishu_single_row_update(context.params)
            if _should_use_controlled_execution(context.params)
            else run_feishu_single_row_update(context.params)
        )
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Updated one Feishu competitor row."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )


def _should_use_controlled_execution(params: dict[str, Any]) -> bool:
    if "control_action" in params:
        return True
    if any(str(key).startswith("execution_control_") for key in params):
        return True
    raw_value = params.get("execution_control_enabled")
    if isinstance(raw_value, bool):
        return raw_value
    normalized = str(raw_value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}
