from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.control_plane.executor.runner import (
    run_refresh_competitor_row_by_url_request,
)
from automation_business_scaffold.domains.tiktok.workflows import (
    build_refresh_competitor_row_by_url_workflow,
)

TASK_CODE = "refresh_competitor_row_by_url"


class RefreshCompetitorRowByUrlTask(RuntimeTaskShell):
    name = TASK_CODE
    description = "Submit, inspect, or advance a competitor row refresh runtime request located by product URL."
    success_message = "Processed the competitor row refresh by URL runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_refresh_competitor_row_by_url_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_refresh_competitor_row_by_url_request(dict(params))
