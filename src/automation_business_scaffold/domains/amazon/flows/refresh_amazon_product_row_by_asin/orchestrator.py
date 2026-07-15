from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    InvalidASINError,
    normalize_asin,
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
            evidence_refs=_mapping_list(browser_result.get("artifact_refs")),
        )
    try:
        _validate_browser_result(
            row=row,
            result=browser_result,
            expected_browser_target_digest=browser_runtime_context[
                "browser_target_digest"
            ],
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
            evidence_refs=_mapping_list(browser_result.get("artifact_refs")),
        )
    return _advance(PERSIST_STAGE_CODE, reason="amazon_capture_ready")


def _advance_persist(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    try:
        row = _read_context(store=store, request=request)
        browser_runtime_context = _browser_runtime_context(request)
        if not browser_runtime_context:
            raise ValueError("amazon_browser_resource_context_missing")
        browser_result = _browser_result(
            store=store,
            request_id=request.request_id,
            row=row,
            expected_browser_target_digest=browser_runtime_context[
                "browser_target_digest"
            ],
        )
    except ValueError as exc:
        return _failure(
            stage_code=PERSIST_STAGE_CODE,
            error_code=str(exc) or "amazon_capture_context_missing",
            message="Persistable Amazon capture context is unavailable.",
        )
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
            "normalized_capture_ref": dict(browser_result["normalized_capture_ref"]),
            "raw_capture_refs": _mapping_list(browser_result["raw_capture_refs"]),
            "media_source_refs": _mapping_list(browser_result.get("media_source_refs")),
            "field_coverage": _mapping(browser_result.get("field_coverage")),
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
        return _failure(
            stage_code=PERSIST_STAGE_CODE,
            error_code=_record_error_code(persist_job) or "amazon_product_row_persist_failed",
            message="Amazon row persistence failed.",
            result=_compact_persist_result(extract_effective_result_payload(persist_job)),
        )
    persist_result = extract_effective_result_payload(persist_job)
    expected_run_id = _stable_run_id(
        request_id=request.request_id,
        source_record_id=row["source_record_id"],
        requested_asin=row["requested_asin"],
    )
    if (
        str(persist_result.get("source_record_id") or "") != row["source_record_id"]
        or str(persist_result.get("requested_asin") or "") != row["requested_asin"]
        or str(persist_result.get("run_id") or "") != expected_run_id
    ):
        return _failure(
            stage_code=PERSIST_STAGE_CODE,
            error_code="amazon_persist_result_identity_mismatch",
            message="Amazon row persistence result does not match the requested source identity.",
        )
    if str(persist_result.get("row_status") or "") not in TERMINAL_ROW_STATUSES:
        return _failure(
            stage_code=PERSIST_STAGE_CODE,
            error_code="invalid_amazon_row_status",
            message="Amazon row persistence returned an invalid terminal status.",
        )
    return _advance(SUMMARY_STAGE_CODE, reason="amazon_row_persisted")


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
) -> dict[str, Any]:
    failure_result = {
        "source_record_id": str(request.payload.get("source_record_id") or "").strip(),
        "row_status": row_status if row_status in FAILURE_ROW_STATUSES else "failed",
        "requested_asin": requested_asin,
        "collection_status": collection_status or row_status,
        "evidence_refs": list(evidence_refs or []),
    }
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
        if str((job.get("payload") or {}).get("writeback_kind") or "")
        == "amazon_terminal_status"
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
    if write_status not in {"success", "partial_success"} or int(
        write_result.get("written_count") or 0
    ) != 1:
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
        "written_count": int(value.get("written_count") or 0),
        "target_record_ids": [
            str(item)
            for item in value.get("target_record_ids", [])
            if str(item).strip()
        ]
        if isinstance(value.get("target_record_ids"), list)
        else [],
    }


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
    request_id: str,
    row: Mapping[str, Any],
    expected_browser_target_digest: str,
) -> dict[str, Any]:
    executions = browser_executions_for_stage(
        store,
        request_id=request_id,
        stage_code=BROWSER_STAGE_CODE,
    )
    if not executions:
        raise ValueError("amazon_capture_context_missing")
    result = extract_effective_result_payload(executions[-1])
    _validate_browser_result(
        row=row,
        result=result,
        expected_browser_target_digest=expected_browser_target_digest,
    )
    return result


