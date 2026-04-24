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
from automation_business_scaffold.business.workflows.achieve import build_refresh_current_competitor_table_workflow


class RefreshCurrentCompetitorTableTask(BaseWorkflowTask):
    name = "refresh_current_competitor_table"
    description = (
        "Refresh the current Feishu competitor table by running cleanup, scanning pending rows, "
        "queueing browser updates, and emitting one final summary notification."
    )

    def build_workflow(self, params: dict[str, Any]):
        run_mode = str(params.get("run_mode", "draft"))
        return build_refresh_current_competitor_table_workflow(
            run_mode=run_mode,
            control_action=str(params.get("control_action", "run") or "run"),
        )

    def execute_workflow_step(self, context) -> FrameworkResult:
        step_id = str(context.step.step_id)

        if step_id == "orchestrate_refresh_current_competitor_table":
            payload = refresh_flow.run_refresh_current_competitor_table(context.params)
            return ok_result(payload, default_message="Refreshed the current competitor table.")

        if step_id == "submit_refresh_request":
            payload = refresh_flow.submit_refresh_current_competitor_table(context.params)
            return ok_result(payload, default_message="Submitted the current competitor table refresh request.")

        if step_id == "plan_refresh_work":
            payload = refresh_flow.execute_executor_once(params_with_request_id(context, "submit_refresh_request"))
            return ok_result(payload, default_message="Planned refresh cleanup, scan, and browser update work.")

        if step_id == "run_refresh_browser_updates":
            payload = run_browser_loop_if_waiting(
                context,
                request_step_id="submit_refresh_request",
                previous_step_id="plan_refresh_work",
                message="No refresh browser work is waiting.",
                browser_loop=refresh_flow.run_phase1_browser_runloop,
            )
            return ok_result(payload, default_message="Ran queued browser updates for the refresh request.")

        if step_id == "finalize_refresh_summary":
            payload = run_executor_or_load_status(
                context,
                request_step_id="submit_refresh_request",
                previous_step_id="run_refresh_browser_updates",
                status_loader=refresh_flow.get_refresh_current_competitor_table_status,
                executor_once=refresh_flow.execute_executor_once,
            )
            return ok_result(payload, default_message="Finalized the refresh request summary.")

        if step_id == "dispatch_refresh_outbox":
            payload = refresh_flow.run_phase1_outbox_dispatcher(loop_params(context))
            return ok_result(payload, default_message="Dispatched refresh summary notifications.")

        if step_id == "load_refresh_result":
            payload = refresh_flow.get_refresh_current_competitor_table_status(
                params_with_request_id(context, "submit_refresh_request")
            )
            return ok_result(payload, default_message="Loaded the final refresh request result.")

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")
