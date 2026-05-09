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


def _fastmoss_security_browser_fallback_cursor(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    return dict(stage_results.get("fastmoss_security_browser_fallback") or {})

def _fastmoss_security_browser_fallback_attempted(*, store: RuntimeStore, request_id: str) -> bool:
    if _fastmoss_security_browser_fallback_cursor(store=store, request_id=request_id):
        return True
    return bool(
        _browser_executions_for_stage(
            store=store,
            request_id=request_id,
            stage_code="fastmoss_security_browser_fallback",
        )
    )

def _candidate_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_candidates = keyword_import.get("candidate_contexts")
    if isinstance(import_candidates, list):
        return [dict(item) for item in import_candidates if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    candidates = import_payload.get("normalized_candidates")
    if isinstance(candidates, list):
        return [dict(item) for item in candidates if isinstance(item, Mapping)]

    processed = dict(stage_results.get("process_product_candidates") or {})
    legacy_candidates = processed.get("candidate_contexts")
    if isinstance(legacy_candidates, list):
        return [dict(item) for item in legacy_candidates if isinstance(item, Mapping)]

    search_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="search_product_candidates")
    search_job = select_latest_successful_api_job(search_jobs, "fastmoss_product_search")
    search_payload = extract_effective_result_payload(search_job)
    return _normalize_search_candidates(
        search_payload.get("candidates"),
        search_query=str(request.payload.get("search_query") or ""),
        output_conditions=dict(request.payload.get("output_conditions") or {}),
        max_candidates=int(request.payload.get("max_candidates") or 0),
    )

def _keyword_seed_import_payload(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    return {**import_payload, **keyword_import}

def _seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_seeds = keyword_import.get("seed_contexts")
    if isinstance(import_seeds, list):
        return [dict(item) for item in import_seeds if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    seeds = import_payload.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]

    inserted = dict(stage_results.get("insert_seed_rows") or {})
    seeds = inserted.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]
    return _build_seed_contexts(
        candidates=_candidate_contexts(store=store, request_id=request_id),
        jobs=_api_jobs_for_stage(store=store, request_id=request_id, stage_code="insert_seed_rows"),
    )

def _seed_context_by_candidate_key(store: RuntimeStore, *, request_id: str) -> dict[str, dict[str, Any]]:
    return {str(item.get("candidate_key") or ""): item for item in _seed_contexts(store=store, request_id=request_id)}

__all__ = [name for name in globals() if not name.startswith('__')]
