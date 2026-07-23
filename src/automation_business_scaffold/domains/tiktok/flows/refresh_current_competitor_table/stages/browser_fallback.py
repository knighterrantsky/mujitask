from __future__ import annotations

# ruff: noqa: F405

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "browser_fallback"

def _advance_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "browser_fallback"
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    fallback_candidates = _browser_fallback_candidates(store=store, request_id=request.request_id)
    if not fallback_candidates and not executions:
        return {
            "action": "advance",
            "next_stage": "collect_product_data",
            "details": {"fallback_row_count": 0},
        }

    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for browser fallback executions to finish.")

    requeued_jobs = _requeue_competitor_rows_after_browser(
        store=store,
        stage_code="collect_product_data",
        fallback_candidates=fallback_candidates,
        executions=executions,
    )
    if requeued_jobs:
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "execution_count": len(executions),
                "requeued_row_count": len(requeued_jobs),
                "status": "success",
            },
        )
        return _waiting(
            stage_code="collect_product_data",
            message="Requeued competitor row refresh after browser fallback.",
            details={
                "requeued_row_count": len(requeued_jobs),
            },
        )

    dispatch_candidates = _fallback_candidates_needing_dispatch(
        fallback_candidates=fallback_candidates,
        executions=executions,
    )[:1]
    if dispatch_candidates:
        candidate = dispatch_candidates[0]
        fallback_handler = str(candidate.get("fallback_handler") or "")
        if fallback_handler:
            job_def = workflow.require_job(fallback_handler)
            payload = _browser_execution_payload(
                request=request,
                workflow=workflow,
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
            dispatch = store.enqueue_task_executions(
                request_id=request.request_id,
                item_code=job_def.job_code,
                workflow_code=workflow.workflow_code,
                items=[
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
                ],
            )
            _update_request_cursor(
                store=store,
                request=request,
                stage_code=stage_code,
                payload={
                    "browser_dispatch": dispatch,
                    "fallback_row_count": len(fallback_candidates),
                    "dispatch_row_count": 1,
                },
            )
            return _waiting(
                stage_code=stage_code,
                message="Enqueued browser fallback execution.",
                details={"created_count": int(dispatch.get("created_count") or 0)},
            )

    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "requeued_row_count": 0,
            "status": "failed" if fallback_candidates else "success",
        },
    )
    return {
        "action": "advance",
        "next_stage": "collect_product_data",
        "details": {"execution_count": len(executions), "requeued_row_count": 0},
    }


def _browser_execution_fallback_key(execution: Any) -> str:
    payload = dict(getattr(execution, "payload", None) or {})
    fallback_handler = str(getattr(execution, "item_code", "") or payload.get("fallback_handler") or "")
    source_record_id = _first_text(payload.get("source_record_id"))
    if not fallback_handler or not source_record_id:
        return ""
    return _row_fallback_key(source_record_id=source_record_id, fallback_handler=fallback_handler)


def _fallback_candidates_needing_dispatch(
    *,
    fallback_candidates: list[dict[str, Any]],
    executions: list[Any],
) -> list[dict[str, Any]]:
    existing_keys = {
        key
        for execution in executions
        if (key := _browser_execution_fallback_key(execution))
    }
    return [
        candidate
        for candidate in fallback_candidates
        if str(candidate.get("fallback_key") or "") not in existing_keys
    ]


def _requeue_competitor_rows_after_browser(
    *,
    store: RuntimeStore,
    stage_code: str,
    fallback_candidates: list[dict[str, Any]],
    executions: list[Any],
) -> list[dict[str, Any]]:
    terminal_by_key: dict[str, Any] = {}
    for execution in executions:
        if str(getattr(execution, "status", "") or "") not in {"finished", "cancelled"}:
            continue
        fallback_key = _browser_execution_fallback_key(execution)
        if fallback_key:
            terminal_by_key[fallback_key] = execution

    for candidate in fallback_candidates:
        execution = terminal_by_key.get(str(candidate.get("fallback_key") or ""))
        if execution is None:
            continue
        return [
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("row_job_id") or ""),
                payload=_after_browser_row_payload(
                    stage_code=stage_code,
                    candidate={
                        **dict(candidate),
                        "browser_execution_id": str(execution.execution_id),
                        "browser_execution_payload": extract_effective_result_payload(execution),
                        "browser_execution_status": extract_handler_result_status(execution),
                    },
                ),
                stage=stage_code,
            ),
        ]
    return []


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_browser_fallback(store=store, request=request, workflow=workflow)
