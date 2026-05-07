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
    recover_browser_fallback_resume_stage,
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
from .runtime_views import *


def _build_row_result(
    *,
    store: RuntimeStore,
    request_id: str,
    row_context: Mapping[str, Any],
) -> dict[str, Any]:
    source_record_id = str(row_context.get("source_record_id") or "")
    collect_jobs = [
        *_api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data"),
        *_api_jobs_for_stage(
            store=store,
            request_id=request_id,
            stage_code="resume_competitor_rows_after_browser_fallback",
        ),
    ]
    row_job = _latest_row_job(collect_jobs, source_record_id=source_record_id, job_code="competitor_row_refresh")
    row_payload = extract_effective_result_payload(row_job)
    step_timeline = row_payload.get("step_timeline") if isinstance(row_payload.get("step_timeline"), list) else []
    step_statuses = {
        str(item.get("step") or ""): str(item.get("status") or "")
        for item in step_timeline
        if isinstance(item, Mapping)
    }
    row_status = str(row_payload.get("row_status") or _record_effective_status(row_job) or "failed")
    return {
        "source_record_id": source_record_id,
        "product_id": str(row_context.get("product_id") or row_context["product_identity"].get("product_id") or ""),
        "row_status": row_status,
        "failure_reason": _row_failure_reason(row_job=row_job, row_payload=row_payload, row_status=row_status),
        "competitor_row_refresh_status": _record_effective_status(row_job),
        "tiktok_status": step_statuses.get("tiktok_request", ""),
        "browser_status": step_statuses.get("browser_fallback", ""),
        "media_status": step_statuses.get("media_sync", ""),
        "fastmoss_status": step_statuses.get("fastmoss_fetch", ""),
        "fact_status": step_statuses.get("fact_db_upsert", ""),
        "writeback_status": step_statuses.get("feishu_writeback", ""),
        "runtime_evidence": dict(row_payload.get("runtime_evidence") or {}) if isinstance(row_payload, Mapping) else {},
    }

def _row_failure_reason(
    *,
    row_job: Mapping[str, Any],
    row_payload: Mapping[str, Any],
    row_status: str,
) -> str:
    if row_status == "success":
        return ""
    for source in (row_payload, row_job):
        for key in ("failure_reason", "error_text", "error_message", "error_code"):
            value = _first_text(source.get(key) if isinstance(source, Mapping) else "")
            if value:
                return value
    result = row_job.get("result") if isinstance(row_job, Mapping) else {}
    handler_result = result.get("handler_result") if isinstance(result, Mapping) else {}
    error = handler_result.get("error") if isinstance(handler_result, Mapping) else {}
    if isinstance(error, Mapping):
        return _first_text(error.get("message"), error.get("error_code"), error.get("error_type"))
    step_timeline = row_payload.get("step_timeline") if isinstance(row_payload.get("step_timeline"), list) else []
    failed_steps = [
        _first_text(item.get("step"))
        for item in step_timeline
        if isinstance(item, Mapping) and _first_text(item.get("status")) == "failed"
    ]
    if failed_steps:
        return f"failed_steps={','.join(failed_steps)}"
    return f"row_status={row_status}" if row_status else "unknown"

def _collect_warnings(row_results: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for row in row_results:
        if row["row_status"] == "partial_success":
            warnings.append(f"row {row['source_record_id']} completed partially")
        if row["row_status"] == "failed":
            warnings.append(f"row {row['source_record_id']} failed")
    return warnings

__all__ = [name for name in globals() if not name.startswith('__')]
