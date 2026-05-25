from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.control_plane.executor.runner import (
    run_tiktok_influencer_outreach_sync_request,
)
from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.domains.tiktok.workflows import build_tiktok_influencer_outreach_sync_workflow


class TikTokInfluencerOutreachSyncTask(RuntimeTaskShell):
    name = "tiktok_influencer_outreach_sync"
    description = "Submit, inspect, or advance the influencer outreach sync runtime request."
    success_message = "Processed the influencer outreach sync runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_tiktok_influencer_outreach_sync_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_tiktok_influencer_outreach_sync_request(dict(params))
