from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import run_feishu_pending_rows_scan
from automation_business_scaffold.business.tasks.workflow_specs import build_single_step_workflow


class FeishuPendingRowsScanTask(BaseWorkflowTask):
    name = "feishu_pending_rows_scan"
    description = (
        "Scan the Feishu table and return rows whose auto-maintained fields are still incomplete."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_single_step_workflow(
            workflow_id="feishu_pending_rows_scan_v1",
            run_mode=run_mode,
            step_id="scan_pending_rows",
            action_type="scan_pending_rows",
            postconditions=["result_data_exists:summary.total"],
            outputs=["summary", "items", "target_rows", "settings"],
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "scan_pending_rows":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_feishu_pending_rows_scan(context.params)
        return FrameworkResult.ok(
            message="Scanned pending Feishu rows.",
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
