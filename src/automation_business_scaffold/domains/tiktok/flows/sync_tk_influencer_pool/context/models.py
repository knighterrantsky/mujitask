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


SYNC_TK_INFLUENCER_POOL_WORKFLOW = get_workflow_definition("sync_tk_influencer_pool")

WORKFLOW_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.workflow_code

TASK_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.task_code

ENTRY_STAGE_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.entry_stage_code

SUMMARY_STAGE_CODE = SYNC_TK_INFLUENCER_POOL_WORKFLOW.summary_policy.summary_stage_code

FINAL_STAGE_CODE = "completed"

READ_STAGE_CODE = "read_competitor_candidates"

DISPATCH_PRODUCT_STAGE_CODE = "dispatch_product_jobs"

DISCOVER_CREATORS_STAGE_CODE = "discover_related_creators"

SYNC_INFLUENCER_POOL_STAGE_CODE = "sync_influencer_pool"

COLLECT_CREATOR_STAGE_CODE = "collect_creator_detail"

PERSIST_FACTS_STAGE_CODE = "persist_creator_facts"

WRITE_POOL_STAGE_CODE = "write_influencer_pool"

FINALIZE_PRODUCT_STAGE_CODE = "finalize_product"

WRITEBACK_STAGE_CODE = "writeback_competitor_status"

STAGE_TO_JOB_CODE = {
    READ_STAGE_CODE: "feishu_table_read",
    DISCOVER_CREATORS_STAGE_CODE: "product_creator_discovery",
    SYNC_INFLUENCER_POOL_STAGE_CODE: "influencer_creator_sync",
    COLLECT_CREATOR_STAGE_CODE: "fastmoss_creator_fetch",
    PERSIST_FACTS_STAGE_CODE: "fact_bundle_upsert",
    WRITE_POOL_STAGE_CODE: "feishu_table_write",
    WRITEBACK_STAGE_CODE: "feishu_table_write",
}

WAITING_STAGES = {
    READ_STAGE_CODE,
    DISCOVER_CREATORS_STAGE_CODE,
    SYNC_INFLUENCER_POOL_STAGE_CODE,
    WRITEBACK_STAGE_CODE,
}

ACTIVE_STATUSES = {"pending", "running"}

SUCCESSFUL_HANDLER_STATUSES = {"success", "partial_success"}

TERMINAL_HANDLER_STATUSES = {"success", "skipped", "partial_success", "failed", "fallback_required"}

__all__ = [name for name in globals() if not name.startswith('__')]
