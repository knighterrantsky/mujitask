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


def _row_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    read_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="read_competitor_rows")
    latest = select_latest_successful_api_job(read_jobs, "feishu_table_read")
    payload = extract_effective_result_payload(latest)
    return _normalize_source_rows(payload.get("source_rows"))

def _row_has_after_browser_terminal(
    store: RuntimeStore,
    *,
    request_id: str,
    source_record_id: str,
) -> bool:
    row_job = _latest_row_job(
        [
            job
            for job in _api_jobs_for_stage(
                store=store,
                request_id=request_id,
                stage_code="collect_product_data",
            )
            if coerce_mapping(job.get("payload")).get("browser_fallback_resolved")
        ],
        source_record_id=source_record_id,
        job_code="competitor_row_refresh",
    )
    return _record_effective_status(row_job) in {"success", "partial_success", "failed", "skipped"}

def _browser_fallback_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    row_index = {row["source_record_id"]: row for row in _row_contexts(store, request_id=request_id)}
    for job in _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data"):
        if str(job.get("job_code") or "") != "competitor_row_refresh":
            continue
        if not _is_fallback_required(job):
            continue
        payload = dict(job.get("payload") or {})
        result = extract_effective_result_payload(job)
        fallback_handler = _first_text(result.get("fallback_handler"))
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = (
            dict(result.get("browser_fallback_payload"))
            if isinstance(result.get("browser_fallback_payload"), Mapping)
            else {}
        )
        source_record_id = _first_text(result.get("source_record_id"), payload.get("source_record_id"))
        if _row_has_after_browser_terminal(
            store=store,
            request_id=request_id,
            source_record_id=source_record_id,
        ):
            continue
        row_context = row_index.get(source_record_id, _minimal_row_context(payload))
        fallback_source_job_id = _first_text(
            browser_payload.get("fallback_source_job_id"),
            result.get("fallback_source_job_id"),
            job.get("job_id"),
        )
        browser_payload = {
            **browser_payload,
            "source_record_id": source_record_id,
            "fallback_source_job_id": fallback_source_job_id,
        }
        candidate = dict(row_context)
        candidate.update(
            {
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_text(result.get("fallback_reason")),
                "fallback_source_job_id": fallback_source_job_id,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": payload,
                "row_result": result,
                "business_entity_key": _first_text(
                    result.get("business_entity_key"),
                    payload.get("business_key"),
                    job.get("business_key"),
                    source_record_id,
                ),
                "browser_fallback_payload": _compact_mapping(browser_payload),
                "normalized_product_result": (
                    dict(result.get("normalized_product_result"))
                    if isinstance(result.get("normalized_product_result"), Mapping)
                    else {}
                ),
            }
        )
        candidates.append(candidate)
    return candidates

def _browser_after_browser_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    fallback_by_key = {
        str(candidate.get("fallback_key") or ""): candidate
        for candidate in _browser_fallback_candidates(store=store, request_id=request_id)
    }
    candidates: list[dict[str, Any]] = []
    for execution in _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback"):
        if _record_effective_status(execution) != "success":
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_text(payload.get("source_record_id"))
        fallback_key = _row_fallback_key(
            source_record_id=source_record_id,
            fallback_handler=fallback_handler,
        )
        fallback_candidate = fallback_by_key.get(fallback_key)
        if not fallback_candidate:
            continue
        execution_payload = extract_effective_result_payload(execution)
        if fallback_handler == "tiktok_product_browser_fetch":
            normalized = execution_payload.get("normalized_product_result")
            if not isinstance(normalized, Mapping) or not normalized:
                continue
        candidates.append(
            {
                **dict(fallback_candidate),
                "browser_execution_id": str(execution.execution_id),
                "browser_execution_payload": execution_payload,
            }
        )
    return candidates

