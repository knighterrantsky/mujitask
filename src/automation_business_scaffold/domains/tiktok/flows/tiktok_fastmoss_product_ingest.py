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
    if stage.stage_code == "selection_row_browser_fallback":
        return _advance_selection_row_browser_fallback(
            store=store, request=request, workflow=workflow, stage_code=stage_code
        )
    if stage.stage_code == "resume_selection_rows_after_browser_fallback":
        return _advance_resume_selection_rows_after_browser_fallback(
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
    row_jobs = _row_refresh_jobs_for_summary(store=store, request_id=request.request_id)
    read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )

    row_results_by_key: dict[str, dict[str, Any]] = {}
    for job in row_jobs:
        handler_result = _job_handler_result(job)
        handler_summary = _mapping(handler_result.get("summary"))
        row_result = _mapping(handler_result.get("result"))
        source_record_id = (
            row_result.get("source_record_id")
            or handler_result.get("source_record_id")
            or handler_summary.get("source_record_id")
            or (job.get("payload") or {}).get("source_record_id", "")
        )
        product_id = (
            row_result.get("product_business_key")
            or row_result.get("business_entity_key")
            or handler_result.get("product_business_key")
            or handler_summary.get("product_business_key")
            or ""
        )
        row_key = _first_non_empty(source_record_id, product_id, job.get("business_key"), job.get("job_id"))
        row_results_by_key[row_key] = {
            "source_record_id": source_record_id,
            "product_id": product_id,
            "row_status": row_result.get("row_status")
            or handler_result.get("row_status")
            or job.get("status", ""),
        }
    row_results = list(row_results_by_key.values())

    counts = _aggregate_request_children(store, request_id=request.request_id)
    final_status = _determine_final_status(
        force_result=force_result,
        row_jobs=row_jobs,
        row_results=row_results,
        counts=counts,
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
    if (
        current_stage == workflow.summary_policy.summary_stage_code
        and _selection_row_browser_resume_candidates(store=store, request_id=request_id)
        and not _api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        )
    ):
        next_stage = "resume_selection_rows_after_browser_fallback"
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage=next_stage,
            progress_stage=next_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            last_progress_at=time.time(),
        )
        _refresh_request_aggregate_counts(store, request_id=request_id)
        return [
            {
                "request_id": request_id,
                "stage_code": next_stage,
                "released": True,
                "next_executor_status": "pending",
            }
        ]
    stage = workflow.require_stage(current_stage)
    if stage.execution_mode != "worker_jobs":
        return []

    child_records = _stage_child_records(store, request_id=request_id, stage_code=current_stage)
    if not child_records:
        return []
    if _has_active_children(child_records):
        return []
    next_stage = current_stage
    if current_stage == "selection_row_browser_fallback" and _selection_row_browser_resume_candidates(
        store=store,
        request_id=request_id,
    ):
        next_stage = "resume_selection_rows_after_browser_fallback"

    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=next_stage,
        progress_stage=next_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        last_progress_at=time.time(),
    )
    _refresh_request_aggregate_counts(store, request_id=request_id)
    return [
        {
            "request_id": request_id,
            "stage_code": next_stage,
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

    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "collect_job_count": len(stage_jobs),
            "fallback_candidate_count": len(fallback_candidates),
        },
    )
    if fallback_candidates:
        workflow.require_stage("selection_row_browser_fallback")
        return {
            "action": "advance",
            "next_stage": "selection_row_browser_fallback",
            "details": {"fallback_candidate_count": len(fallback_candidates)},
        }

    return {"action": "advance", "next_stage": "ready_for_summary"}


def _advance_selection_row_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    executions = _browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=stage_code,
    )
    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    if not fallback_candidates and not executions:
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"fallback_candidate_count": 0},
        }
    if not executions and fallback_candidates:
        dispatches: dict[str, Any] = {}
        for fallback_handler in sorted(
            {str(candidate.get("fallback_handler") or "") for candidate in fallback_candidates}
        ):
            if not fallback_handler:
                continue
            job_def = workflow.require_job(fallback_handler)
            items: list[dict[str, Any]] = []
            for candidate in fallback_candidates:
                if str(candidate.get("fallback_handler") or "") != fallback_handler:
                    continue
                payload = _selection_row_browser_execution_payload(
                    request_payload=request.payload,
                    stage_code=stage_code,
                    candidate=candidate,
                )
                keys = render_job_keys(
                    job_def,
                    request.payload,
                    candidate,
                    payload,
                    request_id=request.request_id,
                    task_code=request.task_code,
                    workflow_code=workflow.workflow_code,
                    stage_code=stage_code,
                    item_code=job_def.job_code,
                )
                items.append(
                    {
                        "business_key": keys["business_key"]
                        or str(candidate.get("business_entity_key") or ""),
                        "dedupe_key": build_stage_local_dedupe_key(
                            keys["dedupe_key"],
                            job_def.job_code,
                            stage_scope=stage_code,
                        ),
                        "resource_code": _row_browser_resource_code(
                            fallback_handler=fallback_handler,
                            payload=payload,
                            candidate=candidate,
                        ),
                        "payload": payload,
                        "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                    }
                )
            if items:
                dispatches[fallback_handler] = store.enqueue_task_executions(
                    request_id=request.request_id,
                    item_code=job_def.job_code,
                    workflow_code=workflow.workflow_code,
                    items=items,
                )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "browser_dispatches": dispatches,
                "fallback_candidate_count": len(fallback_candidates),
            },
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Enqueued selection row browser fallback executions.",
            "details": {
                "created_count": sum(
                    int(dispatch.get("created_count") or 0) for dispatch in dispatches.values()
                ),
                "fallback_candidate_count": len(fallback_candidates),
            },
        }
    if _any_browser_executions_active(executions):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Waiting for selection row browser fallback executions to finish.",
        }
    resumable = _selection_row_browser_resume_candidates(store=store, request_id=request.request_id)
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "resumable_count": len(resumable),
            "status": "success" if resumable else "failed",
        },
    )
    if resumable:
        return {
            "action": "advance",
            "next_stage": "resume_selection_rows_after_browser_fallback",
            "details": {"resumable_count": len(resumable)},
        }
    return {
        "action": "advance",
        "next_stage": "ready_for_summary",
        "details": {"execution_count": len(executions), "resumable_count": 0},
    }


def _advance_resume_selection_rows_after_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    candidates = _selection_row_browser_resume_candidates(
        store=store,
        request_id=request.request_id,
    )
    if candidates:
        row_job_def = workflow.require_job("selection_row_refresh")
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=row_job_def.job_code,
            jobs=[
                _selection_row_resume_job(
                    request=request,
                    workflow=workflow,
                    stage_code=stage_code,
                    row_job_def=row_job_def,
                    candidate=candidate,
                )
                for candidate in candidates
            ],
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "resumable_count": len(candidates),
                "existing_job_count": len(jobs),
                "row_dispatch": dispatch,
            },
        )
        if int(dispatch["created_count"]) > 0:
            return {
                "action": "waiting",
                "current_stage": stage_code,
                "message": "Enqueued missing selection row refresh retries after browser fallback.",
                "details": {
                    "created_count": int(dispatch["created_count"]),
                    "resumable_count": len(candidates),
                    "existing_job_count": len(jobs),
                },
            }
    elif not jobs:
        return {
            "action": "advance",
            "next_stage": "ready_for_summary",
            "details": {"resumable_count": 0},
        }

    jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if _any_api_jobs_active(jobs):
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Waiting for selection row refresh retries after browser fallback to finish.",
        }
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"resumed_job_count": len(jobs)},
    )
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"resumed_job_count": len(jobs)}}


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
