from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    InvalidASINError,
    normalize_asin,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerNextAction,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import build_error
from automation_business_scaffold.domains.amazon.flows.refresh_amazon_product_row_by_asin.orchestrator import (
    _compact_capture_ref,
    _compact_capture_refs,
    _compact_media_source_refs,
    _compact_stage_durations,
    _persist_writeback_converged,
    _status_writeback_converged,
    _stable_run_id,
    _validate_browser_result,
)
from automation_business_scaffold.domains.amazon.projections.feishu_product_projection import (
    AMAZON_PRODUCT_FEISHU_WRITE_FIELDS,
)


_BROWSER_STAGE_CODE = "collect_amazon_product_browsers"
_PERSISTABLE_BROWSER_STATUSES = {"success", "partial_success"}
_TERMINAL_ROW_STATUSES = {"success", "partial_success", "unavailable"}

feishu_table_write_handler = api_handler_callable("feishu_table_write")
amazon_product_row_persist_handler = api_handler_callable("amazon_product_row_persist")


def run_amazon_product_row_refresh_flow(context: HandlerContext) -> HandlerResult:
    try:
        row = _validate_row_payload(context.payload, request_id=context.request_id)
    except (InvalidASINError, TypeError, ValueError) as exc:
        return _failed(
            context,
            row=_failure_row(context.payload),
            error_code="invalid_amazon_row_refresh_payload",
            message=str(exc),
            retryable=False,
        )

    browser_execution = _mapping(context.payload.get("browser_execution"))
    if not browser_execution:
        collecting = _write_status(context, row=row, row_status="collecting")
        if not _status_write_succeeded(collecting, row=row):
            return _failed_from_child(
                context,
                row=row,
                child=collecting,
                error_code="amazon_collecting_status_writeback_failed",
            )
        browser_request = _browser_request(context, row=row)
        return HandlerResult.browser_required(
            context,
            summary={
                "source_record_id": row["source_record_id"],
                "requested_asin": row["requested_asin"],
                "row_status": "waiting_browser",
            },
            result={
                "source_record_id": row["source_record_id"],
                "requested_asin": row["requested_asin"],
                "row_status": "waiting_browser",
                "browser_required": True,
                "browser_request": browser_request,
            },
            next_action=HandlerNextAction(
                type="browser_required",
                payload=browser_request,
            ),
        )

    browser_status = str(browser_execution.get("status") or "").strip()
    browser_result = _mapping(browser_execution.get("result"))
    if browser_status not in _PERSISTABLE_BROWSER_STATUSES:
        row_status = (
            "blocked"
            if str(browser_result.get("collection_status") or "") == "blocked"
            else "failed"
        )
        error_code = str(browser_execution.get("error_code") or "").strip()
        error_code = error_code or "amazon_browser_collection_failed"
        terminal_write = _write_status(
            context,
            row=row,
            row_status=row_status,
            error_code=error_code,
        )
        if not _status_write_succeeded(terminal_write, row=row):
            error_code = "amazon_terminal_status_writeback_failed"
        return _failed(
            context,
            row=row,
            row_status=row_status,
            error_code=error_code,
            message="Amazon browser collection failed for the source row.",
            retryable=False,
        )

    try:
        capture_context = _capture_context(
            context,
            row=row,
            browser_execution=browser_execution,
        )
        _validate_browser_result(
            row=row,
            result=browser_result,
            expected_browser_target_digest=row["runtime_context"]["browser_target_digest"],
            capture_context=capture_context,
        )
    except ValueError as exc:
        terminal_write = _write_status(
            context,
            row=row,
            row_status="failed",
            error_code=str(exc) or "invalid_amazon_browser_result",
        )
        error_code = str(exc) or "invalid_amazon_browser_result"
        if not _status_write_succeeded(terminal_write, row=row):
            error_code = "amazon_terminal_status_writeback_failed"
        return _failed(
            context,
            row=row,
            error_code=error_code,
            message="Amazon browser result failed identity or artifact validation.",
            retryable=False,
        )

    persisting = _write_status(context, row=row, row_status="persisting")
    if not _status_write_succeeded(persisting, row=row):
        return _failed_from_child(
            context,
            row=row,
            child=persisting,
            error_code="amazon_persisting_status_writeback_failed",
        )

    persist_result = amazon_product_row_persist_handler(
        _child_context(
            context,
            handler_code="amazon_product_row_persist",
            step_code="amazon_product_row_persist",
            payload={
                "workflow_code": context.workflow_code,
                "stage_code": context.stage_code,
                "table_ref": row["table_ref"],
                "source_record_id": row["source_record_id"],
                "source_table_identity": dict(row["source_table_identity"]),
                "requested_asin": row["requested_asin"],
                "resolved_asin": str(browser_result.get("resolved_asin") or ""),
                "run_id": row["run_id"],
                "collection_status": str(browser_result.get("collection_status") or ""),
                "normalized_capture_ref": _compact_capture_ref(
                    browser_result.get("normalized_capture_ref"),
                    expected_kind="normalized_capture",
                    capture_context=capture_context,
                ),
                "raw_capture_refs": _compact_capture_refs(
                    browser_result.get("raw_capture_refs"),
                    capture_context=capture_context,
                ),
                "media_source_refs": _compact_media_source_refs(
                    browser_result.get("media_source_refs"),
                    product_id=row["requested_asin"],
                ),
                "field_coverage": _mapping(browser_result.get("field_coverage")),
                "browser_provider_name": str(
                    browser_result.get("browser_provider_name") or ""
                ).strip(),
                "stage_durations_ms": _compact_stage_durations(
                    browser_result.get("stage_durations_ms"),
                    allowed_stages=("navigation", "parse", "artifact"),
                ),
            },
        )
    )
    compact = _compact_persist_result(persist_result.result, row=row)
    if persist_result.status not in {"success", "partial_success"}:
        return _failed_from_child(
            context,
            row=row,
            child=persist_result,
            error_code="amazon_product_row_persist_failed",
            result=compact,
        )
    if compact["row_status"] not in _TERMINAL_ROW_STATUSES:
        return _failed(
            context,
            row=row,
            error_code="invalid_amazon_row_status",
            message="Amazon row persistence returned an invalid terminal status.",
            retryable=False,
            result=compact,
        )
    if not _persist_writeback_converged(
        compact.get("writeback"),
        source_record_id=row["source_record_id"],
    ):
        return _failed(
            context,
            row=row,
            error_code="amazon_persist_writeback_not_converged",
            message="Amazon row persistence did not update exactly the source record.",
            retryable=False,
            result=compact,
        )
    summary = {
        "source_record_id": row["source_record_id"],
        "requested_asin": row["requested_asin"],
        "row_status": compact["row_status"],
    }
    if persist_result.status == "partial_success" or compact["row_status"] == "partial_success":
        return HandlerResult.partial_success(
            context,
            summary=summary,
            result=compact,
            warnings=persist_result.warnings,
        )
    return HandlerResult.success(
        context,
        summary=summary,
        result=compact,
        warnings=persist_result.warnings,
    )


