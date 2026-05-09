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


def _fastmoss_search_settings_from_request_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {}) if isinstance(request_payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("browser_cookies", "browser_cookies"),
        ("execution_control_db_url", "execution_control_db_url"),
        ("db_url", "db_url"),
        ("fastmoss_cookie_cache_namespace", "cookie_cache_namespace"),
        ("fastmoss_cookie_cache_enabled", "cookie_cache_enabled"),
        ("fastmoss_cookie_cache_ttl_seconds", "cookie_cache_ttl_seconds"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", [], {}):
            settings.setdefault(target_key, value)
    settings.setdefault("live_fetch", True)
    settings.setdefault("ensure_logged_in", True)
    return {key: value for key, value in settings.items() if value not in (None, "", [], {})}

def _keyword_seed_import_search_request(
    request_payload: Mapping[str, Any],
    *,
    latest_import_job: Mapping[str, Any] | None,
    retry_after_fastmoss_browser: bool,
) -> dict[str, Any]:
    previous_payload = coerce_mapping((latest_import_job or {}).get("payload"))
    previous_search_request = coerce_mapping(previous_payload.get("search_request"))
    search_request = dict(previous_search_request) if retry_after_fastmoss_browser and previous_search_request else keyword_search_parameter_mapper(request_payload)
    for key in RUNTIME_DB_PASSTHROUGH_KEYS:
        if request_payload.get(key) not in (None, ""):
            search_request[key] = request_payload.get(key)
    if retry_after_fastmoss_browser:
        search_request["fastmoss_security_browser_fallback_attempt"] = 1
    return search_request

def _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs: list[dict[str, Any]]) -> bool:
    return any(
        int(coerce_mapping(job.get("payload")).get("fastmoss_security_browser_fallback_attempt") or 0) > 0
        for job in jobs
    )

def _fastmoss_security_fallback_payload_from_job(import_job: Mapping[str, Any]) -> dict[str, Any]:
    job_payload = coerce_mapping(import_job.get("payload"))
    result_payload = extract_effective_result_payload(import_job)
    search_request = coerce_mapping(job_payload.get("search_request")) or coerce_mapping(result_payload.get("search_request"))
    security_context = coerce_mapping(result_payload.get("security_context"))
    return {
        "search_query": _first_text(
            search_request.get("search_query"),
            search_request.get("keyword"),
            job_payload.get("search_query"),
        ),
        "search_digest": _first_text(job_payload.get("search_digest"), search_request.get("search_digest")),
        "search_request": search_request,
        "security_context": security_context,
        "fallback_source_job_id": _first_text(result_payload.get("fallback_source_job_id"), import_job.get("job_id")),
    }

def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )

def _runtime_child_context(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "task_code": request.task_code,
        "workflow_code": workflow.workflow_code,
        "stage_code": stage_code,
    }

def _positive_int_param(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default

def _non_negative_int_param(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default

def _bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default

def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}

def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in values.items() if value not in (None, "", [], {})}