def _media_sync_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    browser_by_row = {
        str((execution.payload or {}).get("source_record_id") or ""): execution for execution in browser_execs
    }
    tiktok_by_row: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        if str(job.get("job_code") or "") != "tiktok_product_request_fetch":
            continue
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if source_record_id:
            tiktok_by_row[source_record_id] = job

    candidates: list[dict[str, Any]] = []
    for row in _row_contexts(store=store, request_id=request_id):
        source_record_id = row["source_record_id"]
        tiktok_result = _effective_tiktok_result(
            tiktok_job=tiktok_by_row.get(source_record_id),
            browser_execution=browser_by_row.get(source_record_id),
        )
        product_result = dict(tiktok_result.get("normalized_product_result") or {})
        asset_refs = _collect_asset_refs(product_result)
        if not asset_refs:
            continue
        candidates.append({**row, "asset_refs": asset_refs})
    return candidates

def _fact_persist_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    browser_by_row = {
        str((execution.payload or {}).get("source_record_id") or ""): execution for execution in browser_execs
    }
    tiktok_by_row: dict[str, dict[str, Any]] = {}
    fastmoss_by_row: dict[str, dict[str, Any]] = {}
    media_by_row: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if not source_record_id:
            continue
        if str(job.get("job_code") or "") == "tiktok_product_request_fetch":
            tiktok_by_row[source_record_id] = job
        if str(job.get("job_code") or "") == "fastmoss_product_fetch":
            fastmoss_by_row[source_record_id] = job
    for job in media_jobs:
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if source_record_id:
            media_by_row[source_record_id] = job

    candidates: list[dict[str, Any]] = []
    for row in _row_contexts(store=store, request_id=request_id):
        source_record_id = row["source_record_id"]
        browser_execution = browser_by_row.get(source_record_id)
        tiktok_job = tiktok_by_row.get(source_record_id)
        fastmoss_job = fastmoss_by_row.get(source_record_id)
        media_job = media_by_row.get(source_record_id)
        tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
        fastmoss_result = extract_effective_result_payload(fastmoss_job)
        media_result = extract_effective_result_payload(media_job)
        if not tiktok_result and not fastmoss_result and not media_result:
            continue
        product_result = dict(tiktok_result.get("normalized_product_result") or {})
        fact_bundle = _merge_runtime_fact_bundles(
            dict(product_result.get("fact_bundle") or {}),
            dict(fastmoss_result.get("product_fact_bundle") or {}),
            dict(media_result.get("media_fact_bundle") or {}),
        )
        fact_bundle["source_record_id"] = source_record_id
        fact_bundle["product_identity"] = dict(row["product_identity"])
        fact_bundle["source_context"] = dict(row["source_context"])
        candidates.append(
            {
                **row,
                "fact_bundle": fact_bundle,
                "observation_at": str(int(time.time())),
                "product_id": str(
                    product_result.get("product_id")
                    or row.get("product_id")
                    or row["product_identity"].get("product_id")
                    or ""
                ),
            }
        )
    return candidates

def _persist_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    return _fact_persist_candidates(store=store, request_id=request_id)

def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    for job in reversed(jobs):
        payload = dict(job.get("payload") or {})
        if str(job.get("job_code") or "") != job_code:
            continue
        if str(payload.get("source_record_id") or "") == source_record_id:
            return job
    return None

def _latest_row_execution(executions: list[Any], *, source_record_id: str) -> Any | None:
    for execution in reversed(executions):
        if str((execution.payload or {}).get("source_record_id") or "") == source_record_id:
            return execution
    return None

def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if hasattr(record, "payload") and hasattr(record, "status"):
        result = getattr(record, "result", {}) or {}
        if isinstance(result, Mapping):
            if _is_unavailable_result(result):
                return "unavailable"
            handler_result = result.get("handler_result")
            if isinstance(handler_result, Mapping):
                if _is_unavailable_result(handler_result):
                    return "unavailable"
                return str(handler_result.get("status") or record.status or "")
        return str(getattr(record, "status", "") or "")
    if isinstance(record, Mapping):
        result = dict(record.get("result") or {})
        if _is_unavailable_result(result) or _is_unavailable_result(record):
            return "unavailable"
        handler_result = result.get("handler_result")
        if isinstance(handler_result, Mapping):
            if _is_unavailable_result(handler_result):
                return "unavailable"
            return str(handler_result.get("status") or record.get("status") or "")
        return str(record.get("status") or "")
    return ""

__all__ = [name for name in globals() if not name.startswith('__')]
