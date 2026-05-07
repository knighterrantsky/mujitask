from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Mapping

from automation_business_scaffold.control_plane.runtime_config.settings import (
    REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE,
    REFRESH_TASK_CODE,
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
    has_active_records as _has_active_children,
    recover_browser_fallback_resume_stage,
    render_job_keys,
    select_latest_successful_api_job,
    stage_child_records as _stage_child_records,
    summarize_stage_children,
    summarize_child_outcomes,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)

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
SUPPORTED_REFRESH_TASK_CODES = {REFRESH_TASK_CODE, REFRESH_COMPETITOR_ROW_BY_URL_TASK_CODE}


def _resume_stage_from_premature_summary(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    current_stage: str,
) -> str:
    return recover_browser_fallback_resume_stage(
        store,
        request_id=request.request_id,
        current_stage=current_stage,
        summary_stage_code=workflow.summary_policy.summary_stage_code,
        continuation_stage_codes=("resume_competitor_rows_after_browser_fallback",),
        continuation_candidate_ready=bool(
            _browser_resume_candidates(store=store, request_id=request.request_id)
        ),
        browser_stage_code="browser_fallback",
        resume_stage_code="resume_competitor_rows_after_browser_fallback",
    )


def _empty_row_delete_records(read_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows = read_payload.get("raw_rows_all") or read_payload.get("raw_rows") or []
    records: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        record_id = str(row.get("record_id") or "").strip()
        fields = row.get("fields")
        if record_id and isinstance(fields, Mapping) and not _any_field_has_value(fields):
            records.append(
                {
                    "op": "delete",
                    "record_id": record_id,
                    "business_entity_key": f"empty-row:{record_id}",
                    "source_context": {"cleanup_reason": "empty_row"},
                }
            )
    return records


def _any_field_has_value(fields: Mapping[str, Any]) -> bool:
    return any(_field_has_value(value) for value in fields.values())


def _field_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_field_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_field_has_value(item) for item in value)
    return True


def _advance_sync_media(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "sync_media"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidates = _media_sync_candidates(store=store, request_id=request.request_id)
        if not candidates:
            return {"action": "advance", "next_stage": "persist_facts", "details": {"media_candidate_count": 0}}

        media_job_def = workflow.require_job("media_asset_sync")
        media_jobs: list[dict[str, Any]] = []
        for candidate in candidates:
            asset_refs = list(candidate.get("asset_refs") or [])
            media_payload = {
                **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
                **_payload_subset(request.payload, ARTIFACT_PASSTHROUGH_KEYS),
                "stage_code": stage_code,
                "source_record_id": candidate["source_record_id"],
                "asset_refs": asset_refs,
                "entity_keys": [candidate["business_key"]],
                "source_context": dict(candidate["source_context"]),
            }
            media_payload.update(_artifact_settings_from_request_payload(request.payload))
            media_keys = render_job_keys(
                media_job_def,
                request.payload,
                candidate,
                media_payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=media_job_def.job_code,
            )
            media_jobs.append(
                {
                    "business_key": media_keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(media_keys["dedupe_key"], media_job_def.job_code),
                    "payload": media_payload,
                    "max_execution_seconds": _timeout_seconds(workflow, media_job_def.job_code),
                }
            )

        media_dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=media_job_def.job_code,
            jobs=media_jobs,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "media_candidate_count": len(candidates),
                "media_dispatch": media_dispatch,
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued media sync jobs.",
            details={"media_created_count": int(media_dispatch["created_count"])},
        )

    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for media sync jobs to finish.")
    return {"action": "advance", "next_stage": "persist_facts", "details": {"media_job_count": len(jobs)}}


def _advance_persist_facts(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "persist_facts"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        candidate_rows = _fact_persist_candidates(store=store, request_id=request.request_id)
        if not candidate_rows:
            return {"action": "advance", "next_stage": "writeback_competitor_rows", "details": {"persist_count": 0}}

        fact_job_def = workflow.require_job("fact_bundle_upsert")
        fact_jobs: list[dict[str, Any]] = []
        for candidate in candidate_rows:
            fact_payload = {
                **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
                **_payload_subset(request.payload, FACT_PERSISTENCE_PASSTHROUGH_KEYS),
                "stage_code": stage_code,
                "source_record_id": candidate["source_record_id"],
                "fact_bundle": dict(candidate["fact_bundle"]),
                "observation_at": str(candidate.get("observation_at") or ""),
                "observation_context": {
                    "source_record_id": candidate["source_record_id"],
                    "product_id": candidate.get("product_id") or "",
                },
            }
            fact_keys = render_job_keys(
                fact_job_def,
                request.payload,
                candidate,
                fact_payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=fact_job_def.job_code,
            )
            fact_jobs.append(
                {
                    "business_key": fact_keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(fact_keys["dedupe_key"], fact_job_def.job_code),
                    "payload": fact_payload,
                    "max_execution_seconds": _timeout_seconds(workflow, fact_job_def.job_code),
                }
            )

        fact_dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=fact_job_def.job_code,
            jobs=fact_jobs,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "persist_candidate_count": len(candidate_rows),
                "fact_dispatch": fact_dispatch,
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued fact persistence jobs.",
            details={
                "fact_created_count": int(fact_dispatch["created_count"]),
            },
        )

    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for persistence jobs to finish.")
    return {"action": "advance", "next_stage": "writeback_competitor_rows", "details": {"persist_job_count": len(jobs)}}


