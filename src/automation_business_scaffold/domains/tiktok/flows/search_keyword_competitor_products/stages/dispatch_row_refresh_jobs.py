from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    api_jobs_for_stage as _api_jobs_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    render_job_keys,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.models import (
    ARTIFACT_PASSTHROUGH_KEYS,
    FACT_PERSISTENCE_PASSTHROUGH_KEYS,
    FASTMOSS_PRODUCT_PASSTHROUGH_KEYS,
    TIKTOK_REQUEST_PASSTHROUGH_KEYS,
)
from ..context.stage_inputs import (
    _first_text,
    _payload_subset,
)
from ..context.runtime_views import (
    _seed_contexts,
)


STAGE_CODE = "dispatch_row_refresh_jobs"
FINAL_ROW_RESULT_STATUSES = {"success", "partial_success", "failed", "skipped"}
ACTIVE_ROW_LIFECYCLE_STATUSES = {"pending", "running"}


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_row_refresh_jobs"
    seed_contexts = _successful_seed_contexts(store=store, request_id=request.request_id)
    if not seed_contexts:
        _update_request_cursor(store=store, request=request, stage_code=stage_code, payload={"dispatched_row_count": 0})
        return {"action": "advance", "next_stage": "refresh_competitor_rows", "details": {"dispatched_row_count": 0}}

    row_dispatch = enqueue_next_competitor_row_refresh(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="refresh_competitor_rows",
        seed_contexts=seed_contexts,
    )
    dispatched_row_count = int(row_dispatch.get("created_count") or 0)
    selected_seed = dict(row_dispatch.get("selected_seed") or {})
    pending_seed_count = int(row_dispatch.get("pending_seed_count") or 0)
    already_active = bool(row_dispatch.get("already_active"))
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "dispatched_row_count": dispatched_row_count,
            "pending_seed_count": pending_seed_count,
            "selected_source_record_id": str(selected_seed.get("source_record_id") or ""),
            "row_dispatch": row_dispatch,
        },
    )
    return {
        "action": "advance",
        "next_stage": "refresh_competitor_rows",
        "details": {
            "dispatched_row_count": dispatched_row_count,
            "pending_seed_count": pending_seed_count,
            "already_active": already_active,
        },
    }


def enqueue_next_competitor_row_refresh(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    seed_contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    seed_contexts = seed_contexts or _successful_seed_contexts(store=store, request_id=request.request_id)
    row_jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    active_seed = _active_row_seed(seed_contexts=seed_contexts, row_jobs=row_jobs)
    if active_seed is not None:
        return {
            "created_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "created_records": [],
            "updated_records": [],
            "skipped_records": [],
            "already_active": True,
            "pending_seed_count": len(_pending_row_seeds(seed_contexts=seed_contexts, row_jobs=row_jobs)),
            "selected_seed": active_seed,
        }
    pending_seeds = _pending_row_seeds(seed_contexts=seed_contexts, row_jobs=row_jobs)
    if not pending_seeds:
        return {
            "created_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "created_records": [],
            "updated_records": [],
            "skipped_records": [],
            "already_active": False,
            "pending_seed_count": 0,
            "selected_seed": {},
        }
    selected_seed = pending_seeds[0]
    row_job_def = workflow.require_job("competitor_row_refresh")
    source_table_ref = str(request.payload.get("seed_table_ref") or request.payload.get("target_table_ref") or request.payload.get("table_url") or "")
    row_job = _row_refresh_job(
        request=request,
        workflow=workflow,
        row_job_def=row_job_def,
        seed=selected_seed,
        stage_code=stage_code,
        source_table_ref=source_table_ref,
    )
    row_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=[row_job],
    )
    return {
        **row_dispatch,
        "already_active": False,
        "pending_seed_count": len(pending_seeds),
        "selected_seed": selected_seed,
    }


def _row_refresh_job(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    row_job_def: Any,
    seed: dict[str, Any],
    stage_code: str,
    source_table_ref: str,
) -> dict[str, Any]:
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
        "stage_code": stage_code,
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
        stage_code=stage_code,
        job_code=row_job_def.job_code,
    )
    return {
        "business_key": row_keys["business_key"],
        "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
        "payload": row_payload,
        "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
    }


def _successful_seed_contexts(*, store: RuntimeStore, request_id: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _seed_contexts(store=store, request_id=request_id)
        if item.get("seed_status") == "success" and str(item.get("source_record_id") or "")
    ]


def _active_row_seed(*, seed_contexts: list[dict[str, Any]], row_jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for seed in seed_contexts:
        source_record_id = str(seed.get("source_record_id") or "")
        for job in _row_jobs_for_source(row_jobs=row_jobs, source_record_id=source_record_id):
            if str(job.get("status") or "") in ACTIVE_ROW_LIFECYCLE_STATUSES:
                return seed
            if _row_job_waiting_for_fallback(job):
                return seed
    return None


def _pending_row_seeds(*, seed_contexts: list[dict[str, Any]], row_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        seed
        for seed in seed_contexts
        if not _seed_has_final_row_result(seed=seed, row_jobs=row_jobs)
        and _active_row_seed(seed_contexts=[seed], row_jobs=row_jobs) is None
    ]


def _seed_has_final_row_result(*, seed: dict[str, Any], row_jobs: list[dict[str, Any]]) -> bool:
    source_record_id = str(seed.get("source_record_id") or "")
    for job in _row_jobs_for_source(row_jobs=row_jobs, source_record_id=source_record_id):
        if _row_job_waiting_for_fallback(job):
            continue
        result_payload = extract_effective_result_payload(job)
        if bool(result_payload.get("fallback_required")):
            continue
        status = str(job.get("result_status") or "")
        if not status:
            handler_result = (job.get("result") or {}).get("handler_result") if isinstance(job.get("result"), dict) else {}
            status = str(handler_result.get("status") or "")
        if status in FINAL_ROW_RESULT_STATUSES:
            return True
    return False


def _row_jobs_for_source(*, row_jobs: list[dict[str, Any]], source_record_id: str) -> list[dict[str, Any]]:
    return [
        job
        for job in row_jobs
        if str(job.get("job_code") or "") == "competitor_row_refresh"
        and str((job.get("payload") or {}).get("source_record_id") or "") == source_record_id
    ]


def _row_job_waiting_for_fallback(job: dict[str, Any]) -> bool:
    if str(job.get("status") or "") == "waiting":
        return True
    result_payload = extract_effective_result_payload(job)
    return bool(result_payload.get("fallback_required"))
