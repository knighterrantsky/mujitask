from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    InvalidASINError,
    normalize_asin,
    normalize_amazon_media_url,
)
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    api_jobs_for_stage,
    browser_executions_for_stage,
    extract_effective_result_payload,
    extract_handler_result,
    extract_handler_result_status,
    render_job_keys,
    timeout_seconds_for_workflow,
)
from automation_business_scaffold.control_plane.executor.request_aggregation import (
    build_runtime_request_payload,
)
from automation_business_scaffold.domains.amazon.mappers.feishu_product_source_mapper import (
    AMAZON_PRODUCT_SOURCE_FIELDS,
)
from automation_business_scaffold.domains.amazon.projections.feishu_product_projection import (
    AMAZON_PRODUCT_FEISHU_WRITE_FIELDS,
)


TASK_CODE = "refresh_amazon_product_row_by_asin"
READ_STAGE_CODE = "read_amazon_product_row"
BROWSER_STAGE_CODE = "collect_amazon_product_detail"
PERSIST_STAGE_CODE = "persist_amazon_product_detail"
SUMMARY_STAGE_CODE = "ready_for_summary"
ACTIVE_STATUSES = {"pending", "running", "waiting"}
PERSISTABLE_COLLECTION_STATUSES = {"success", "partial_success", "unavailable"}
TERMINAL_ROW_STATUSES = {"success", "partial_success", "unavailable"}
READ_ERROR_CODES = {
    "not_found": "source_row_not_found",
    "ambiguous_match": "source_row_ambiguous",
    "invalid_asin": "invalid_asin",
    "unsupported_marketplace": "unsupported_marketplace",
    "identity_mismatch": "identity_mismatch",
}
READ_FAILURE_WRITEBACK_CODES = {
    "identity_mismatch",
    "invalid_asin",
    "unsupported_marketplace",
}
FAILURE_ROW_STATUSES = {"blocked", "failed"}
ROW_STATUS_CODES = (
    "success",
    "partial_success",
    "unavailable",
    "blocked",
    "failed",
    "skipped",
)
OBSERVABLE_ROW_STATUSES = frozenset(ROW_STATUS_CODES)
_BROWSER_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PERSIST_STEP_CODES = (
    "media_asset_sync",
    "amazon_product_fact_upsert",
    "feishu_table_write",
)
_CAPTURE_REF_POLICY = {
    "normalized_capture": ("application/json", "normalized"),
    "html": ("application/gzip", "sanitized"),
    "network_data": ("application/json", "allowlisted"),
    "screenshot": ("image/png", "not_applicable"),
}
_CAPTURE_FILE_NAMES = {
    "normalized_capture": "normalized.json",
    "html": "page.html.gz",
    "network_data": "page-data.json",
    "screenshot": "page.png",
}
_CAPTURE_REF_TEXT_FIELDS = (
    "collected_at",
    "created_at",
)
_HEX_32 = re.compile(r"^[a-f0-9]{32}$")
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_ARTIFACT_OBJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,2047}$")
_SENSITIVE_TEXT = re.compile(
    r"(?:<\s*/?\s*(?:html|body|script)\b|\b(?:bearer|cookie|authorization|"
    r"access[_-]?token|secret[_-]?key|session[_-]?secret)\b)",
    re.IGNORECASE,
)


def advance_stage(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    stage_code: str,
) -> dict[str, Any]:
    if stage_code == READ_STAGE_CODE:
        return _advance_read(store=store, request=request, workflow=workflow)
    if stage_code == BROWSER_STAGE_CODE:
        return _advance_browser(store=store, request=request, workflow=workflow)
    if stage_code == PERSIST_STAGE_CODE:
        return _advance_persist(store=store, request=request, workflow=workflow)
    if stage_code == SUMMARY_STAGE_CODE:
        return {"action": "advance", "next_stage": SUMMARY_STAGE_CODE}
    return _failure(
        stage_code=stage_code,
        error_code="unsupported_amazon_workflow_stage",
        message="Amazon product workflow reached an unsupported stage.",
    )


def release_request_after_child_completion(
    store: Any,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        return []
    if str(request.status or "") in {"finished", "cancelled"}:
        return []
    stage_code = str(request.current_stage or "").strip() or READ_STAGE_CODE
    if stage_code == SUMMARY_STAGE_CODE:
        return []
    children = _children_for_stage(store=store, request_id=request_id, stage_code=stage_code)
    if not children or _has_active(children):
        return []
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=stage_code,
        progress_stage=stage_code,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return [{"request_id": request_id, "stage_code": stage_code, "released": True}]


def finalize_request(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    if _has_active_amazon_children(store=store, request_id=request.request_id):
        store.update_task_request(
            request_id=request.request_id,
            status="waiting",
            current_stage=SUMMARY_STAGE_CODE,
            progress_stage=SUMMARY_STAGE_CODE,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
        )
        return build_runtime_request_payload(
            store=store,
            request_id=request.request_id,
            control_action="executor_once",
            message="Amazon product summary is waiting for active child work.",
        )

    summary, result, final_status, error = _final_payload(
        store=store,
        request=request,
        force_result=force_result,
    )
    finished_at = time.time()
    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
        summary=summary,
        result=result,
        error_text=error["message"] if final_status == "failed" else "",
        error_type=error["error_type"] if final_status == "failed" else "",
        error_code=error["error_code"] if final_status == "failed" else "",
        dead_letter_reason="",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        finished_at=finished_at,
    )
    store.create_notification_outbox(
        channel_code=str(getattr(request, "source_channel_code", "") or "noop"),
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(getattr(request, "reply_target", "") or ""),
        payload={
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": TASK_CODE,
            "summary_payload": summary,
            "result": result,
            "message_text": _outbox_message(summary=summary, result=result),
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    payload = build_runtime_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="executor_once",
        message="Executor finalized the Amazon product row request.",
    )
    payload["request_status"] = updated.result_status or updated.status
    return payload


def _advance_read(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    jobs = _jobs_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if not jobs:
        payload = {
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": workflow.workflow_code,
            "stage_code": READ_STAGE_CODE,
            "source_table_ref": str(request.payload.get("table_ref") or "").strip(),
            "source_record_id": str(request.payload.get("source_record_id") or "").strip(),
            "request_payload": dict(request.payload or {}),
            "adapter_code": "amazon_product_table_source_adapter",
            "field_names": list(AMAZON_PRODUCT_SOURCE_FIELDS),
        }
        job_def = workflow.require_job("feishu_table_read")
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=READ_STAGE_CODE,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow, job_def.job_code
                    ),
                }
            ],
        )
        return _waiting(
            READ_STAGE_CODE,
            "Executor dispatched the Amazon source row read.",
            dispatch=dispatch,
        )
    if _has_active(jobs):
        return _waiting(READ_STAGE_CODE, "Amazon source row read is still running.")
    read_job = jobs[-1]
    if extract_handler_result_status(read_job) not in {"success", "partial_success"}:
        return _failure(
            stage_code=READ_STAGE_CODE,
            error_code=_record_error_code(read_job) or "feishu_table_read_failed",
            message="Amazon source row read failed.",
        )
    try:
        _read_context(store=store, request=request)
    except ValueError as exc:
        code = str(exc) or "invalid_amazon_source_row"
        if code in READ_FAILURE_WRITEBACK_CODES:
            return _terminal_failure_with_writeback(
                store=store,
                request=request,
                workflow=workflow,
                stage_code=READ_STAGE_CODE,
                row_status="failed",
                error_code=code,
                message="Amazon source row identity validation failed.",
            )
        return _failure(
            stage_code=READ_STAGE_CODE,
            error_code=code,
            message="Amazon source row identity validation failed.",
        )
    return _advance(BROWSER_STAGE_CODE, reason="amazon_source_row_validated")