def _validate_browser_result(
    *,
    row: Mapping[str, Any],
    result: Mapping[str, Any],
    expected_browser_target_digest: str,
) -> None:
    if str(result.get("marketplace_code") or "") != "US":
        raise ValueError("unsupported_marketplace")
    if str(result.get("requested_asin") or "") != row["requested_asin"]:
        raise ValueError("identity_mismatch")
    if (
        str(result.get("browser_target_digest") or "")
        != expected_browser_target_digest
    ):
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
    if not _mapping(result.get("normalized_capture_ref")):
        raise ValueError("normalized_capture_ref_missing")
    if not _mapping_list(result.get("raw_capture_refs")):
        raise ValueError("raw_capture_refs_missing")
    if "capture" in result or "html" in result:
        raise ValueError("inline_amazon_capture_forbidden")


def _browser_runtime_context(request: Any) -> dict[str, str]:
    stage_cursor = _mapping(getattr(request, "stage_cursor", {}))
    runtime_context = _mapping(stage_cursor.get("runtime_context"))
    browser_target_digest = str(
        runtime_context.get("browser_target_digest") or ""
    ).strip()
    browser_resource_code = str(
        runtime_context.get("browser_resource_code") or ""
    ).strip()
    if (
        not browser_target_digest
        or browser_resource_code != f"browser:amazon:{browser_target_digest}"
    ):
        return {}
    return {
        "browser_target_digest": browser_target_digest,
        "browser_resource_code": browser_resource_code,
    }


def _final_payload(
    *,
    store: Any,
    request: Any,
    force_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, str]]:
    if force_result is not None:
        forced_result = _mapping(force_result.get("result"))
        error_code = str(
            force_result.get("error_code")
            or forced_result.get("error_code")
            or "amazon_product_workflow_failed"
        )
        row_status = str(forced_result.get("row_status") or "failed")
        if row_status not in FAILURE_ROW_STATUSES:
            row_status = "failed"
        row_result = {
            "source_record_id": str(request.payload.get("source_record_id") or ""),
            "row_status": row_status,
            "error_code": error_code,
            **_compact_failure_result(forced_result),
        }
        summary = {
            "final_status": "failed",
            "row_total_count": 1,
            "row_status_counts": {row_status: 1},
            "failed_stage": str(force_result.get("failed_stage") or ""),
            "error_code": error_code,
        }
        result = {
            "workflow_code": TASK_CODE,
            "row_total_count": 1,
            "row_results": [row_result],
            "error_code": error_code,
        }
        return summary, result, "failed", {
            "error_type": "business_workflow_failure",
            "error_code": error_code,
            "message": "Amazon product row workflow failed.",
        }

    jobs = _jobs_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=PERSIST_STAGE_CODE,
        job_code="amazon_product_row_persist",
    )
    persist_result = extract_effective_result_payload(jobs[-1]) if jobs else {}
    row_result = _compact_persist_result(persist_result)
    row_status = str(row_result.get("row_status") or "failed")
    final_status = (
        "partial_success"
        if row_status == "partial_success"
        else "success"
        if row_status in {"success", "unavailable"}
        else "failed"
    )
    summary = {
        "final_status": final_status,
        "row_total_count": 1,
        "row_status_counts": {row_status: 1},
    }
    result = {
        "workflow_code": TASK_CODE,
        "row_total_count": 1,
        "row_results": [row_result],
    }
    return summary, result, final_status, {
        "error_type": "persistence_failure" if final_status == "failed" else "",
        "error_code": "amazon_product_row_persist_failed" if final_status == "failed" else "",
        "message": "Amazon product row persistence failed." if final_status == "failed" else "",
    }


def _compact_persist_result(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "row_status",
        "source_record_id",
        "requested_asin",
        "resolved_asin",
        "run_id",
        "step_statuses",
        "fact_refs",
        "media_coverage",
        "writeback",
        "failed_step",
    )
    return {key: value[key] for key in allowed if value.get(key) not in (None, "", [], {})}


def _compact_failure_result(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "requested_asin",
        "collection_status",
        "evidence_refs",
        "original_error_code",
        "writeback",
    )
    return {key: value[key] for key in allowed if value.get(key) not in (None, "", [], {})}


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
    row_status = str(result_payload.get("row_status") or "failed")
    if row_status not in FAILURE_ROW_STATUSES:
        row_status = "failed"
    return {
        "action": "finalize",
        "final_status": "failed",
        "failed_stage": stage_code,
        "error_code": error_code,
        "summary": {
            "final_status": "failed",
            "row_total_count": 1,
            "row_status_counts": {row_status: 1},
            "failed_stage": stage_code,
            "error_code": error_code,
        },
        "result": {"error_code": error_code, **result_payload},
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
