from __future__ import annotations

from typing import Any


def cancel(task_code: str, params: dict[str, Any]) -> dict[str, Any]:
    del task_code, params
    raise NotImplementedError("task_request cancel is not wired in the current runtime store yet.")


__all__ = ["cancel"]
