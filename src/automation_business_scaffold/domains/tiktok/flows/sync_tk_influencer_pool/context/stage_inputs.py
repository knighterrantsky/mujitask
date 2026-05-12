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


def _build_creator_detail_jobs(*, request: Any, product_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(COLLECT_CREATOR_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    jobs_to_enqueue: list[dict[str, Any]] = []
    for product_job in product_jobs:
        if extract_handler_result_status(product_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(product_job)
        source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(
            source_context.get("product_id"),
            result_payload.get("product_id"),
            ((product_job.get("payload") or {}).get("product_identity") or {}).get("product_id"),
        )
        product_context = _product_job_business_context(product_job)
        if not source_record_id or not product_id:
            continue
        for creator in list(result_payload.get("related_creators") or []):
            if not isinstance(creator, Mapping):
                continue
            creator_identity = _normalize_creator_identity(creator.get("creator_identity"), creator)
            creator_id = _first_non_empty(
                creator_identity.get("creator_id"),
                creator.get("creator_id"),
                creator.get("influencer_id"),
            )
            if not creator_id:
                continue
            template_context = {
                "source_record_id": source_record_id,
                "product_id": product_id,
                "creator_id": creator_id,
                "product_id_or_group": product_id,
            }
            keys = render_job_keys(
                resolved_job,
                template_context,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=COLLECT_CREATOR_STAGE_CODE,
            )
            jobs_to_enqueue.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": COLLECT_CREATOR_STAGE_CODE,
                        "creator_identity": creator_identity,
                        "detail_level": _creator_detail_level_from_request(request_payload),
                        **_fastmoss_common_payload(request_payload),
                        "fetch_plan": _creator_fetch_plan_from_request(request_payload),
                        "relation_policy": _creator_relation_policy_from_request(request_payload),
                        "source_context": {
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                            "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
                            "creator_candidate": dict(creator),
                            "product_job_id": str(product_job.get("job_id") or ""),
                            **product_context,
                            **_creator_candidate_business_context(creator),
                        },
                    },
                }
            )
    return jobs_to_enqueue

def _build_influencer_creator_sync_jobs(*, request: Any, product_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(SYNC_INFLUENCER_POOL_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    creator_groups: dict[str, dict[str, Any]] = {}
    product_candidate_counts: dict[str, int] = {}

    for product_job in product_jobs:
        if extract_handler_result_status(product_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(product_job)
        source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
        product_context = _product_job_business_context(product_job)
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(
            source_context.get("product_id"),
            result_payload.get("product_id"),
            ((product_job.get("payload") or {}).get("product_identity") or {}).get("product_id"),
        )
        if not source_record_id or not product_id:
            continue
        product_key = _product_group_key(source_record_id=source_record_id, product_id=product_id)
        candidates = _creator_candidates_from_result_payload(result_payload)
        product_candidate_counts[product_key] = len(candidates)
        for creator in candidates:
            creator_identity = _normalize_creator_identity(creator.get("creator_identity"), creator)
            creator_id = _first_non_empty(
                creator_identity.get("creator_id"),
                creator.get("creator_id"),
                creator.get("influencer_id"),
                creator_identity.get("unique_id"),
                creator_identity.get("uid"),
            )
            if not creator_id:
                continue
            group = creator_groups.setdefault(
                creator_id,
                {
                    "creator_id": creator_id,
                    "creator_identity": creator_identity,
                    "product_hits": [],
                    "seen_product_keys": set(),
                },
            )
            if not group.get("creator_identity"):
                group["creator_identity"] = creator_identity
            seen_product_keys = group["seen_product_keys"]
            if product_key in seen_product_keys:
                continue
            seen_product_keys.add(product_key)
            hit_context = dict(result_payload.get("product_hit_context") or {})
            group["product_hits"].append(
                {
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "product_key": product_key,
                    "product_identity": dict((product_job.get("payload") or {}).get("product_identity") or {}),
                    "creator_candidate": dict(creator),
                    "product_job_id": str(product_job.get("job_id") or ""),
                    "product_hit_context": hit_context,
                    **product_context,
                    **_creator_candidate_business_context(creator),
                }
            )

    jobs_to_enqueue: list[dict[str, Any]] = []
    for creator_id, group in creator_groups.items():
        product_hits: list[dict[str, Any]] = []
        for hit in group["product_hits"]:
            hit_payload = dict(hit)
            product_key = _first_non_empty(hit_payload.get("product_key"))
            creator_count = int(product_candidate_counts.get(product_key, 0))
            hit_payload["product_group_creator_count"] = creator_count
            hit_payload["product_group_terminal"] = creator_count <= 1
            product_hits.append(hit_payload)
        keys = render_job_keys(
            resolved_job,
            {"creator_id": creator_id},
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        )
        jobs_to_enqueue.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE,
                    "request_payload": request_payload,
                    "creator_identity": dict(group["creator_identity"]),
                    "creator_id": creator_id,
                    "product_hits": product_hits,
                    "detail_level": _creator_detail_level_from_request(request_payload),
                    "fetch_plan": _creator_fetch_plan_from_request(request_payload),
                    "relation_policy": _creator_relation_policy_from_request(request_payload),
                    "sync_plan": {
                        "creator_detail_handler": "fastmoss_creator_fetch",
                        "fact_upsert_handler": "fact_bundle_upsert",
                        "media_sync_handler": "media_asset_sync",
                        "influencer_pool_write_handler": "feishu_table_write",
                        "competitor_status_write_handler": "feishu_table_write",
                    },
                    "requires_fact_db": True,
                    "requires_object_storage": True,
                    "require_database_persistence": True,
                    "require_object_storage": True,
                    **_fastmoss_common_payload(request_payload),
                    **_feishu_common_payload(request_payload),
                },
            }
        )
    return jobs_to_enqueue

