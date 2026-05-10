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


OPTIONAL_FINAL_STATUS_CODES: tuple[str, ...] = ()

FACT_BUNDLE_LIST_KEYS = (
    "products",
    "product_skus",
    "shops",
    "creators",
    "videos",
    "media_assets",
    "raw_api_responses",
    "raw_entity_links",
    "product_metric_snapshots",
    "product_daily_metrics",
    "product_distribution_snapshots",
    "product_sku_metric_snapshots",
)

FACT_BUNDLE_RELATION_KEYS = (
    "product_shops",
    "creator_products",
    "creator_videos",
    "video_products",
    "shop_creators",
)

FEISHU_READ_PASSTHROUGH_KEYS = (
    "access_token",
    "access_token_env",
    "feishu_access_token",
    "feishu_table",
    "field_names",
    "pagination",
    "product_id",
    "product_url",
    "raw_rows",
    "read_policy",
    "records",
    "snapshot_policy",
    "source_record_ids",
    "source_table_url",
    "table_refs",
    "table_url",
    "validate_schema",
)

FEISHU_WRITE_PASSTHROUGH_KEYS = (
    "access_token",
    "access_token_env",
    "feishu_access_token",
    "feishu_table",
    "raw_capture_policy",
    "table_refs",
    "table_url",
    "target_table_url",
    "validate_schema",
    "write_policy",
)

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

FACT_PERSISTENCE_PASSTHROUGH_KEYS = (
    "db_url",
    "fact_db_url",
    "persistence",
)

ARTIFACT_PASSTHROUGH_KEYS = (
    "artifact_bucket",
    "artifact_object_prefix",
    "artifact_root",
    "artifact_store",
    "artifact_store_provider",
    "db_url",
    "execution_control_fact_db_url",
    "fact_db_url",
    "minio_access_key",
    "minio_create_bucket",
    "minio_endpoint",
    "minio_region",
    "minio_secret_key",
    "minio_secure",
    "persistence",
)

DEFAULT_COMPETITOR_AUTO_FIELDS = (
    "产品链接",
    "SKU-ID",
    "图片",
    "标题",
    "节日",
    "卖家",
    "价格",
    "Fastmoss价格",
    "昨日销量",
    "近7天销量",
    "近90天销量",
    "记录日期",
)

DEFAULT_COMPETITOR_READ_FIELDS = (*DEFAULT_COMPETITOR_AUTO_FIELDS, "商品状态")

DEFAULT_COMPETITOR_FILTER_SPEC = {
    "candidate_policy": "missing_auto_maintained_fields",
    "skip_product_status": ["已下架/区域不可售"],
}

SUPPORTED_REFRESH_TASK_CODES = {"refresh_current_competitor_table", "refresh_competitor_row_by_url"}

__all__ = [name for name in globals() if not name.startswith('__')]
