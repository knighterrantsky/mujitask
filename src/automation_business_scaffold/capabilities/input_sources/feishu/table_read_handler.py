from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.capabilities.input_sources.feishu.table_common import (
    adapt_source_rows,
    build_feishu_client,
    resolve_read_target,
)
from automation_business_scaffold.config import get_execution_control_defaults
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
    compact_dict,
    failed_result,
    first_non_empty,
    now_timestamp,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    create_store_from_settings,
    sync_artifact_specs,
)

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
    except Exception as exc:  # pragma: no cover - defensive boundary uses classified payloads in tests
        return _feishu_failed_result(context, exc, table_ref=payload.get("source_table_ref"))

    snapshot_policy = coerce_mapping(payload.get("snapshot_policy"))
    try:
        snapshot_payload = _store_raw_snapshot(
            context=context,
            payload=payload,
            table_ref=first_non_empty(payload.get("source_table_ref"), target.table_ref),
            field_names=field_names,
            pagination=pagination,
            raw_rows=raw_rows,
            raw_rows_all=raw_rows_all,
            snapshot_policy=snapshot_policy,
        )
    except Exception as exc:
        return _artifact_failed_result(context, exc, table_ref=payload.get("source_table_ref"))

    raw_snapshot_ref = first_non_empty(snapshot_payload.get("raw_snapshot_ref"))
    adapter_input_payload = {**payload, "raw_snapshot_ref": raw_snapshot_ref} if raw_snapshot_ref else payload
    try:
        adapter_payload = adapt_source_rows(raw_rows, adapter_input_payload)
    except Exception as exc:
        return _adapter_failed_result(context, exc, table_ref=payload.get("source_table_ref"))

    result = {
        "source_rows": adapter_payload["source_rows"],
        "schema": {"field_names": field_names},
        "pagination": pagination,
        "raw_snapshot_ref": raw_snapshot_ref,
        "raw_snapshot_artifacts": snapshot_payload.get("raw_snapshot_artifacts", []),
        "empty_row_records": _empty_row_records(raw_rows_all),
        "candidate_keys": adapter_payload["candidate_keys"],
        "adapter_summary": adapter_payload["adapter_summary"],
    }
    if coerce_bool(snapshot_policy.get("include_raw_rows_in_result")):
        result["raw_rows"] = raw_rows
        result["raw_rows_all"] = raw_rows_all
    summary = {
        "source_table_ref": first_non_empty(payload.get("source_table_ref"), target.table_ref),
        "raw_row_count": len(raw_rows),
        "source_row_count": len(adapter_payload["source_rows"]),
        "empty_row_count": len(result["empty_row_records"]),
        "adapter_code": first_non_empty(payload.get("adapter_code")),
        "has_more": bool(pagination.get("has_more")),
        "raw_snapshot_ref": raw_snapshot_ref,
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


def _artifact_failed_result(context: HandlerContext, exc: Exception, *, table_ref: Any = "") -> HandlerResult:
    return failed_result(
        context,
        error=build_error(
            error_type="infrastructure",
            error_code="feishu_raw_snapshot_write_failed",
            message=str(exc) or "Failed to persist Feishu raw table snapshot.",
            retryable=True,
            details={"table_ref": first_non_empty(table_ref)},
        ),
        summary={"table_ref": first_non_empty(table_ref), "error_code": "feishu_raw_snapshot_write_failed"},
    )


def _adapter_failed_result(context: HandlerContext, exc: Exception, *, table_ref: Any = "") -> HandlerResult:
    return failed_result(
        context,
        error=build_error(
            error_type="contract",
            error_code="feishu_source_adapter_failed",
            message=str(exc) or "Feishu source adapter failed.",
            retryable=False,
            details={"table_ref": first_non_empty(table_ref)},
        ),
        summary={"table_ref": first_non_empty(table_ref), "error_code": "feishu_source_adapter_failed"},
    )


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [coerce_str(item) for item in value if coerce_str(item)]
    if isinstance(value, tuple):
        return [coerce_str(item) for item in value if coerce_str(item)]
    text = coerce_str(value)
    return [text] if text else []


def _store_raw_snapshot(
    *,
    context: HandlerContext,
    payload: Mapping[str, Any],
    table_ref: str,
    field_names: list[str],
    pagination: Mapping[str, Any],
    raw_rows: list[Mapping[str, Any]],
    raw_rows_all: list[Mapping[str, Any]],
    snapshot_policy: Mapping[str, Any],
) -> dict[str, Any]:
    if not coerce_bool(snapshot_policy.get("store_raw_rows")):
        return {}
    artifact_settings = _resolve_artifact_settings(payload)
    artifact_store = create_store_from_settings(artifact_settings)
    artifact_root = Path(
        first_non_empty(
            payload.get("artifact_root"),
            payload.get("execution_control_artifact_root"),
            artifact_settings.get("artifact_root"),
            tempfile.gettempdir(),
        )
    )
    artifact_bucket = first_non_empty(
        payload.get("artifact_bucket"),
        artifact_settings.get("artifact_bucket"),
        "runtime-artifacts",
    )
    artifact_object_prefix = first_non_empty(
        payload.get("artifact_object_prefix"),
        artifact_settings.get("artifact_object_prefix"),
    )
    run_id = _safe_path_part(first_non_empty(payload.get("run_id"), context.metadata.get("run_id"), context.job_id))
    namespace = _safe_relative_namespace(
        first_non_empty(snapshot_policy.get("raw_snapshot_namespace"), "feishu/common/read")
    )
    relative_name = f"artifacts/{namespace}/page-1.json"
    raw_path = artifact_root / "runs" / run_id / relative_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {
                "request_id": context.request_id,
                "job_id": context.job_id,
                "handler_code": context.handler_code,
                "source_table_ref": table_ref,
                "schema": {"field_names": field_names},
                "pagination": dict(pagination),
                "raw_rows": list(raw_rows),
                "raw_rows_all": list(raw_rows_all),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    records, _artifact_uri_prefix = sync_artifact_specs(
        run_id=run_id,
        request_id=context.request_id,
        execution_id=context.job_id,
        artifact_root=artifact_root,
        artifact_bucket=artifact_bucket,
        artifact_object_prefix=artifact_object_prefix,
        specs=[
            ArtifactFileSpec(
                kind="feishu_table_read_raw_json",
                step_id=context.handler_code,
                relative_name=relative_name,
                path=raw_path,
                content_type="application/json",
                metadata={"source_table_ref": table_ref},
            )
        ],
        artifact_store=artifact_store,
        created_at=now_timestamp(),
    )
    artifacts = [record.to_dict() for record in records]
    ref = ""
    if records:
        record = records[0]
        ref = first_non_empty(
            record.metadata.get("remote_uri"),
            record.metadata.get("local_uri"),
            Path(record.source_path).resolve().as_uri(),
        )
    return compact_dict(
        {
            "raw_snapshot_ref": ref or raw_path.resolve().as_uri(),
            "raw_snapshot_artifacts": artifacts,
        }
    )


def _resolve_artifact_settings(payload: Mapping[str, Any]) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    settings = compact_dict(
        {
            "artifact_store_provider": defaults.artifact_store_provider,
            "artifact_bucket": defaults.artifact_bucket,
            "artifact_object_prefix": defaults.artifact_object_prefix,
            "artifact_root": defaults.artifact_root,
            "minio_endpoint": defaults.minio_endpoint,
            "minio_access_key": defaults.minio_access_key,
            "minio_secret_key": defaults.minio_secret_key,
            "minio_secure": defaults.minio_secure,
            "minio_region": defaults.minio_region,
            "minio_create_bucket": defaults.minio_create_bucket,
        }
    )
    request_payload = coerce_mapping(payload.get("request_payload"))
    for source in (coerce_mapping(request_payload.get("artifact_store")), coerce_mapping(payload.get("artifact_store"))):
        for source_key, target_key in (
            ("provider", "artifact_store_provider"),
            ("artifact_store_provider", "artifact_store_provider"),
            ("bucket", "artifact_bucket"),
            ("artifact_bucket", "artifact_bucket"),
            ("object_prefix", "artifact_object_prefix"),
            ("artifact_object_prefix", "artifact_object_prefix"),
            ("artifact_root", "artifact_root"),
        ):
            if source.get(source_key) not in (None, ""):
                settings[target_key] = source[source_key]
    for source in (request_payload, payload):
        for source_key, target_key in (
            ("artifact_store_provider", "artifact_store_provider"),
            ("execution_control_artifact_store_provider", "artifact_store_provider"),
            ("artifact_bucket", "artifact_bucket"),
            ("execution_control_artifact_bucket", "artifact_bucket"),
            ("artifact_object_prefix", "artifact_object_prefix"),
            ("execution_control_artifact_object_prefix", "artifact_object_prefix"),
            ("artifact_root", "artifact_root"),
            ("execution_control_artifact_root", "artifact_root"),
            ("minio_endpoint", "minio_endpoint"),
            ("execution_control_minio_endpoint", "minio_endpoint"),
            ("minio_access_key", "minio_access_key"),
            ("execution_control_minio_access_key", "minio_access_key"),
            ("minio_secret_key", "minio_secret_key"),
            ("execution_control_minio_secret_key", "minio_secret_key"),
            ("minio_secure", "minio_secure"),
            ("execution_control_minio_secure", "minio_secure"),
            ("minio_region", "minio_region"),
            ("execution_control_minio_region", "minio_region"),
            ("minio_create_bucket", "minio_create_bucket"),
            ("execution_control_minio_create_bucket", "minio_create_bucket"),
        ):
            if source.get(source_key) not in (None, ""):
                settings[target_key] = source[source_key]
    return settings


def _safe_relative_namespace(value: str) -> str:
    parts = [_safe_path_part(part) for part in str(value or "").split("/")]
    return "/".join(part for part in parts if part) or "feishu/common/read"


def _safe_path_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-") or "artifact"


def _empty_row_records(raw_rows: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for row in raw_rows:
        record_id = coerce_str(row.get("record_id"))
        fields = coerce_mapping(row.get("fields"))
        if record_id and fields and not any(_field_has_value(value) for value in fields.values()):
            records.append({"record_id": record_id})
    return records


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


__all__ = ["CONTRACT", "HANDLER_CODE", "feishu_table_read_handler"]