def _advance_browser(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    try:
        row = _read_context(store=store, request=request)
    except ValueError as exc:
        return _failure(
            stage_code=BROWSER_STAGE_CODE,
            error_code=str(exc) or "amazon_source_context_missing",
            message="Validated Amazon source context is unavailable.",
        )
    browser_runtime_context = _browser_runtime_context(request)
    if not browser_runtime_context:
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=BROWSER_STAGE_CODE,
            row_status="failed",
            error_code="amazon_browser_resource_context_missing",
            message="Amazon browser resource context is unavailable.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
        )
    stage_status = _ensure_stage_status_writeback(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=BROWSER_STAGE_CODE,
        row=row,
        row_status="collecting",
    )
    if stage_status is not None:
        return stage_status
    executions = browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=BROWSER_STAGE_CODE,
    )
    if not executions:
        job_def = workflow.require_job("amazon_product_browser_fetch")
        payload = {
            "workflow_code": workflow.workflow_code,
            "stage_code": BROWSER_STAGE_CODE,
            "source_record_id": row["source_record_id"],
            "requested_asin": row["requested_asin"],
            "run_id": _stable_run_id(
                request_id=request.request_id,
                source_record_id=row["source_record_id"],
                requested_asin=row["requested_asin"],
            ),
            "artifact_bucket": browser_runtime_context["artifact_bucket"],
            "artifact_object_prefix": browser_runtime_context["artifact_object_prefix"],
        }
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=BROWSER_STAGE_CODE,
            item_code=job_def.job_code,
        )
        dispatch = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code=job_def.job_code,
            workflow_code=workflow.workflow_code,
            items=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "resource_code": browser_runtime_context["browser_resource_code"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow, job_def.job_code
                    ),
                }
            ],
        )
        return _waiting(
            BROWSER_STAGE_CODE,
            "Executor dispatched primary Amazon browser collection.",
            dispatch=dispatch,
        )
    if _has_active(executions):
        return _waiting(BROWSER_STAGE_CODE, "Amazon browser collection is still running.")
    execution = executions[-1]
    handler_status = extract_handler_result_status(execution)
    browser_result = extract_effective_result_payload(execution)
    try:
        capture_context = _browser_capture_context(
            request=request,
            row=row,
            execution=execution,
        )
    except ValueError:
        capture_context = {}
    if handler_status not in {"success", "partial_success"}:
        collection_status = str(browser_result.get("collection_status") or "failed")
        error_code = _record_error_code(execution) or "amazon_browser_collection_failed"
        actual_target_digest = str(browser_result.get("browser_target_digest") or "")
        if (
            actual_target_digest
            and actual_target_digest != browser_runtime_context["browser_target_digest"]
        ):
            collection_status = "failed"
            error_code = "browser_target_identity_mismatch"
        raw_evidence_refs = _mapping_list(browser_result.get("artifact_refs"))
        evidence_refs = (
            _compact_capture_refs(
                raw_evidence_refs,
                capture_context=capture_context,
            )
            if capture_context
            else []
        )
        if not capture_context or len(evidence_refs) != len(raw_evidence_refs):
            collection_status = "failed"
            error_code = "invalid_amazon_capture_provenance"
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=BROWSER_STAGE_CODE,
            row_status="blocked" if collection_status == "blocked" else "failed",
            error_code=error_code,
            message="Amazon browser collection failed.",
            requested_asin=row["requested_asin"],
            collection_status=collection_status,
            evidence_refs=evidence_refs,
            observability=_browser_failure_observability(
                browser_result,
                final_status=("blocked" if collection_status == "blocked" else "failed"),
                error_code=error_code,
            ),
        )
    try:
        _validate_browser_result(
            row=row,
            result=browser_result,
            expected_browser_target_digest=browser_runtime_context["browser_target_digest"],
            capture_context=capture_context,
        )
    except ValueError as exc:
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=BROWSER_STAGE_CODE,
            row_status="failed",
            error_code=str(exc) or "invalid_amazon_browser_result",
            message="Amazon browser result failed identity or reference validation.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
            evidence_refs=(
                _compact_capture_refs(
                    browser_result.get("artifact_refs"),
                    capture_context=capture_context,
                )
                if capture_context
                else []
            ),
            observability=_browser_failure_observability(
                browser_result,
                final_status="failed",
                error_code=str(exc) or "invalid_amazon_browser_result",
            ),
        )
    return _advance(PERSIST_STAGE_CODE, reason="amazon_capture_ready")


def _advance_persist(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    try:
        row = _read_context(store=store, request=request)
    except ValueError as exc:
        return _failure(
            stage_code=PERSIST_STAGE_CODE,
            error_code=str(exc) or "amazon_source_context_missing",
            message="Validated Amazon source context is unavailable for persistence.",
        )
    try:
        browser_runtime_context = _browser_runtime_context(request)
        if not browser_runtime_context:
            raise ValueError("amazon_browser_resource_context_missing")
        browser_result, capture_context = _browser_result(
            store=store,
            request=request,
            row=row,
            expected_browser_target_digest=browser_runtime_context["browser_target_digest"],
        )
    except ValueError as exc:
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=PERSIST_STAGE_CODE,
            row_status="failed",
            error_code=str(exc) or "amazon_capture_context_missing",
            message="Persistable Amazon capture context is unavailable.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
        )
    browser_evidence_refs = _compact_capture_refs(
        browser_result.get("artifact_refs"),
        capture_context=capture_context,
    )
    stage_status = _ensure_stage_status_writeback(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=PERSIST_STAGE_CODE,
        row=row,
        row_status="persisting",
    )
    if stage_status is not None:
        return stage_status
    jobs = _jobs_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_STAGE_CODE,
        job_code="amazon_product_row_persist",
    )
    if not jobs:
        job_def = workflow.require_job("amazon_product_row_persist")
        payload = {
            "workflow_code": workflow.workflow_code,
            "stage_code": PERSIST_STAGE_CODE,
            "table_ref": str(request.payload.get("table_ref") or "").strip(),
            "source_record_id": row["source_record_id"],
            "source_table_identity": dict(row["source_table_identity"]),
            "requested_asin": row["requested_asin"],
            "resolved_asin": str(browser_result.get("resolved_asin") or ""),
            "run_id": _stable_run_id(
                request_id=request.request_id,
                source_record_id=row["source_record_id"],
                requested_asin=row["requested_asin"],
            ),
            "collection_status": str(browser_result["collection_status"]),
            "normalized_capture_ref": _compact_capture_ref(
                browser_result["normalized_capture_ref"],
                expected_kind="normalized_capture",
                capture_context=capture_context,
            ),
            "raw_capture_refs": _compact_capture_refs(
                browser_result["raw_capture_refs"],
                capture_context=capture_context,
            ),
            "media_source_refs": _compact_media_source_refs(
                browser_result.get("media_source_refs"),
                product_id=row["requested_asin"],
            ),
            "field_coverage": _mapping(browser_result.get("field_coverage")),
            "browser_provider_name": str(browser_result.get("browser_provider_name") or "").strip(),
            "stage_durations_ms": _compact_stage_durations(
                browser_result.get("stage_durations_ms"),
                allowed_stages=("navigation", "parse", "artifact"),
            ),
        }
        job_def = workflow.require_job("amazon_product_row_persist")
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=PERSIST_STAGE_CODE,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow, job_def.job_code
                    ),
                }
            ],
        )
        return _waiting(
            PERSIST_STAGE_CODE,
            "Executor dispatched Amazon media, fact, and Feishu convergence.",
            dispatch=dispatch,
        )
    if _has_active(jobs):
        return _waiting(PERSIST_STAGE_CODE, "Amazon row persistence is still running.")
    persist_job = jobs[-1]
    if extract_handler_result_status(persist_job) not in {"success", "partial_success"}:
        error_code = _record_error_code(persist_job) or "amazon_product_row_persist_failed"
        persist_result = extract_effective_result_payload(persist_job)
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=PERSIST_STAGE_CODE,
            row_status="failed",
            error_code=error_code,
            message="Amazon row persistence failed.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
            evidence_refs=browser_evidence_refs,
            observability=_mapping(persist_result.get("observability")),
            step_statuses=_mapping(persist_result.get("step_statuses")),
        )
    persist_result = extract_effective_result_payload(persist_job)
    expected_run_id = _stable_run_id(
        request_id=request.request_id,
        source_record_id=row["source_record_id"],
        requested_asin=row["requested_asin"],
    )
    expected_resolved_asin = _safe_asin(browser_result.get("resolved_asin"))
    persisted_resolved_asin = _safe_asin(persist_result.get("resolved_asin"))
    if (
        str(persist_result.get("source_record_id") or "") != row["source_record_id"]
        or str(persist_result.get("requested_asin") or "") != row["requested_asin"]
        or str(persist_result.get("run_id") or "") != expected_run_id
        or not expected_resolved_asin
        or persisted_resolved_asin != expected_resolved_asin
    ):
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=PERSIST_STAGE_CODE,
            row_status="failed",
            error_code="amazon_persist_result_identity_mismatch",
            message="Amazon row persistence result does not match the requested source identity.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
            evidence_refs=browser_evidence_refs,
            observability=_mapping(persist_result.get("observability")),
            step_statuses=_mapping(persist_result.get("step_statuses")),
        )
    if str(persist_result.get("row_status") or "") not in TERMINAL_ROW_STATUSES:
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=PERSIST_STAGE_CODE,
            row_status="failed",
            error_code="invalid_amazon_row_status",
            message="Amazon row persistence returned an invalid terminal status.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
            evidence_refs=browser_evidence_refs,
            observability=_mapping(persist_result.get("observability")),
            step_statuses=_mapping(persist_result.get("step_statuses")),
        )
    if not _persist_writeback_converged(
        persist_result.get("writeback"),
        source_record_id=row["source_record_id"],
    ):
        return _terminal_failure_with_writeback(
            store=store,
            request=request,
            workflow=workflow,
            stage_code=PERSIST_STAGE_CODE,
            row_status="failed",
            error_code="amazon_persist_writeback_not_converged",
            message="Amazon row persistence did not update exactly the source record.",
            requested_asin=row["requested_asin"],
            collection_status="failed",
            evidence_refs=browser_evidence_refs,
            observability=_mapping(persist_result.get("observability")),
            step_statuses={
                **_mapping(persist_result.get("step_statuses")),
                "feishu_table_write": "failed",
            },
        )
    return _advance(SUMMARY_STAGE_CODE, reason="amazon_row_persisted")


