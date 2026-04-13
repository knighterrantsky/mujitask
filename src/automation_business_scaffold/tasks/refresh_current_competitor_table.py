from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.flows import run_refresh_current_competitor_table
from automation_business_scaffold.workflows import build_refresh_current_competitor_table_workflow


class RefreshCurrentCompetitorTableTask(BaseWorkflowTask):
    name = "refresh_current_competitor_table"
    description = (
        "Refresh the current Feishu competitor table by running cleanup, scanning pending rows, "
        "queueing browser updates, and emitting one final summary notification."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_refresh_current_competitor_table_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "orchestrate_refresh_current_competitor_table":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_refresh_current_competitor_table(context.params)
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Refreshed the current competitor table."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