def _advance_writeback_competitor_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "writeback_competitor_rows"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not jobs:
        row_contexts = _row_contexts(store, request_id=request.request_id)
        if not row_contexts:
            return {"action": "advance", "next_stage": "ready_for_summary", "details": {"writeback_count": 0}}

        job_def = workflow.require_job("feishu_table_write")
        payloads: list[dict[str, Any]] = []
        target_table_ref = _source_table_ref_from_request_payload(request.payload)
        for row in row_contexts:
            projection = _build_writeback_projection(store=store, request_id=request.request_id, row_context=row)
            payload = build_projection_write_payload(
                stage_code=stage_code,
                request_id=request.request_id,
                target_table_ref=target_table_ref,
                records=[projection],
                mapper_code="competitor_table_projection_mapper",
                write_mode="upsert",
                source_record_id=row["source_record_id"],
            )
            payload.update(_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code))
            payload.update(_payload_subset(request.payload, FEISHU_WRITE_PASSTHROUGH_KEYS))
            keys = render_job_keys(
                job_def,
                request.payload,
                row,
                payload,
                request_id=request.request_id,
                task_code=request.task_code,
                workflow_code=workflow.workflow_code,
                stage_code=stage_code,
                job_code=job_def.job_code,
            )
            payloads.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            )

        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=payloads,
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"writeback_dispatch": dispatch, "writeback_count": len(payloads)},
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued competitor row writeback jobs.",
            details={"writeback_created_count": int(dispatch["created_count"])},
        )

    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for competitor row writeback jobs to finish.")
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"writeback_job_count": len(jobs)}}


def _row_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    read_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="read_competitor_rows")
    latest = select_latest_successful_api_job(read_jobs, "feishu_table_read")
    payload = extract_effective_result_payload(latest)
    return _normalize_source_rows(payload.get("source_rows"))


def _browser_fallback_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    row_index = {row["source_record_id"]: row for row in _row_contexts(store, request_id=request_id)}
    for job in _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data"):
        if str(job.get("job_code") or "") != "competitor_row_refresh":
            continue
        if not _is_fallback_required(job):
            continue
        payload = dict(job.get("payload") or {})
        result = extract_effective_result_payload(job)
        fallback_handler = _first_text(result.get("fallback_handler"))
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = (
            dict(result.get("browser_fallback_payload"))
            if isinstance(result.get("browser_fallback_payload"), Mapping)
            else {}
        )
        source_record_id = _first_text(result.get("source_record_id"), payload.get("source_record_id"))
        row_context = row_index.get(source_record_id, _minimal_row_context(payload))
        fallback_source_job_id = _first_text(
            browser_payload.get("fallback_source_job_id"),
            result.get("fallback_source_job_id"),
            job.get("job_id"),
        )
        browser_payload = {
            **browser_payload,
            "source_record_id": source_record_id,
            "fallback_source_job_id": fallback_source_job_id,
        }
        candidate = dict(row_context)
        candidate.update(
            {
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_text(result.get("fallback_reason")),
                "fallback_source_job_id": fallback_source_job_id,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": payload,
                "row_result": result,
                "business_entity_key": _first_text(
                    result.get("business_entity_key"),
                    payload.get("business_key"),
                    job.get("business_key"),
                    source_record_id,
                ),
                "browser_fallback_payload": _compact_mapping(browser_payload),
                "normalized_product_result": (
                    dict(result.get("normalized_product_result"))
                    if isinstance(result.get("normalized_product_result"), Mapping)
                    else {}
                ),
            }
        )
        candidates.append(candidate)
    return candidates


