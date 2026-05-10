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


def _browser_stage_from_premature_summary(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    current_stage: str,
) -> str:
    from .runtime_views import _browser_fallback_candidates

    if current_stage != workflow.summary_policy.summary_stage_code:
        return ""
    if _browser_fallback_candidates(store=store, request_id=request.request_id):
        return "browser_fallback"
    return ""

def _resolve_final_status_from_rows(
    *,
    workflow: WorkflowDefinition,
    row_results: list[dict[str, Any]],
    child_records: list[Any],
    explicit_status: str,
) -> str:
    fallback_status = compute_final_status(
        workflow.summary_policy,
        child_records=child_records,
        optional_codes=OPTIONAL_FINAL_STATUS_CODES,
        explicit_status=explicit_status,
    )
    if not row_results:
        return fallback_status
    row_statuses = {str(item.get("row_status") or "") for item in row_results if str(item.get("row_status") or "")}
    if row_statuses == {"success"}:
        return "success"
    if row_statuses == {"failed"}:
        return "failed"
    if "failed" in row_statuses and "success" not in row_statuses and "partial_success" not in row_statuses:
        return "failed"
    if "failed" in row_statuses or "partial_success" in row_statuses:
        return "partial_success"
    return fallback_status

def _derive_row_status(
    *,
    tiktok_job: Mapping[str, Any] | None,
    fastmoss_job: Mapping[str, Any] | None,
    browser_execution: Any,
    media_job: Mapping[str, Any] | None,
    fact_job: Mapping[str, Any] | None,
    write_job: Mapping[str, Any] | None,
) -> str:
    statuses = [
        _record_effective_status(tiktok_job),
        _record_effective_status(fastmoss_job),
        _record_effective_status(browser_execution),
        _record_effective_status(media_job),
        _record_effective_status(fact_job),
        _record_effective_status(write_job),
    ]
    if "unavailable" in statuses:
        return "unavailable"
    if str((write_job or {}).get("status") or "") == "success" and "failed" not in statuses:
        return "success"
    if str((fact_job or {}).get("status") or "") == "success" and "failed" not in statuses:
        return "success"
    if _record_effective_status(tiktok_job) == "failed" and _record_effective_status(fastmoss_job) != "success":
        return "failed"
    if _record_effective_status(tiktok_job) == "fallback_required" and _record_effective_status(browser_execution) in {"", "pending"}:
        return "partial_success"
    if "success" in statuses or "partial_success" in statuses:
        if "failed" in statuses or "fallback_required" in statuses:
            return "partial_success"
        return "partial_success"
    if "failed" in statuses:
        return "failed"
    return "failed"

def _is_unavailable_result(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("availability_status") or payload.get("status") or "").strip().lower() == "unavailable":
        return True
    effective = extract_effective_result_payload(payload)
    if effective and effective is not payload and _is_unavailable_result(effective):
        return True
    normalized = payload.get("normalized_product_result")
    if isinstance(normalized, Mapping):
        if _is_unavailable_result(normalized):
            return True
    logical_fields = payload.get("logical_fields")
    if isinstance(logical_fields, Mapping) and _is_unavailable_result(logical_fields):
        return True
    product = payload.get("product")
    if isinstance(product, Mapping):
        if str(product.get("availability_status") or "").strip().lower() == "unavailable":
            return True
        facts = product.get("facts")
        if isinstance(facts, Mapping) and _is_unavailable_result(facts):
            return True
    return False

def _is_fallback_required(job: Mapping[str, Any]) -> bool:
    if not isinstance(job, Mapping):
        return False
    result = dict(job.get("result") or {})
    handler_result = dict(result.get("handler_result") or {})
    if str(handler_result.get("status") or "") == "fallback_required":
        return True
    payload = extract_effective_result_payload(job)
    return bool(payload.get("fallback_required"))

def _waiting(*, stage_code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
    }
    if details:
        payload["details"] = dict(details)
    return payload

__all__ = [name for name in globals() if not name.startswith('__')]