def _build_fact_upsert_jobs(
    *,
    request: Any,
    product_jobs: list[dict[str, Any]],
    creator_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.require_job("fact_bundle_upsert")
    request_payload = dict(request.payload or {})
    jobs_to_enqueue: list[dict[str, Any]] = []
    seen_dedupe: set[str] = set()

    for product_job in product_jobs:
        if extract_handler_result_status(product_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(product_job)
        fact_bundle = merge_fact_bundles(dict(result_payload.get("product_fact_bundle") or {}))
        entity_keys = bundle_entity_keys(fact_bundle)
        if not entity_keys:
            continue
        source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"), _job_product_id(product_job))
        dedupe_key = f"{request.request_id}:{PERSIST_FACTS_STAGE_CODE}:product:{source_record_id}:{product_id}"
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        jobs_to_enqueue.append(
            {
                "business_key": ",".join(entity_keys),
                "dedupe_key": dedupe_key,
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_payload": request_payload,
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": PERSIST_FACTS_STAGE_CODE,
                    "source_job_ids": [str(product_job.get("job_id") or "")],
                    "source_context": {
                        **source_context,
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                    },
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "fact_subject": "product",
                    },
                    "entity_business_keys": ",".join(entity_keys),
                    "observation_at": _first_non_empty(result_payload.get("observed_at")),
                    "fact_bundle": fact_bundle,
                    "requires_fact_db": True,
                    "require_database_persistence": True,
                },
            }
        )

    for creator_job in creator_jobs:
        if extract_handler_result_status(creator_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(creator_job)
        fact_bundle = merge_fact_bundles(dict(result_payload.get("fact_bundle") or {}))
        entity_keys = bundle_entity_keys(fact_bundle)
        if not entity_keys:
            continue
        payload = dict(creator_job.get("payload") or {})
        source_context = dict(payload.get("source_context") or {})
        creator_identity = dict(payload.get("creator_identity") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"), _job_product_id(creator_job))
        creator_id = _first_non_empty(
            dict(result_payload.get("creator_fact_bundle") or {}).get("creator_id"),
            creator_identity.get("creator_id"),
            creator_identity.get("unique_id"),
            creator_identity.get("uid"),
        )
        dedupe_key = f"{request.request_id}:{PERSIST_FACTS_STAGE_CODE}:creator:{source_record_id}:{product_id}:{creator_id}"
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        jobs_to_enqueue.append(
            {
                "business_key": ",".join(entity_keys),
                "dedupe_key": dedupe_key,
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_payload": request_payload,
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": PERSIST_FACTS_STAGE_CODE,
                    "source_job_ids": [str(creator_job.get("job_id") or "")],
                    "source_context": {
                        **source_context,
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                    },
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                        "fact_subject": "creator",
                    },
                    "entity_business_keys": ",".join(entity_keys),
                    "observation_at": _first_non_empty(result_payload.get("observed_at")),
                    "fact_bundle": fact_bundle,
                    "requires_fact_db": True,
                    "require_database_persistence": True,
                },
            }
        )

    return jobs_to_enqueue

