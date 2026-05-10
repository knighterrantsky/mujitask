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


def _waiting_stage_result(*, current_stage: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "waiting", "current_stage": current_stage, "message": message}
    if details:
        payload["details"] = details
    return payload

def _advance_stage_result(*, next_stage: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "advance", "next_stage": next_stage}
    if details:
        payload["details"] = details
    return payload

def _count_product_group_statuses(group_summaries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group in group_summaries:
        status = str(group.get("final_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts

def _derive_final_status(group_summaries: list[dict[str, Any]]) -> str:
    if not group_summaries:
        return "failed"
    status_counts = _count_product_group_statuses(group_summaries)
    if status_counts.get("failed", 0) == len(group_summaries):
        return "failed"
    if status_counts.get("failed", 0) > 0 or status_counts.get("partial_success", 0) > 0:
        return "partial_success"
    return "success"

def _build_summary_warnings(group_summaries: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for group in group_summaries:
        for warning in list(group.get("warnings") or []):
            if isinstance(warning, str) and warning and warning not in warnings:
                warnings.append(warning)
    return warnings

__all__ = [name for name in globals() if not name.startswith('__')]
