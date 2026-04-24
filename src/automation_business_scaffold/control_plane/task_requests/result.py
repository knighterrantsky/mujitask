from __future__ import annotations

from typing import Any

from automation_business_scaffold.control_plane.executor.runner import get_task_request_status


def get_result(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    return get_task_request_status(task_code, {**params, "control_action": "result"})


__all__ = ["get_result", "get_task_request_status"]
