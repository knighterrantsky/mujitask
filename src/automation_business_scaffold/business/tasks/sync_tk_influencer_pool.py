from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

import automation_business_scaffold.business.flows.refresh_current_competitor_table_flow as runtime_flow
from automation_business_scaffold.business.tasks.workflow_step_helpers import ok_result
from automation_business_scaffold.business.workflows import build_sync_tk_influencer_pool_workflow


class SyncTKInfluencerPoolTask(BaseWorkflowTask):
    name = "sync_tk_influencer_pool"
    description = (
        "Synchronize pending competitor products into the TK influencer pool via FastMoss HTTP APIs."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_sync_tk_influencer_pool_workflow(
            run_mode=run_mode,
            control_action=str(params.get("control_action", "run") or "run"),
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "orchestrate_sync_tk_influencer_pool":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = runtime_flow.run_sync_tk_influencer_pool_request(context.params)
        return ok_result(payload, default_message="Processed the TK influencer pool sync request.")
