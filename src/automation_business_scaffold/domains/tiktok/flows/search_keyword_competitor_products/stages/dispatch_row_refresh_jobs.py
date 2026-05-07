from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    build_stage_local_dedupe_key,
    render_job_keys,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context import (
    ARTIFACT_PASSTHROUGH_KEYS,
    FACT_PERSISTENCE_PASSTHROUGH_KEYS,
    FASTMOSS_PRODUCT_PASSTHROUGH_KEYS,
    TIKTOK_REQUEST_PASSTHROUGH_KEYS,
    _first_text,
    _payload_subset,
    _seed_contexts,
)


STAGE_CODE = "dispatch_row_refresh_jobs"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_row_refresh_jobs"
    seed_contexts = [item for item in _seed_contexts(store=store, request_id=request.request_id) if item.get("seed_status") == "success"]
    if not seed_contexts:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"dispatched_row_count": 0})
        return {"action": "advance", "next_stage": "refresh_competitor_rows", "details": {"dispatched_row_count": 0}}

    row_job_def = workflow.require_job("competitor_row_refresh")
    source_table_ref = str(request.payload.get("seed_table_ref") or request.payload.get("target_table_ref") or request.payload.get("table_url") or "")
    row_jobs: list[dict[str, Any]] = []
    for seed in seed_contexts:
        product_identity = dict(seed.get("product_identity") or {})
        row_payload = {
            **_payload_subset(
                request.payload,
                TIKTOK_REQUEST_PASSTHROUGH_KEYS
                + FASTMOSS_PRODUCT_PASSTHROUGH_KEYS
                + FACT_PERSISTENCE_PASSTHROUGH_KEYS
                + ARTIFACT_PASSTHROUGH_KEYS
                + ("table_refs", "access_token", "access_token_env", "validate_schema"),
            ),
            "request_payload": dict(request.payload or {}),
            "stage_code": "refresh_competitor_rows",
            "source_record_id": seed["source_record_id"],
            "source_record_id_or_product_id": _first_text(seed.get("source_record_id"), seed.get("product_id")),
            "business_key": seed.get("business_entity_key") or seed.get("candidate_key") or "",
            "product_identity": product_identity,
            "normalized_product_url": seed.get("normalized_product_url") or product_identity.get("normalized_product_url") or "",
            "source_table_ref": source_table_ref,
            "source_context": dict(seed.get("source_context") or {}),
            "fallback_allowed": bool(request.payload.get("fallback_allowed", True)),
        }
        row_keys = render_job_keys(
            row_job_def,
            request.payload,
            seed,
            row_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code="refresh_competitor_rows",
            job_code=row_job_def.job_code,
        )
        row_jobs.append(
            {
                "business_key": row_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
                "payload": row_payload,
                "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
            }
        )

    row_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=row_jobs,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"dispatched_row_count": len(seed_contexts), "row_dispatch": row_dispatch},
    )
    return {
        "action": "advance",
        "next_stage": "refresh_competitor_rows",
        "details": {
            "dispatched_row_count": len(seed_contexts),
            "row_refresh_created_count": int(row_dispatch["created_count"]),
        },
    }
