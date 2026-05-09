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


def _row_refresh_jobs_for_summary(*, store: RuntimeStore, request_id: str) -> list[dict[str, Any]]:
    return _api_jobs_for_stage(store, request_id=request_id, stage_code="collect_selection_rows")

def _selection_row_has_after_browser_terminal(
    store: RuntimeStore,
    *,
    request_id: str,
    source_record_id: str,
) -> bool:
    row_job = _latest_row_job(
        [
            job
            for job in _api_jobs_for_stage(
                store,
                request_id=request_id,
                stage_code="collect_selection_rows",
            )
            if _mapping(job.get("payload")).get("browser_fallback_resolved")
        ],
        source_record_id=source_record_id,
        job_code="selection_row_refresh",
    )
    return _handler_status_from_api_job(row_job) in {"success", "partial_success", "failed", "skipped"}

def _selection_row_browser_fallback_candidates(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in _api_jobs_for_stage(
        store,
        request_id=request_id,
        stage_code="collect_selection_rows",
    ):
        if str(job.get("job_code") or "") != "selection_row_refresh":
            continue
        if not is_fallback_required(job):
            continue
        row_payload = dict(job.get("payload") or {})
        handler_result = _job_handler_result(job)
        handler_summary = _mapping(handler_result.get("summary"))
        result_payload = extract_effective_result_payload(job)
        fallback_handler = _first_non_empty(
            result_payload.get("fallback_handler"),
            handler_summary.get("fallback_handler"),
        )
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = _mapping(result_payload.get("browser_fallback_payload"))
        if not browser_payload:
            next_action_payload = _mapping(_mapping(handler_result.get("next_action")).get("payload"))
            browser_payload = _mapping(next_action_payload.get("payload")) or next_action_payload
        source_record_id = _first_non_empty(
            result_payload.get("source_record_id"),
            row_payload.get("source_record_id"),
        )
        if _selection_row_has_after_browser_terminal(
            store=store,
            request_id=request_id,
            source_record_id=source_record_id,
        ):
            continue
        business_entity_key = _first_non_empty(
            result_payload.get("business_entity_key"),
            row_payload.get("business_key"),
            job.get("business_key"),
            source_record_id,
        )
        fallback_source_job_id = _first_non_empty(
            browser_payload.get("fallback_source_job_id"),
            result_payload.get("fallback_source_job_id"),
            job.get("job_id"),
        )
        browser_payload = {
            **browser_payload,
            "source_record_id": source_record_id,
            "business_entity_key": business_entity_key,
            "fallback_source_job_id": fallback_source_job_id,
        }
        product_identity = _mapping(result_payload.get("product_identity")) or _mapping(
            row_payload.get("product_identity")
        )
        candidates.append(
            {
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    business_entity_key=business_entity_key,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_non_empty(result_payload.get("fallback_reason")),
                "source_record_id": source_record_id,
                "business_entity_key": business_entity_key,
                "candidate_key": business_entity_key,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": row_payload,
                "row_result": result_payload,
                "browser_fallback_payload": compact_dict(browser_payload),
                "product_identity": product_identity,
                "normalized_product_url": _first_non_empty(
                    browser_payload.get("normalized_product_url"),
                    row_payload.get("normalized_product_url"),
                    product_identity.get("normalized_product_url"),
                ),
                "normalized_product_result": _mapping(
                    result_payload.get("normalized_product_result")
                ),
            }
        )
    return candidates

def _selection_row_after_browser_candidates(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    fallback_by_key = {
        str(candidate.get("fallback_key") or ""): candidate
        for candidate in _selection_row_browser_fallback_candidates(store=store, request_id=request_id)
    }
    candidates: list[dict[str, Any]] = []
    for execution in _browser_executions_for_stage(
        store,
        request_id=request_id,
        stage_code="selection_row_browser_fallback",
    ):
        if _handler_status_from_execution(execution) != "success":
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_non_empty(payload.get("source_record_id"))
        business_entity_key = _first_non_empty(payload.get("business_entity_key"))
        fallback_key = _row_fallback_key(
            source_record_id=source_record_id,
            business_entity_key=business_entity_key,
            fallback_handler=fallback_handler,
        )
        fallback_candidate = fallback_by_key.get(fallback_key)
        if not fallback_candidate:
            continue
        execution_payload = extract_effective_result_payload(execution)
        if fallback_handler == "tiktok_product_browser_fetch" and not _mapping(
            execution_payload.get("normalized_product_result")
        ):
            continue
        candidates.append(
            {
                **dict(fallback_candidate),
                "browser_execution_id": str(execution.execution_id),
                "browser_execution_payload": execution_payload,
            }
        )
    return candidates

def _resolve_candidate_rows(
    store: RuntimeStore,
    *,
    request: Any,
    request_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    product_url = str(request_payload.get("product_url") or "").strip()
    product_id = str(request_payload.get("product_id") or "").strip()
    selection_record_id = str(request_payload.get("selection_record_id") or "").strip()

    if product_url or product_id:
        identity = _resolve_product_identity(request_payload)
        return [
            {
                "source_record_id": selection_record_id,
                "product_identity": identity,
                "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                "source_context": {},
            }
        ]

    read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )
    if not read_job:
        return []

    handler_result = _job_handler_result(read_job)
    nested_result = (
        handler_result.get("result") if isinstance(handler_result.get("result"), Mapping) else {}
    )
    source_rows = (nested_result or handler_result).get("source_rows") or []
    if not isinstance(source_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if not isinstance(row, Mapping):
            continue
        rows.append(
            {
                "source_record_id": str(row.get("source_record_id") or ""),
                "product_identity": _mapping(row.get("product_identity")),
                "source_table_ref": str(
                    row.get("source_table_ref") or request_payload.get("selection_table_ref") or ""
                ),
                "source_context": _mapping(row.get("source_context")),
            }
        )
    return rows

def _aggregate_request_children(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    counts: dict[str, int] = {}
    success_count = 0
    failed_count = 0
    skipped_count = 0
    active_count = 0

    for job in api_jobs:
        handler_status = _handler_status_from_api_job(job)
        if str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            pass
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or str(job.get("status") or "unknown")
        counts[status_key] = counts.get(status_key, 0) + 1

    for execution in executions:
        handler_status = _handler_status_from_execution(execution)
        if execution.status in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            pass
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or execution.status or "unknown"
        counts[status_key] = counts.get(status_key, 0) + 1

    total = len(api_jobs) + len(executions)
    terminal_count = max(total - active_count, 0)
    return {
        "total": total,
        "counts": counts,
        "terminal_count": terminal_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "active_count": active_count,
    }

def _api_jobs_for_stage(
    store: RuntimeStore, *, request_id: str, stage_code: str
) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]

def _latest_api_job_by_code(jobs: list[dict[str, Any]], job_code: str) -> dict[str, Any]:
    for job in reversed(jobs):
        if str(job.get("job_code") or "") == job_code:
            return job
    return {}

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

def _any_api_jobs_active(jobs: list[dict[str, Any]]) -> bool:
    return any(str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES for job in jobs)

def _any_failed_api_jobs(jobs: list[dict[str, Any]]) -> bool:
    return any(_job_failed(job) for job in jobs)

def _handler_status_from_api_job(job: Mapping[str, Any] | None) -> str:
    if not job:
        return ""
    handler_result = _job_handler_result(job)
    return str(handler_result.get("status") or job.get("result_status") or job.get("status") or "")

def _handler_status_from_execution(execution: Any) -> str:
    if execution is None:
        return ""
    result = dict(execution.result or {})
    handler_result = result.get("handler_result")
    if isinstance(handler_result, Mapping):
        return str(handler_result.get("status") or getattr(execution, "result_status", "") or execution.status or "")
    return str(getattr(execution, "result_status", "") or execution.status or "")

def _job_handler_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    handler_result = result.get("handler_result")
    return dict(handler_result or {}) if isinstance(handler_result, Mapping) else {}

def _job_effective_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    if "handler_result" in result:
        result = {key: value for key, value in result.items() if key != "handler_result"}
    return result

def _job_failed(job: Mapping[str, Any] | None) -> bool:
    if not job:
        return False
    return str(job.get("status") or "") == "failed" or _handler_status_from_api_job(job) == "failed"

__all__ = [name for name in globals() if not name.startswith('__')]
