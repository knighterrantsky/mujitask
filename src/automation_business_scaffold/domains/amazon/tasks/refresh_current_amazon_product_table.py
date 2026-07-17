from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.control_plane.executor.runner import (
    run_refresh_current_amazon_product_table_request,
)
from automation_business_scaffold.domains.amazon.workflows import (
    build_refresh_current_amazon_product_table_workflow,
)


TASK_CODE = "refresh_current_amazon_product_table"


class RefreshCurrentAmazonProductTableTask(RuntimeTaskShell):
    name = TASK_CODE
    description = "Collect Amazon竞品表 rows whose 采集标签 is T."
    success_message = "Submitted the Amazon competitor-table batch request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_refresh_current_amazon_product_table_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_refresh_current_amazon_product_table_request(dict(params))


__all__ = ["RefreshCurrentAmazonProductTableTask", "TASK_CODE"]
