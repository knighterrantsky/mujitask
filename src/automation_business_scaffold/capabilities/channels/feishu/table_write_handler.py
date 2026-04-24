from __future__ import annotations

from automation_business_scaffold.capabilities.input_sources.feishu.table_common import (
    build_feishu_client,
    classify_feishu_exception,
    execute_write_records,
    map_write_records,
    resolve_write_target,
    validate_write_schema,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    failed_result,
    first_non_empty,
    partial_success_result,
    skipped_result,
    success_result,
)
from typing import Any

HANDLER_CODE = "feishu_table_write"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def feishu_table_write_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        target = resolve_write_target(payload)
        client = build_feishu_client(target)
        records = map_write_records(payload)
        if not records:
            return skipped_result(
                context,
                summary={
                    "target_table_ref": first_non_empty(payload.get("target_table_ref"), target.table_ref),
                    "written_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                },
                result={
                    "written_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                    "target_record_ids": [],
                    "records": [],
                    "writeback_context": {
                        "target_table_ref": first_non_empty(payload.get("target_table_ref"), target.table_ref),
                        "mapper_code": first_non_empty(payload.get("mapper_code")),
                    },
                },
                warnings=("No Feishu write records were produced.",),
            )
        write_policy = coerce_mapping(payload.get("write_policy"))
        if coerce_bool(write_policy.get("validate_schema")) or coerce_bool(payload.get("validate_schema")):
            validate_write_schema(client, target, records)
        write_result = execute_write_records(client, target, records, payload)
    except Exception as exc:  # pragma: no cover - defensive boundary uses classified payloads in tests
        return _feishu_failed_result(context, exc, table_ref=payload.get("target_table_ref"))

    written_count = int(write_result.get("written_count") or 0)
    skipped_count = int(write_result.get("skipped_count") or 0)
    failed_count = int(write_result.get("failed_count") or 0)
    summary = {
        "target_table_ref": first_non_empty(payload.get("target_table_ref"), target.table_ref),
        "mapper_code": first_non_empty(payload.get("mapper_code")),
        "written_count": written_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
    }
    if failed_count <= 0:
        if written_count <= 0 and skipped_count > 0:
            return skipped_result(
                context,
                summary=summary,
                result=write_result,
                warnings=("All Feishu write records were skipped.",),
            )
        return success_result(context, summary=summary, result=write_result)

    partial_allowed = coerce_bool(coerce_mapping(payload.get("write_policy")).get("partial_success_allowed"), default=True)
    if partial_allowed and (written_count > 0 or skipped_count > 0):
        return partial_success_result(
            context,
            summary=summary,
            result=write_result,
            warnings=("Some Feishu records failed to write.",),
        )
    error = _error_from_failed_write_records(write_result)
    return failed_result(context, error=error, summary=summary, result=write_result)


def _feishu_failed_result(context: HandlerContext, exc: Exception, *, table_ref: Any = "") -> HandlerResult:
    error = classify_feishu_exception(exc)
    return failed_result(
        context,
        error=build_error(
            error_type=error.error_type,
            error_code=error.error_code,
            message=error.message,
            retryable=error.retryable,
            details={**(error.details or {}), "table_ref": first_non_empty(table_ref)},
        ),
        summary={"table_ref": first_non_empty(table_ref), "error_code": error.error_code},
    )


def _error_from_failed_write_records(write_result: dict[str, Any]):
    failed_records = [
        record
        for record in coerce_mapping_list(write_result.get("records"))
        if first_non_empty(record.get("status")) == "failed"
    ]
    first_failed = failed_records[0] if failed_records else {}
    error_type = first_non_empty(first_failed.get("error_type"), "upstream_error")
    error_code = first_non_empty(first_failed.get("error_code"), "feishu_write_failed")
    retryable = error_type in {"rate_limited", "timeout", "upstream_error"}
    return build_error(
        error_type=error_type,
        error_code=error_code,
        message=first_non_empty(first_failed.get("message"), "Feishu write failed."),
        retryable=retryable,
        details={"failed_count": int(write_result.get("failed_count") or 0), "failed_records": failed_records[:3]},
    )


__all__ = ["CONTRACT", "HANDLER_CODE", "feishu_table_write_handler"]
