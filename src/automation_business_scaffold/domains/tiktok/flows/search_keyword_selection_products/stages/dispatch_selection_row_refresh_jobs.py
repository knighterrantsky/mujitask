from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context import (
    _dispatch_next_selection_row_refresh_job,
    _successful_seed_contexts,
)


STAGE_CODE = "dispatch_selection_row_refresh_jobs"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_selection_row_refresh_jobs"
    seed_contexts = _successful_seed_contexts(store=store, request_id=request.request_id)
    if not seed_contexts:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"dispatched_row_count": 0})
        return {"action": "advance", "next_stage": "refresh_selection_rows", "details": {"dispatched_row_count": 0}}

    row_dispatch = _dispatch_next_selection_row_refresh_job(
        store=store,
        request=request,
        workflow=workflow,
        seed_contexts=seed_contexts,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"eligible_row_count": len(seed_contexts), "row_dispatch": row_dispatch},
    )
    return {
        "action": "advance",
        "next_stage": "refresh_selection_rows",
        "details": {
            "eligible_row_count": len(seed_contexts),
            "row_refresh_created_count": int(row_dispatch["created_count"]),
        },
    }
