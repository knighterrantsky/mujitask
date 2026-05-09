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


def _determine_final_status(
    *,
    force_result: Mapping[str, Any] | None,
    row_jobs: list[dict[str, Any]],
    row_results: list[dict[str, Any]],
    counts: Mapping[str, Any],
) -> str:
    if force_result and str(force_result.get("final_status") or "") in {
        "success",
        "partial_success",
        "failed",
    }:
        return str(force_result["final_status"])
    if row_results:
        row_statuses = {str(row.get("row_status") or "") for row in row_results}
        if row_statuses <= {"success", "skipped"} and "success" in row_statuses:
            return "success"
        if row_statuses <= {"skipped"}:
            return "success"
        if row_statuses & {"success", "partial_success", "skipped"}:
            return "partial_success"
        return "failed"
    if not row_jobs:
        return "failed"
    failed_count = int(counts.get("failed_count") or 0)
    success_count = int(counts.get("success_count") or 0)
    if success_count == 0:
        return "failed"
    if failed_count > 0:
        return "partial_success"
    return "success"

__all__ = [name for name in globals() if not name.startswith('__')]