def _browser_execution_payload(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    fallback_payload = (
        dict(candidate.get("browser_fallback_payload"))
        if isinstance(candidate.get("browser_fallback_payload"), Mapping)
        else {}
    )
    payload = {
        **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
        **_payload_subset(request.payload, ARTIFACT_PASSTHROUGH_KEYS),
        **fallback_payload,
        "stage_code": stage_code,
        "source_record_id": str(candidate.get("source_record_id") or ""),
        "business_entity_key": str(candidate.get("business_entity_key") or ""),
        "fallback_handler": fallback_handler,
        "fallback_source_job_id": _first_text(
            fallback_payload.get("fallback_source_job_id"),
            candidate.get("row_job_id"),
        ),
    }
    payload.update(_artifact_settings_from_request_payload(request.payload))
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault("search_query", str(candidate.get("business_entity_key") or ""))
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        if not isinstance(payload.get("search_request"), Mapping):
            payload["search_request"] = {}
        if not isinstance(payload.get("verification_request"), Mapping):
            payload["verification_request"] = {}
        fastmoss_settings = _fastmoss_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
    return _compact_mapping(payload)


def _browser_resume_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    fallback_by_key = {
        str(candidate.get("fallback_key") or ""): candidate
        for candidate in _browser_fallback_candidates(store=store, request_id=request_id)
    }
    candidates: list[dict[str, Any]] = []
    for execution in _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback"):
        if _record_effective_status(execution) != "success":
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_text(payload.get("source_record_id"))
        fallback_key = _row_fallback_key(
            source_record_id=source_record_id,
            fallback_handler=fallback_handler,
        )
        fallback_candidate = fallback_by_key.get(fallback_key)
        if not fallback_candidate:
            continue
        execution_payload = extract_effective_result_payload(execution)
        if fallback_handler == "tiktok_product_browser_fetch":
            normalized = execution_payload.get("normalized_product_result")
            if not isinstance(normalized, Mapping) or not normalized:
                continue
        candidates.append(
            {
                **dict(fallback_candidate),
                "browser_execution_id": str(execution.execution_id),
                "browser_execution_payload": execution_payload,
            }
        )
    return candidates


def _resume_row_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(candidate.get("row_payload") or {}) if isinstance(candidate.get("row_payload"), Mapping) else {}
    browser_payload = (
        dict(candidate.get("browser_execution_payload"))
        if isinstance(candidate.get("browser_execution_payload"), Mapping)
        else {}
    )
    payload.update(
        {
            "stage_code": stage_code,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": fallback_handler,
            "browser_execution_id": str(candidate.get("browser_execution_id") or ""),
            "fallback_source_job_id": str(candidate.get("row_job_id") or ""),
            "force_fallback": False,
            "fallback_reason": "",
        }
    )
    if fallback_handler == "tiktok_product_browser_fetch":
        normalized = browser_payload.get("normalized_product_result")
        if isinstance(normalized, Mapping):
            payload["normalized_product_result"] = dict(normalized)
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized = candidate.get("normalized_product_result")
        if isinstance(normalized, Mapping) and normalized:
            payload["normalized_product_result"] = dict(normalized)
    return _compact_mapping(payload)


def _row_fallback_key(*, source_record_id: str, fallback_handler: str) -> str:
    return f"{fallback_handler}:{source_record_id}"


def _search_digest_for_row_fallback(candidate: Mapping[str, Any]) -> str:
    value = _first_text(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("row_job_id"),
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _row_browser_resource_code(
    *,
    fallback_handler: str,
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    if fallback_handler == "fastmoss_security_browser_resolve":
        return _fastmoss_browser_resource_code(payload)
    return _browser_resource_code(candidate)


def _media_sync_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    browser_by_row = {
        str((execution.payload or {}).get("source_record_id") or ""): execution for execution in browser_execs
    }
    tiktok_by_row: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        if str(job.get("job_code") or "") != "tiktok_product_request_fetch":
            continue
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if source_record_id:
            tiktok_by_row[source_record_id] = job

    candidates: list[dict[str, Any]] = []
    for row in _row_contexts(store=store, request_id=request_id):
        source_record_id = row["source_record_id"]
        tiktok_result = _effective_tiktok_result(
            tiktok_job=tiktok_by_row.get(source_record_id),
            browser_execution=browser_by_row.get(source_record_id),
        )
        product_result = dict(tiktok_result.get("normalized_product_result") or {})
        asset_refs = _collect_asset_refs(product_result)
        if not asset_refs:
            continue
        candidates.append({**row, "asset_refs": asset_refs})
    return candidates


def _fact_persist_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    browser_by_row = {
        str((execution.payload or {}).get("source_record_id") or ""): execution for execution in browser_execs
    }
    tiktok_by_row: dict[str, dict[str, Any]] = {}
    fastmoss_by_row: dict[str, dict[str, Any]] = {}
    media_by_row: dict[str, dict[str, Any]] = {}
    for job in collect_jobs:
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if not source_record_id:
            continue
        if str(job.get("job_code") or "") == "tiktok_product_request_fetch":
            tiktok_by_row[source_record_id] = job
        if str(job.get("job_code") or "") == "fastmoss_product_fetch":
            fastmoss_by_row[source_record_id] = job
    for job in media_jobs:
        source_record_id = str((job.get("payload") or {}).get("source_record_id") or "")
        if source_record_id:
            media_by_row[source_record_id] = job

    candidates: list[dict[str, Any]] = []
    for row in _row_contexts(store=store, request_id=request_id):
        source_record_id = row["source_record_id"]
        browser_execution = browser_by_row.get(source_record_id)
        tiktok_job = tiktok_by_row.get(source_record_id)
        fastmoss_job = fastmoss_by_row.get(source_record_id)
        media_job = media_by_row.get(source_record_id)
        tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
        fastmoss_result = extract_effective_result_payload(fastmoss_job)
        media_result = extract_effective_result_payload(media_job)
        if not tiktok_result and not fastmoss_result and not media_result:
            continue
        product_result = dict(tiktok_result.get("normalized_product_result") or {})
        fact_bundle = _merge_runtime_fact_bundles(
            dict(product_result.get("fact_bundle") or {}),
            dict(fastmoss_result.get("product_fact_bundle") or {}),
            dict(media_result.get("media_fact_bundle") or {}),
        )
        fact_bundle["source_record_id"] = source_record_id
        fact_bundle["product_identity"] = dict(row["product_identity"])
        fact_bundle["source_context"] = dict(row["source_context"])
        candidates.append(
            {
                **row,
                "fact_bundle": fact_bundle,
                "observation_at": str(int(time.time())),
                "product_id": str(
                    product_result.get("product_id")
                    or row.get("product_id")
                    or row["product_identity"].get("product_id")
                    or ""
                ),
            }
        )
    return candidates


def _persist_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    return _fact_persist_candidates(store=store, request_id=request_id)


def _build_writeback_projection(
    *,
    store: RuntimeStore,
    request_id: str,
    row_context: Mapping[str, Any],
) -> dict[str, Any]:
    row_result = _build_row_result(store=store, request_id=request_id, row_context=row_context)
    projection_fields = _build_competitor_projection_fields(
        store=store,
        request_id=request_id,
        row_context=row_context,
    )
    status_field = _competitor_status_text(str(row_result["row_status"]))
    if status_field:
        projection_fields["商品状态"] = status_field
    return build_projection_record(
        request_id=request_id,
        source_record_id=str(row_context["source_record_id"]),
        product_id=str(row_context.get("product_id") or row_context["product_identity"].get("product_id") or ""),
        product_url=str(row_context.get("normalized_product_url") or row_context["product_identity"].get("product_url") or ""),
        refresh_status=str(row_result["row_status"]),
        details=row_result,
        candidate_key=str(row_context.get("business_key") or ""),
        extra_fields={
            "business_entity_key": str(row_context.get("business_key") or ""),
            "projection_fields": projection_fields,
            "source_fields": _source_fields_from_row_context(row_context),
        },
    )


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


def _build_competitor_projection_fields(
    *,
    store: RuntimeStore,
    request_id: str,
    row_context: Mapping[str, Any],
) -> dict[str, Any]:
    source_record_id = str(row_context.get("source_record_id") or "")
    collect_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="collect_product_data")
    media_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="sync_media")
    browser_execs = _browser_executions_for_stage(store=store, request_id=request_id, stage_code="browser_fallback")
    tiktok_job = _latest_row_job(collect_jobs, source_record_id=source_record_id, job_code="tiktok_product_request_fetch")
    fastmoss_job = _latest_row_job(collect_jobs, source_record_id=source_record_id, job_code="fastmoss_product_fetch")
    media_job = _latest_row_job(media_jobs, source_record_id=source_record_id, job_code="media_asset_sync")
    browser_execution = _latest_row_execution(browser_execs, source_record_id=source_record_id)

    tiktok_result = _effective_tiktok_result(tiktok_job=tiktok_job, browser_execution=browser_execution)
    fastmoss_result = extract_effective_result_payload(fastmoss_job)
    media_result = extract_effective_result_payload(media_job)
    product_result = dict(tiktok_result.get("normalized_product_result") or {})
    tiktok_product = dict(product_result.get("product") or {})
    logical_fields = dict(product_result.get("logical_fields") or {})
    fastmoss_bundle = dict(fastmoss_result.get("product_fact_bundle") or {})
    daily_metrics = [
        dict(item)
        for item in fastmoss_bundle.get("product_daily_metrics", [])
        if isinstance(item, Mapping)
    ]
    fastmoss_product = _fact_bundle_product(
        fastmoss_bundle,
        product_id=str(row_context.get("product_id") or row_context.get("product_identity", {}).get("product_id") or ""),
    )
    metrics_snapshot = dict(fastmoss_result.get("metrics_snapshot") or {})
    overview_metrics = dict(metrics_snapshot.get("overview") or {})

    product_id = _first_text(
        tiktok_product.get("product_id"),
        product_result.get("product_id"),
        fastmoss_product.get("product_id"),
        row_context.get("product_id"),
        row_context.get("product_identity", {}).get("product_id") if isinstance(row_context.get("product_identity"), Mapping) else "",
    )
    product_url = _first_text(
        tiktok_product.get("normalized_url"),
        tiktok_product.get("product_url"),
        product_result.get("normalized_product_url"),
        row_context.get("normalized_product_url"),
        row_context.get("product_url"),
        fastmoss_product.get("product_url"),
    )
    title = _first_text(
        logical_fields.get("title"),
        tiktok_product.get("title"),
        fastmoss_product.get("title"),
    )
    seller_name = _first_text(
        logical_fields.get("shop_name"),
        tiktok_product.get("seller_name"),
        tiktok_product.get("shop_name"),
        fastmoss_product.get("seller_name"),
        fastmoss_product.get("shop_name"),
    )
    image_url = _first_text(
        _first_media_asset_url(media_result),
        logical_fields.get("main_image_url"),
        _first_media_asset_url(product_result),
        _first_media_asset_url(fastmoss_bundle),
    )
    price_text = _price_number_text(
        logical_fields.get("price_text"),
        tiktok_product.get("price_text"),
        tiktok_product.get("price_amount"),
        overview_metrics.get("front_price"),
        overview_metrics.get("real_price"),
        overview_metrics.get("price"),
    )
    fastmoss_price = _price_number_text(
        overview_metrics.get("fastmoss_price"),
        overview_metrics.get("real_price"),
        overview_metrics.get("price"),
        price_text,
    )

    fields = {
        "SKU-ID": product_id,
        "产品链接": _normalize_tiktok_product_url(product_url),
        "图片": image_url,
        "标题": title,
        "卖家": seller_name,
        "价格": price_text,
        "Fastmoss价格": fastmoss_price,
        "昨日销量": _first_text(
            _metric_text(
                overview_metrics,
                "yday_sold_count",
                "yesterday_sold_count",
                "day1_sold_count",
                "yday_sales",
                "yesterday_sales",
            ),
            _daily_sales_text(daily_metrics, window_days=1),
        ),
        "近7天销量": _first_text(
            _metric_text(
                overview_metrics,
                "day7_sold_count",
                "sales_7d",
                "day7_sales",
                "sold_count_7d",
            ),
            _daily_sales_text(daily_metrics, window_days=7),
        ),
        "近90天销量": _first_text(
            _metric_text(
                overview_metrics,
                "day90_sold_count",
                "sales_90d",
                "day90_sales",
                "sold_count_90d",
            ),
            _daily_sales_text(daily_metrics, window_days=90),
        ),
    }
    return {key: value for key, value in fields.items() if value not in ("", None, [], {})}


def _competitor_status_text(row_status: str) -> str:
    return {
        "unavailable": "已下架/区域不可售",
    }.get(str(row_status or ""), "")


def _fact_bundle_product(fact_bundle: Mapping[str, Any], *, product_id: str) -> dict[str, Any]:
    products = fact_bundle.get("products") if isinstance(fact_bundle, Mapping) else []
    fallback: dict[str, Any] = {}
    for item in products if isinstance(products, list) else []:
        if not isinstance(item, Mapping):
            continue
        current = dict(item)
        if not fallback:
            fallback = current
        if product_id and str(current.get("product_id") or "") == product_id:
            return current
    return fallback


def _first_media_asset_url(payload: Mapping[str, Any]) -> str:
    assets = []
    if isinstance(payload, Mapping):
        for key in ("media_assets", "synced_assets"):
            value = payload.get(key)
            if isinstance(value, list):
                assets.extend(value)
    for asset in _prefer_main_image_assets(assets):
        if isinstance(asset, Mapping):
            source_url = _first_text(
                asset.get("remote_uri"),
                asset.get("source_url"),
                asset.get("object_key"),
                asset.get("local_path"),
            )
            if source_url:
                return source_url
    for nested_key in ("media_fact_bundle", "fact_bundle"):
        nested = payload.get(nested_key) if isinstance(payload, Mapping) else None
        if isinstance(nested, Mapping):
            found = _first_media_asset_url(nested)
            if found:
                return found
    return ""


def _prefer_main_image_assets(assets: list[Any]) -> list[Any]:
    main_assets: list[Any] = []
    other_assets: list[Any] = []
    for asset in assets if isinstance(assets, list) else []:
        if isinstance(asset, Mapping) and str(asset.get("media_role") or "") == "product_main_image":
            main_assets.append(asset)
        else:
            other_assets.append(asset)
    return [*main_assets, *other_assets]


def _source_fields_from_row_context(row_context: Mapping[str, Any]) -> dict[str, Any]:
    for source in (row_context, row_context.get("source_context")):
        if not isinstance(source, Mapping):
            continue
        fields = source.get("source_fields") or source.get("fields")
        if isinstance(fields, Mapping):
            return dict(fields)
        nested = source.get("source_context")
        if isinstance(nested, Mapping):
            fields = nested.get("source_fields") or nested.get("fields")
            if isinstance(fields, Mapping):
                return dict(fields)
    return {}


def _metric_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key) if isinstance(payload, Mapping) else None
        text = _first_text(value)
        if text:
            return text
    return ""


