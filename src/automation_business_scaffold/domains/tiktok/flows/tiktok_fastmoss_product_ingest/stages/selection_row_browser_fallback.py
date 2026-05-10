from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "selection_row_browser_fallback"

def _advance_selection_row_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    executions = _browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=stage_code,
    )
    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    if not fallback_candidates and not executions:
        return {
            "action": "advance",
            "next_stage": "collect_selection_rows",
            "details": {"fallback_candidate_count": 0},
        }
    if not executions and fallback_candidates:
        dispatches: dict[str, Any] = {}
        for fallback_handler in sorted(
            {str(candidate.get("fallback_handler") or "") for candidate in fallback_candidates}
        ):
            if not fallback_handler:
                continue
            job_def = workflow.require_job(fallback_handler)
            items: list[dict[str, Any]] = []
            for candidate in fallback_candidates:
                if str(candidate.get("fallback_handler") or "") != fallback_handler:
                    continue
                payload = _selection_row_browser_execution_payload(
                    request_payload=request.payload,
                    stage_code=stage_code,
                    candidate=candidate,
                )
                keys = render_job_keys(
                    job_def,
                    request.payload,
                    candidate,
                    payload,
                    request_id=request.request_id,
                    task_code=request.task_code,
                    workflow_code=workflow.workflow_code,
                    stage_code=stage_code,
                    item_code=job_def.job_code,
                )
                items.append(
                    {
                        "business_key": keys["business_key"]
                        or str(candidate.get("business_entity_key") or ""),
                        "dedupe_key": build_stage_local_dedupe_key(
                            keys["dedupe_key"],
                            job_def.job_code,
                            stage_scope=stage_code,
                        ),
                        "resource_code": _row_browser_resource_code(
                            fallback_handler=fallback_handler,
                            payload=payload,
                            candidate=candidate,
                        ),
                        "payload": payload,
                        "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                    }
                )
            if items:
                dispatches[fallback_handler] = store.enqueue_task_executions(
                    request_id=request.request_id,
                    item_code=job_def.job_code,
                    workflow_code=workflow.workflow_code,
                    items=items,
                )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "browser_dispatches": dispatches,
                "fallback_candidate_count": len(fallback_candidates),
            },
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Enqueued selection row browser fallback executions.",
            "details": {
                "created_count": sum(
                    int(dispatch.get("created_count") or 0) for dispatch in dispatches.values()
                ),
                "fallback_candidate_count": len(fallback_candidates),
            },
        }
    if _any_browser_executions_active(executions):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Waiting for selection row browser fallback executions to finish.",
        }
    requeued_jobs = _requeue_selection_rows_after_browser(
        store=store,
        stage_code="collect_selection_rows",
        fallback_candidates=fallback_candidates,
        executions=executions,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "requeued_row_count": len(requeued_jobs),
            "status": "success" if requeued_jobs else "failed",
        },
    )
    if requeued_jobs:
        return {
            "action": "waiting",
            "current_stage": "collect_selection_rows",
            "message": "Requeued selection row refresh after browser fallback.",
            "details": {
                "requeued_row_count": len(requeued_jobs),
            },
        }
    return {
        "action": "advance",
        "next_stage": "collect_selection_rows",
        "details": {"execution_count": len(executions), "requeued_row_count": 0},
    }


def _requeue_selection_rows_after_browser(
    *,
    store: RuntimeStore,
    stage_code: str,
    fallback_candidates: list[dict[str, Any]],
    executions: list[Any],
) -> list[dict[str, Any]]:
    requeued: list[dict[str, Any]] = []
    terminal_by_key: dict[str, Any] = {}
    for execution in executions:
        if str(getattr(execution, "status", "") or "") not in {"finished", "cancelled"}:
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_non_empty(payload.get("source_record_id"))
        business_entity_key = _first_non_empty(payload.get("business_entity_key"))
        terminal_by_key[
            _row_fallback_key(
                source_record_id=source_record_id,
                business_entity_key=business_entity_key,
                fallback_handler=fallback_handler,
            )
        ] = execution

    for candidate in fallback_candidates:
        execution = terminal_by_key.get(str(candidate.get("fallback_key") or ""))
        if execution is None:
            continue
        requeued.append(
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("row_job_id") or ""),
                payload=_selection_row_after_browser_payload(
                    stage_code=stage_code,
                    candidate={
                        **dict(candidate),
                        "browser_execution_id": str(execution.execution_id),
                        "browser_execution_payload": extract_effective_result_payload(execution),
                        "browser_execution_status": extract_handler_result_status(execution),
                    },
                ),
                stage=stage_code,
            )
        )
    return requeued


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_selection_row_browser_fallback(store=store, request=request, workflow=workflow, stage_code=STAGE_CODE)
