from __future__ import annotations

from typing import Any, Callable

from automation_framework.core import FrameworkResult

PayloadRunner = Callable[[dict[str, Any]], dict[str, Any]]


def ok_result(payload: dict[str, Any], *, default_message: str) -> FrameworkResult:
    return FrameworkResult.ok(
        message=str(payload.get("message", "") or default_message),
        data=payload,
        metadata={"artifacts_payload": {"state_dump": payload}},
    )


def request_id_from_step(context: Any, step_id: str) -> str:
    return str(context.get_step_output(step_id).get("request_id", "") or "").strip()


def params_with_request_id(context: Any, step_id: str) -> dict[str, Any]:
    params = dict(context.params)
    request_id = request_id_from_step(context, step_id)
    if request_id:
        params["request_id"] = request_id
    return params


def loop_params(context: Any) -> dict[str, Any]:
    params = dict(context.params)
    params.setdefault("execution_control_stop_when_idle", True)
    params.setdefault("execution_control_max_idle_cycles", 1)
    return params


def skipped_loop_payload(
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
        "request_id": request_id_from_step(context, request_step_id),
        "request_status": str(previous.get("request_status", "") or ""),
        "current_stage": str(previous.get("current_stage", "") or ""),
        "message": message,
    }


def run_browser_loop_if_waiting(
    context: Any,
    *,
    request_step_id: str,
    previous_step_id: str,
    message: str,
    browser_loop: PayloadRunner,
) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") != "waiting_children":
        return skipped_loop_payload(
            context,
            request_step_id=request_step_id,
            previous_step_id=previous_step_id,
            message=message,
        )
    return browser_loop(loop_params(context))


def run_executor_or_load_status(
    context: Any,
    *,
    request_step_id: str,
    previous_step_id: str,
    status_loader: PayloadRunner,
    executor_once: PayloadRunner,
) -> dict[str, Any]:
    previous = context.get_step_output(previous_step_id)
    if str(previous.get("request_status", "") or "") == "success":
        return status_loader(params_with_request_id(context, request_step_id))
    payload = executor_once(params_with_request_id(context, request_step_id))
    if not str(payload.get("request_id", "") or "").strip():
        return status_loader(params_with_request_id(context, request_step_id))
    return payload