def _daily_sales_text(daily_metrics: list[Mapping[str, Any]], *, window_days: int) -> str:
    if not daily_metrics or window_days <= 0:
        return ""
    ordered = sorted(
        (dict(item) for item in daily_metrics if isinstance(item, Mapping)),
        key=lambda item: _first_text(item.get("metric_date"), item.get("date"), item.get("dt")),
    )
    if len(ordered) < window_days:
        return ""
    selected = ordered[-window_days:]
    values: list[float] = []
    for item in selected:
        value = _number_value(
            item.get("sold_count"),
            dict(item.get("payload") or {}).get("inc_sold_count") if isinstance(item.get("payload"), Mapping) else None,
        )
        if value is None:
            return ""
        values.append(value)
    total = sum(values)
    return str(int(total)) if float(total).is_integer() else str(total)


def _price_number_text(*values: Any) -> str:
    text = ""
    for value in values:
        candidate = _first_text(value)
        if not candidate:
            continue
        if "*" in candidate:
            continue
        text = candidate
        break
    if not text:
        return ""
    normalized = text.strip().replace(",", "")
    normalized = re.sub(r"^(?:US\$|USD\s*|\$|￥|¥|CNY\s*|RMB\s*)", "", normalized, flags=re.IGNORECASE).strip()
    normalized = re.sub(r"\s*(?:USD|US\$|美元|元)$", "", normalized, flags=re.IGNORECASE).strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if match is None:
        return normalized
    number = match.group(0)
    return number.rstrip("0").rstrip(".") if "." in number else number


