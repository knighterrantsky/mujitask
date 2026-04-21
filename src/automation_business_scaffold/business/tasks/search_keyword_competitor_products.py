from __future__ import annotations

from typing import Any

from automation_framework.core import BaseWorkflowTask, FrameworkResult

import automation_business_scaffold.business.flows.refresh_current_competitor_table_flow as refresh_flow
from automation_business_scaffold.business.workflows import build_search_keyword_competitor_products_workflow


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
            return _ok(payload, default_message="Queued keyword competitor search.")

        if step_id == "submit_keyword_request":
            payload = refresh_flow.submit_search_keyword_competitor_products(context.params)
            return _ok(payload, default_message="Submitted the keyword competitor search request.")

        if step_id == "enqueue_keyword_discovery":
            payload = refresh_flow.execute_executor_once(_params_with_request_id(context, "submit_keyword_request"))
            return _ok(payload, default_message="Queued keyword discovery browser work.")

        if step_id == "run_keyword_discovery_browser":
            payload = _run_browser_loop_if_waiting(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="enqueue_keyword_discovery",
                message="No keyword discovery browser work is waiting.",
            )
            return _ok(payload, default_message="Ran keyword discovery browser work.")

        if step_id == "process_keyword_candidates":
            payload = _run_executor_or_load_status(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="run_keyword_discovery_browser",
                status_loader=refresh_flow.get_search_keyword_competitor_products_status,
            )
            return _ok(payload, default_message="Inserted keyword seed rows and queued detail updates.")

        if step_id == "run_keyword_detail_updates":
            payload = _run_browser_loop_if_waiting(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="process_keyword_candidates",
                message="No keyword detail browser work is waiting.",
            )
            return _ok(payload, default_message="Ran keyword detail browser updates.")

        if step_id == "finalize_keyword_summary":
            payload = _run_executor_or_load_status(
                context,
                request_step_id="submit_keyword_request",
                previous_step_id="run_keyword_detail_updates",
                status_loader=refresh_flow.get_search_keyword_competitor_products_status,
            )
            return _ok(payload, default_message="Finalized the keyword competitor search summary.")

        if step_id == "dispatch_keyword_outbox":
            payload = refresh_flow.run_phase1_outbox_dispatcher(_loop_params(context))
            return _ok(payload, default_message="Dispatched keyword search notifications.")

        if step_id == "load_keyword_result":
            payload = refresh_flow.get_search_keyword_competitor_products_status(
                _params_with_request_id(context, "submit_keyword_request")
            )
            return _ok(payload, default_message="Loaded the final keyword search result.")

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


def _skipped_loop_payload(
    context: Any,
    *,
    request_step_id: str,
    previous_step_id: str,
    message: str,
) -> dict[str, Any]:
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
        "request_id": _request_id_from_step(context, request_step_id),
        "request_status": str(previous.get("request_status", "") or ""),
        "current_stage": str(previous.get("current_stage", "") or ""),
        "message": message,
    }


def _run_browser_loop_if_waiting(
    context: Any,
    *,
    request_step_id: str,
    previous_step_id: str,
    message: str,
) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") != "waiting_children":
        return _skipped_loop_payload(
            context,
            request_step_id=request_step_id,
            previous_step_id=previous_step_id,
            message=message,
        )
    return refresh_flow.run_phase1_browser_runloop(_loop_params(context))


def _run_executor_or_load_status(
    context: Any,
    *,
    request_step_id: str,
    previous_step_id: str,
    status_loader: Any,
) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") == "success":
        return status_loader(_params_with_request_id(context, request_step_id))
    payload = refresh_flow.execute_executor_once(_params_with_request_id(context, request_step_id))
    if not str(payload.get("request_id", "") or "").strip():
        return status_loader(_params_with_request_id(context, request_step_id))
    return payload
