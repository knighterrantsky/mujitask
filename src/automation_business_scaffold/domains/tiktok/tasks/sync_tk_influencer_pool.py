from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.control_plane.executor.runner import (
    run_sync_tk_influencer_pool_request,
)
from automation_business_scaffold.domains.tiktok.workflows import build_sync_tk_influencer_pool_workflow

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell


class SyncTKInfluencerPoolTask(RuntimeTaskShell):
    name = "sync_tk_influencer_pool"
    description = "Submit, inspect, or advance the influencer pool sync runtime request."
    success_message = "Processed the influencer pool sync runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_sync_tk_influencer_pool_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_sync_tk_influencer_pool_request(dict(params))