def _number_value(*values: Any) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        text = _first_text(value).replace(",", "")
        if not text:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            text = str(value).strip()
        if text:
            return text
    return ""


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _first_text(item))]
    if isinstance(value, tuple):
        return [text for item in value if (text := _first_text(item))]
    text = _first_text(value)
    return [text] if text else []


def _has_explicit_identity_lookup(payload: Mapping[str, Any]) -> bool:
    return bool(_first_text(payload.get("product_url"), payload.get("product_id")))


def _has_explicit_record_selection(payload: Mapping[str, Any]) -> bool:
    return bool(_list_text(payload.get("source_record_ids")))


def _resolve_final_status_from_rows(
    *,
    workflow: WorkflowDefinition,
    row_results: list[dict[str, Any]],
    child_records: list[Any],
    explicit_status: str,
) -> str:
    fallback_status = compute_final_status(
        workflow.summary_policy,
        child_records=child_records,
        optional_codes=OPTIONAL_FINAL_STATUS_CODES,
        explicit_status=explicit_status,
    )
    if not row_results:
        return fallback_status
    row_statuses = {str(item.get("row_status") or "") for item in row_results if str(item.get("row_status") or "")}
    if row_statuses == {"success"}:
        return "success"
    if row_statuses == {"failed"}:
        return "failed"
    if "failed" in row_statuses and "success" not in row_statuses and "partial_success" not in row_statuses:
        return "failed"
    if "failed" in row_statuses or "partial_success" in row_statuses:
        return "partial_success"
    return fallback_status


