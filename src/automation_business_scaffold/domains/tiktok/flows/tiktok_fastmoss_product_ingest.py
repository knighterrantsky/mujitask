from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.control_plane.runtime_config.settings import (
    PRODUCT_INGEST_TASK_CODE,
    build_request_payload,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
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


def advance_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    stage = workflow.require_stage(stage_code)
    if stage.stage_code == "read_selection_rows":
        return _advance_read_selection_rows(
            store=store, request=request, workflow=workflow, stage_code=stage_code
        )
    if stage.stage_code == "dispatch_selection_row_refresh":
        return _advance_dispatch_selection_row_refresh(
            store=store, request=request, workflow=workflow, stage_code=stage_code
        )
    if stage.stage_code == "collect_selection_rows":
        return _advance_collect_selection_rows(
            store=store, request=request, workflow=workflow, stage_code=stage_code
        )
    if stage.stage_code == workflow.summary_policy.summary_stage_code:
        return {"action": "finalize"}
    return {
        "action": "finalize",
        "final_status": "failed",
        "result": {"status": "failed", "message": f"Unsupported ingest stage {stage_code}."},
        "summary": {"total": 0, "counts": {"unsupported_stage": 1}},
        "details": {"unsupported_stage": stage_code},
    }


def finalize_request(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    row_jobs = _api_jobs_for_stage(
        store, request_id=request.request_id, stage_code="collect_selection_rows"
    )
    read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )

    row_results = []
    for job in row_jobs:
        handler_result = _job_handler_result(job)
        handler_summary = _mapping(handler_result.get("summary"))
        row_result = _mapping(handler_result.get("result"))
        row_results.append(
            {
                "source_record_id": (
                    row_result.get("source_record_id")
                    or handler_result.get("source_record_id")
                    or handler_summary.get("source_record_id")
                    or (job.get("payload") or {}).get("source_record_id", "")
                ),
                "product_id": (
                    row_result.get("product_business_key")
                    or row_result.get("business_entity_key")
                    or handler_result.get("product_business_key")
                    or handler_summary.get("product_business_key")
                    or ""
                ),
                "row_status": row_result.get("row_status")
                or handler_result.get("row_status")
                or job.get("status", ""),
            }
        )

    counts = _aggregate_request_children(store, request_id=request.request_id)
    final_status = _determine_final_status(
        force_result=force_result, row_jobs=row_jobs, counts=counts
    )
    summary = {
        "final_status": final_status,
        "total": counts["total"],
        "counts": counts["counts"],
        "child_success_count": counts["success_count"],
        "child_failed_count": counts["failed_count"],
        "child_skipped_count": counts["skipped_count"],
    }
    if force_result and isinstance(force_result.get("summary"), Mapping):
        summary.update(dict(force_result.get("summary") or {}))

    final_result = {
        "row_count": len(row_results),
        "rows": row_results,
        "selection_table_read": _job_effective_result(read_job),
    }
    if force_result and isinstance(force_result.get("result"), Mapping):
        final_result.update(dict(force_result.get("result") or {}))

    error_text = "" if final_status != "failed" else str(final_result.get("message") or "")
    store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage="completed",
        summary=summary,
        result=final_result,
        error_text=error_text,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=time.time(),
    )
    _ensure_request_outbox(store=store, request_id=request.request_id)
    _refresh_request_aggregate_counts(store, request_id=request.request_id)
    payload = build_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Executor finalized the product ingest request.",
    )
    payload["final_status"] = final_status
    return payload


def release_request_after_child_completion(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != PRODUCT_INGEST_TASK_CODE:
        return []
    workflow = get_workflow_definition(request.task_code)
    current_stage = str(request.current_stage or "").strip()
    if not current_stage:
        return []
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    api_jobs = _api_jobs_for_stage(store, request_id=request_id, stage_code=current_stage)
    if not api_jobs:
        return []
    if _any_api_jobs_active(api_jobs):
        return []

    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=current_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return [
        {
            "request_id": request_id,
            "stage_code": current_stage,
            "released": True,
            "next_executor_status": "pending",
        }
    ]


# ---------------------------------------------------------------------------
# Stage: read_selection_rows
# ---------------------------------------------------------------------------


def _advance_read_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    if not _selection_mode_enabled(request_payload):
        return {
            "action": "advance",
            "next_stage": "dispatch_selection_row_refresh",
            "details": {"stage_transition": "direct_ingest_skip_selection_read"},
        }

    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="feishu_table_read",
            jobs=[
                {
                    "business_key": str(
                        request_payload.get("selection_table_ref") or request.request_id
                    ),
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_table_read",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "selection_record_id": str(
                            request_payload.get("selection_record_id") or ""
                        ),
                        "product_url": str(request_payload.get("product_url") or ""),
                        "product_id": str(request_payload.get("product_id") or ""),
                        "adapter_code": "selection_table_source_adapter",
                        "table_refs": request_payload.get("table_refs") or {},
                        "access_token": request_payload.get("access_token") or "",
                        "access_token_env": request_payload.get("access_token_env") or "",
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the selection table read stage.",
            "details": {"dispatch_payload": {"feishu_table_read": enqueue_payload}},
        }

    if _any_api_jobs_active(stage_jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Selection table read is still running.",
        }

    if _any_failed_api_jobs(stage_jobs):
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "Selection table read failed."},
            "summary": {"total": 1, "counts": {"selection_table_read_failed": 1}},
            "details": {"failed_jobs": stage_jobs},
        }

    return {
        "action": "advance",
        "next_stage": "dispatch_selection_row_refresh",
        "details": {
            "selection_table_read": _latest_api_job_by_code(stage_jobs, "feishu_table_read")
        },
    }


