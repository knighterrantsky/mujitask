from __future__ import annotations

from typing import Any


STAGE_CODE = "ready_for_summary"


def advance(
    *,
    store: Any,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    del store, request
    return {"action": "advance", "next_stage": workflow.summary_policy.summary_stage_code}
