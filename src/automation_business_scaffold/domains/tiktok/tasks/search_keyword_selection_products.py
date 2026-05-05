from __future__ import annotations

from automation_framework.runtime import WorkflowSpec

from automation_business_scaffold.control_plane.executor.runner import (
    run_search_keyword_selection_products_request,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    SELECTION_KEYWORD_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.workflows import build_search_keyword_selection_products_workflow

from automation_business_scaffold.contracts.workflow import RuntimeTaskShell


TASK_CODE = SELECTION_KEYWORD_TASK_CODE


class SearchKeywordSelectionProductsTask(RuntimeTaskShell):
    name = TASK_CODE
    description = "Submit, inspect, or advance the keyword selection search runtime request."
    success_message = "Processed the keyword selection search runtime request."

    def build_runtime_workflow(
        self,
        *,
        run_mode: str,
        control_action: str,
    ) -> WorkflowSpec:
        return build_search_keyword_selection_products_workflow(
            run_mode=run_mode,
            control_action=control_action,
        )

    def run_runtime_request(self, params: dict[str, object]) -> dict[str, object]:
        return run_search_keyword_selection_products_request(dict(params))