def _ensure_stage_status_writeback(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    stage_code: str,
    row: Mapping[str, Any],
    row_status: str,
) -> dict[str, Any] | None:
    if row_status not in {"collecting", "persisting"}:
        raise ValueError("Amazon stage status must be collecting or persisting.")
    writeback_kind = "amazon_stage_status"
    jobs = [
        job
        for job in _jobs_for_stage(
            store=store,
            request_id=request.request_id,
            stage_code=stage_code,
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == writeback_kind
        and str((job.get("payload") or {}).get("row_status") or "") == row_status
    ]
    if not jobs:
        source_table_identity = _mapping(row.get("source_table_identity"))
        payload = {
            "request_id": request.request_id,
            "workflow_code": workflow.workflow_code,
            "stage_code": stage_code,
            "target_table_ref": str(request.payload.get("table_ref") or "").strip(),
            "source_record_id": str(row.get("source_record_id") or "").strip(),
            "row_status": row_status,
            "error_code": "",
            "feishu_table": {
                "app_token": str(source_table_identity.get("base_id") or "").strip(),
                "table_id": str(source_table_identity.get("table_id") or "").strip(),
            },
            "records": [
                {
                    "source_record_id": str(row.get("source_record_id") or "").strip(),
                    "requested_asin": str(row.get("requested_asin") or "").strip(),
                    "collection_status": row_status,
                }
            ],
            "mapper_code": "amazon_product_projection_mapper",
            "write_mode": "update_existing",
            "write_policy": {
                "ignore_missing_fields": True,
                "field_allowlist": list(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS),
            },
            "writeback_kind": writeback_kind,
        }
        job_def = workflow.require_job("feishu_table_write")
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow, job_def.job_code
                    ),
                }
            ],
        )
        return _waiting(
            stage_code,
            f"Executor dispatched the Amazon {row_status} status writeback.",
            dispatch=dispatch,
        )
    if _has_active(jobs):
        return _waiting(
            stage_code,
            f"Amazon {row_status} status writeback is still running.",
        )

    write_job = jobs[-1]
    write_result = extract_effective_result_payload(write_job)
    source_record_id = str(row.get("source_record_id") or "").strip()
    if not _status_writeback_converged(
        handler_status=extract_handler_result_status(write_job),
        value=write_result,
        source_record_id=source_record_id,
    ):
        error_code = _record_error_code(write_job) or f"amazon_{row_status}_status_writeback_failed"
        observability = _with_feishu_writeback_duration({}, write_job)
        return _failure(
            stage_code=stage_code,
            error_code=error_code,
            message=f"Amazon {row_status} status could not be written to the source row.",
            result={
                "source_record_id": source_record_id,
                "requested_asin": str(row.get("requested_asin") or "").strip(),
                "row_status": "failed",
                "collection_status": "failed",
                "writeback": _compact_status_writeback(write_result),
                **({"observability": observability} if observability else {}),
            },
        )
    return None


def _terminal_failure_with_writeback(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    stage_code: str,
    row_status: str,
    error_code: str,
    message: str,
    requested_asin: str = "",
    collection_status: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    observability: Mapping[str, Any] | None = None,
    step_statuses: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error_code = _safe_error_code(error_code) or "amazon_product_workflow_failed"
    failure_result = {
        "source_record_id": str(request.payload.get("source_record_id") or "").strip(),
        "row_status": row_status if row_status in FAILURE_ROW_STATUSES else "failed",
        "requested_asin": requested_asin,
        "collection_status": collection_status or row_status,
        "evidence_refs": list(evidence_refs or []),
    }
    compact_observability = _compact_observability(
        observability,
        final_status=failure_result["row_status"],
        error_code=error_code,
    )
    if compact_observability:
        failure_result["observability"] = compact_observability
    compact_step_statuses = _compact_step_statuses(step_statuses)
    if compact_step_statuses:
        failure_result["step_statuses"] = compact_step_statuses
    source_table_identity = _source_table_identity(store=store, request_id=request.request_id)
    if not source_table_identity:
        return _failure(
            stage_code=stage_code,
            error_code=error_code,
            message=message,
            result=failure_result,
        )

    jobs = [
        job
        for job in _jobs_for_stage(
            store=store,
            request_id=request.request_id,
            stage_code=stage_code,
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == "amazon_terminal_status"
    ]
    if not jobs:
        source_record_id = failure_result["source_record_id"]
        target_table_ref = str(request.payload.get("table_ref") or "").strip()
        record = {
            "source_record_id": source_record_id,
            "requested_asin": requested_asin,
            "collection_status": failure_result["row_status"],
            "collected_at": _utc_timestamp(),
            "error_code": error_code,
            "error_message": message,
        }
        payload = {
            "request_id": request.request_id,
            "workflow_code": workflow.workflow_code,
            "stage_code": stage_code,
            "target_table_ref": target_table_ref,
            "source_record_id": source_record_id,
            "row_status": failure_result["row_status"],
            "error_code": error_code,
            "feishu_table": {
                "app_token": source_table_identity["base_id"],
                "table_id": source_table_identity["table_id"],
            },
            "records": [record],
            "mapper_code": "amazon_product_projection_mapper",
            "write_mode": "update_existing",
            "write_policy": {
                "ignore_missing_fields": True,
                "field_allowlist": list(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS),
            },
            "writeback_kind": "amazon_terminal_status",
        }
        job_def = workflow.require_job("feishu_table_write")
        keys = render_job_keys(
            job_def,
            payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
        )
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                    "max_attempts": 3,
                    "max_execution_seconds": timeout_seconds_for_workflow(
                        workflow, job_def.job_code
                    ),
                }
            ],
        )
        return _waiting(
            stage_code,
            "Executor dispatched the terminal Amazon status writeback.",
            dispatch=dispatch,
        )
    if _has_active(jobs):
        return _waiting(stage_code, "Amazon terminal status writeback is still running.")

    write_job = jobs[-1]
    write_status = extract_handler_result_status(write_job)
    write_result = extract_effective_result_payload(write_job)
    writeback_observability = _with_feishu_writeback_duration(
        failure_result.get("observability"),
        write_job,
    )
    if writeback_observability:
        failure_result["observability"] = writeback_observability
    if not _status_writeback_converged(
        handler_status=write_status,
        value=write_result,
        source_record_id=failure_result["source_record_id"],
    ):
        writeback_error = _record_error_code(write_job) or "amazon_terminal_status_writeback_failed"
        return _failure(
            stage_code=stage_code,
            error_code=writeback_error,
            message="Amazon terminal status could not be written to the source row.",
            result={
                **failure_result,
                "original_error_code": error_code,
                "writeback": _compact_status_writeback(write_result),
            },
        )
    return _failure(
        stage_code=stage_code,
        error_code=error_code,
        message=message,
        result={
            **failure_result,
            "writeback": _compact_status_writeback(write_result),
        },
    )


