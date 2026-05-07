from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "dispatch_selection_row_refresh"

def _advance_dispatch_selection_row_refresh(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    existing_jobs = _api_jobs_for_stage(
        store, request_id=request.request_id, stage_code="collect_selection_rows"
    )
    if existing_jobs:
        return {
            "action": "advance",
            "next_stage": "collect_selection_rows",
            "details": {"reason": "row jobs already dispatched"},
        }

    candidate_rows = _resolve_candidate_rows(
        store, request=request, request_payload=request_payload
    )
    candidate_rows = _limit_candidate_rows(candidate_rows, request_payload=request_payload)
    if not candidate_rows:
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"reason": "no candidate rows to refresh"},
        }

    jobs = []
    for row in candidate_rows:
        source_record_id = str(row.get("source_record_id") or "")
        product_identity = _mapping(row.get("product_identity"))
        business_key = _first_non_empty(
            product_identity.get("product_id"),
            product_identity.get("normalized_product_url"),
            source_record_id,
        )
        row_payload = {
            **_payload_subset(
                request_payload, FACT_PERSISTENCE_PASSTHROUGH_KEYS + ARTIFACT_PASSTHROUGH_KEYS
            ),
            "request_payload": request_payload,
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": request.task_code,
            "stage_code": "collect_selection_rows",
            "source_record_id": source_record_id,
            "source_table_ref": str(
                row.get("source_table_ref") or request_payload.get("selection_table_ref") or ""
            ),
            "target_table_ref": str(request_payload.get("selection_table_ref") or ""),
            "product_identity": product_identity,
            "source_context": _mapping(row.get("source_context")),
            "fallback_allowed": bool(request_payload.get("fallback_allowed", True)),
            "writeback_enabled": bool(request_payload.get("writeback_enabled", True)),
            "fastmoss_phone": str(request_payload.get("fastmoss_phone") or ""),
            "fastmoss_password": str(request_payload.get("fastmoss_password") or ""),
            "fastmoss_phone_env": str(
                request_payload.get("fastmoss_phone_env") or "FASTMOSS_PHONE"
            ),
            "fastmoss_password_env": str(
                request_payload.get("fastmoss_password_env") or "FASTMOSS_PASSWORD"
            ),
            "fastmoss_live_fetch": str(request_payload.get("fastmoss_live_fetch") or ""),
            "table_refs": request_payload.get("table_refs") or {},
            "access_token": request_payload.get("access_token") or "",
            "access_token_env": request_payload.get("access_token_env") or "",
        }
        row_payload["requires_fact_db"] = True
        row_payload["requires_object_storage"] = True
        row_payload["require_database_persistence"] = True
        row_payload["require_object_storage"] = True
        jobs.append(
            {
                "business_key": business_key,
                "dedupe_key": f"{request.request_id}:collect_selection_rows:{source_record_id or business_key}",
                "max_attempts": 1,
                "payload": row_payload,
            }
        )

    enqueue_payload = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code="selection_row_refresh",
        jobs=jobs,
    )
    return {
        "action": "advance",
        "next_stage": "collect_selection_rows",
        "details": {
            "dispatch_payload": enqueue_payload,
            "row_count": len(jobs),
        },
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_dispatch_selection_row_refresh(store=store, request=request, workflow=workflow, stage_code=STAGE_CODE)