def _build_seed_contexts(*, candidates: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    job_by_candidate = {
        str((job.get("payload") or {}).get("candidate_key") or ""): job
        for job in jobs
        if str((job.get("payload") or {}).get("candidate_key") or "")
    }
    seed_contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = candidate["candidate_key"]
        job = job_by_candidate.get(candidate_key)
        result_payload = extract_effective_result_payload(job)
        target_record_ids = result_payload.get("target_record_ids") if isinstance(result_payload.get("target_record_ids"), list) else []
        source_record_id = str(target_record_ids[0] if target_record_ids else candidate_key)
        seed_contexts.append(
            {
                **candidate,
                "source_record_id": source_record_id,
                "seed_status": _record_effective_status(job),
                "seed_result": result_payload,
                "target_record_ids": [str(item) for item in target_record_ids],
            }
        )
    return seed_contexts

def _normalize_search_candidates(
    raw_candidates: Any,
    *,
    search_query: str,
    output_conditions: Mapping[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_candidates, start=1):
        if not isinstance(row, Mapping):
            continue
        product_identity = _resolve_product_identity(row)
        raw_entity_key = str(
            product_identity.get("product_id")
            or product_identity.get("normalized_product_url")
            or product_identity.get("product_url")
            or product_identity.get("product_key")
            or row.get("candidate_key")
            or index
        )
        business_entity_key = _product_business_entity_key(raw_entity_key)
        if not business_entity_key or business_entity_key in seen:
            continue
        candidate_context = {
            "candidate_key": business_entity_key,
            "business_entity_key": business_entity_key,
            "product_identity": product_identity,
            "product_id": str(product_identity.get("product_id") or ""),
            "product_url": str(product_identity.get("product_url") or ""),
            "normalized_product_url": str(product_identity.get("normalized_product_url") or ""),
            "search_query": search_query,
            "search_rank": int(row.get("rank") or index),
            "source_context": dict(row),
        }
        if not _candidate_allowed(candidate_context, output_conditions):
            continue
        normalized.append(candidate_context)
        seen.add(business_entity_key)
        if max_candidates > 0 and len(normalized) >= max_candidates:
            break
    return normalized

def _candidate_allowed(candidate: Mapping[str, Any], conditions: Mapping[str, Any]) -> bool:
    allowed_ids = {str(item) for item in conditions.get("allowed_product_ids") or [] if str(item)}
    excluded_ids = {str(item) for item in conditions.get("exclude_product_ids") or [] if str(item)}
    require_url = bool(conditions.get("require_product_url", False))
    product_id = str(candidate.get("product_id") or "")
    normalized_product_url = str(candidate.get("normalized_product_url") or "")
    if allowed_ids and product_id not in allowed_ids:
        return False
    if excluded_ids and product_id in excluded_ids:
        return False
    if require_url and not normalized_product_url:
        return False
    return True

def _latest_candidate_job(
    jobs: list[dict[str, Any]],
    *,
    candidate_key: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("candidate_key") or "") != candidate_key:
            continue
        selected = job
    return selected

def _latest_job(jobs: list[dict[str, Any]], *, job_code: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        selected = job
    return selected

def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("source_record_id") or "") != source_record_id:
            continue
        selected = job
    return selected

def _latest_candidate_execution(
    executions: list[Any],
    *,
    candidate_key: str,
) -> Any:
    selected = None
    for execution in executions:
        if str((execution.payload or {}).get("candidate_key") or "") != candidate_key:
            continue
        selected = execution
    return selected

def _effective_tiktok_result(
    *,
    tiktok_job: Mapping[str, Any] | None,
    browser_execution: Any | None,
) -> dict[str, Any]:
    browser_payload = extract_effective_result_payload(browser_execution)
    if isinstance(browser_payload.get("normalized_product_result"), Mapping):
        return browser_payload
    return extract_effective_result_payload(tiktok_job)

def _collect_asset_refs(product_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_assets: list[Any] = []
    media_assets = product_result.get("media_assets")
    if isinstance(media_assets, list):
        raw_assets.extend(media_assets)
    images = product_result.get("images")
    if isinstance(images, list):
        raw_assets.extend(images)
    videos = product_result.get("videos")
    if isinstance(videos, list):
        raw_assets.extend(videos)

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_assets:
        if isinstance(item, Mapping):
            asset = dict(item)
        elif isinstance(item, str):
            asset = {"source_url": item, "source_type": "image"}
        else:
            continue
        source_url = str(asset.get("source_url") or asset.get("url") or "").strip()
        if not source_url or source_url in seen:
            continue
        seen.add(source_url)
        normalized.append(
            {
                "source_url": source_url,
                "source_type": str(asset.get("source_type") or asset.get("type") or "image"),
                "mime_type": str(asset.get("mime_type") or ""),
            }
        )
    return normalized

def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if isinstance(record, Mapping):
        status = str(record.get("status") or "")
        handler_status = extract_handler_result_status(record)
        return handler_status or status
    status = str(getattr(record, "status", "") or "")
    handler_status = extract_handler_result_status(record)
    return handler_status or status

def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""

def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_entity_key") or candidate.get("candidate_key") or "")
    return f"tiktok_product:{business_key}" if business_key else ""

def _search_digest(*, search_query: str, filters: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {
            "search_query": str(search_query or "").strip(),
            "filters": dict(filters or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

def _resolve_product_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("product_identity")
    if isinstance(nested, Mapping):
        base = dict(nested)
    else:
        base = {}
    product_url = str(
        base.get("product_url")
        or row.get("product_url")
        or row.get("url")
        or row.get("normalized_product_url")
        or ""
    ).strip()
    normalized_product_url = _normalize_product_url(product_url)
    product_id = str(
        base.get("product_id")
        or row.get("product_id")
        or row.get("id")
        or row.get("productId")
        or _extract_tiktok_product_id(normalized_product_url)
        or ""
    ).strip()
    if not normalized_product_url and product_id:
        normalized_product_url = _tiktok_product_url(product_id)
    if not product_url and normalized_product_url:
        product_url = normalized_product_url
    product_key = str(base.get("product_key") or row.get("product_key") or row.get("fastmoss_product_key") or "").strip()
    return {
        "product_id": product_id,
        "product_key": product_key or product_id or normalized_product_url,
        "product_url": product_url,
        "normalized_product_url": normalized_product_url,
    }

def _normalize_product_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[?#].*$", "", text)
    product_id = _extract_tiktok_product_id(normalized)
    if product_id:
        return _tiktok_product_url(product_id)
    return normalized

def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product|detail)/(\d+)", text)
    if match:
        return str(match.group(1))
    return ""

def _product_business_entity_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("product:"):
        return text
    return f"product:{text}"

def _tiktok_product_url(product_id: str) -> str:
    return f"https://www.tiktok.com/shop/pdp/{product_id}" if product_id else ""

def _asset_source(asset: Mapping[str, Any]) -> str:
    return str(asset.get("source_url") or asset.get("url") or "")

def _minimal_seed_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    product_identity = dict(payload.get("product_identity") or {})
    business_entity_key = str(payload.get("business_entity_key") or payload.get("candidate_key") or "")
    return {
        "candidate_key": str(payload.get("candidate_key") or business_entity_key),
        "business_entity_key": business_entity_key,
        "source_record_id": str(payload.get("source_record_id") or business_entity_key),
        "product_identity": product_identity,
        "product_id": str(product_identity.get("product_id") or ""),
        "normalized_product_url": str(product_identity.get("normalized_product_url") or payload.get("normalized_product_url") or ""),
        "source_context": dict(payload.get("source_context") or {}),
    }

__all__ = [name for name in globals() if not name.startswith('__')]
