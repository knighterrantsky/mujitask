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


OPTIONAL_FINAL_STATUS_CODES = ("tiktok_product_browser_fetch",)

TIKTOK_REQUEST_PASSTHROUGH_KEYS = (
    "fallback_reason",
    "force_failure",
    "force_fallback",
    "mock_response",
    "normalized_product_result",
    "raw_request_result",
    "request_result",
    "source_payload",
    "tiktok_request_result",
)

FASTMOSS_PRODUCT_PASSTHROUGH_KEYS = (
    "fastmoss_bundle",
    "fastmoss_result",
    "mock_fastmoss_bundle",
    "product_fact_bundle",
    "required",
)

RUNTIME_DB_PASSTHROUGH_KEYS: tuple[str, ...] = ()

FASTMOSS_BROWSER_PASSTHROUGH_KEYS = (
    "browser_headless",
    "browser_force_open",
    "browser_timeout_ms",
    "fastmoss_browser_timeout_ms",
    "fastmoss_slider_max_attempts",
    "fastmoss_slider_appear_timeout_ms",
    "fastmoss_slider_settle_ms",
    "fastmoss_slider_confirm_ms",
    "mock_fastmoss_security_browser_resolve",
)

FACT_PERSISTENCE_PASSTHROUGH_KEYS = (
    "persistence",
    "require_database_persistence",
    "requires_fact_db",
)

ARTIFACT_PASSTHROUGH_KEYS = (
    "artifact_store",
    "require_object_storage",
    "requires_object_storage",
)

@dataclass(frozen=True)
class StageContext:
    request_id: str
    task_code: str
    workflow_code: str
    stage_code: str
    payload: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class SummaryInputs:
    candidate_contexts: list[dict[str, Any]]
    row_results: list[dict[str, Any]]
    child_records: list[Any]

__all__ = [name for name in globals() if not name.startswith('__')]
