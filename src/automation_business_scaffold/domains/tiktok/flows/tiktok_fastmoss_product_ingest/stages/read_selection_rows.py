from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "read_selection_rows"

def _advance_read_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    if not _selection_mode_enabled(request_payload):
        return {
            "action": "advance",
            "next_stage": "dispatch_selection_row_refresh",
            "details": {"stage_transition": "direct_ingest_skip_selection_read"},
        }

    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="feishu_table_read",
            jobs=[
                {
                    "business_key": str(
                        request_payload.get("selection_table_ref") or request.request_id
                    ),
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_table_read",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "selection_record_id": str(
                            request_payload.get("selection_record_id") or ""
                        ),
                        "product_url": str(request_payload.get("product_url") or ""),
                        "product_id": str(request_payload.get("product_id") or ""),
                        "adapter_code": "selection_table_source_adapter",
                        "table_refs": request_payload.get("table_refs") or {},
                        "access_token": request_payload.get("access_token") or "",
                        "access_token_env": request_payload.get("access_token_env") or "",
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the selection table read stage.",
            "details": {"dispatch_payload": {"feishu_table_read": enqueue_payload}},
        }

    if _any_api_jobs_active(stage_jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Selection table read is still running.",
        }

    if _any_failed_api_jobs(stage_jobs):
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "Selection table read failed."},
            "summary": {"total": 1, "counts": {"selection_table_read_failed": 1}},
            "details": {"failed_jobs": stage_jobs},
        }

    return {
        "action": "advance",
        "next_stage": "dispatch_selection_row_refresh",
        "details": {
            "selection_table_read": _latest_api_job_by_code(stage_jobs, "feishu_table_read")
        },
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_read_selection_rows(store=store, request=request, workflow=workflow, stage_code=STAGE_CODE)