def _build_influencer_pool_write_jobs(
    *,
    request: Any,
    creator_jobs: list[dict[str, Any]],
    fact_jobs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITE_POOL_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    target_table_ref = _first_non_empty(
        request.payload.get("influencer_pool_table_ref"),
        request.payload.get("target_table_ref"),
        request.payload.get("target_table_url"),
        request.payload.get("source_table_ref"),
        request.payload.get("table_url"),
    )
    fact_success_keys = _successful_fact_persist_keys(fact_jobs or [])
    require_fact_success = bool(fact_jobs)
    jobs_to_enqueue: list[dict[str, Any]] = []
    for creator_job in creator_jobs:
        if extract_handler_result_status(creator_job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(creator_job)
        creator_fact_bundle = dict(result_payload.get("creator_fact_bundle") or {})
        source_context = dict((creator_job.get("payload") or {}).get("source_context") or {})
        source_record_id = _first_non_empty(source_context.get("source_record_id"))
        product_id = _first_non_empty(source_context.get("product_id"))
        creator_id = _first_non_empty(
            creator_fact_bundle.get("creator_id"),
            ((creator_job.get("payload") or {}).get("creator_identity") or {}).get("creator_id"),
        )
        if require_fact_success and _creator_fact_key(source_record_id, product_id, creator_id) not in fact_success_keys:
            continue
        if not target_table_ref or not source_record_id or not product_id or not creator_id:
            continue
        record = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "creator_id": creator_id,
            "creator_name": _first_non_empty(
                creator_fact_bundle.get("display_name"),
                creator_fact_bundle.get("nickname"),
                source_context.get("creator_candidate", {}).get("display_name") if isinstance(source_context.get("creator_candidate"), Mapping) else "",
            ),
            "creator_fact_bundle": creator_fact_bundle,
            "fact_bundle": dict(result_payload.get("fact_bundle") or {}),
            "entities": dict(result_payload.get("entities") or {}),
            "relations": list(result_payload.get("relations") or []),
            "observations": list(result_payload.get("observations") or []),
            "media_refs": list(result_payload.get("media_refs") or []),
            "product_relations": list(result_payload.get("product_relations") or []),
            "source_context": source_context,
            **_write_record_business_context(source_context, result_payload),
            "product_key": _product_group_key(source_record_id=source_record_id, product_id=product_id),
        }
        keys = render_job_keys(
            resolved_job,
            {
                "target_table_ref": target_table_ref,
                "business_entity_key": creator_id,
                "creator_id": creator_id,
                "product_id": product_id,
                "source_record_id": source_record_id,
            },
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=WRITE_POOL_STAGE_CODE,
        )
        jobs_to_enqueue.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": WRITE_POOL_STAGE_CODE,
                    "target_table_ref": target_table_ref,
                    "request_payload": request_payload,
                    "mapper_code": "influencer_pool_projection_mapper",
                    "write_mode": "upsert",
                    "records": [record],
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                        "creator_id": creator_id,
                    },
                    "business_entity_key": creator_id,
                    **_feishu_common_payload(request_payload),
                },
            }
        )
    return jobs_to_enqueue