# ---------------------------------------------------------------------------
# Stage: dispatch_selection_row_refresh
# ---------------------------------------------------------------------------


def _advance_dispatch_selection_row_refresh(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    existing_jobs = _api_jobs_for_stage(
        store, request_id=request.request_id, stage_code="collect_selection_rows"
    )
    if existing_jobs:
        return {
            "action": "advance",
            "next_stage": "collect_selection_rows",
            "details": {"reason": "row jobs already dispatched"},
        }

    candidate_rows = _resolve_candidate_rows(
        store, request=request, request_payload=request_payload
    )
    candidate_rows = _limit_candidate_rows(candidate_rows, request_payload=request_payload)
    if not candidate_rows:
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"reason": "no candidate rows to refresh"},
        }

    jobs = []
    for row in candidate_rows:
        source_record_id = str(row.get("source_record_id") or "")
        product_identity = _mapping(row.get("product_identity"))
        business_key = _first_non_empty(
            product_identity.get("product_id"),
            product_identity.get("normalized_product_url"),
            source_record_id,
        )
        row_payload = {
            **_payload_subset(
                request_payload, FACT_PERSISTENCE_PASSTHROUGH_KEYS + ARTIFACT_PASSTHROUGH_KEYS
            ),
            "request_payload": request_payload,
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": request.task_code,
            "stage_code": "collect_selection_rows",
            "source_record_id": source_record_id,
            "source_table_ref": str(
                row.get("source_table_ref") or request_payload.get("selection_table_ref") or ""
            ),
            "target_table_ref": str(request_payload.get("selection_table_ref") or ""),
            "product_identity": product_identity,
            "source_context": _mapping(row.get("source_context")),
            "fallback_allowed": bool(request_payload.get("fallback_allowed", True)),
            "writeback_enabled": bool(request_payload.get("writeback_enabled", True)),
            "fastmoss_phone": str(request_payload.get("fastmoss_phone") or ""),
            "fastmoss_password": str(request_payload.get("fastmoss_password") or ""),
            "fastmoss_phone_env": str(
                request_payload.get("fastmoss_phone_env") or "FASTMOSS_PHONE"
            ),
            "fastmoss_password_env": str(
                request_payload.get("fastmoss_password_env") or "FASTMOSS_PASSWORD"
            ),
            "fastmoss_live_fetch": str(request_payload.get("fastmoss_live_fetch") or ""),
            "table_refs": request_payload.get("table_refs") or {},
            "access_token": request_payload.get("access_token") or "",
            "access_token_env": request_payload.get("access_token_env") or "",
        }
        row_payload["requires_fact_db"] = True
        row_payload["requires_object_storage"] = True
        row_payload["require_database_persistence"] = True
        row_payload["require_object_storage"] = True
        jobs.append(
            {
                "business_key": business_key,
                "dedupe_key": f"{request.request_id}:collect_selection_rows:{source_record_id or business_key}",
                "max_attempts": 1,
                "payload": row_payload,
            }
        )

    enqueue_payload = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code="selection_row_refresh",
        jobs=jobs,
    )
    return {
        "action": "advance",
        "next_stage": "collect_selection_rows",
        "details": {
            "dispatch_payload": enqueue_payload,
            "row_count": len(jobs),
        },
    }


# ---------------------------------------------------------------------------
# Stage: collect_selection_rows
# ---------------------------------------------------------------------------


def _advance_collect_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "No selection row refresh jobs found."},
            "summary": {"total": 0, "counts": {"no_row_jobs": 1}},
        }

    if _any_api_jobs_active(stage_jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Selection row refresh jobs are still running.",
        }

    return {"action": "advance", "next_stage": "ready_for_summary"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _ensure_request_outbox(*, store: RuntimeStore, request_id: str) -> None:
    request = store.load_task_request(request_id=request_id)
    summary = dict(request.summary or {})
    result = dict(request.result or {})
    store.create_notification_outbox(
        channel_code=request.source_channel_code or "noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=request.reply_target,
        payload={
            "message_text": build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
                message_format=str(request.payload.get("outbox_message_format") or ""),
                message_template=str(request.payload.get("outbox_message_template") or ""),
            ),
            "request_id": request.request_id,
            "task_code": request.task_code,
            "summary": summary,
            "result": result,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )


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
        elif handler_status in {"success", "partial_success", "fallback_required"}:
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
        elif handler_status in {"success", "partial_success", "fallback_required"}:
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
    counts: Mapping[str, Any],
) -> str:
    if force_result and str(force_result.get("final_status") or "") in {
        "success",
        "partial_success",
        "failed",
    }:
        return str(force_result["final_status"])
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
