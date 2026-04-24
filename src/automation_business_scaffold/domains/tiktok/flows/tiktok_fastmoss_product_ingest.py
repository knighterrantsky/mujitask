from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.control_plane.runtime_config.settings import (
    PRODUCT_INGEST_TASK_CODE,
    build_request_payload,
    build_outbox_message_text,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

ACTIVE_API_JOB_STATUSES = {"pending", "running", "retry_wait"}
ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}


def advance_stage(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    stage = workflow.require_stage(stage_code)
    if stage.stage_code == "read_selection_rows":
        return _advance_ingest_read_selection_rows(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "collect_product_data":
        return _advance_ingest_collect_product_data(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "browser_fallback":
        return _advance_ingest_browser_fallback(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "sync_media":
        return _advance_ingest_sync_media(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "persist_facts":
        return _advance_ingest_persist_facts(store=store, request=request, workflow=workflow, stage_code=stage_code)
    if stage.stage_code == "writeback_selection_rows":
        return _advance_ingest_writeback_selection_rows(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=stage_code,
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
    request_payload = dict(request.payload or {})
    collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
    browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
    media_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="sync_media"),
        "media_asset_sync",
    )
    fact_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="persist_facts"),
        "fact_bundle_upsert",
    )
    writeback_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="writeback_selection_rows"),
        "feishu_table_write",
    )
    tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
    fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
    identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job, fact_job)

    final_result = {
        "product_id": identity["product_id"],
        "normalized_product_url": identity["normalized_product_url"],
        "tiktok_product": _job_effective_result(tiktok_source) if isinstance(tiktok_source, Mapping) else {},
        "fastmoss_product": _job_effective_result(fastmoss_job),
        "media_asset_sync": _job_effective_result(media_job),
        "fact_bundle_upsert": _job_effective_result(fact_job),
        "selection_writeback": _job_effective_result(writeback_job),
        "browser_fallback_executions": [execution.to_dict() for execution in browser_execs],
    }
    if force_result and isinstance(force_result.get("result"), Mapping):
        final_result.update(dict(force_result.get("result") or {}))

    counts = _aggregate_request_children(store, request_id=request.request_id)
    summary = {
        "total": counts["total"],
        "counts": counts["counts"],
        "child_success_count": counts["success_count"],
        "child_failed_count": counts["failed_count"],
        "child_skipped_count": counts["skipped_count"],
    }
    if force_result and isinstance(force_result.get("summary"), Mapping):
        summary.update(dict(force_result.get("summary") or {}))

    final_status = _determine_product_ingest_final_status(
        force_result=force_result,
        fact_job=fact_job,
        tiktok_source=tiktok_source,
        counts=counts,
    )
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
    executions = _browser_executions_for_stage(store, request_id=request_id, stage_code=current_stage)
    if not (api_jobs or executions):
        return []
    if _any_api_jobs_active(api_jobs) or _any_browser_executions_active(executions):
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
            ),
            "request_id": request.request_id,
            "task_code": request.task_code,
            "summary": summary,
            "result": result,
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )


def _advance_ingest_read_selection_rows(
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
            "next_stage": "collect_product_data",
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
                    "business_key": str(request_payload.get("selection_record_id") or request_payload.get("selection_table_ref") or request.request_id),
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_table_read",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "source_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "selection_record_id": str(request_payload.get("selection_record_id") or ""),
                        "product_url": str(request_payload.get("product_url") or ""),
                        "product_id": str(request_payload.get("product_id") or ""),
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
        return {"action": "waiting", "current_stage": stage_code, "message": "Selection table read is still running."}

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
        "next_stage": "collect_product_data",
        "details": {"selection_table_read": _latest_api_job_by_code(stage_jobs, "feishu_table_read")},
    }


def _advance_ingest_collect_product_data(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    table_read_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="read_selection_rows"),
        "feishu_table_read",
    )
    identity = _resolve_product_identity(request_payload, table_read_job)

    if not stage_jobs:
        common_payload = {
            "request_payload": request_payload,
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": request.task_code,
            "stage_code": stage_code,
            "product_identity": identity,
            "normalized_product_url": identity["normalized_product_url"],
            "product_id": identity["product_id"],
            "selection_context": _job_effective_result(table_read_job),
            "fallback_allowed": bool(request_payload.get("fallback_allowed", True)),
        }
        dispatches: dict[str, Any] = {}
        dispatches["tiktok_product_request_fetch"] = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="tiktok_product_request_fetch",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:tiktok:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": dict(common_payload),
                }
            ],
        )
        dispatches["fastmoss_product_fetch"] = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="fastmoss_product_fetch",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:fastmoss:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": dict(common_payload),
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched TikTok request and FastMoss product jobs.",
            "details": {"dispatch_payload": dispatches, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Product collection jobs are still running."}

    tiktok_job = _latest_api_job_by_code(stage_jobs, "tiktok_product_request_fetch")
    fastmoss_job = _latest_api_job_by_code(stage_jobs, "fastmoss_product_fetch")
    tiktok_handler_status = _handler_status_from_api_job(tiktok_job)
    if tiktok_handler_status == "fallback_required":
        return {
            "action": "advance",
            "next_stage": "browser_fallback",
            "details": {
                "tiktok_product_request_fetch": tiktok_job,
                "fastmoss_product_fetch": fastmoss_job,
                "product_identity": identity,
            },
        }
    if _job_failed(tiktok_job):
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "TikTok request-first collection failed."},
            "summary": {"total": 2, "counts": {"tiktok_request_failed": 1}},
            "details": {"tiktok_product_request_fetch": tiktok_job, "fastmoss_product_fetch": fastmoss_job},
        }
    return {
        "action": "advance",
        "next_stage": "sync_media",
        "details": {
            "tiktok_product_request_fetch": tiktok_job,
            "fastmoss_product_fetch": fastmoss_job,
            "product_identity": identity,
        },
    }


