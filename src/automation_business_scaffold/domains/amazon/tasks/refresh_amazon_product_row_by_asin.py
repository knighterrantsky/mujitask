from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell
from automation_business_scaffold.control_plane.executor.runner import (
    run_refresh_amazon_product_row_by_asin_request,
)
from automation_business_scaffold.domains.amazon.workflows import (
    build_refresh_amazon_product_row_by_asin_workflow,
)


TASK_CODE = "refresh_amazon_product_row_by_asin"


class RefreshAmazonProductRowByAsinTask(RuntimeTaskShell):
    name = TASK_CODE
    description = "Collect one Amazon US product from a Feishu ASIN row and update that row."
    success_message = "Processed the Amazon product row runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_refresh_amazon_product_row_by_asin_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_refresh_amazon_product_row_by_asin_request(dict(params))


__all__ = ["RefreshAmazonProductRowByAsinTask", "TASK_CODE"]
