from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.control_plane.runtime_config.settings import (
    PRODUCT_INGEST_TASK_CODE,
    build_request_payload,
)
from automation_business_scaffold.contracts.handler.shared import compact_dict
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_browser_executions_active as _any_browser_executions_active,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    has_active_records as _has_active_children,
    is_fallback_required,
    render_job_keys,
    stage_child_records as _stage_child_records,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running", "retry_wait"}

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


# ---------------------------------------------------------------------------
# Stage: read_selection_rows
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stage: dispatch_selection_row_refresh
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stage: collect_selection_rows
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_refresh_jobs_for_summary(*, store: RuntimeStore, request_id: str) -> list[dict[str, Any]]:
    return [
        *_api_jobs_for_stage(store, request_id=request_id, stage_code="collect_selection_rows"),
        *_api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        ),
    ]


def _selection_row_browser_fallback_candidates(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in _api_jobs_for_stage(
        store,
        request_id=request_id,
        stage_code="collect_selection_rows",
    ):
        if str(job.get("job_code") or "") != "selection_row_refresh":
            continue
        if not is_fallback_required(job):
            continue
        row_payload = dict(job.get("payload") or {})
        handler_result = _job_handler_result(job)
        handler_summary = _mapping(handler_result.get("summary"))
        result_payload = extract_effective_result_payload(job)
        fallback_handler = _first_non_empty(
            result_payload.get("fallback_handler"),
            handler_summary.get("fallback_handler"),
        )
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = _mapping(result_payload.get("browser_fallback_payload"))
        if not browser_payload:
            next_action_payload = _mapping(_mapping(handler_result.get("next_action")).get("payload"))
            browser_payload = _mapping(next_action_payload.get("payload")) or next_action_payload
        source_record_id = _first_non_empty(
            result_payload.get("source_record_id"),
            row_payload.get("source_record_id"),
        )
        business_entity_key = _first_non_empty(
            result_payload.get("business_entity_key"),
            row_payload.get("business_key"),
            job.get("business_key"),
            source_record_id,
        )
        fallback_source_job_id = _first_non_empty(
            browser_payload.get("fallback_source_job_id"),
            result_payload.get("fallback_source_job_id"),
            job.get("job_id"),
        )
        browser_payload = {
            **browser_payload,
            "source_record_id": source_record_id,
            "business_entity_key": business_entity_key,
            "fallback_source_job_id": fallback_source_job_id,
        }
        product_identity = _mapping(result_payload.get("product_identity")) or _mapping(
            row_payload.get("product_identity")
        )
        candidates.append(
            {
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    business_entity_key=business_entity_key,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_non_empty(result_payload.get("fallback_reason")),
                "source_record_id": source_record_id,
                "business_entity_key": business_entity_key,
                "candidate_key": business_entity_key,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": row_payload,
                "row_result": result_payload,
                "browser_fallback_payload": compact_dict(browser_payload),
                "product_identity": product_identity,
                "normalized_product_url": _first_non_empty(
                    browser_payload.get("normalized_product_url"),
                    row_payload.get("normalized_product_url"),
                    product_identity.get("normalized_product_url"),
                ),
                "normalized_product_result": _mapping(
                    result_payload.get("normalized_product_result")
                ),
            }
        )
    return candidates


def _selection_row_browser_execution_payload(
    *,
    request_payload: Mapping[str, Any],
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = {
        **_payload_subset(request_payload, FASTMOSS_BROWSER_PASSTHROUGH_KEYS + RUNTIME_DB_PASSTHROUGH_KEYS),
        **_mapping(candidate.get("browser_fallback_payload")),
        "stage_code": stage_code,
        "source_record_id": str(candidate.get("source_record_id") or ""),
        "business_entity_key": str(candidate.get("business_entity_key") or ""),
        "candidate_key": str(candidate.get("candidate_key") or ""),
        "fallback_handler": fallback_handler,
        "fallback_source_job_id": _first_non_empty(
            _mapping(candidate.get("browser_fallback_payload")).get("fallback_source_job_id"),
            candidate.get("row_job_id"),
        ),
        "product_identity": _mapping(candidate.get("product_identity")),
        "normalized_product_url": str(candidate.get("normalized_product_url") or ""),
    }
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault("search_query", str(candidate.get("business_entity_key") or ""))
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        payload.setdefault("search_request", _mapping(payload.get("search_request")))
        payload.setdefault("verification_request", _mapping(payload.get("verification_request")))
    return compact_dict(payload)


def _selection_row_browser_resume_candidates(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    fallback_by_key = {
        str(candidate.get("fallback_key") or ""): candidate
        for candidate in _selection_row_browser_fallback_candidates(store=store, request_id=request_id)
    }
    candidates: list[dict[str, Any]] = []
    for execution in _browser_executions_for_stage(
        store,
        request_id=request_id,
        stage_code="selection_row_browser_fallback",
    ):
        if _handler_status_from_execution(execution) != "success":
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_non_empty(payload.get("source_record_id"))
        business_entity_key = _first_non_empty(payload.get("business_entity_key"))
        fallback_key = _row_fallback_key(
            source_record_id=source_record_id,
            business_entity_key=business_entity_key,
            fallback_handler=fallback_handler,
        )
        fallback_candidate = fallback_by_key.get(fallback_key)
        if not fallback_candidate:
            continue
        execution_payload = extract_effective_result_payload(execution)
        if fallback_handler == "tiktok_product_browser_fetch" and not _mapping(
            execution_payload.get("normalized_product_result")
        ):
            continue
        candidates.append(
            {
                **dict(fallback_candidate),
                "browser_execution_id": str(execution.execution_id),
                "browser_execution_payload": execution_payload,
            }
        )
    return candidates


def _selection_row_resume_job(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    row_job_def: Any,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _selection_row_resume_payload(stage_code=stage_code, candidate=candidate)
    product_identity = _mapping(candidate.get("product_identity"))
    resume_key = _first_non_empty(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("candidate_key"),
        product_identity.get("product_id"),
        product_identity.get("normalized_product_url"),
    )
    candidate_context = {
        **dict(candidate),
        "source_record_id_or_product_id": resume_key,
    }
    payload_context = {
        **payload,
        "source_record_id_or_product_id": resume_key,
    }
    keys = render_job_keys(
        row_job_def,
        request.payload,
        candidate_context,
        payload_context,
        request_id=request.request_id,
        task_code=request.task_code,
        workflow_code=workflow.workflow_code,
        stage_code=stage_code,
        job_code=row_job_def.job_code,
    )
    dedupe_base = keys["dedupe_key"] or f"{request.request_id}:{stage_code}:{resume_key}"
    return {
        "business_key": keys["business_key"] or resume_key,
        "dedupe_key": build_stage_local_dedupe_key(
            f"{dedupe_base}:after-browser-fallback",
            row_job_def.job_code,
        ),
        "payload": payload,
        "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
    }


def _selection_row_resume_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(_mapping(candidate.get("row_payload")))
    browser_payload = _mapping(candidate.get("browser_execution_payload"))
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
        payload["normalized_product_result"] = _mapping(
            browser_payload.get("normalized_product_result")
        )
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized_product = _mapping(candidate.get("normalized_product_result"))
        if normalized_product:
            payload["normalized_product_result"] = normalized_product
    return compact_dict(payload)


def _row_fallback_key(*, source_record_id: str, business_entity_key: str, fallback_handler: str) -> str:
    row_key = _first_non_empty(source_record_id, business_entity_key)
    return f"{fallback_handler}:{row_key}"


def _row_browser_resource_code(
    *,
    fallback_handler: str,
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    if fallback_handler == "fastmoss_security_browser_resolve":
        return _fastmoss_browser_resource_code(payload)
    return _browser_resource_code(candidate)


def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_entity_key") or candidate.get("candidate_key") or "")
    return f"tiktok_product:{business_key}" if business_key else ""


def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_non_empty(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )


def _search_digest_for_row_fallback(candidate: Mapping[str, Any]) -> str:
    value = _first_non_empty(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("row_job_id"),
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _resolve_candidate_rows(
    store: RuntimeStore,
    *,
    request: Any,
    request_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    product_url = str(request_payload.get("product_url") or "").strip()
    product_id = str(request_payload.get("product_id") or "").strip()
    selection_record_id = str(request_payload.get("selection_record_id") or "").strip()

    if product_url or product_id:
        identity = _resolve_product_identity(request_payload)
        return [
            {
                "source_record_id": selection_record_id,
                "product_identity": identity,
                "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                "source_context": {},
            }
        ]

    read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )
    if not read_job:
        return []

    handler_result = _job_handler_result(read_job)
    nested_result = (
        handler_result.get("result") if isinstance(handler_result.get("result"), Mapping) else {}
    )
    source_rows = (nested_result or handler_result).get("source_rows") or []
    if not isinstance(source_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    for row in source_rows:
        if not isinstance(row, Mapping):
            continue
        rows.append(
            {
                "source_record_id": str(row.get("source_record_id") or ""),
                "product_identity": _mapping(row.get("product_identity")),
                "source_table_ref": str(
                    row.get("source_table_ref") or request_payload.get("selection_table_ref") or ""
                ),
                "source_context": _mapping(row.get("source_context")),
            }
        )
    return rows


def _refresh_request_aggregate_counts(store: RuntimeStore, *, request_id: str) -> None:
    counts = _aggregate_request_children(store, request_id=request_id)
    store.update_task_request(
        request_id=request_id,
        child_total_count=counts["total"],
        child_terminal_count=counts["terminal_count"],
        child_success_count=counts["success_count"],
        child_failed_count=counts["failed_count"],
        child_skipped_count=counts["skipped_count"],
    )


def _aggregate_request_children(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    api_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    executions = store.list_task_executions(request_id=request_id)
    counts: dict[str, int] = {}
    success_count = 0
    failed_count = 0
    skipped_count = 0
    active_count = 0

    for job in api_jobs:
        handler_status = _handler_status_from_api_job(job)
        if str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            pass
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or str(job.get("status") or "unknown")
        counts[status_key] = counts.get(status_key, 0) + 1

    for execution in executions:
        handler_status = _handler_status_from_execution(execution)
        if execution.status in ACTIVE_API_JOB_STATUSES:
            active_count += 1
        elif handler_status == "skipped":
            skipped_count += 1
        elif handler_status == "fallback_required":
            pass
        elif handler_status in {"success", "partial_success"}:
            success_count += 1
        else:
            failed_count += 1
        status_key = handler_status or execution.status or "unknown"
        counts[status_key] = counts.get(status_key, 0) + 1

    total = len(api_jobs) + len(executions)
    terminal_count = max(total - active_count, 0)
    return {
        "total": total,
        "counts": counts,
        "terminal_count": terminal_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "active_count": active_count,
    }


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


def _api_jobs_for_stage(
    store: RuntimeStore, *, request_id: str, stage_code: str
) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _latest_api_job_by_code(jobs: list[dict[str, Any]], job_code: str) -> dict[str, Any]:
    for job in reversed(jobs):
        if str(job.get("job_code") or "") == job_code:
            return job
    return {}


def _any_api_jobs_active(jobs: list[dict[str, Any]]) -> bool:
    return any(str(job.get("status") or "") in ACTIVE_API_JOB_STATUSES for job in jobs)


def _any_failed_api_jobs(jobs: list[dict[str, Any]]) -> bool:
    return any(_job_failed(job) for job in jobs)


def _handler_status_from_api_job(job: Mapping[str, Any] | None) -> str:
    if not job:
        return ""
    handler_result = _job_handler_result(job)
    return str(handler_result.get("status") or job.get("status") or "")


def _handler_status_from_execution(execution: Any) -> str:
    if execution is None:
        return ""
    result = dict(execution.result or {})
    handler_result = result.get("handler_result")
    if isinstance(handler_result, Mapping):
        return str(handler_result.get("status") or execution.status or "")
    return str(execution.status or "")


def _job_handler_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    handler_result = result.get("handler_result")
    return dict(handler_result or {}) if isinstance(handler_result, Mapping) else {}


def _job_effective_result(job: Mapping[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    result = dict(job.get("result") or {})
    if "handler_result" in result:
        result = {key: value for key, value in result.items() if key != "handler_result"}
    return result


def _job_failed(job: Mapping[str, Any] | None) -> bool:
    if not job:
        return False
    return str(job.get("status") or "") == "failed" or _handler_status_from_api_job(job) == "failed"


def _selection_mode_enabled(request_payload: Mapping[str, Any]) -> bool:
    return bool(
        str(request_payload.get("selection_table_ref") or "").strip()
        or str(request_payload.get("selection_record_id") or "").strip()
    )


def _limit_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    request_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    limit = _candidate_row_limit(request_payload)
    if limit <= 0:
        return rows
    return rows[:limit]


def _candidate_row_limit(request_payload: Mapping[str, Any]) -> int:
    for key in ("selection_limit", "selection_max_rows", "max_selection_rows", "max_rows"):
        raw_value = request_payload.get(key)
        if raw_value in (None, ""):
            continue
        try:
            return max(int(raw_value), 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: payload[key] for key in keys if key in payload and payload.get(key) not in (None, "")
    }


def _resolve_product_identity(*sources: Any) -> dict[str, str]:
    product_url = _first_non_empty(
        *[
            _lookup_nested(
                source, "normalized_product_url", "normalized_url", "product_url", "source_url"
            )
            for source in sources
        ]
    )
    product_id = _first_non_empty(*[_lookup_nested(source, "product_id") for source in sources])
    if not product_id:
        product_id = _extract_tiktok_product_id(product_url)
    normalized_url = _normalize_tiktok_product_url(product_url) if product_url else ""
    if not product_url and normalized_url:
        product_url = normalized_url
    business_key = product_id or normalized_url or product_url
    return {
        "product_id": product_id,
        "product_url": product_url,
        "normalized_product_url": normalized_url or product_url,
        "business_key": business_key,
    }


def _lookup_nested(source: Any, *keys: str) -> str:
    if source is None:
        return ""
    if hasattr(source, "to_dict"):
        source = source.to_dict()
    if not isinstance(source, Mapping):
        return ""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    result = source.get("result")
    if isinstance(result, Mapping):
        for key in keys:
            value = result.get(key)
            if value not in (None, ""):
                return str(value)
        normalized_product = result.get("normalized_product_result")
        if isinstance(normalized_product, Mapping):
            for key in keys:
                value = normalized_product.get(key)
                if value not in (None, ""):
                    return str(value)
            logical_fields = normalized_product.get("logical_fields")
            if isinstance(logical_fields, Mapping):
                for key in keys:
                    value = logical_fields.get(key)
                    if value not in (None, ""):
                        return str(value)
    payload = source.get("payload")
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_tiktok_product_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/(?:pdp|product)/(\d+)", text)
    if match:
        return str(match.group(1))
    fallback = re.search(r"(\d{8,})", text)
    return str(fallback.group(1)) if fallback else ""


def _normalize_tiktok_product_url(value: str) -> str:
    product_id = _extract_tiktok_product_id(value)
    if not product_id:
        return str(value or "").strip()
    return f"https://www.tiktok.com/shop/pdp/{product_id}"


__all__ = [
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
]

__all__ = [name for name in globals() if not name.startswith("__")]