def _build_competitor_status_write_jobs(*, request: Any, group_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved_job = SYNC_TK_INFLUENCER_POOL_WORKFLOW.resolve_stage_jobs(WRITEBACK_STAGE_CODE)[0]
    request_payload = dict(request.payload or {})
    target_table_ref = _first_non_empty(
        request.payload.get("competitor_status_table_ref"),
        request.payload.get("source_table_ref"),
        request.payload.get("source_table_url"),
        request.payload.get("table_url"),
    )
    jobs_to_enqueue: list[dict[str, Any]] = []
    for group in group_summaries:
        source_record_id = _first_non_empty(group.get("source_record_id"))
        product_id = _first_non_empty(group.get("product_id"))
        product_key = _first_non_empty(group.get("product_key"))
        if not target_table_ref or not source_record_id or not product_id or not product_key:
            continue
        record = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "product_key": product_key,
            "influencer_sync_status": group.get("final_status"),
            "creator_candidate_count": int(group.get("creator_candidate_count") or 0),
            "creator_detail_success_count": int(group.get("creator_detail_success_count") or 0),
            "creator_detail_failed_count": int(group.get("creator_detail_failed_count") or 0),
            "influencer_write_success_count": int(group.get("influencer_write_success_count") or 0),
            "warning_count": len(list(group.get("warnings") or [])),
            "warnings": list(group.get("warnings") or []),
        }
        keys = render_job_keys(
            resolved_job,
            {
                "target_table_ref": target_table_ref,
                "business_entity_key": product_key,
                "source_record_id": source_record_id,
                "product_id": product_id,
            },
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=WRITEBACK_STAGE_CODE,
        )
        jobs_to_enqueue.append(
            {
                "business_key": keys["business_key"],
                "dedupe_key": keys["dedupe_key"],
                "max_execution_seconds": _timeout_seconds_for(resolved_job.job_code),
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": WRITEBACK_STAGE_CODE,
                    "target_table_ref": target_table_ref,
                    "request_payload": request_payload,
                    "mapper_code": "competitor_influencer_status_projection_mapper",
                    "write_mode": "upsert",
                    "records": [record],
                    "idempotency_context": {
                        "source_record_id": source_record_id,
                        "product_id": product_id,
                    },
                    "business_entity_key": product_key,
                    **_feishu_common_payload(request_payload),
                },
            }
        )
    return jobs_to_enqueue

def _collect_creator_candidates_from_product_jobs(product_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in product_jobs:
        if extract_handler_result_status(job) not in SUCCESSFUL_HANDLER_STATUSES:
            continue
        result_payload = extract_effective_result_payload(job)
        for candidate in _creator_candidates_from_result_payload(result_payload):
            creator_id = _first_non_empty(
                candidate.get("creator_id"),
                candidate.get("influencer_id"),
                (candidate.get("creator_identity") or {}).get("creator_id") if isinstance(candidate.get("creator_identity"), Mapping) else "",
            )
            if not creator_id or creator_id in seen:
                continue
            seen.add(creator_id)
            candidates.append(dict(candidate))
    return candidates

def _creator_candidates_from_result_payload(result_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = (
        result_payload.get("normalized_creator_candidates")
        or result_payload.get("related_creators")
        or result_payload.get("creator_candidates")
        or []
    )
    candidates: list[dict[str, Any]] = []
    for candidate in list(raw_candidates):
        if isinstance(candidate, Mapping):
            candidates.append(dict(candidate))
    return candidates

def _timeout_seconds_for(job_code: str) -> float:
    for rule in SYNC_TK_INFLUENCER_POOL_WORKFLOW.timeout_policy:
        if str(rule.target_code or "") == job_code:
            return float(rule.timeout_seconds)
    return 0.0

def _build_candidate_filter(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    filter_spec = dict(request_payload.get("candidate_filter") or {})
    filter_spec.setdefault("candidate_status", ["", "待查找", "失败重试", "处理中"])
    filter_spec.setdefault("skip_product_status", ["已下架/区域不可售"])
    source_record_ids = list(request_payload.get("source_record_ids") or [])
    if source_record_ids:
        filter_spec["source_record_ids"] = source_record_ids
    return filter_spec

def _source_table_ref_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("source_table_ref"),
        request_payload.get("source_table_url"),
        request_payload.get("table_url"),
    )

def _view_ref_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("view_ref"),
        request_payload.get("view_id"),
        request_payload.get("source_view_ref"),
        request_payload.get("source_view_id"),
    )

def _feishu_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "access_token",
        "access_token_env",
        "feishu_access_token",
        "feishu_access_token_env",
        "table_refs",
        "feishu_table",
        "source_table_url",
        "target_table_url",
    ):
        value = request_payload.get(key)
        if value not in (None, "", {}, []):
            payload[key] = value
    return payload