def _advance_ingest_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_executions = _browser_executions_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    tiktok_job = _latest_api_job_by_code(
        _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data"),
        "tiktok_product_request_fetch",
    )
    identity = _resolve_product_identity(request_payload, tiktok_job)

    if not stage_executions:
        handler_result = _job_handler_result(tiktok_job)
        next_action_payload = dict((handler_result.get("next_action") or {}).get("payload") or {})
        enqueue_payload = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code="tiktok_product_browser_fetch",
            workflow_code=request.task_code,
            items=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:browser:{identity['business_key']}",
                    "resource_code": str(request_payload.get("browser_profile_ref") or "roxy-tiktok"),
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "product_identity": identity,
                        "normalized_product_url": identity["normalized_product_url"],
                        "product_id": identity["product_id"],
                        **next_action_payload,
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched a TikTok browser fallback execution.",
            "details": {"dispatch_payload": {"tiktok_product_browser_fetch": enqueue_payload}, "product_identity": identity},
        }

    if _any_browser_executions_active(stage_executions):
        return {"action": "waiting", "current_stage": stage_code, "message": "TikTok browser fallback is still running."}

    browser_execution = stage_executions[-1]
    if str(browser_execution.status) == "failed":
        return {
            "action": "finalize",
            "final_status": "failed",
            "result": {"status": "failed", "message": "TikTok browser fallback failed."},
            "summary": {"total": 1, "counts": {"browser_fallback_failed": 1}},
            "details": {"browser_execution": browser_execution.to_dict()},
        }
    return {
        "action": "advance",
        "next_stage": "sync_media",
        "details": {"browser_execution": browser_execution.to_dict(), "product_identity": identity},
    }


