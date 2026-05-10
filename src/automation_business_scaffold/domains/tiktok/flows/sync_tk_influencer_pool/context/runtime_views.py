from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from automation_business_scaffold.contracts.handler.shared import (
    bundle_entity_keys,
    coerce_mapping,
    compact_dict,
    merge_fact_bundles,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    all_child_records as _all_child_records,
    any_api_jobs_active as _any_api_jobs_active,
    any_browser_executions_active as _any_browser_executions_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_projection_record,
    build_projection_write_payload,
    build_stage_local_dedupe_key,
    compute_final_status,
    extract_effective_result_payload,
    extract_handler_result_status,
    has_active_records as _has_active_children,
    is_fallback_required,
    render_job_keys,
    select_latest_successful_api_job,
    select_latest_successful_api_job_result,
    stage_child_records as _stage_child_records,
    summarize_child_outcomes,
    summarize_stage_children,
    timeout_seconds_for_workflow as _timeout_seconds,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from .models import *
from .stage_inputs import *
from .decision_models import *


def _collect_product_candidates(*, store: RuntimeStore, request: Any) -> list[dict[str, Any]]:
    read_result = select_latest_successful_api_job_result(
        _stage_api_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read"),
        "feishu_table_read",
    )
    rows = list(read_result.get("source_rows") or [])
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_record_id = _first_non_empty(
            row.get("source_record_id"),
            row.get("record_id"),
            row.get("row_id"),
        )
        product_identity = _normalize_product_identity(row.get("product_identity"), row)
        product_id = _first_non_empty(product_identity.get("product_id"), row.get("product_id"))
        if not source_record_id or not product_id:
            continue
        candidates.append(
            {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
                "product_identity": product_identity,
                "candidate_row": dict(row),
            }
        )
    return candidates

def _build_product_group_summaries(*, store: RuntimeStore, request: Any) -> list[dict[str, Any]]:
    candidates = _collect_product_candidates(store=store, request=request)
    product_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="product_creator_discovery",
    )
    sync_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        job_code="influencer_creator_sync",
    )
    groups: list[dict[str, Any]] = []
    for candidate in candidates:
        source_record_id = candidate["source_record_id"]
        product_id = candidate["product_id"]
        product_key = candidate["product_key"]
        matched_product_jobs = [
            job for job in product_jobs if _job_product_key(job) == product_key
        ]
        matched_sync_jobs = [job for job in sync_jobs if _sync_job_has_product_key(job, product_key)]
        creator_candidates = _collect_creator_candidates_from_product_jobs(matched_product_jobs)
        creator_sync_success_count = sum(
            1 for job in matched_sync_jobs if extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES
        )
        creator_sync_failed_count = sum(
            1
            for job in matched_sync_jobs
            if str(job.get("result_status") or job.get("status") or "") in {"failed", "cancelled"}
            or extract_handler_result_status(job) in {"failed", "fallback_required"}
        )
        influencer_write_success_count = sum(_sync_influencer_write_success_count(job) for job in matched_sync_jobs)
        influencer_write_created_count = sum(_sync_influencer_write_op_count(job, ("append", "create", "created")) for job in matched_sync_jobs)
        influencer_write_updated_count = sum(_sync_influencer_write_op_count(job, ("update", "updated")) for job in matched_sync_jobs)
        fact_persist_success_count = sum(_sync_step_success_count(job, "fact_upsert") for job in matched_sync_jobs)
        fact_persist_failed_count = sum(_sync_step_failed_count(job, "fact_upsert") for job in matched_sync_jobs)
        product_job_success = any(
            extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES for job in matched_product_jobs
        )
        product_job_failed = any(
            str(job.get("result_status") or job.get("status") or "") in {"failed", "cancelled"}
            or extract_handler_result_status(job) in {"failed", "fallback_required"}
            for job in matched_product_jobs
        )
        final_status = "success"
        warnings: list[str] = []
        if product_job_failed and not product_job_success:
            final_status = "failed"
            warnings.append("product_discovery_failed")
        elif fact_persist_failed_count > 0 and fact_persist_success_count == 0:
            final_status = "failed"
            warnings.append("fact_persist_failed")
        elif creator_sync_failed_count > 0 and influencer_write_success_count == 0:
            final_status = "failed"
            warnings.append("creator_sync_failed")
        elif creator_sync_failed_count > 0 or product_job_failed or fact_persist_failed_count > 0:
            final_status = "partial_success"
            warnings.append("partial_creator_projection")
        elif not creator_candidates:
            final_status = "success"
            warnings.append("no_related_creators")
        groups.append(
            {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "product_key": product_key,
                "creator_candidate_count": len(creator_candidates),
                "creator_sync_success_count": creator_sync_success_count,
                "creator_sync_failed_count": creator_sync_failed_count,
                "creator_detail_success_count": creator_sync_success_count,
                "creator_detail_failed_count": creator_sync_failed_count,
                "fact_persist_success_count": fact_persist_success_count,
                "fact_persist_failed_count": fact_persist_failed_count,
                "influencer_write_success_count": influencer_write_success_count,
                "influencer_write_created_count": influencer_write_created_count,
                "influencer_write_updated_count": influencer_write_updated_count,
                "final_status": final_status,
                "warnings": warnings,
            }
        )
    return groups

