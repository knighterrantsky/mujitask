from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.business.flows.runtime_orchestrator import (
    run_tiktok_fastmoss_product_ingest_request,
)
from automation_business_scaffold.business.workflows import build_tiktok_fastmoss_product_ingest_workflow

from .runtime_task_shell import RuntimeTaskShell


class TikTokFastMossProductIngestTask(RuntimeTaskShell):
    name = "tiktok_fastmoss_product_ingest"
    description = "Submit, inspect, or advance the TikTok plus FastMoss product ingest runtime request."
    success_message = "Processed the TikTok plus FastMoss product ingest runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_tiktok_fastmoss_product_ingest_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_tiktok_fastmoss_product_ingest_request(dict(params))