def _normalize_source_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        source_record_id = str(
            row.get("source_record_id")
            or row.get("record_id")
            or row.get("recordId")
            or ""
        ).strip()
        product_identity = _resolve_product_identity(row.get("product_identity"), row)
        business_key = str(product_identity.get("business_key") or source_record_id)
        normalized.append(
            {
                "source_record_id": source_record_id or business_key,
                "product_identity": product_identity,
                "product_id": str(product_identity.get("product_id") or ""),
                "product_url": str(product_identity.get("product_url") or ""),
                "normalized_product_url": str(product_identity.get("normalized_product_url") or ""),
                "business_key": business_key,
                "source_context": dict(row),
            }
        )
    return normalized


def _minimal_row_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    identity = _resolve_product_identity(payload.get("product_identity"), payload)
    source_record_id = str(payload.get("source_record_id") or identity.get("business_key") or "")
    return {
        "source_record_id": source_record_id,
        "product_identity": identity,
        "product_id": str(identity.get("product_id") or ""),
        "product_url": str(identity.get("product_url") or ""),
        "normalized_product_url": str(identity.get("normalized_product_url") or ""),
        "business_key": str(identity.get("business_key") or source_record_id),
        "source_context": dict(payload),
    }


def _resolve_product_identity(*sources: Any) -> dict[str, str]:
    product_id = ""
    product_url = ""
    for source in sources:
        product_id = product_id or _lookup_nested(source, "product_id")
        product_url = product_url or _lookup_nested(source, "normalized_product_url", "product_url", "url")
        nested_identity = source.get("product_identity") if isinstance(source, Mapping) else None
        if isinstance(nested_identity, Mapping):
            product_id = product_id or str(nested_identity.get("product_id") or "")
            product_url = product_url or str(
                nested_identity.get("normalized_product_url") or nested_identity.get("product_url") or ""
            )
    if not product_id:
        product_id = _extract_tiktok_product_id(product_url)
    normalized_url = _normalize_tiktok_product_url(product_url)
    if not product_url:
        product_url = normalized_url
    business_key = product_id or normalized_url or product_url
    return {
        "product_id": product_id,
        "product_url": product_url,
        "normalized_product_url": normalized_url or product_url,
        "business_key": business_key,
    }