def _sync_job_has_product_key(job: Mapping[str, Any], product_key: str) -> bool:
    payload = dict(job.get("payload") or {})
    for hit in list(payload.get("product_hits") or []):
        if isinstance(hit, Mapping) and _first_non_empty(hit.get("product_key")) == product_key:
            return True
    result_payload = extract_effective_result_payload(job)
    for writeback in list(result_payload.get("product_status_writebacks") or []):
        if isinstance(writeback, Mapping) and _first_non_empty(writeback.get("product_key")) == product_key:
            return True
    return False

def _sync_influencer_write_success_count(job: Mapping[str, Any]) -> int:
    result_payload = extract_effective_result_payload(job)
    write_result = dict(result_payload.get("influencer_pool_write") or {})
    status = _first_non_empty(write_result.get("status"), write_result.get("handler_status"))
    if status in SUCCESSFUL_HANDLER_STATUSES:
        return 1
    if extract_handler_result_status(job) in SUCCESSFUL_HANDLER_STATUSES and write_result:
        return 1
    return 0

def _sync_influencer_write_op_count(job: Mapping[str, Any], ops: tuple[str, ...]) -> int:
    result_payload = extract_effective_result_payload(job)
    write_result = dict(dict(result_payload.get("influencer_pool_write") or {}).get("write_result") or {})
    records = [dict(item) for item in write_result.get("records", []) if isinstance(item, Mapping)]
    allowed_ops = {str(op).strip() for op in ops if str(op).strip()}
    count = 0
    for record in records:
        if str(record.get("status") or "").strip() != "success":
            continue
        if str(record.get("op") or "").strip() in allowed_ops:
            count += 1
    return count

def _sync_step_success_count(job: Mapping[str, Any], step_code: str) -> int:
    result_payload = extract_effective_result_payload(job)
    internal_steps = dict(result_payload.get("internal_steps") or {})
    return 1 if _first_non_empty(internal_steps.get(step_code)) in SUCCESSFUL_HANDLER_STATUSES else 0

def _sync_step_failed_count(job: Mapping[str, Any], step_code: str) -> int:
    result_payload = extract_effective_result_payload(job)
    internal_steps = dict(result_payload.get("internal_steps") or {})
    return 1 if _first_non_empty(internal_steps.get(step_code)) in {"failed", "fallback_required"} else 0

def _load_request(*, store: RuntimeStore, request_id: str) -> Any:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        raise ValueError(f"Request {request_id} is not a {TASK_CODE} runtime request.")
    return request

def _current_stage(request: Any) -> str:
    return str(request.current_stage or "").strip() or ENTRY_STAGE_CODE

def _stage_api_jobs(*, store: RuntimeStore, request_id: str, stage_code: str, job_code: str = "") -> list[dict[str, Any]]:
    jobs = store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
    return [job for job in jobs if _job_stage_code(job) == stage_code]

def _job_stage_code(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    return str(payload.get("stage_code") or job.get("stage") or "").strip()

def _stage_has_children(*, store: RuntimeStore, request_id: str, stage_code: str, job_code: str) -> bool:
    return bool(_stage_api_jobs(store=store, request_id=request_id, stage_code=stage_code, job_code=job_code))

def _successful_fact_persist_keys(fact_jobs: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for job in fact_jobs:
        if extract_handler_result_status(job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        payload = dict(job.get("payload") or {})
        idempotency_context = dict(payload.get("idempotency_context") or {})
        if idempotency_context.get("fact_subject") != "creator":
            continue
        key = _creator_fact_key(
            _first_non_empty(idempotency_context.get("source_record_id")),
            _first_non_empty(idempotency_context.get("product_id")),
            _first_non_empty(idempotency_context.get("creator_id")),
        )
        if key:
            keys.add(key)
    return keys

__all__ = [name for name in globals() if not name.startswith('__')]
