from __future__ import annotations

from typing import Any

from automation_business_scaffold.control_plane.executor.runner import submit_task_request


def submit(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    return submit_task_request(task_code, params)


__all__ = ["submit", "submit_task_request"]
