from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

import automation_business_scaffold.business.flows.refresh_current_competitor_table_flow as refresh_flow
from automation_business_scaffold.business.workflows import build_refresh_current_competitor_table_workflow


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
            return _ok(payload, default_message="Refreshed the current competitor table.")

        if step_id == "submit_refresh_request":
            payload = refresh_flow.submit_refresh_current_competitor_table(context.params)
            return _ok(payload, default_message="Submitted the current competitor table refresh request.")

        if step_id == "plan_refresh_work":
            payload = refresh_flow.execute_executor_once(_params_with_request_id(context, "submit_refresh_request"))
            return _ok(payload, default_message="Planned refresh cleanup, scan, and browser update work.")

        if step_id == "run_refresh_browser_updates":
            payload = _run_browser_loop_if_waiting(
                context,
                previous_step_id="plan_refresh_work",
            )
            return _ok(payload, default_message="Ran queued browser updates for the refresh request.")

        if step_id == "finalize_refresh_summary":
            payload = _run_executor_or_load_status(
                context,
                previous_step_id="run_refresh_browser_updates",
                status_loader=refresh_flow.get_refresh_current_competitor_table_status,
            )
            return _ok(payload, default_message="Finalized the refresh request summary.")

        if step_id == "dispatch_refresh_outbox":
            payload = refresh_flow.run_phase1_outbox_dispatcher(_loop_params(context))
            return _ok(payload, default_message="Dispatched refresh summary notifications.")

        if step_id == "load_refresh_result":
            payload = refresh_flow.get_refresh_current_competitor_table_status(
                _params_with_request_id(context, "submit_refresh_request")
            )
            return _ok(payload, default_message="Loaded the final refresh request result.")

        raise RuntimeError(f"Unknown workflow step: {context.step.step_id}")


def _ok(payload: dict[str, Any], *, default_message: str) -> FrameworkResult:
    return FrameworkResult.ok(
        message=str(payload.get("message", "") or default_message),
        data=payload,
        metadata={"artifacts_payload": {"state_dump": payload}},
    )


def _request_id_from_step(context: Any, step_id: str) -> str:
    return str(context.get_step_output(step_id).get("request_id", "") or "").strip()


def _params_with_request_id(context: Any, step_id: str) -> dict[str, Any]:
    params = dict(context.params)
    request_id = _request_id_from_step(context, step_id)
    if request_id:
        params["request_id"] = request_id
    return params


def _loop_params(context: Any) -> dict[str, Any]:
    params = dict(context.params)
    params.setdefault("execution_control_stop_when_idle", True)
    params.setdefault("execution_control_max_idle_cycles", 1)
    return params


def _skipped_loop_payload(context: Any, *, previous_step_id: str, message: str) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    return {
        "control_action": str(context.step.action.type),
        "daemon_status": "skipped",
        "processed_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "summary": {"total": 0, "counts": {}},
        "item": {},
        "items": [],
        "request_id": _request_id_from_step(context, "submit_refresh_request"),
        "request_status": str(previous.get("request_status", "") or ""),
        "current_stage": str(previous.get("current_stage", "") or ""),
        "message": message,
    }


def _run_browser_loop_if_waiting(context: Any, *, previous_step_id: str) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") != "waiting_children":
        return _skipped_loop_payload(
            context,
            previous_step_id=previous_step_id,
            message="No refresh browser work is waiting.",
        )
    return refresh_flow.run_phase1_browser_runloop(_loop_params(context))


def _run_executor_or_load_status(
    context: Any,
    *,
    previous_step_id: str,
    status_loader: Any,
) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") == "success":
        return status_loader(_params_with_request_id(context, "submit_refresh_request"))
    payload = refresh_flow.execute_executor_once(_params_with_request_id(context, "submit_refresh_request"))
    if not str(payload.get("request_id", "") or "").strip():
        return status_loader(_params_with_request_id(context, "submit_refresh_request"))
    return payload