def _lookup_nested(source: Any, *keys: str) -> str:
    if not isinstance(source, Mapping):
        return ""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    for nested_key in ("payload", "result", "fields"):
        nested = source.get(nested_key)
        if isinstance(nested, Mapping):
            found = _lookup_nested(nested, *keys)
            if found:
                return found
    return ""


def _effective_tiktok_result(*, tiktok_job: Mapping[str, Any] | None, browser_execution: Any) -> dict[str, Any]:
    if browser_execution is not None and str(browser_execution.status or "") == "success":
        return extract_effective_result_payload(browser_execution)
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

    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in raw_assets:
        if isinstance(asset, Mapping):
            item = dict(asset)
        elif isinstance(asset, str):
            item = {"source_url": asset, "source_type": "image"}
        else:
            continue
        source_url = str(item.get("source_url") or item.get("url") or "").strip()
        local_path = str(item.get("local_path") or "").strip()
        object_key = str(item.get("object_key") or "").strip()
        dedupe_key = source_url or local_path or object_key
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        assets.append(
            _compact_mapping(
                {
                    "source_url": source_url,
                    "source_type": str(item.get("source_type") or item.get("type") or "image"),
                    "file_name": str(item.get("file_name") or ""),
                    "mime_type": str(item.get("mime_type") or ""),
                    "local_path": local_path,
                    "object_key": object_key,
                    "remote_uri": str(item.get("remote_uri") or ""),
                    "entity_type": str(item.get("entity_type") or ""),
                    "entity_external_id": str(item.get("entity_external_id") or item.get("product_id") or ""),
                    "media_role": str(item.get("media_role") or ""),
                    "source_platform": str(item.get("source_platform") or "tiktok"),
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {},
                }
            )
        )
    return assets


def _derive_row_status(
    *,
    tiktok_job: Mapping[str, Any] | None,
    fastmoss_job: Mapping[str, Any] | None,
    browser_execution: Any,
    media_job: Mapping[str, Any] | None,
    fact_job: Mapping[str, Any] | None,
    write_job: Mapping[str, Any] | None,
) -> str:
    statuses = [
        _record_effective_status(tiktok_job),
        _record_effective_status(fastmoss_job),
        _record_effective_status(browser_execution),
        _record_effective_status(media_job),
        _record_effective_status(fact_job),
        _record_effective_status(write_job),
    ]
    if "unavailable" in statuses:
        return "unavailable"
    if str((write_job or {}).get("status") or "") == "success" and "failed" not in statuses:
        return "success"
    if str((fact_job or {}).get("status") or "") == "success" and "failed" not in statuses:
        return "success"
    if _record_effective_status(tiktok_job) == "failed" and _record_effective_status(fastmoss_job) != "success":
        return "failed"
    if _record_effective_status(tiktok_job) == "fallback_required" and _record_effective_status(browser_execution) in {"", "pending"}:
        return "partial_success"
    if "success" in statuses or "partial_success" in statuses:
        if "failed" in statuses or "fallback_required" in statuses:
            return "partial_success"
        return "partial_success"
    if "failed" in statuses:
        return "failed"
    return "failed"


def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    for job in reversed(jobs):
        payload = dict(job.get("payload") or {})
        if str(job.get("job_code") or "") != job_code:
            continue
        if str(payload.get("source_record_id") or "") == source_record_id:
            return job
    return None


def _latest_row_execution(executions: list[Any], *, source_record_id: str) -> Any | None:
    for execution in reversed(executions):
        if str((execution.payload or {}).get("source_record_id") or "") == source_record_id:
            return execution
    return None


def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if hasattr(record, "payload") and hasattr(record, "status"):
        result = getattr(record, "result", {}) or {}
        if isinstance(result, Mapping):
            if _is_unavailable_result(result):
                return "unavailable"
            handler_result = result.get("handler_result")
            if isinstance(handler_result, Mapping):
                if _is_unavailable_result(handler_result):
                    return "unavailable"
                return str(handler_result.get("status") or record.status or "")
        return str(getattr(record, "status", "") or "")
    if isinstance(record, Mapping):
        result = dict(record.get("result") or {})
        if _is_unavailable_result(result) or _is_unavailable_result(record):
            return "unavailable"
        handler_result = result.get("handler_result")
        if isinstance(handler_result, Mapping):
            if _is_unavailable_result(handler_result):
                return "unavailable"
            return str(handler_result.get("status") or record.get("status") or "")
        return str(record.get("status") or "")
    return ""


