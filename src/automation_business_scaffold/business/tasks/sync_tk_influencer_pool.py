from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows.influencer_pool_sync_flow import run_sync_tk_influencer_pool
from automation_business_scaffold.business.workflows import build_sync_tk_influencer_pool_workflow


class SyncTKInfluencerPoolTask(BaseWorkflowTask):
    name = "sync_tk_influencer_pool"
    description = (
        "Synchronize pending competitor products into the TK influencer pool via FastMoss HTTP APIs."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_sync_tk_influencer_pool_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "orchestrate_sync_tk_influencer_pool":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_sync_tk_influencer_pool(context.params)
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Synchronized the TK influencer pool."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