def _validate_row_payload(
    payload: Mapping[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    source_record_id = str(payload.get("source_record_id") or "").strip()
    if not source_record_id:
        raise ValueError("source_record_id is required")
    requested_asin = normalize_asin(payload.get("requested_asin"))
    canonical_url = str(payload.get("canonical_url") or "").strip()
    if canonical_url != f"https://www.amazon.com/dp/{requested_asin}":
        raise ValueError("canonical_url does not match requested_asin")
    table_ref = str(payload.get("table_ref") or "").strip()
    if not table_ref:
        raise ValueError("table_ref is required")
    identity = _mapping(payload.get("source_table_identity"))
    source_table_identity = {
        "base_id": str(identity.get("base_id") or "").strip(),
        "table_id": str(identity.get("table_id") or "").strip(),
    }
    if not all(source_table_identity.values()):
        raise ValueError("source_table_identity is required")
    runtime = _mapping(payload.get("runtime_context"))
    browser_target_digest = str(runtime.get("browser_target_digest") or "").strip()
    runtime_context = {
        "browser_target_digest": browser_target_digest,
        "browser_resource_code": str(runtime.get("browser_resource_code") or "").strip(),
        "artifact_bucket": str(runtime.get("artifact_bucket") or "").strip(),
        "artifact_object_prefix": str(runtime.get("artifact_object_prefix") or "").strip("/"),
    }
    if (
        not browser_target_digest
        or runtime_context["browser_resource_code"]
        != f"browser:amazon:{browser_target_digest}"
        or not runtime_context["artifact_bucket"]
    ):
        raise ValueError("runtime_context is invalid")
    return {
        "source_record_id": source_record_id,
        "requested_asin": requested_asin,
        "canonical_url": canonical_url,
        "table_ref": table_ref,
        "source_table_identity": source_table_identity,
        "runtime_context": runtime_context,
        "run_id": _stable_run_id(
            request_id=request_id,
            source_record_id=source_record_id,
            requested_asin=requested_asin,
        ),
    }


def _browser_request(context: HandlerContext, *, row: Mapping[str, Any]) -> dict[str, Any]:
    runtime = _mapping(row.get("runtime_context"))
    return {
        "handler_code": "amazon_product_browser_fetch",
        "resource_code": runtime["browser_resource_code"],
        "payload": {
            "workflow_code": context.workflow_code,
            "stage_code": _BROWSER_STAGE_CODE,
            "source_record_id": row["source_record_id"],
            "requested_asin": row["requested_asin"],
            "run_id": row["run_id"],
            "artifact_bucket": runtime["artifact_bucket"],
            "artifact_object_prefix": runtime["artifact_object_prefix"],
        },
    }


def _capture_context(
    context: HandlerContext,
    *,
    row: Mapping[str, Any],
    browser_execution: Mapping[str, Any],
) -> dict[str, str]:
    execution_id = str(browser_execution.get("execution_id") or "").strip()
    if not execution_id:
        raise ValueError("browser execution_id is required")
    runtime = _mapping(row.get("runtime_context"))
    return {
        "request_id": context.request_id,
        "execution_id": execution_id,
        "run_id": str(row["run_id"]),
        "requested_asin": str(row["requested_asin"]),
        "artifact_bucket": str(runtime["artifact_bucket"]),
        "artifact_object_prefix": str(runtime["artifact_object_prefix"]),
    }


def _write_status(
    context: HandlerContext,
    *,
    row: Mapping[str, Any],
    row_status: str,
    error_code: str = "",
) -> HandlerResult:
    identity = _mapping(row.get("source_table_identity"))
    return feishu_table_write_handler(
        _child_context(
            context,
            handler_code="feishu_table_write",
            step_code=f"status_{row_status}",
            payload={
                "request_id": context.request_id,
                "workflow_code": context.workflow_code,
                "stage_code": context.stage_code,
                "target_table_ref": row["table_ref"],
                "source_record_id": row["source_record_id"],
                "row_status": row_status,
                "error_code": error_code,
                "feishu_table": {
                    "app_token": str(identity.get("base_id") or ""),
                    "table_id": str(identity.get("table_id") or ""),
                },
                "records": [
                    {
                        "source_record_id": row["source_record_id"],
                        "requested_asin": row["requested_asin"],
                        "collection_status": row_status,
                        "error_code": error_code,
                    }
                ],
                "mapper_code": "amazon_product_projection_mapper",
                "write_mode": "update_existing",
                "write_policy": {
                    "ignore_missing_fields": True,
                    "field_allowlist": list(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS),
                },
                "writeback_kind": (
                    "amazon_stage_status"
                    if row_status in {"collecting", "persisting"}
                    else "amazon_terminal_status"
                ),
            },
        )
    )


def _status_write_succeeded(
    result: HandlerResult,
    *,
    row: Mapping[str, Any],
) -> bool:
    return _status_writeback_converged(
        handler_status=result.status,
        value=result.result,
        source_record_id=str(row.get("source_record_id") or ""),
    )


def _child_context(
    parent: HandlerContext,
    *,
    handler_code: str,
    payload: dict[str, Any],
    step_code: str,
) -> HandlerContext:
    return HandlerContext(
        request_id=parent.request_id,
        job_id=f"{parent.job_id}:{step_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code=parent.workflow_code,
        stage_code=parent.stage_code,
        job_code=handler_code,
        business_key=parent.business_key,
        dedupe_key=(
            f"{parent.dedupe_key}:{step_code}"
            if parent.dedupe_key
            else f"{parent.job_id}:{step_code}"
        ),
        worker_id=parent.worker_id,
        metadata=dict(parent.metadata),
    )


def _compact_persist_result(value: Mapping[str, Any], *, row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["source_record_id"] = row["source_record_id"]
    result["requested_asin"] = row["requested_asin"]
    result["run_id"] = row["run_id"]
    result["row_status"] = str(value.get("row_status") or "failed")
    result.pop("browser_required", None)
    result.pop("browser_request", None)
    return result


def _failed_from_child(
    context: HandlerContext,
    *,
    row: Mapping[str, Any],
    child: HandlerResult,
    error_code: str,
    result: Mapping[str, Any] | None = None,
) -> HandlerResult:
    child_error = child.error
    return _failed(
        context,
        row=row,
        error_code=(child_error.error_code if child_error else error_code),
        message=(child_error.message if child_error else "Amazon row refresh child step failed."),
        retryable=(child_error.retryable if child_error else False),
        result=result,
    )


def _failed(
    context: HandlerContext,
    *,
    row: Mapping[str, Any],
    error_code: str,
    message: str,
    retryable: bool,
    row_status: str = "failed",
    result: Mapping[str, Any] | None = None,
) -> HandlerResult:
    payload = {
        "source_record_id": str(row.get("source_record_id") or ""),
        "requested_asin": str(row.get("requested_asin") or ""),
        "row_status": row_status,
        **dict(result or {}),
    }
    payload["row_status"] = row_status
    return HandlerResult.failed(
        context,
        error=build_error(
            error_type="amazon_row_refresh_failure",
            error_code=error_code,
            message=message,
            retryable=retryable,
        ),
        summary={
            "source_record_id": payload["source_record_id"],
            "requested_asin": payload["requested_asin"],
            "row_status": row_status,
            "error_code": error_code,
        },
        result=payload,
    )


def _failure_row(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_record_id": str(payload.get("source_record_id") or "").strip(),
        "requested_asin": str(payload.get("requested_asin") or "").strip(),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = ["run_amazon_product_row_refresh_flow"]