def _is_unavailable_result(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("availability_status") or payload.get("status") or "").strip().lower() == "unavailable":
        return True
    effective = extract_effective_result_payload(payload)
    if effective and effective is not payload and _is_unavailable_result(effective):
        return True
    normalized = payload.get("normalized_product_result")
    if isinstance(normalized, Mapping):
        if _is_unavailable_result(normalized):
            return True
    logical_fields = payload.get("logical_fields")
    if isinstance(logical_fields, Mapping) and _is_unavailable_result(logical_fields):
        return True
    product = payload.get("product")
    if isinstance(product, Mapping):
        if str(product.get("availability_status") or "").strip().lower() == "unavailable":
            return True
        facts = product.get("facts")
        if isinstance(facts, Mapping) and _is_unavailable_result(facts):
            return True
    return False


def _is_fallback_required(job: Mapping[str, Any]) -> bool:
    if not isinstance(job, Mapping):
        return False
    result = dict(job.get("result") or {})
    handler_result = dict(result.get("handler_result") or {})
    if str(handler_result.get("status") or "") == "fallback_required":
        return True
    payload = extract_effective_result_payload(job)
    return bool(payload.get("fallback_required"))


def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_key") or candidate.get("source_record_id") or "")
    return f"tiktok_product:{business_key}" if business_key else ""


def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )


def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product)/(\d+)", text)
    if match:
        return str(match.group(1))
    fallback = re.search(r"(\d{6,})", text)
    return str(fallback.group(1)) if fallback else ""


def _normalize_tiktok_product_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    product_id = _extract_tiktok_product_id(text)
    if not product_id:
        return text
    return f"https://www.tiktok.com/shop/pdp/{product_id}"


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


def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in keys
        if payload.get(key) not in (None, "", [], {})
    }


def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in values.items() if value not in (None, "", [], {})}


def _fastmoss_settings_from_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(payload.get("fastmoss") or {}) if isinstance(payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("fastmoss_window_days", "window_days"),
        ("browser_cookies", "browser_cookies"),
        ("fastmoss_live_fetch", "live_fetch"),
        ("ensure_fastmoss_logged_in", "ensure_logged_in"),
    ):
        if payload.get(source_key) not in (None, "", [], {}):
            settings[target_key] = payload.get(source_key)
    return settings


def _source_table_ref_from_request_payload(payload: Mapping[str, Any]) -> str:
    return _first_text(payload.get("source_table_ref"), payload.get("table_url"))


def _artifact_settings_from_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for source_key, target_key in (
        ("execution_control_artifact_root", "artifact_root"),
        ("execution_control_artifact_bucket", "artifact_bucket"),
        ("execution_control_artifact_store_provider", "artifact_store_provider"),
        ("execution_control_artifact_object_prefix", "artifact_object_prefix"),
        ("execution_control_minio_endpoint", "minio_endpoint"),
        ("execution_control_minio_access_key", "minio_access_key"),
        ("execution_control_minio_secret_key", "minio_secret_key"),
        ("execution_control_minio_region", "minio_region"),
        ("execution_control_minio_secure", "minio_secure"),
        ("execution_control_minio_create_bucket", "minio_create_bucket"),
    ):
        if payload.get(source_key) not in (None, "", [], {}):
            settings[target_key] = payload.get(source_key)
    return settings


def _merge_runtime_fact_bundles(*bundles: Mapping[str, Any]) -> dict[str, Any]:
    merged = {
        **{key: [] for key in FACT_BUNDLE_LIST_KEYS},
        "relations": {key: [] for key in FACT_BUNDLE_RELATION_KEYS},
    }
    for bundle in bundles:
        if not isinstance(bundle, Mapping):
            continue
        for key in FACT_BUNDLE_LIST_KEYS:
            value = bundle.get(key)
            if isinstance(value, list):
                merged[key].extend(dict(item) for item in value if isinstance(item, Mapping))
        relations = bundle.get("relations")
        if isinstance(relations, Mapping):
            for key in FACT_BUNDLE_RELATION_KEYS:
                value = relations.get(key)
                if isinstance(value, list):
                    merged["relations"][key].extend(dict(item) for item in value if isinstance(item, Mapping))
    return merged


def _dedupe_asset_refs(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        source_url = str(asset.get("source_url") or "")
        key = source_url or str(asset.get("local_path") or asset.get("object_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(asset)
    return deduped


def _collect_warnings(row_results: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for row in row_results:
        if row["row_status"] == "partial_success":
            warnings.append(f"row {row['source_record_id']} completed partially")
        if row["row_status"] == "failed":
            warnings.append(f"row {row['source_record_id']} failed")
    return warnings


def _waiting(*, stage_code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
    }
    if details:
        payload["details"] = dict(details)
    return payload


def _require_refresh_workflow(task_code: str) -> WorkflowDefinition:
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

    workflow = get_workflow_definition(task_code)
    if workflow.workflow_code not in SUPPORTED_REFRESH_TASK_CODES:
        raise ValueError(f"Expected refresh workflow definition, got {workflow.workflow_code}")
    return workflow


__all__ = [
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
]

__all__ = [name for name in globals() if not name.startswith("__")]
