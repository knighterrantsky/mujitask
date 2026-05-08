from __future__ import annotations

from automation_business_scaffold.capabilities.input_sources.feishu.table_common import (
    adapt_source_rows,
    build_feishu_client,
    resolve_read_target,
)
from automation_business_scaffold.capabilities.input_sources.feishu.row_reading import (
    read_feishu_records,
)
from automation_business_scaffold.capabilities.input_sources.feishu.schema_normalization import (
    normalize_raw_rows,
    validate_read_schema,
)
from automation_business_scaffold.capabilities.input_sources.feishu.transport_errors import (
    classify_feishu_exception,
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
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from typing import Any

HANDLER_CODE = "feishu_table_read"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def feishu_table_read_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        target = resolve_read_target(payload)
        client = build_feishu_client(target, payload)
        field_names = _list_text(payload.get("field_names"))
        read_policy = coerce_mapping(payload.get("read_policy"))
        if coerce_bool(read_policy.get("validate_schema")) or coerce_bool(payload.get("validate_schema")):
            validate_read_schema(client, target, field_names)
        raw_records, pagination = read_feishu_records(client, target, payload)
        raw_rows_all = normalize_raw_rows(raw_records, field_names=[])
        raw_rows = normalize_raw_rows(raw_records, field_names=field_names)
        adapter_payload = adapt_source_rows(raw_rows, payload)
    except Exception as exc:  # pragma: no cover - defensive boundary uses classified payloads in tests
        return _feishu_failed_result(context, exc, table_ref=payload.get("source_table_ref"))

    raw_snapshot_ref = ""
    snapshot_policy = coerce_mapping(payload.get("snapshot_policy"))
    if coerce_bool(snapshot_policy.get("store_raw_rows")):
        namespace = first_non_empty(snapshot_policy.get("raw_snapshot_namespace"), "feishu/common/read")
        raw_snapshot_ref = f"artifact://{namespace}/{first_non_empty(payload.get('request_id'), context.request_id)}/page-1.json"

    result = {
        "raw_rows": raw_rows,
        "raw_rows_all": raw_rows_all,
        "source_rows": adapter_payload["source_rows"],
        "schema": {"field_names": field_names},
        "pagination": pagination,
        "raw_snapshot_ref": raw_snapshot_ref,
        "candidate_keys": adapter_payload["candidate_keys"],
        "adapter_summary": adapter_payload["adapter_summary"],
    }
    summary = {
        "source_table_ref": first_non_empty(payload.get("source_table_ref"), target.table_ref),
        "raw_row_count": len(raw_rows),
        "source_row_count": len(adapter_payload["source_rows"]),
        "adapter_code": first_non_empty(payload.get("adapter_code")),
        "has_more": bool(pagination.get("has_more")),
    }
    return success_result(context, summary=summary, result=result)


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


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [coerce_str(item) for item in value if coerce_str(item)]
    if isinstance(value, tuple):
        return [coerce_str(item) for item in value if coerce_str(item)]
    text = coerce_str(value)
    return [text] if text else []


__all__ = ["CONTRACT", "HANDLER_CODE", "feishu_table_read_handler"]
