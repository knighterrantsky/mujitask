from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.runtime_views import (
    _successful_seed_contexts,
)
from ..context.stage_inputs import (
    _selection_row_refresh_job_item,
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


def _dispatch_next_selection_row_refresh_job(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    seed_contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not seed_contexts:
        return {"created_count": 0, "updated_count": 0, "skipped_count": 0}
    row_job_def = workflow.require_job("selection_row_refresh")
    seed = dict(seed_contexts[0])
    return store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=[
            _selection_row_refresh_job_item(
                request=request,
                workflow=workflow,
                row_job_def=row_job_def,
                seed=seed,
            )
        ],
    )
