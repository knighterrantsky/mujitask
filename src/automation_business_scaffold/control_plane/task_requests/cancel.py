from __future__ import annotations

from typing import Any

from automation_business_scaffold.control_plane.executor.request_aggregation import refresh_request_aggregate_counts
from automation_business_scaffold.control_plane.runtime_config.settings import (
    build_request_payload,
    build_runtime_settings,
    create_runtime_store,
)


def cancel(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    del task_code
    request_id = str(params.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("request_id is required for cancel.")
    settings = build_runtime_settings(params)
    store = create_runtime_store(settings)
    outcome = store.cancel_task_request(request_id=request_id)
    refresh_request_aggregate_counts(store, request_id=request_id)
    request = outcome["request"]
    message = "Task request cancellation requested." if outcome["applied"] else f"Task request is already {request.status}."
    payload = build_request_payload(
        store=store,
        request_id=request_id,
        control_action="cancel",
        message=message,
    )
    payload["cancel"] = {
        "applied": bool(outcome["applied"]),
        "previous_status": str(outcome["previous_status"]),
        "cancelled_api_worker_job_count": int(outcome["cancelled_api_worker_job_count"]),
        "cancelled_task_execution_count": int(outcome["cancelled_task_execution_count"]),
        "running_child_count": int(outcome.get("running_count", 0)),
    }
    return payload


__all__ = ["cancel"]