def _source_table_identity(*, store: Any, request_id: str) -> dict[str, str]:
    jobs = _jobs_for_stage(
        store=store,
        request_id=request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if not jobs:
        return {}
    identity = _mapping(extract_effective_result_payload(jobs[-1]).get("source_table_identity"))
    result = {
        "base_id": str(identity.get("base_id") or "").strip(),
        "table_id": str(identity.get("table_id") or "").strip(),
    }
    return result if all(result.values()) else {}


def _compact_status_writeback(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "written_count": _nonnegative_int(value.get("written_count")),
        "skipped_count": _nonnegative_int(value.get("skipped_count")),
        "failed_count": _nonnegative_int(value.get("failed_count")),
        "target_record_ids": _compact_string_list(
            value.get("target_record_ids"),
            max_length=256,
        ),
    }


def _status_writeback_converged(
    *,
    handler_status: str,
    value: Mapping[str, Any],
    source_record_id: str,
) -> bool:
    if handler_status == "success":
        return _persist_writeback_converged(
            value,
            source_record_id=source_record_id,
        )
    if handler_status != "skipped":
        return False
    count_fields = ("written_count", "skipped_count", "failed_count")
    if any(type(value.get(field)) is not int for field in count_fields):
        return False
    records = _mapping_list(value.get("records"))
    return (
        value.get("written_count") == 0
        and value.get("skipped_count") == 1
        and value.get("failed_count") == 0
        and value.get("target_record_ids") == []
        and len(records) == 1
        and str(records[0].get("record_id") or "").strip() == source_record_id
        and str(records[0].get("status") or "").strip() == "skipped"
        and str(records[0].get("message") or "").strip() == "empty_fields"
    )


def _persist_writeback_converged(value: Any, *, source_record_id: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    count_fields = ("written_count", "skipped_count", "failed_count")
    if any(type(value.get(field)) is not int for field in count_fields):
        return False
    target_record_ids = value.get("target_record_ids")
    return (
        value.get("written_count") == 1
        and value.get("skipped_count") == 0
        and value.get("failed_count") == 0
        and isinstance(target_record_ids, list)
        and target_record_ids == [source_record_id]
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_context(*, store: Any, request: Any) -> dict[str, Any]:
    jobs = _jobs_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if not jobs:
        raise ValueError("amazon_source_context_missing")
    result = extract_effective_result_payload(jobs[-1])
    adapter_summary = _mapping(result.get("adapter_summary"))
    lookup_status = str(adapter_summary.get("lookup_status") or "")
    if lookup_status != "matched":
        raise ValueError(READ_ERROR_CODES.get(lookup_status, "invalid_amazon_source_row"))
    source_rows = _mapping_list(result.get("source_rows"))
    if len(source_rows) != 1:
        raise ValueError("source_row_ambiguous")
    source_row = source_rows[0]
    source_record_id = str(source_row.get("source_record_id") or "").strip()
    expected_record_id = str(request.payload.get("source_record_id") or "").strip()
    if source_record_id != expected_record_id:
        raise ValueError("source_record_identity_mismatch")
    try:
        requested_asin = normalize_asin(source_row.get("requested_asin"))
    except InvalidASINError as exc:
        raise ValueError("invalid_asin") from exc
    identity = _mapping(result.get("source_table_identity"))
    source_table_identity = {
        "base_id": str(identity.get("base_id") or "").strip(),
        "table_id": str(identity.get("table_id") or "").strip(),
    }
    if not all(source_table_identity.values()):
        raise ValueError("source_table_identity_missing")
    return {
        "source_record_id": source_record_id,
        "requested_asin": requested_asin,
        "canonical_url": str(source_row.get("canonical_url") or "").strip(),
        "business_key": str(source_row.get("business_key") or "").strip(),
        "source_table_identity": source_table_identity,
    }


def _browser_result(
    *,
    store: Any,
    request: Any,
    row: Mapping[str, Any],
    expected_browser_target_digest: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    executions = browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=BROWSER_STAGE_CODE,
    )
    if not executions:
        raise ValueError("amazon_capture_context_missing")
    execution = executions[-1]
    result = extract_effective_result_payload(execution)
    capture_context = _browser_capture_context(
        request=request,
        row=row,
        execution=execution,
    )
    _validate_browser_result(
        row=row,
        result=result,
        expected_browser_target_digest=expected_browser_target_digest,
        capture_context=capture_context,
    )
    return result, capture_context


def _validate_browser_result(
    *,
    row: Mapping[str, Any],
    result: Mapping[str, Any],
    expected_browser_target_digest: str,
    capture_context: Mapping[str, str],
) -> None:
    if str(result.get("marketplace_code") or "") != "US":
        raise ValueError("unsupported_marketplace")
    if str(result.get("requested_asin") or "") != row["requested_asin"]:
        raise ValueError("identity_mismatch")
    if str(result.get("browser_target_digest") or "") != expected_browser_target_digest:
        raise ValueError("browser_target_identity_mismatch")
    try:
        resolved_asin = normalize_asin(result.get("resolved_asin"))
        parent_value = str(result.get("parent_asin") or "").strip()
        parent_asin = normalize_asin(parent_value) if parent_value else ""
    except InvalidASINError as exc:
        raise ValueError("invalid_amazon_browser_result") from exc
    collection_status = str(result.get("collection_status") or "")
    parent_redirect = (
        resolved_asin != row["requested_asin"]
        and parent_asin == row["requested_asin"]
        and collection_status == "partial_success"
    )
    if resolved_asin != row["requested_asin"] and not parent_redirect:
        raise ValueError("identity_mismatch")
    if collection_status not in PERSISTABLE_COLLECTION_STATUSES:
        raise ValueError("invalid_amazon_collection_status")
    normalized_capture_ref = _compact_capture_ref(
        result.get("normalized_capture_ref"),
        expected_kind="normalized_capture",
        capture_context=capture_context,
    )
    raw_capture_inputs = _mapping_list(result.get("raw_capture_refs"))
    raw_capture_refs = _compact_capture_refs(
        result.get("raw_capture_refs"),
        capture_context=capture_context,
    )
    if not normalized_capture_ref:
        raise ValueError("normalized_capture_ref_missing")
    if not raw_capture_refs:
        raise ValueError("raw_capture_refs_missing")
    if len(raw_capture_refs) != len(raw_capture_inputs):
        raise ValueError("invalid_amazon_capture_ref")
    if normalized_capture_ref not in raw_capture_refs:
        raise ValueError("normalized_capture_ref_mismatch")
    if len(raw_capture_refs) > len(_CAPTURE_REF_POLICY):
        raise ValueError("invalid_amazon_capture_ref")
    if len({ref["capture_kind"] for ref in raw_capture_refs}) != len(raw_capture_refs):
        raise ValueError("duplicate_amazon_capture_kind")
    coordinates = {(ref["bucket"], ref["object_key"]) for ref in raw_capture_refs}
    if len(coordinates) != len(raw_capture_refs):
        raise ValueError("duplicate_amazon_capture_coordinate")
    artifact_inputs = _mapping_list(result.get("artifact_refs"))
    artifact_refs = _compact_capture_refs(
        result.get("artifact_refs"),
        capture_context=capture_context,
    )
    if (
        len(artifact_refs) != len(artifact_inputs)
        or artifact_refs != raw_capture_refs
    ):
        raise ValueError("artifact_capture_ref_mismatch")
    media_inputs = _mapping_list(result.get("media_source_refs"))
    media_source_refs = _compact_media_source_refs(
        result.get("media_source_refs"),
        product_id=row["requested_asin"],
    )
    if len(media_source_refs) != len(media_inputs):
        raise ValueError("invalid_amazon_media_source_ref")
    if "capture" in result or "html" in result:
        raise ValueError("inline_amazon_capture_forbidden")


def _browser_capture_context(
    *,
    request: Any,
    row: Mapping[str, Any],
    execution: Any,
) -> dict[str, str]:
    request_id = _compact_hex_identifier(getattr(request, "request_id", ""), length=32)
    execution_id = _compact_hex_identifier(
        getattr(execution, "execution_id", ""),
        length=32,
    )
    requested_asin = _safe_asin(row.get("requested_asin"))
    source_record_id = _compact_text(row.get("source_record_id"), max_length=256)
    stable_run_id = _stable_run_id(
        request_id=request_id,
        source_record_id=source_record_id,
        requested_asin=requested_asin,
    )
    execution_payload = _mapping(getattr(execution, "payload", {}))
    runtime_context = _browser_runtime_context(request)
    if (
        not request_id
        or not execution_id
        or not requested_asin
        or not source_record_id
        or getattr(execution, "request_id", "") != request_id
        or execution_payload.get("source_record_id") != source_record_id
        or execution_payload.get("requested_asin") != requested_asin
        or execution_payload.get("run_id") != stable_run_id
        or not runtime_context
    ):
        raise ValueError("invalid_amazon_capture_provenance")
    return {
        "request_id": request_id,
        "execution_id": execution_id,
        "run_id": stable_run_id,
        "requested_asin": requested_asin,
        "artifact_bucket": runtime_context["artifact_bucket"],
        "artifact_object_prefix": runtime_context["artifact_object_prefix"],
    }


def _final_capture_context(*, store: Any, request: Any) -> dict[str, str]:
    if store is None or not getattr(request, "request_id", ""):
        return {}
    try:
        row = _read_context(store=store, request=request)
        executions = browser_executions_for_stage(
            store,
            request_id=request.request_id,
            stage_code=BROWSER_STAGE_CODE,
        )
        if not executions:
            return {}
        return _browser_capture_context(
            request=request,
            row=row,
            execution=executions[-1],
        )
    except (AttributeError, TypeError, ValueError):
        return {}


def _browser_runtime_context(request: Any) -> dict[str, str]:
    stage_cursor = _mapping(getattr(request, "stage_cursor", {}))
    runtime_context = _mapping(stage_cursor.get("runtime_context"))
    browser_target_digest = str(runtime_context.get("browser_target_digest") or "").strip()
    browser_resource_code = str(runtime_context.get("browser_resource_code") or "").strip()
    artifact_bucket = _compact_text(runtime_context.get("artifact_bucket"), max_length=63)
    raw_object_prefix = _compact_text(
        runtime_context.get("artifact_object_prefix"),
        max_length=1024,
    )
    artifact_object_prefix = _compact_artifact_object_prefix(raw_object_prefix)
    if (
        not browser_target_digest
        or browser_resource_code != f"browser:amazon:{browser_target_digest}"
        or "artifact_bucket" not in runtime_context
        or "artifact_object_prefix" not in runtime_context
        or not _ARTIFACT_BUCKET.fullmatch(artifact_bucket)
        or _SENSITIVE_TEXT.search(artifact_bucket)
        or raw_object_prefix != artifact_object_prefix
    ):
        return {}
    return {
        "browser_target_digest": browser_target_digest,
        "browser_resource_code": browser_resource_code,
        "artifact_bucket": artifact_bucket,
        "artifact_object_prefix": artifact_object_prefix,
    }


def _final_payload(
    *,
    store: Any,
    request: Any,
    force_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, str]]:
    capture_context = _final_capture_context(store=store, request=request)
    if force_result is not None:
        forced_result = _mapping(force_result.get("result"))
        error_code = (
            _safe_error_code(force_result.get("error_code") or forced_result.get("error_code"))
            or "amazon_product_workflow_failed"
        )
        row_status = str(forced_result.get("row_status") or "failed")
        if row_status not in FAILURE_ROW_STATUSES:
            row_status = "failed"
        row_result = {
            "source_record_id": str(request.payload.get("source_record_id") or ""),
            "row_status": row_status,
            "error_code": error_code,
            **_compact_failure_result(
                forced_result,
                source_record_id=str(request.payload.get("source_record_id") or ""),
                capture_context=capture_context,
            ),
        }
        summary = _top_level_summary(
            final_status="failed",
            row_status=row_status,
            row_result=row_result,
            failed_stage=str(force_result.get("failed_stage") or ""),
            error_code=error_code,
        )
        result = {
            "workflow_code": TASK_CODE,
            "row_total_count": 1,
            "row_results": [row_result],
            "error_code": error_code,
        }
        return (
            summary,
            result,
            "failed",
            {
                "error_type": "business_workflow_failure",
                "error_code": error_code,
                "message": "Amazon product row workflow failed.",
            },
        )

    jobs = _jobs_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_STAGE_CODE,
        job_code="amazon_product_row_persist",
    )
    persist_result = extract_effective_result_payload(jobs[-1]) if jobs else {}
    row_result = _compact_persist_result(
        persist_result,
        capture_context=capture_context,
    )
    row_status = str(row_result.get("row_status") or "failed")
    final_status = (
        "partial_success"
        if row_status == "partial_success"
        else "success"
        if row_status in {"success", "unavailable"}
        else "failed"
    )
    error_code = "amazon_product_row_persist_failed" if final_status == "failed" else ""
    summary = _top_level_summary(
        final_status=final_status,
        row_status=row_status,
        row_result=row_result,
        error_code=error_code,
    )
    result = {
        "workflow_code": TASK_CODE,
        "row_total_count": 1,
        "row_results": [row_result],
    }
    return (
        summary,
        result,
        final_status,
        {
            "error_type": "persistence_failure" if final_status == "failed" else "",
            "error_code": "amazon_product_row_persist_failed" if final_status == "failed" else "",
            "message": "Amazon product row persistence failed." if final_status == "failed" else "",
        },
    )


def _compact_persist_result(
    value: Mapping[str, Any],
    *,
    capture_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    row_status = str(value.get("row_status") or "")
    result: dict[str, Any] = {
        "row_status": row_status if row_status in TERMINAL_ROW_STATUSES else "failed"
    }
    source_record_id = _compact_text(value.get("source_record_id"), max_length=256)
    requested_asin = _safe_asin(value.get("requested_asin"))
    resolved_asin = _safe_asin(value.get("resolved_asin"))
    run_id = _compact_hex_identifier(value.get("run_id"), length=64)
    if source_record_id:
        result["source_record_id"] = source_record_id
    if requested_asin:
        result["requested_asin"] = requested_asin
    if resolved_asin:
        result["resolved_asin"] = resolved_asin
    if run_id:
        result["run_id"] = run_id
    fact_refs = _compact_fact_refs(
        value.get("fact_refs"),
        capture_context=capture_context,
    )
    if fact_refs:
        result["fact_refs"] = fact_refs
    media_coverage = _compact_media_coverage(value.get("media_coverage"))
    if media_coverage:
        result["media_coverage"] = media_coverage
    writeback = _compact_writeback(
        value.get("writeback"),
        source_record_id=source_record_id,
    )
    if writeback:
        result["writeback"] = writeback
    failed_step = _compact_text(value.get("failed_step"), max_length=64)
    if failed_step in _PERSIST_STEP_CODES:
        result["failed_step"] = failed_step
    step_statuses = _compact_step_statuses(value.get("step_statuses"))
    if step_statuses:
        result["step_statuses"] = step_statuses
    observability = _compact_observability(
        value.get("observability"),
        final_status=result["row_status"],
        error_code=str(_mapping(value.get("observability")).get("error_code") or ""),
    )
    if observability:
        result["observability"] = observability
    return result


def _compact_failure_result(
    value: Mapping[str, Any],
    *,
    source_record_id: str = "",
    capture_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    requested_asin = _safe_asin(value.get("requested_asin"))
    resolved_asin = _safe_asin(value.get("resolved_asin"))
    collection_status = _compact_text(value.get("collection_status"), max_length=32)
    if requested_asin:
        result["requested_asin"] = requested_asin
    if resolved_asin:
        result["resolved_asin"] = resolved_asin
    if collection_status in OBSERVABLE_ROW_STATUSES:
        result["collection_status"] = collection_status
    evidence_refs = (
        _compact_capture_refs(
            value.get("evidence_refs"),
            capture_context=capture_context,
        )
        if capture_context
        else []
    )
    if evidence_refs:
        result["evidence_refs"] = evidence_refs
    writeback = _compact_writeback(
        value.get("writeback"),
        source_record_id=_compact_text(source_record_id, max_length=256),
    )
    if writeback:
        result["writeback"] = writeback
    original_error_code = _safe_error_code(value.get("original_error_code"))
    if original_error_code:
        result["original_error_code"] = original_error_code
    step_statuses = _compact_step_statuses(value.get("step_statuses"))
    if step_statuses:
        result["step_statuses"] = step_statuses
    observability = _compact_observability(
        value.get("observability"),
        final_status=str(value.get("row_status") or "failed"),
        error_code=str(
            value.get("error_code") or _mapping(value.get("observability")).get("error_code") or ""
        ),
    )
    if observability:
        result["observability"] = observability
    return result


def _compact_fact_refs(
    value: Any,
    *,
    capture_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    raw = _mapping(value)
    refs: dict[str, Any] = {}
    for field_name in ("product_id", "snapshot_id", "binding_id"):
        identifier = _compact_hex_identifier(raw.get(field_name), length=32)
        if identifier:
            refs[field_name] = identifier
    raw_capture_ids = (
        [
            identifier
            for item in raw.get("raw_capture_ids", [])
            if (identifier := _compact_hex_identifier(item, length=32))
        ]
        if isinstance(raw.get("raw_capture_ids"), list)
        else []
    )
    if raw_capture_ids:
        refs["raw_capture_ids"] = raw_capture_ids
    normalized_capture_ref = _compact_capture_ref(
        raw.get("normalized_capture_ref"),
        expected_kind="normalized_capture",
        capture_context=capture_context,
    )
    if normalized_capture_ref and capture_context:
        refs["normalized_capture_ref"] = normalized_capture_ref
    return refs


def _compact_media_coverage(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    if not raw:
        return {}
    expected = _optional_nonnegative_int(raw.get("expected"))
    materialized = _optional_nonnegative_int(raw.get("materialized"))
    missing = _optional_nonnegative_int(raw.get("missing"))
    coverage: dict[str, Any] = {}
    if expected is not None:
        coverage["expected"] = expected
    if materialized is not None:
        coverage["materialized"] = materialized
    if expected is not None and materialized is not None:
        coverage["missing"] = max(expected - materialized, 0)
        coverage["complete"] = materialized >= expected
    else:
        if missing is not None:
            coverage["missing"] = missing
        if isinstance(raw.get("complete"), bool):
            coverage["complete"] = raw["complete"]
    return coverage


def _compact_writeback(
    value: Any,
    *,
    source_record_id: str,
) -> dict[str, Any]:
    raw = _mapping(value)
    if not raw:
        return {}
    target_record_ids = _compact_string_list(
        raw.get("target_record_ids"),
        max_length=256,
    )
    if source_record_id:
        target_record_ids = [
            record_id for record_id in target_record_ids if record_id == source_record_id
        ]
    return {
        "written_count": _nonnegative_int(raw.get("written_count")),
        "skipped_count": _nonnegative_int(raw.get("skipped_count")),
        "failed_count": _nonnegative_int(raw.get("failed_count")),
        "target_record_ids": target_record_ids,
    }


def _compact_capture_refs(
    value: Any,
    *,
    capture_context: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > len(_CAPTURE_REF_POLICY):
        return []
    refs: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_coordinates: set[tuple[str, str]] = set()
    for item in value:
        ref = _compact_capture_ref(item, capture_context=capture_context)
        if not ref:
            return []
        coordinate = (ref["bucket"], ref["object_key"])
        if ref["capture_kind"] in seen_kinds or coordinate in seen_coordinates:
            return []
        seen_kinds.add(ref["capture_kind"])
        seen_coordinates.add(coordinate)
        refs.append(ref)
    return refs


def _compact_media_source_refs(value: Any, *, product_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    expected_product_id = _safe_asin(product_id)
    refs: list[dict[str, Any]] = []
    seen_coordinates: set[tuple[str, int]] = set()
    for item in value:
        raw = _mapping(item)
        source_url = normalize_amazon_media_url(raw.get("source_url"))
        item_product_id = _safe_asin(raw.get("product_id"))
        media_role = _compact_text(raw.get("media_role"), max_length=32)
        position = _optional_nonnegative_int(raw.get("position"))
        coordinate = (media_role, position) if position is not None else None
        if (
            not source_url
            or _SENSITIVE_TEXT.search(source_url)
            or coordinate in seen_coordinates
            or raw.get("source_platform") != "amazon"
            or raw.get("marketplace_code") != "US"
            or not expected_product_id
            or item_product_id != expected_product_id
            or media_role not in {"main_image", "gallery_image"}
            or position is None
            or (media_role == "main_image" and position != 0)
        ):
            continue
        seen_coordinates.add((media_role, position))
        refs.append(
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": item_product_id,
                "media_role": media_role,
                "position": position,
            }
        )
    return refs


def _compact_capture_ref(
    value: Any,
    *,
    expected_kind: str = "",
    capture_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    raw = _mapping(value)
    capture_kind = _compact_text(raw.get("capture_kind"), max_length=32)
    policy = _CAPTURE_REF_POLICY.get(capture_kind)
    if policy is None or (expected_kind and capture_kind != expected_kind):
        return {}
    content_digest = _compact_text(raw.get("content_digest"), max_length=64).lower()
    bucket = _compact_text(raw.get("bucket"), max_length=63)
    request_id = _compact_hex_identifier(raw.get("request_id"), length=32)
    execution_id = _compact_hex_identifier(raw.get("execution_id"), length=32)
    run_id = _compact_hex_identifier(raw.get("run_id"), length=64)
    context = _mapping(capture_context)
    expected_request_id = _compact_hex_identifier(context.get("request_id"), length=32)
    expected_execution_id = _compact_hex_identifier(
        context.get("execution_id"),
        length=32,
    )
    expected_run_id = _compact_hex_identifier(context.get("run_id"), length=64)
    expected_asin = _safe_asin(context.get("requested_asin"))
    expected_bucket = _compact_text(context.get("artifact_bucket"), max_length=63)
    expected_object_prefix = _compact_artifact_object_prefix(
        context.get("artifact_object_prefix")
    )
    requires_binding = capture_context is not None
    expected_content_type, expected_sanitization_status = policy
    object_key = _compact_capture_object_key(
        raw.get("object_key"),
        capture_kind=capture_kind,
        content_digest=content_digest,
        expected_asin=expected_asin,
        expected_run_id=expected_run_id,
        expected_object_prefix=expected_object_prefix,
        require_exact_prefix=requires_binding,
    )
    if (
        not _HEX_64.fullmatch(content_digest)
        or not _ARTIFACT_BUCKET.fullmatch(bucket)
        or _SENSITIVE_TEXT.search(bucket)
        or not object_key
        or not request_id
        or not execution_id
        or not run_id
        or (
            requires_binding
            and (
                "artifact_object_prefix" not in context
                or not expected_request_id
                or not expected_execution_id
                or not expected_run_id
                or not expected_asin
                or not expected_bucket
                or request_id != expected_request_id
                or execution_id != expected_execution_id
                or run_id != expected_run_id
                or bucket != expected_bucket
            )
        )
        or raw.get("content_type") != expected_content_type
        or raw.get("sanitization_status") != expected_sanitization_status
    ):
        return {}
    ref: dict[str, Any] = {
        "capture_kind": capture_kind,
        "bucket": bucket,
        "object_key": object_key,
        "content_digest": content_digest,
        "content_type": expected_content_type,
        "sanitization_status": expected_sanitization_status,
        "request_id": request_id,
        "execution_id": execution_id,
        "run_id": run_id,
    }
    for field_name in _CAPTURE_REF_TEXT_FIELDS:
        timestamp = _compact_iso_timestamp(raw.get(field_name))
        if timestamp:
            ref[field_name] = timestamp
    if requires_binding:
        collected_at = ref.get("collected_at")
        if not collected_at or not _capture_date_matches_object_key(
            object_key,
            collected_at=collected_at,
            object_prefix=expected_object_prefix,
        ):
            return {}
    return ref


def _browser_failure_observability(
    browser_result: Mapping[str, Any],
    *,
    final_status: str,
    error_code: str,
) -> dict[str, Any]:
    return _compact_observability(
        {
            "browser_provider_name": browser_result.get("browser_provider_name"),
            "stage_durations_ms": browser_result.get("stage_durations_ms"),
            "field_coverage": browser_result.get("field_coverage"),
            "artifact_count": len(_mapping_list(browser_result.get("artifact_refs"))),
            "media_observed_count": len(_mapping_list(browser_result.get("media_source_refs"))),
            "media_materialized_count": 0,
        },
        final_status=final_status,
        error_code=error_code,
    )


def _top_level_summary(
    *,
    final_status: str,
    row_status: str,
    row_result: Mapping[str, Any],
    failed_stage: str = "",
    error_code: str = "",
) -> dict[str, Any]:
    normalized_row_status = row_status if row_status in OBSERVABLE_ROW_STATUSES else "failed"
    normalized_result = {**row_result, "row_status": normalized_row_status}
    safe_error_code = _safe_error_code(error_code)
    row_summary = _row_summary(normalized_result, error_code=safe_error_code)
    return {
        "final_status": (
            final_status if final_status in {"success", "partial_success", "failed"} else "failed"
        ),
        "row_total_count": 1,
        "row_status_counts": {
            status: int(status == normalized_row_status) for status in ROW_STATUS_CODES
        },
        "aggregate_metrics": _aggregate_metrics(
            normalized_result,
            row_summary=row_summary,
            row_status=normalized_row_status,
        ),
        "row_summary": row_summary,
        "failed_stage": _safe_stage_code(failed_stage),
        "error_code": safe_error_code,
    }


def _aggregate_metrics(
    row_result: Mapping[str, Any],
    *,
    row_summary: Mapping[str, Any],
    row_status: str,
) -> dict[str, float]:
    durations = _mapping(row_summary.get("stage_durations_ms"))
    row_duration_ms = round(
        sum(float(value) for value in durations.values() if isinstance(value, (int, float))),
        3,
    )
    coverage = _mapping(row_summary.get("field_coverage"))
    coverage_percentage = coverage.get("percentage")
    if isinstance(coverage_percentage, bool) or not isinstance(coverage_percentage, (int, float)):
        coverage_percentage = 0.0
    observed_count = _nonnegative_int(row_summary.get("media_observed_count"))
    materialized_count = _nonnegative_int(row_summary.get("media_materialized_count"))
    media_failure_rate = (
        min(max((observed_count - materialized_count) / observed_count, 0.0), 1.0)
        if observed_count
        else 0.0
    )
    return {
        "average_row_duration_ms": row_duration_ms,
        "max_row_duration_ms": row_duration_ms,
        "blocked_rate": 1.0 if row_status == "blocked" else 0.0,
        "average_parse_coverage_percentage": round(float(coverage_percentage), 2),
        "media_failure_rate": round(media_failure_rate, 6),
        "feishu_failure_rate": _feishu_failure_rate(row_result),
    }


def _feishu_failure_rate(row_result: Mapping[str, Any]) -> float:
    step_status = _compact_step_statuses(row_result.get("step_statuses")).get("feishu_table_write")
    if step_status and step_status not in {"success", "skipped"}:
        return 1.0

    writeback = _mapping(row_result.get("writeback"))
    count_fields = ("written_count", "skipped_count", "failed_count")
    attempted = bool(step_status and step_status != "skipped") or any(
        field in writeback for field in (*count_fields, "target_record_ids")
    )
    if not attempted:
        return 0.0
    source_record_id = str(row_result.get("source_record_id") or "").strip()
    if (
        any(type(writeback.get(field)) is not int for field in count_fields)
        or not isinstance(writeback.get("target_record_ids"), list)
        or writeback.get("written_count") != 1
        or writeback.get("skipped_count") != 0
        or writeback.get("failed_count") != 0
        or [str(item) for item in writeback.get("target_record_ids", [])] != [source_record_id]
    ):
        return 1.0
    return 0.0


def _compact_step_statuses(value: Any) -> dict[str, str]:
    raw = _mapping(value)
    allowed_statuses = {"success", "partial_success", "failed", "skipped"}
    result: dict[str, str] = {}
    for step_code in _PERSIST_STEP_CODES:
        status = str(raw.get(step_code) or "")
        if status in allowed_statuses:
            result[step_code] = status
    return result


def _row_summary(
    row_result: Mapping[str, Any],
    *,
    error_code: str,
) -> dict[str, Any]:
    final_status = str(row_result.get("row_status") or "failed")
    observability = _compact_observability(
        row_result.get("observability"),
        final_status=final_status,
        error_code=error_code,
    )
    summary = {
        "source_record_id": str(row_result.get("source_record_id") or ""),
        "requested_asin": str(row_result.get("requested_asin") or ""),
        **observability,
    }
    resolved_asin = _safe_asin(row_result.get("resolved_asin"))
    if resolved_asin:
        summary["resolved_asin"] = resolved_asin
    return summary


def _compact_observability(
    value: Any,
    *,
    final_status: str,
    error_code: str,
) -> dict[str, Any]:
    raw = _mapping(value)
    status = final_status if final_status in OBSERVABLE_ROW_STATUSES else "failed"
    observation: dict[str, Any] = {
        "stage_durations_ms": _compact_stage_durations(
            raw.get("stage_durations_ms"),
            allowed_stages=(
                "navigation",
                "parse",
                "artifact",
                "media",
                "fact",
                "feishu",
            ),
        ),
        "field_coverage": _compact_field_coverage(raw.get("field_coverage")),
        "artifact_count": _nonnegative_int(raw.get("artifact_count")),
        "media_observed_count": _nonnegative_int(raw.get("media_observed_count")),
        "media_materialized_count": _nonnegative_int(raw.get("media_materialized_count")),
        "final_status": status,
        "error_code": _safe_error_code(error_code),
    }
    provider_name = str(raw.get("browser_provider_name") or "").strip()
    if _BROWSER_PROVIDER_NAME.fullmatch(provider_name):
        observation["browser_provider_name"] = provider_name
    return observation


def _compact_stage_durations(
    value: Any,
    *,
    allowed_stages: tuple[str, ...],
) -> dict[str, float]:
    raw = _mapping(value)
    durations: dict[str, float] = {}
    for stage_name in allowed_stages:
        duration = raw.get(stage_name)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            continue
        normalized = float(duration)
        if math.isfinite(normalized) and normalized >= 0:
            durations[stage_name] = round(normalized, 3)
    return durations


def _compact_field_coverage(value: Any) -> dict[str, int | float]:
    raw = _mapping(value)
    coverage: dict[str, int | float] = {}
    for key in ("total", "observed", "explicitly_unavailable", "missing"):
        item = raw.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            continue
        coverage[key] = item
    if "total" in coverage:
        total = int(coverage["total"])
        covered = int(coverage.get("observed", 0)) + int(coverage.get("explicitly_unavailable", 0))
        coverage["percentage"] = (
            round((covered / total) * 100.0, 2) if total > 0 and covered <= total else 0.0
        )
    return coverage


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _compact_text(value: Any, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text and len(text) <= max_length else ""


def _compact_string_list(value: Any, *, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _compact_text(item, max_length=max_length)
        if text:
            result.append(text)
    return result


def _compact_hex_identifier(value: Any, *, length: int) -> str:
    text = _compact_text(value, max_length=length).lower()
    pattern = _HEX_32 if length == 32 else _HEX_64 if length == 64 else None
    return text if pattern is not None and pattern.fullmatch(text) else ""


def _compact_capture_object_key(
    value: Any,
    *,
    capture_kind: str,
    content_digest: str,
    expected_asin: str = "",
    expected_run_id: str = "",
    expected_object_prefix: str = "",
    require_exact_prefix: bool = False,
) -> str:
    object_key = _compact_text(value, max_length=2048)
    if (
        not object_key
        or not _ARTIFACT_OBJECT_KEY.fullmatch(object_key)
        or _SENSITIVE_TEXT.search(object_key)
    ):
        return ""
    parts = object_key.split("/")
    governed_path = ("raw-captures", "amazon", "us")
    governed_indexes = [
        index
        for index in range(len(parts) - len(governed_path) + 1)
        if tuple(parts[index : index + len(governed_path)]) == governed_path
    ]
    if len(governed_indexes) != 1:
        return ""
    governed_index = governed_indexes[0]
    prefix_parts = expected_object_prefix.split("/") if expected_object_prefix else []
    suffix = parts[governed_index:]
    if (
        any(part in {"", ".", ".."} for part in parts)
        or len(suffix) != 10
        or suffix[:3] != list(governed_path)
        or not _safe_asin(suffix[3])
        or not re.fullmatch(r"\d{4}", suffix[4])
        or not re.fullmatch(r"\d{2}", suffix[5])
        or not re.fullmatch(r"\d{2}", suffix[6])
        or not _HEX_64.fullmatch(suffix[7])
        or suffix[8] != content_digest
        or suffix[9] != _CAPTURE_FILE_NAMES[capture_kind]
        or (expected_asin and suffix[3] != expected_asin)
        or (expected_run_id and suffix[7] != expected_run_id)
        or (
            require_exact_prefix
            and (
                governed_index != len(prefix_parts)
                or parts[:governed_index] != prefix_parts
            )
        )
    ):
        return ""
    try:
        datetime(int(suffix[4]), int(suffix[5]), int(suffix[6]))
    except ValueError:
        return ""
    return object_key


def _compact_artifact_object_prefix(value: Any) -> str:
    if value == "":
        return ""
    prefix = _compact_text(value, max_length=1024)
    if (
        not prefix
        or not _ARTIFACT_OBJECT_KEY.fullmatch(prefix)
        or _SENSITIVE_TEXT.search(prefix)
    ):
        return ""
    parts = prefix.split("/")
    if any(part in {"", ".", "..", "raw-captures"} for part in parts):
        return ""
    return prefix


def _capture_date_matches_object_key(
    object_key: str,
    *,
    collected_at: str,
    object_prefix: str,
) -> bool:
    try:
        observed_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed_at.tzinfo is None:
        return False
    observed_at = observed_at.astimezone(timezone.utc)
    prefix_length = len(object_prefix.split("/")) if object_prefix else 0
    parts = object_key.split("/")
    return parts[prefix_length + 4 : prefix_length + 7] == [
        f"{observed_at.year:04d}",
        f"{observed_at.month:02d}",
        f"{observed_at.day:02d}",
    ]


def _compact_iso_timestamp(value: Any) -> str:
    timestamp = _compact_text(value, max_length=64)
    if not timestamp or _SENSITIVE_TEXT.search(timestamp):
        return ""
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return timestamp if parsed.tzinfo is not None else ""


def _safe_asin(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return normalize_asin(value)
    except InvalidASINError:
        return ""


def _with_feishu_writeback_duration(value: Any, record: Any) -> dict[str, Any]:
    observability = _mapping(value)
    duration_ms = _record_duration_ms(record)
    if duration_ms is None:
        return observability
    durations = _compact_stage_durations(
        observability.get("stage_durations_ms"),
        allowed_stages=(
            "navigation",
            "parse",
            "artifact",
            "media",
            "fact",
            "feishu",
        ),
    )
    durations["feishu"] = round(float(durations.get("feishu", 0.0)) + duration_ms, 3)
    return {**observability, "stage_durations_ms": durations}


def _record_duration_ms(record: Any) -> float | None:
    if not isinstance(record, Mapping):
        return None
    started_at = record.get("started_at")
    finished_at = record.get("finished_at")
    if (
        isinstance(started_at, bool)
        or isinstance(finished_at, bool)
        or not isinstance(started_at, (int, float))
        or not isinstance(finished_at, (int, float))
    ):
        return None
    started = float(started_at)
    finished = float(finished_at)
    if not math.isfinite(started) or not math.isfinite(finished) or finished < started:
        return None
    return round((finished - started) * 1_000.0, 3)


def _safe_error_code(value: Any) -> str:
    error_code = str(value or "").strip()
    return error_code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", error_code) else ""


def _safe_stage_code(value: Any) -> str:
    stage_code = str(value or "").strip()
    return stage_code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", stage_code) else ""


def _has_active_amazon_children(*, store: Any, request_id: str) -> bool:
    children: list[Any] = []
    for stage_code in (READ_STAGE_CODE, BROWSER_STAGE_CODE, PERSIST_STAGE_CODE):
        children.extend(
            _children_for_stage(
                store=store,
                request_id=request_id,
                stage_code=stage_code,
            )
        )
    return _has_active(children)


def _children_for_stage(*, store: Any, request_id: str, stage_code: str) -> list[Any]:
    if stage_code == BROWSER_STAGE_CODE:
        return [
            *browser_executions_for_stage(
                store,
                request_id=request_id,
                stage_code=stage_code,
            ),
            *api_jobs_for_stage(store, request_id=request_id, stage_code=stage_code),
        ]
    return api_jobs_for_stage(store, request_id=request_id, stage_code=stage_code)


def _jobs_for_stage(
    *,
    store: Any,
    request_id: str,
    stage_code: str,
    job_code: str,
) -> list[dict[str, Any]]:
    return [
        job
        for job in api_jobs_for_stage(
            store,
            request_id=request_id,
            stage_code=stage_code,
        )
        if str(job.get("job_code") or "") == job_code
    ]


def _has_active(records: list[Any]) -> bool:
    return any(_record_status(record) in ACTIVE_STATUSES for record in records)


def _record_status(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(record.get("status") or "")
    return str(getattr(record, "status", "") or "")


def _record_error_code(record: Any) -> str:
    handler_result = extract_handler_result(record)
    error = _mapping(handler_result.get("error"))
    if error.get("error_code"):
        return str(error["error_code"])
    if isinstance(record, Mapping):
        return str(record.get("error_code") or "")
    return str(getattr(record, "error_code", "") or "")


def _stable_run_id(*, request_id: str, source_record_id: str, requested_asin: str) -> str:
    seed = f"{request_id}:{source_record_id}:{requested_asin}".encode()
    return hashlib.sha256(seed).hexdigest()


def _advance(next_stage: str, *, reason: str) -> dict[str, Any]:
    return {"action": "advance", "next_stage": next_stage, "details": {"reason": reason}}


def _waiting(
    stage_code: str,
    message: str,
    *,
    dispatch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    details = {}
    if dispatch is not None:
        details["dispatch"] = {
            "created_count": int(dispatch.get("created_count") or 0),
            "skipped_count": int(dispatch.get("skipped_count") or 0),
        }
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "wait": {"stage_code": stage_code},
        "details": details,
    }


def _failure(
    *,
    stage_code: str,
    error_code: str,
    message: str,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result_payload = dict(result or {})
    safe_error_code = _safe_error_code(error_code) or "amazon_product_workflow_failed"
    result_payload.pop("error_code", None)
    original_error_code = _safe_error_code(result_payload.get("original_error_code"))
    if original_error_code:
        result_payload["original_error_code"] = original_error_code
    else:
        result_payload.pop("original_error_code", None)
    row_status = str(result_payload.get("row_status") or "failed")
    if row_status not in FAILURE_ROW_STATUSES:
        row_status = "failed"
    row_result = {**result_payload, "row_status": row_status}
    return {
        "action": "finalize",
        "final_status": "failed",
        "failed_stage": stage_code,
        "error_code": safe_error_code,
        "summary": _top_level_summary(
            final_status="failed",
            row_status=row_status,
            row_result=row_result,
            failed_stage=stage_code,
            error_code=safe_error_code,
        ),
        "result": {**result_payload, "error_code": safe_error_code},
        "message": message,
    }


def _outbox_message(*, summary: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    row_results = _mapping_list(result.get("row_results"))
    row_status = str(row_results[0].get("row_status") or "failed") if row_results else "failed"
    return (
        "Amazon 商品采集完成："
        f"status={summary.get('final_status', 'failed')}, row_status={row_status}"
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


__all__ = [
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
]
