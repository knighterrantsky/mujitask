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


ACTIVE_API_JOB_STATUSES = {"pending", "running"}

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

RUNTIME_DB_PASSTHROUGH_KEYS = (
    "execution_control_db_url",
    "db_url",
)

FASTMOSS_BROWSER_PASSTHROUGH_KEYS = (
    "browser_profile_ref",
    "browser_profile_id",
    "browser_provider_name",
    "browser_workspace_id",
    "browser_headless",
    "browser_force_open",
    "browser_timeout_ms",
    "fastmoss_browser_profile_ref",
    "fastmoss_browser_profile_id",
    "fastmoss_browser_provider_name",
    "fastmoss_browser_workspace_id",
    "fastmoss_browser_timeout_ms",
    "fastmoss_slider_max_attempts",
    "fastmoss_slider_appear_timeout_ms",
    "fastmoss_slider_settle_ms",
    "fastmoss_slider_confirm_ms",
    "mock_fastmoss_security_browser_resolve",
)

__all__ = [name for name in globals() if not name.startswith('__')]