def _advance_ingest_sync_media(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
        browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
        tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
        fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
        identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job)
        asset_refs = _collect_asset_refs(
            tiktok_result=_job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
            fastmoss_result=_job_effective_result(fastmoss_job),
        )
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="media_asset_sync",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:media:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "asset_refs": asset_refs,
                        "entity_keys": [identity["product_id"]] if identity["product_id"] else [identity["business_key"]],
                        "product_identity": identity,
                        "tiktok_result": _job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
                        "fastmoss_result": _job_effective_result(fastmoss_job),
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the media sync stage.",
            "details": {"dispatch_payload": {"media_asset_sync": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Media sync is still running."}
    return {"action": "advance", "next_stage": "persist_facts", "details": {"media_asset_sync": stage_jobs[-1]}}


def _advance_ingest_persist_facts(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        collect_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code="collect_product_data")
        browser_execs = _browser_executions_for_stage(store, request_id=request.request_id, stage_code="browser_fallback")
        media_job = _latest_api_job_by_code(
            _api_jobs_for_stage(store, request_id=request.request_id, stage_code="sync_media"),
            "media_asset_sync",
        )
        tiktok_source = _effective_tiktok_result(collect_jobs=collect_jobs, browser_execs=browser_execs)
        fastmoss_job = _latest_api_job_by_code(collect_jobs, "fastmoss_product_fetch")
        identity = _resolve_product_identity(request_payload, tiktok_source, fastmoss_job)
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="fact_bundle_upsert",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:facts:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "product_identity": identity,
                        "tiktok_result": _job_effective_result(tiktok_source) if isinstance(tiktok_source, dict) else {},
                        "fastmoss_result": _job_effective_result(fastmoss_job),
                        "media_sync_result": _job_effective_result(media_job),
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the fact persistence stage.",
            "details": {"dispatch_payload": {"fact_bundle_upsert": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Fact persistence is still running."}

    if _selection_mode_enabled(request_payload) and bool(request_payload.get("writeback_enabled", True)):
        return {"action": "advance", "next_stage": "writeback_selection_rows", "details": {"fact_bundle_upsert": stage_jobs[-1]}}
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"fact_bundle_upsert": stage_jobs[-1]}}


def _advance_ingest_writeback_selection_rows(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    request_payload = dict(request.payload or {})
    if not (_selection_mode_enabled(request_payload) and bool(request_payload.get("writeback_enabled", True))):
        return {"action": "advance", "next_stage": "ready_for_summary"}

    stage_jobs = _api_jobs_for_stage(store, request_id=request.request_id, stage_code=stage_code)
    if not stage_jobs:
        fact_job = _latest_api_job_by_code(
            _api_jobs_for_stage(store, request_id=request.request_id, stage_code="persist_facts"),
            "fact_bundle_upsert",
        )
        identity = _resolve_product_identity(request_payload, fact_job)
        enqueue_payload = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code="feishu_table_write",
            jobs=[
                {
                    "business_key": identity["business_key"],
                    "dedupe_key": f"{request.request_id}:{stage_code}:feishu_write:{identity['business_key']}",
                    "max_attempts": 1,
                    "payload": {
                        "request_payload": request_payload,
                        "request_id": request.request_id,
                        "task_code": request.task_code,
                        "workflow_code": request.task_code,
                        "stage_code": stage_code,
                        "target_table_ref": str(request_payload.get("selection_table_ref") or ""),
                        "records": [],
                        "fact_result": _job_effective_result(fact_job),
                        "product_identity": identity,
                    },
                }
            ],
        )
        return {
            "action": "waiting",
            "current_stage": stage_code,
            "message": "Executor dispatched the selection writeback stage.",
            "details": {"dispatch_payload": {"feishu_table_write": enqueue_payload}, "product_identity": identity},
        }

    if _any_api_jobs_active(stage_jobs):
        return {"action": "waiting", "current_stage": stage_code, "message": "Selection writeback is still running."}
    return {"action": "advance", "next_stage": "ready_for_summary", "details": {"feishu_table_write": stage_jobs[-1]}}


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
        if execution.status in ACTIVE_EXECUTION_STATUSES:
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


def _api_jobs_for_stage(store: RuntimeStore, *, request_id: str, stage_code: str) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _browser_executions_for_stage(store: RuntimeStore, *, request_id: str, stage_code: str) -> list[Any]:
    return [
        execution
        for execution in store.list_task_executions(request_id=request_id)
        if str((execution.payload or {}).get("stage_code") or "") == stage_code
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


def _any_browser_executions_active(executions: list[Any]) -> bool:
    return any(execution.status in ACTIVE_EXECUTION_STATUSES for execution in executions)


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


def _execution_effective_result(execution: Any) -> dict[str, Any]:
    if execution is None:
        return {}
    result = dict(execution.result or {})
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


def _resolve_product_identity(*sources: Any) -> dict[str, str]:
    product_url = _first_non_empty(
        *[_lookup_nested(source, "normalized_product_url", "normalized_url", "product_url", "source_url") for source in sources]
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


def _effective_tiktok_result(*, collect_jobs: list[dict[str, Any]], browser_execs: list[Any]) -> Mapping[str, Any]:
    if browser_execs:
        for execution in reversed(browser_execs):
            if execution.status in {"success", "skipped"}:
                return {"result": _execution_effective_result(execution), "source": "browser"}
    return _latest_api_job_by_code(collect_jobs, "tiktok_product_request_fetch")


def _collect_asset_refs(*, tiktok_result: Mapping[str, Any], fastmoss_result: Mapping[str, Any]) -> list[dict[str, Any]]:
    asset_refs: list[dict[str, Any]] = []
    logical_fields = {}
    if isinstance(tiktok_result.get("normalized_product_result"), Mapping):
        logical_fields = dict((tiktok_result.get("normalized_product_result") or {}).get("logical_fields") or {})
    elif isinstance(tiktok_result.get("logical_fields"), Mapping):
        logical_fields = dict(tiktok_result.get("logical_fields") or {})

    product_id = _first_non_empty(
        logical_fields.get("product_id"),
        tiktok_result.get("product_id"),
        fastmoss_result.get("product_id"),
    )
    main_image_url = _first_non_empty(logical_fields.get("main_image_url"))
    main_image_local_path = _first_non_empty(logical_fields.get("main_image_local_path"))
    if main_image_url or main_image_local_path:
        asset_refs.append(
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": "product_main_image",
                "source_url": main_image_url,
                "local_path": main_image_local_path,
                "file_name": _first_non_empty(logical_fields.get("main_image_file_name")),
                "mime_type": _first_non_empty(logical_fields.get("main_image_mime_type")),
            }
        )
    return asset_refs


def _determine_product_ingest_final_status(
    *,
    force_result: Mapping[str, Any] | None,
    fact_job: Mapping[str, Any] | None,
    tiktok_source: Mapping[str, Any] | None,
    counts: Mapping[str, Any],
) -> str:
    if force_result and str(force_result.get("final_status") or "") in {"success", "partial_success", "failed"}:
        return str(force_result["final_status"])
    fact_status = _handler_status_from_api_job(fact_job)
    tiktok_status = _handler_status_from_api_job(tiktok_source) if isinstance(tiktok_source, Mapping) else ""
    if fact_status in {"success", "partial_success"}:
        return "partial_success" if int(counts.get("failed_count") or 0) > 0 else "success"
    if tiktok_status in {"success", "partial_success"}:
        return "partial_success"
    return "failed"


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