def _fastmoss_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {})
    phone_env = _first_non_empty(request_payload.get("fastmoss_phone_env"), settings.get("phone_env"))
    password_env = _first_non_empty(request_payload.get("fastmoss_password_env"), settings.get("password_env"))
    phone = _first_non_empty(settings.get("phone"), request_payload.get("fastmoss_phone"), os.environ.get(phone_env, ""))
    password = _first_non_empty(
        settings.get("password"),
        request_payload.get("fastmoss_password"),
        os.environ.get(password_env, ""),
    )
    if phone:
        settings["phone"] = phone
    if password:
        settings["password"] = password
    for source_key, target_key in (
        ("fastmoss_region", "region"),
        ("fastmoss_base_url", "base_url"),
        ("fastmoss_timeout", "timeout"),
        ("fastmoss_window_days", "window_days"),
        ("fastmoss_ensure_logged_in", "ensure_logged_in"),
        ("verify_fastmoss_login", "ensure_logged_in"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", {}, []):
            settings.setdefault(target_key, value)
    if "live_fetch" not in settings and settings:
        settings["live_fetch"] = True
    return {"fastmoss": settings} if settings else {}

def _relation_policy_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = dict(request_payload.get("relation_policy") or {})
    for key in ("creator_sold_count_min", "creator_follower_count_min"):
        value = request_payload.get(key)
        if value not in (None, ""):
            policy.setdefault(key, value)
    return policy

def _creator_relation_policy_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = _relation_policy_from_request(request_payload)
    policy.setdefault("include_source_product_relation", True)
    if "min_source_product_sold_count" not in policy and policy.get("creator_sold_count_min") not in (None, ""):
        policy["min_source_product_sold_count"] = policy["creator_sold_count_min"]
    return policy

def _creator_fetch_plan_from_request(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    fetch_plan = dict(request_payload.get("creator_fetch_plan") or request_payload.get("fetch_plan") or {})
    fetch_plan.setdefault("date_type", request_payload.get("fastmoss_window_days") or 28)
    fetch_plan.setdefault(
        "endpoints",
        ["base_info", "author_index", "stat_info", "contact", "cargo_summary", "shop_list", "goods_list", "video_list"],
    )
    return fetch_plan

def _creator_detail_level_from_request(request_payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        request_payload.get("creator_detail_level"),
        request_payload.get("detail_level"),
        "profile_metrics_contact_goods_video",
    )

def _candidate_business_context(candidate: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(candidate.get("candidate_row") or {})
    business_fields = dict(row.get("business_fields") or {})
    source_context = dict(row.get("source_context") or {})
    source_fields = dict(source_context.get("source_fields") or {})
    return {
        "source_table_ref": _first_non_empty(row.get("source_table_ref"), source_context.get("source_table_ref")),
        "holiday": _first_non_empty(business_fields.get("holiday"), source_fields.get("节日")),
        "product_status": _first_non_empty(business_fields.get("product_status"), source_fields.get("商品状态")),
        "source_product_images": _source_product_images_from_fields(source_fields),
    }

def _product_job_business_context(product_job: Mapping[str, Any]) -> dict[str, Any]:
    source_context = dict((product_job.get("payload") or {}).get("source_context") or {})
    candidate_row = dict(source_context.get("candidate_row") or {})
    business_fields = dict(candidate_row.get("business_fields") or {})
    nested_source_context = dict(candidate_row.get("source_context") or {})
    source_fields = dict(nested_source_context.get("source_fields") or {})
    return {
        "source_table_ref": _first_non_empty(source_context.get("source_table_ref"), candidate_row.get("source_table_ref")),
        "holiday": _first_non_empty(source_context.get("holiday"), business_fields.get("holiday"), source_fields.get("节日")),
        "product_status": _first_non_empty(source_context.get("product_status"), business_fields.get("product_status"), source_fields.get("商品状态")),
        "source_product_images": source_context.get("source_product_images") or _source_product_images_from_fields(source_fields),
    }

def _creator_candidate_business_context(creator: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(creator.get("metrics") or {})
    return {
        "matched_product_sold_count": _first_non_empty(
            metrics.get("sold_count"),
            creator.get("sold_count"),
            creator.get("product_sold_count"),
        ),
        "candidate_follower_count": _first_non_empty(
            metrics.get("follower_count"),
            creator.get("follower_count"),
            creator.get("fans_count"),
        ),
    }

def _write_record_business_context(source_context: Mapping[str, Any], result_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "holiday": _first_non_empty(source_context.get("holiday")),
        "matched_product_sold_count": _first_non_empty(source_context.get("matched_product_sold_count")),
        "source_product_images": source_context.get("source_product_images") or [],
        "quality": dict(result_payload.get("quality") or {}),
    }

def _source_product_images_from_fields(source_fields: Mapping[str, Any]) -> list[Any]:
    for key in ("图片", "商品图片", "带货商品图", "商品主图", "image", "image_url"):
        value = source_fields.get(key)
        if isinstance(value, list):
            return list(value)
        if value not in (None, "", {}, []):
            return [value]
    return []

def _normalize_product_identity(raw_identity: Any, fallback_row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_identity, Mapping):
        identity = dict(raw_identity)
    else:
        identity = {}
    product_id = _first_non_empty(identity.get("product_id"), fallback_row.get("product_id"))
    if product_id:
        identity["product_id"] = product_id
    product_url = _first_non_empty(identity.get("product_url"), fallback_row.get("product_url"))
    if product_url:
        identity["product_url"] = product_url
    return identity

def _normalize_creator_identity(raw_identity: Any, fallback_row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw_identity, Mapping):
        identity = dict(raw_identity)
    else:
        identity = {}
    creator_id = _first_non_empty(
        identity.get("creator_id"),
        fallback_row.get("creator_id"),
        fallback_row.get("influencer_id"),
    )
    if creator_id:
        identity["creator_id"] = creator_id
    uid = _first_non_empty(identity.get("uid"), fallback_row.get("uid"), fallback_row.get("author_uid"))
    if uid:
        identity["uid"] = uid
    unique_id = _first_non_empty(identity.get("unique_id"), fallback_row.get("unique_id"), fallback_row.get("author_unique_id"))
    if unique_id:
        identity["unique_id"] = unique_id
    profile_url = _first_non_empty(identity.get("profile_url"), fallback_row.get("profile_url"), fallback_row.get("author_url"))
    if profile_url:
        identity["profile_url"] = profile_url
    return identity

def _build_product_job_context(*, request: Any, candidate: Mapping[str, Any], stage_code: str) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "task_code": TASK_CODE,
        "workflow_code": WORKFLOW_CODE,
        "stage_code": stage_code,
        "source_record_id": candidate["source_record_id"],
        "product_id": candidate["product_id"],
        "product_id_or_fastmoss_key": _first_non_empty(candidate["product_id"], candidate["product_key"]),
        "product_identity": dict(candidate["product_identity"]),
    }

def _product_group_key(*, source_record_id: str, product_id: str) -> str:
    return f"{source_record_id}:{product_id}"

def _job_product_key(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    source_context = dict(payload.get("source_context") or {})
    source_record_id = _first_non_empty(source_context.get("source_record_id"))
    product_id = _first_non_empty(
        source_context.get("product_id"),
        (payload.get("product_identity") or {}).get("product_id") if isinstance(payload.get("product_identity"), Mapping) else "",
    )
    if not source_record_id or not product_id:
        first_record = _first_payload_record(payload)
        source_record_id = _first_non_empty(
            source_record_id,
            first_record.get("source_record_id"),
            dict(payload.get("idempotency_context") or {}).get("source_record_id"),
        )
        product_id = _first_non_empty(
            product_id,
            first_record.get("product_id"),
            dict(payload.get("idempotency_context") or {}).get("product_id"),
        )
        product_key = _first_non_empty(
            first_record.get("product_key"),
            payload.get("product_key"),
            payload.get("business_entity_key"),
        )
        if product_key:
            return product_key
    if not source_record_id or not product_id:
        return ""
    return _product_group_key(source_record_id=source_record_id, product_id=product_id)

def _job_product_id(job: Mapping[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    source_context = dict(payload.get("source_context") or {})
    return _first_non_empty(
        source_context.get("product_id"),
        dict(payload.get("product_identity") or {}).get("product_id") if isinstance(payload.get("product_identity"), Mapping) else "",
        dict(payload.get("idempotency_context") or {}).get("product_id"),
    )

def _creator_fact_key(source_record_id: str, product_id: str, creator_id: str) -> str:
    if not source_record_id or not product_id or not creator_id:
        return ""
    return f"{source_record_id}:{product_id}:{creator_id}"

def _first_payload_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    records = list(payload.get("records") or [])
    if not records:
        return {}
    first_record = records[0]
    return dict(first_record) if isinstance(first_record, Mapping) else {}

def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""

__all__ = [name for name in globals() if not name.startswith('__')]
