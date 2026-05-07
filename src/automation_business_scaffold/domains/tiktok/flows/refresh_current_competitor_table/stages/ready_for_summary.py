from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "ready_for_summary"

def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del store, request
    return {"action": "advance", "next_stage": workflow.summary_policy.summary_stage_code}
