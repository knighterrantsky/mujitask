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


def workflow_stage_context(*, request: Any, workflow: Any, stage_code: str) -> StageContext:
    return StageContext(
        request_id=str(request.request_id),
        task_code=str(request.task_code),
        workflow_code=str(workflow.workflow_code),
        stage_code=stage_code,
        payload=dict(request.payload or {}),
    )

def _fastmoss_search_settings_from_request_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {}) if isinstance(request_payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("fastmoss_cookie_cache_namespace", "cookie_cache_namespace"),
        ("fastmoss_cookie_cache_enabled", "cookie_cache_enabled"),
        ("fastmoss_cookie_cache_ttl_seconds", "cookie_cache_ttl_seconds"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", [], {}):
            settings.setdefault(target_key, value)
    settings.setdefault("live_fetch", True)
    settings.setdefault("ensure_logged_in", True)
    return {key: value for key, value in settings.items() if value not in (None, "", [], {})}

def _keyword_seed_import_search_request(
    request_payload: Mapping[str, Any],
    *,
    latest_import_job: Mapping[str, Any] | None,
    retry_after_fastmoss_browser: bool,
) -> dict[str, Any]:
    previous_payload = coerce_mapping((latest_import_job or {}).get("payload"))
    previous_search_request = coerce_mapping(previous_payload.get("search_request"))
    search_request = dict(previous_search_request) if retry_after_fastmoss_browser and previous_search_request else keyword_search_parameter_mapper(request_payload)
    for key in RUNTIME_DB_PASSTHROUGH_KEYS:
        if request_payload.get(key) not in (None, ""):
            search_request[key] = request_payload.get(key)
    if retry_after_fastmoss_browser:
        search_request["fastmoss_security_browser_fallback_attempt"] = 1
    return search_request

def _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs: list[dict[str, Any]]) -> bool:
    return any(
        int(coerce_mapping(job.get("payload")).get("fastmoss_security_browser_fallback_attempt") or 0) > 0
        for job in jobs
    )

def _fastmoss_security_fallback_payload_from_job(import_job: Mapping[str, Any]) -> dict[str, Any]:
    job_payload = coerce_mapping(import_job.get("payload"))
    result_payload = extract_effective_result_payload(import_job)
    search_request = coerce_mapping(job_payload.get("search_request")) or coerce_mapping(result_payload.get("search_request"))
    security_context = coerce_mapping(result_payload.get("security_context"))
    return {
        "search_query": _first_text(
            search_request.get("search_query"),
            search_request.get("keyword"),
            job_payload.get("search_query"),
        ),
        "search_digest": _first_text(job_payload.get("search_digest"), search_request.get("search_digest")),
        "search_request": search_request,
        "security_context": security_context,
    }

def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )

def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}

def _selection_row_source_record_id(seed: Mapping[str, Any]) -> str:
    return _first_text(seed.get("source_record_id"), seed.get("product_id"), seed.get("candidate_key"))


def _selection_row_refresh_job_item(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    row_job_def: Any,
    seed: Mapping[str, Any],
) -> dict[str, Any]:
    source_table_ref = str(
        request.payload.get("selection_table_ref")
        or request.payload.get("seed_table_ref")
        or request.payload.get("target_table_ref")
        or request.payload.get("table_url")
        or ""
    )
    product_identity = dict(seed.get("product_identity") or {})
    source_record_id = _selection_row_source_record_id(seed)
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
        "stage_code": "refresh_selection_rows",
        "source_record_id": source_record_id,
        "source_record_id_or_product_id": _first_text(source_record_id, seed.get("product_id")),
        "business_key": seed.get("business_entity_key") or seed.get("candidate_key") or "",
        "product_identity": product_identity,
        "normalized_product_url": seed.get("normalized_product_url")
        or product_identity.get("normalized_product_url")
        or "",
        "source_table_ref": source_table_ref,
        "target_table_ref": source_table_ref,
        "source_context": dict(seed.get("source_context") or {}),
        "fallback_allowed": bool(request.payload.get("fallback_allowed", True)),
        "writeback_enabled": bool(request.payload.get("writeback_enabled", True)),
        "requires_fact_db": True,
        "requires_object_storage": True,
        "require_database_persistence": True,
        "require_object_storage": True,
    }
    row_keys = render_job_keys(
        row_job_def,
        request.payload,
        dict(seed),
        row_payload,
        request_id=request.request_id,
        task_code=request.task_code,
        workflow_code=workflow.workflow_code,
        stage_code="refresh_selection_rows",
        job_code=row_job_def.job_code,
    )
    return {
        "business_key": row_keys["business_key"],
        "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
        "payload": row_payload,
        "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
    }

def _build_seed_contexts(*, candidates: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    job_by_candidate = {
        str((job.get("payload") or {}).get("candidate_key") or ""): job
        for job in jobs
        if str((job.get("payload") or {}).get("candidate_key") or "")
    }
    seed_contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = candidate["candidate_key"]
        job = job_by_candidate.get(candidate_key)
        result_payload = extract_effective_result_payload(job)
        target_record_ids = result_payload.get("target_record_ids") if isinstance(result_payload.get("target_record_ids"), list) else []
        source_record_id = str(target_record_ids[0] if target_record_ids else candidate_key)
        seed_contexts.append(
            {
                **candidate,
                "source_record_id": source_record_id,
                "seed_status": _record_effective_status(job),
                "seed_result": result_payload,
                "target_record_ids": [str(item) for item in target_record_ids],
            }
        )
    return seed_contexts

def _normalize_search_candidates(
    raw_candidates: Any,
    *,
    search_query: str,
    output_conditions: Mapping[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    from .policies.candidate_filter import normalize_search_candidates

    return normalize_search_candidates(
        raw_candidates,
        search_query=search_query,
        output_conditions=output_conditions,
        max_candidates=max_candidates,
    )

def _latest_job(jobs: list[dict[str, Any]], *, job_code: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        selected = job
    return selected

def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("source_record_id") or "") != source_record_id:
            continue
        selected = job
    return selected

def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if isinstance(record, Mapping):
        status = str(record.get("result_status") or record.get("status") or "")
        handler_status = extract_handler_result_status(record)
        return handler_status or status
    status = str(getattr(record, "result_status", "") or getattr(record, "status", "") or "")
    handler_status = extract_handler_result_status(record)
    return handler_status or status

def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""

__all__ = [name for name in globals() if not name.startswith('__')]
