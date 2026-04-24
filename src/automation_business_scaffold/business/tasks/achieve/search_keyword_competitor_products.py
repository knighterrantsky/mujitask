from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

import automation_business_scaffold.business.flows.refresh_current_competitor_table_flow as refresh_flow
from automation_business_scaffold.business.tasks.workflow_step_helpers import (
    loop_params,
    ok_result,
    params_with_request_id,
    run_browser_loop_if_waiting,
    run_executor_or_load_status,
)
from automation_business_scaffold.business.workflows.achieve import build_search_keyword_competitor_products_workflow


class SearchKeywordCompetitorProductsTask(BaseWorkflowTask):
    name = "search_keyword_competitor_products"
    description = (
        "Search FastMoss by keyword, insert new Feishu seed rows, queue browser detail updates, "
        "and emit one final summary notification."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_search_keyword_competitor_products_workflow(
            run_mode=run_mode,
            control_action=str(params.get("control_action", "run") or "run"),
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        step_id = str(context.step.step_id)

        if step_id == "orchestrate_search_keyword_competitor_products":
            payload = refresh_flow.run_search_keyword_competitor_products(context.params)
            return ok_result(payload, default_message="Queued keyword competitor search.")

        if step_id == "submit_keyword_request":
            payload = refresh_flow.submit_search_keyword_competitor_products(context.params)
            return ok_result(payload, default_message="Submitted the keyword competitor search request.")

        if step_id == "enqueue_keyword_discovery":
            payload = refresh_flow.execute_executor_once(params_with_request_id(context, "submit_keyword_request"))
            return ok_result(payload, default_message="Queued keyword discovery browser work.")

        if step_id == "run_keyword_discovery_browser":
            payload = run_browser_loop_if_waiting(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="enqueue_keyword_discovery",
                message="No keyword discovery browser work is waiting.",
                browser_loop=refresh_flow.run_phase1_browser_runloop,
            )
            return ok_result(payload, default_message="Ran keyword discovery browser work.")

        if step_id == "process_keyword_candidates":
            payload = run_executor_or_load_status(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="run_keyword_discovery_browser",
                status_loader=refresh_flow.get_search_keyword_competitor_products_status,
                executor_once=refresh_flow.execute_executor_once,
            )
            return ok_result(payload, default_message="Inserted keyword seed rows and queued detail updates.")

        if step_id == "run_keyword_detail_updates":
            payload = run_browser_loop_if_waiting(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="process_keyword_candidates",
                message="No keyword detail browser work is waiting.",
                browser_loop=refresh_flow.run_phase1_browser_runloop,
            )
            return ok_result(payload, default_message="Ran keyword detail browser updates.")

        if step_id == "finalize_keyword_summary":
            payload = run_executor_or_load_status(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="run_keyword_detail_updates",
                status_loader=refresh_flow.get_search_keyword_competitor_products_status,
                executor_once=refresh_flow.execute_executor_once,
            )
            return ok_result(payload, default_message="Finalized the keyword competitor search summary.")

        if step_id == "dispatch_keyword_outbox":
            payload = refresh_flow.run_phase1_outbox_dispatcher(loop_params(context))
            return ok_result(payload, default_message="Dispatched keyword search notifications.")

        if step_id == "load_keyword_result":
            payload = refresh_flow.get_search_keyword_competitor_products_status(
                params_with_request_id(context, "submit_keyword_request")
            )
            return ok_result(payload, default_message="Loaded the final keyword search result.")

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
