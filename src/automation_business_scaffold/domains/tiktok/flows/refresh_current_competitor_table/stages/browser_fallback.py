from __future__ import annotations

from typing import Any, Mapping

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
            "next_stage": "ready_for_summary",
            "details": {"fallback_row_count": 0},
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
            if not items:
                continue
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
            payload={"browser_dispatches": dispatches, "fallback_row_count": len(fallback_candidates)},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued browser fallback executions.",
            details={
                "created_count": sum(int(dispatch.get("created_count") or 0) for dispatch in dispatches.values())
            },
        )

    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for browser fallback executions to finish.")
    after_browser_candidates = _browser_after_browser_candidates(
        store=store,
        request_id=request.request_id,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "after_browser_candidate_count": len(after_browser_candidates),
            "status": "success" if after_browser_candidates else "failed",
        },
    )
    if after_browser_candidates:
        row_stage_code = "collect_product_data"
        row_job_def = workflow.require_job("competitor_row_refresh")
        row_jobs = [
            _after_browser_row_job(
                request=request,
                workflow=workflow,
                stage_code=row_stage_code,
                row_job_def=row_job_def,
                candidate=candidate,
            )
            for candidate in after_browser_candidates
        ]
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=row_job_def.job_code,
            jobs=row_jobs,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "execution_count": len(executions),
                "after_browser_candidate_count": len(after_browser_candidates),
                "row_dispatch": dispatch,
                "status": "success",
            },
        )
        return _waiting(
            stage_code=row_stage_code,
            message="Enqueued competitor row refresh after browser fallback.",
            details={
                "created_count": int(dispatch["created_count"]),
                "after_browser_candidate_count": len(after_browser_candidates),
            },
        )
    return {
        "action": "advance",
        "next_stage": "ready_for_summary",
        "details": {"execution_count": len(executions), "after_browser_candidate_count": 0},
    }


def _after_browser_row_job(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    row_job_def: Any,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _after_browser_row_payload(stage_code=stage_code, candidate=candidate)
    keys = render_job_keys(
        row_job_def,
        request.payload,
        candidate,
        payload,
        request_id=request.request_id,
        task_code=request.task_code,
        workflow_code=workflow.workflow_code,
        stage_code=stage_code,
        job_code=row_job_def.job_code,
    )
    return {
        "business_key": keys["business_key"],
        "dedupe_key": build_stage_local_dedupe_key(
            f"{keys['dedupe_key']}:after-browser-fallback",
            row_job_def.job_code,
        ),
        "payload": payload,
        "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_browser_fallback(store=store, request=request, workflow=workflow)
