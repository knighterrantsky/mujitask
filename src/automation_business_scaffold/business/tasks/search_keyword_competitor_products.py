from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

from automation_business_scaffold.business.flows import run_search_keyword_competitor_products
from automation_business_scaffold.business.workflows import build_search_keyword_competitor_products_workflow


class SearchKeywordCompetitorProductsTask(BaseWorkflowTask):
    name = "search_keyword_competitor_products"
    description = (
        "Search FastMoss by keyword, insert new Feishu seed rows, queue browser detail updates, "
        "and emit one final summary notification."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_search_keyword_competitor_products_workflow(run_mode=run_mode)

    def execute_workflow_step(self, context) -> FrameworkResult:
        if context.step.step_id != "orchestrate_search_keyword_competitor_products":
            raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
        payload = run_search_keyword_competitor_products(context.params)
        return FrameworkResult.ok(
            message=str(payload.get("message", "") or "Queued keyword competitor search."),
            data=payload,
            metadata={"artifacts_payload": {"state_dump": payload}},
        )
