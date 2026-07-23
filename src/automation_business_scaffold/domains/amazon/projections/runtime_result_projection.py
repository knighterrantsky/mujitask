from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    normalize_amazon_media_url,
)
from automation_business_scaffold.contracts.handler.domain_mapping import (
    RuntimeFailureProjection,
    RuntimeStorageProjection,
)

_AMAZON_CAPTURE_POLICIES = {
    "normalized_capture": ("application/json", "normalized", "normalized.json"),
    "screenshot": ("image/png", "not_applicable", "page.png"),
}
_AMAZON_CAPTURE_RUNTIME_FIELDS = (
    "capture_kind",
    "bucket",
    "object_key",
    "content_digest",
    "content_type",
    "sanitization_status",
    "request_id",
    "execution_id",
    "run_id",
    "collected_at",
    "created_at",
)
_AMAZON_BROWSER_COLLECTION_STATUSES = frozenset(
    {"success", "partial_success", "unavailable", "blocked", "failed"}
)
_AMAZON_ROW_STATUSES = frozenset(
    {"success", "partial_success", "unavailable", "blocked", "failed", "skipped"}
)
_AMAZON_STEP_STATUSES = frozenset({"success", "partial_success", "skipped", "failed"})
_AMAZON_STEP_CODES = frozenset(
    {"media_asset_sync", "amazon_product_fact_upsert", "feishu_table_write"}
)
_AMAZON_PROVIDER_CODES = frozenset({"chrome", "chrome_cdp", "roxy"})
_AMAZON_SUPERVISOR_STATUSES = frozenset(
    {
        "completed",
        "timed_out",
        "child_process_error",
        "handler_completed",
        "handler_failed",
        "exception",
    }
)
_AMAZON_EXECUTION_MODES = frozenset({"inline", "child_process"})
_AMAZON_FAILURE_DISPOSITIONS = frozenset({"none", "retryable", "terminal"})
_AMAZON_PROGRESS_STAGES = {
    "amazon_product_browser_fetch": frozenset({"navigation", "parse", "artifact"}),
    "amazon_product_row_persist": frozenset({"media", "fact", "projection", "feishu"}),
}
_AMAZON_BROWSER_ERROR_TYPES = frozenset(
    {
        "amazon_browser_failure",
        "browser_failure",
        "runtime_artifact_validation_failure",
        "runtime_artifact_index_failure",
    }
)
_AMAZON_BROWSER_ERROR_CODES = frozenset(
    {
        "access_blocked",
        "amazon_browser_collection_failed",
        "amazon_product_extraction_failed",
        "artifact_index_failed",
        "artifact_size_limit_exceeded",
        "artifact_validation_failed",
        "artifact_write_failed",
        "browser_profile_unavailable",
        "captcha_required",
        "identity_mismatch",
        "invalid_amazon_capture",
        "invalid_asin",
        "invalid_browser_request",
        "invalid_product_url",
        "navigation_timeout",
        "object_storage_required",
        "rate_limited",
        "required_failure_evidence_missing",
        "transient_page_failure",
        "unsupported_marketplace",
    }
)
_AMAZON_PERSIST_ERROR_TYPES = frozenset(
    {
        "amazon_row_persistence_failure",
        "child_handler_failure",
        "contract_error",
        "invalid_input",
        "media_sync_failed",
        "persistence_failure",
        "runtime_result_validation_failure",
        "upstream_error",
    }
)
_AMAZON_PERSIST_ERROR_CODES = frozenset(
    {
        "amazon_fact_reference_mismatch",
        "amazon_product_fact_upsert_failed",
        "amazon_projection_facts_missing",
        "amazon_projection_identity_mismatch",
        "amazon_row_persistence_failed",
        "feishu_table_write_failed",
        "feishu_write_failed",
        "feishu_writeback_not_converged",
        "invalid_amazon_persist_payload",
        "invalid_child_handler_status",
        "invalid_handler_result",
        "media_asset_materialization_failed",
        "media_asset_sync_failed",
        "media_sync_failed",
    }
)
_AMAZON_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_AMAZON_IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
_AMAZON_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_AMAZON_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_AMAZON_OBJECT_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,2047}$")


def _api_storage_payload(
    outcome: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if outcome.context.handler_code != "amazon_product_row_persist":
        return outcome.storage_summary(), outcome.storage_result()
    if outcome.worker_result.status not in {"success", "partial_success", "failed"}:
        raise ValueError("Amazon row persistence returned an unsupported handler status.")
    safe_result = _project_amazon_row_persist_result(outcome)
    _validate_amazon_row_persist_success_result(outcome, safe_result)
    stored_result = _amazon_api_storage_envelope(outcome)
    stored_result.update(safe_result)
    summary: dict[str, Any] = {
        "handler_status": _amazon_handler_status(outcome.worker_result.status),
        "supervisor_status": _amazon_supervisor_status(outcome.supervisor_status),
        "heartbeat_count": _native_nonnegative_int(outcome.heartbeat_count),
        "execution_mode": _amazon_execution_mode(outcome.execution_mode),
    }
    for field in (
        "row_status",
        "source_record_id",
        "requested_asin",
        "resolved_asin",
        "run_id",
        "step_statuses",
        "media_coverage",
        "observability",
    ):
        if field in safe_result:
            summary[field] = safe_result[field]
    error = outcome.error
    if error is not None:
        error_type, error_code = _api_runtime_error_codes(outcome)
        summary.update(
            {
                "error_type": error_type,
                "error_code": error_code,
                "retryable": bool(error.retryable),
                "terminal_error": bool(error.terminal),
            }
        )
    elif outcome.worker_result.error is not None:
        error_type, error_code = _api_runtime_error_codes(outcome)
        summary.update(
            {
                "error_type": error_type,
                "error_code": error_code,
                "retryable": bool(outcome.worker_result.error.retryable),
                "terminal_error": not bool(outcome.worker_result.error.retryable),
            }
        )
    return (
        {key: value for key, value in summary.items() if value not in (None, "")},
        stored_result,
    )


def _amazon_api_storage_envelope(outcome: Any) -> dict[str, Any]:
    handler_result = {
        "status": _amazon_handler_status(outcome.worker_result.status),
        "handler_code": "amazon_product_row_persist",
        "request_id": _amazon_identifier(outcome.context.request_id),
        "job_id": _amazon_identifier(outcome.context.job_id),
        "contract_revision": _amazon_contract_revision(outcome.worker_result.contract_revision),
    }
    supervisor: dict[str, Any] = {
        "supervisor_status": _amazon_supervisor_status(outcome.supervisor_status),
        "execution_mode": _amazon_execution_mode(outcome.execution_mode),
        "worker_type": "api_worker",
        "runtime_table": "api_worker_job",
        "request_id": _amazon_identifier(outcome.context.request_id),
        "job_id": _amazon_identifier(outcome.context.job_id),
        "handler_code": "amazon_product_row_persist",
        "started_at": _finite_nonnegative_number(outcome.started_at),
        "finished_at": _finite_nonnegative_number(outcome.finished_at),
        "duration_seconds": _finite_nonnegative_number(outcome.duration_seconds),
        "heartbeat_count": _native_nonnegative_int(outcome.heartbeat_count),
        "progress_stage": _amazon_progress_stage(
            outcome.context.handler_code,
            outcome.progress_stage,
        ),
        "failure_disposition": _amazon_failure_disposition(outcome.failure_disposition),
    }
    if outcome.error is not None:
        error_type, error_code = _api_runtime_error_codes(outcome)
        supervisor["error"] = {
            "error_type": error_type,
            "error_code": error_code,
            "retryable": bool(outcome.error.retryable),
            "terminal": bool(outcome.error.terminal),
        }
    return {
        "handler_result": {
            key: value for key, value in handler_result.items() if value not in (None, "")
        },
        "supervisor": {key: value for key, value in supervisor.items() if value not in (None, "")},
    }


def _project_amazon_row_persist_result(
    outcome: Any,
) -> dict[str, Any]:
    raw = outcome.worker_result.result
    payload = outcome.context.payload
    result: dict[str, Any] = {}
    row_status = raw.get("row_status")
    if row_status in _AMAZON_ROW_STATUSES:
        result["row_status"] = row_status
    source_record_id = payload.get("source_record_id")
    if (
        isinstance(source_record_id, str)
        and source_record_id
        and raw.get("source_record_id") == source_record_id
    ):
        result["source_record_id"] = source_record_id
    requested_asin = _amazon_asin(payload.get("requested_asin"))
    if requested_asin and _amazon_asin(raw.get("requested_asin")) == requested_asin:
        result["requested_asin"] = requested_asin
    resolved_asin = _amazon_asin(raw.get("resolved_asin"))
    expected_resolved_asin = _amazon_asin(payload.get("resolved_asin"))
    if resolved_asin and resolved_asin == expected_resolved_asin:
        result["resolved_asin"] = resolved_asin
    stable_run_id = payload.get("run_id")
    if isinstance(stable_run_id, str) and stable_run_id and raw.get("run_id") == stable_run_id:
        result["run_id"] = stable_run_id
    step_statuses = _amazon_step_statuses(raw.get("step_statuses"))
    if step_statuses:
        result["step_statuses"] = step_statuses
    fact_refs = _amazon_fact_refs(
        raw.get("fact_refs"),
        outcome=outcome,
        require_normalized_capture_ref=(
            outcome.worker_result.status in {"success", "partial_success"}
        ),
    )
    if fact_refs:
        result["fact_refs"] = fact_refs
    media_coverage = _amazon_media_coverage(
        raw.get("media_coverage"),
        expected_count=(
            len(payload["media_source_refs"])
            if isinstance(payload.get("media_source_refs"), list)
            else 0
        ),
    )
    if media_coverage:
        result["media_coverage"] = media_coverage
    writeback = _amazon_writeback(
        raw.get("writeback"),
        expected_source_record_id=source_record_id,
    )
    if writeback:
        result["writeback"] = writeback
    observability = _amazon_observability(
        raw.get("observability"),
        payload=payload,
        row_status=row_status,
        media_coverage=media_coverage,
    )
    if observability:
        result["observability"] = observability
    failed_step = raw.get("failed_step")
    if failed_step in _AMAZON_STEP_CODES:
        result["failed_step"] = failed_step
    error_code = _amazon_error_code("amazon_product_row_persist", raw.get("error_code"))
    if error_code:
        result["error_code"] = error_code
    return result


def _amazon_step_statuses(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        step_code: raw[step_code]
        for step_code in _AMAZON_STEP_CODES
        if raw.get(step_code) in _AMAZON_STEP_STATUSES
    }


def _amazon_fact_refs(
    value: Any,
    *,
    outcome: Any,
    require_normalized_capture_ref: bool,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    refs: dict[str, Any] = {}
    for field in ("product_id", "snapshot_id", "binding_id"):
        identifier = raw.get(field)
        if isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{32}", identifier):
            refs[field] = identifier
    raw_capture_ids = raw.get("raw_capture_ids")
    if isinstance(raw_capture_ids, list) and len(raw_capture_ids) <= 4:
        identifiers = [
            item
            for item in raw_capture_ids
            if isinstance(item, str) and re.fullmatch(r"[a-f0-9]{32}", item)
        ]
        if len(identifiers) == len(raw_capture_ids):
            refs["raw_capture_ids"] = identifiers
    normalized_value = raw.get("normalized_capture_ref")
    if normalized_value is not None or require_normalized_capture_ref:
        expected_ref = _validated_amazon_api_capture_ref(
            outcome,
            outcome.context.payload.get("normalized_capture_ref"),
        )
        normalized_ref = _validated_amazon_api_capture_ref(outcome, normalized_value)
        if normalized_ref != expected_ref:
            raise ValueError(
                "Amazon fact normalized_capture_ref does not match the browser evidence."
            )
        refs["normalized_capture_ref"] = normalized_ref
    return refs


def _validated_amazon_api_capture_ref(
    outcome: Any,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Amazon fact normalized_capture_ref must be an object.")
    payload = outcome.context.payload
    expected_asin = _amazon_asin(payload.get("requested_asin"))
    expected_run_id = payload.get("run_id")
    content_digest = value.get("content_digest")
    bucket = value.get("bucket")
    object_key = value.get("object_key")
    request_id = value.get("request_id")
    execution_id = value.get("execution_id")
    run_id = value.get("run_id")
    if (
        value.get("capture_kind") != "normalized_capture"
        or not isinstance(bucket, str)
        or not _AMAZON_BUCKET.fullmatch(bucket)
        or not isinstance(object_key, str)
        or not _AMAZON_OBJECT_PATH.fullmatch(object_key)
        or "%" in object_key
        or "\\" in object_key
        or not isinstance(content_digest, str)
        or not _AMAZON_DIGEST.fullmatch(content_digest)
        or value.get("content_type") != "application/json"
        or value.get("sanitization_status") != "normalized"
        or request_id != outcome.context.request_id
        or not isinstance(request_id, str)
        or not _AMAZON_IDENTIFIER.fullmatch(request_id)
        or not isinstance(execution_id, str)
        or not _AMAZON_IDENTIFIER.fullmatch(execution_id)
        or not isinstance(expected_run_id, str)
        or not _AMAZON_DIGEST.fullmatch(expected_run_id)
        or run_id != expected_run_id
        or not expected_asin
    ):
        raise ValueError("Amazon fact normalized_capture_ref provenance is invalid.")
    collected_at = _amazon_capture_timestamp(value.get("collected_at"))
    parts = object_key.split("/")
    suffix = [
        "raw-captures",
        "amazon",
        "us",
        expected_asin,
        f"{collected_at.year:04d}",
        f"{collected_at.month:02d}",
        f"{collected_at.day:02d}",
        expected_run_id,
        content_digest,
        "normalized.json",
    ]
    if len(parts) < len(suffix) or parts[-len(suffix) :] != suffix:
        raise ValueError("Amazon fact normalized_capture_ref object_key is invalid.")
    prefix_parts = parts[: -len(suffix)]
    if any(part in {"", ".", "..", "raw-captures"} for part in prefix_parts):
        raise ValueError("Amazon fact normalized_capture_ref prefix is invalid.")
    ref = {field: value[field] for field in _AMAZON_CAPTURE_RUNTIME_FIELDS if field in value}
    ref["collected_at"] = collected_at.isoformat().replace("+00:00", "Z")
    created_at_value = value.get("created_at")
    if created_at_value is not None:
        created_at = _amazon_capture_timestamp(created_at_value)
        ref["created_at"] = created_at.isoformat().replace("+00:00", "Z")
    return ref


def _amazon_media_coverage(value: Any, *, expected_count: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    expected = max(expected_count, 0)
    materialized = min(_native_nonnegative_int(value.get("materialized")), expected)
    missing = expected - materialized
    return {
        "expected": expected,
        "materialized": materialized,
        "missing": missing,
        "complete": missing == 0,
    }


def _amazon_writeback(
    value: Any,
    *,
    expected_source_record_id: Any,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    count_fields = ("written_count", "skipped_count", "failed_count")
    if any(type(raw.get(field)) is not int or raw[field] < 0 for field in count_fields):
        return {}
    target_record_ids = raw.get("target_record_ids")
    if (
        not isinstance(expected_source_record_id, str)
        or not expected_source_record_id
        or raw.get("written_count") != 1
        or raw.get("skipped_count") != 0
        or raw.get("failed_count") != 0
        or target_record_ids != [expected_source_record_id]
    ):
        return {}
    return {
        "written_count": raw["written_count"],
        "skipped_count": raw["skipped_count"],
        "failed_count": raw["failed_count"],
        "target_record_ids": list(target_record_ids),
    }


def _validate_amazon_row_persist_success_result(
    outcome: Any,
    result: Mapping[str, Any],
) -> None:
    handler_status = _amazon_handler_status(outcome.worker_result.status)
    if handler_status not in {"success", "partial_success"}:
        return
    required_fields = {
        "row_status",
        "source_record_id",
        "requested_asin",
        "resolved_asin",
        "run_id",
        "step_statuses",
        "fact_refs",
        "media_coverage",
        "writeback",
        "observability",
    }
    if not required_fields.issubset(result):
        raise ValueError("Amazon row persistence success result is incomplete.")
    payload = outcome.context.payload
    expected_source_record_id = payload.get("source_record_id")
    expected_requested_asin = _amazon_asin(payload.get("requested_asin"))
    expected_resolved_asin = _amazon_asin(payload.get("resolved_asin"))
    expected_run_id = payload.get("run_id")
    if (
        not isinstance(expected_source_record_id, str)
        or not expected_source_record_id
        or result.get("source_record_id") != expected_source_record_id
        or not expected_requested_asin
        or result.get("requested_asin") != expected_requested_asin
        or not expected_resolved_asin
        or result.get("resolved_asin") != expected_resolved_asin
        or not isinstance(expected_run_id, str)
        or not expected_run_id
        or result.get("run_id") != expected_run_id
    ):
        raise ValueError("Amazon row persistence result identity is invalid.")
    row_status = result.get("row_status")
    if (handler_status == "partial_success" and row_status != "partial_success") or (
        handler_status == "success" and row_status not in {"success", "unavailable"}
    ):
        raise ValueError("Amazon row handler and row statuses do not converge.")
    step_statuses = result.get("step_statuses")
    if not isinstance(step_statuses, Mapping) or set(step_statuses) != set(_AMAZON_STEP_CODES):
        raise ValueError("Amazon row persistence result lacks complete step statuses.")
    fact_status = step_statuses["amazon_product_fact_upsert"]
    feishu_status = step_statuses["feishu_table_write"]
    media_status = step_statuses["media_asset_sync"]
    media_coverage = result.get("media_coverage")
    expected_media_count = (
        _native_nonnegative_int(media_coverage.get("expected"))
        if isinstance(media_coverage, Mapping)
        else 0
    )
    if fact_status not in {"success", "partial_success"} or feishu_status not in {
        "success",
        "partial_success",
    }:
        raise ValueError("Amazon row success result contains an incomplete required step.")
    if handler_status == "success" and any(
        status in {"partial_success", "failed"} for status in step_statuses.values()
    ):
        raise ValueError("Amazon successful row contains a degraded step status.")
    if (expected_media_count > 0 and media_status == "skipped") or (
        expected_media_count == 0 and media_status != "skipped"
    ):
        raise ValueError("Amazon media step status does not match observed media evidence.")
    if handler_status == "success" and expected_media_count > 0 and media_status != "success":
        raise ValueError("Amazon successful row did not complete media materialization.")
    if handler_status == "success" and (
        not isinstance(media_coverage, Mapping) or media_coverage.get("complete") is not True
    ):
        raise ValueError("Amazon successful row has incomplete media coverage.")
    if handler_status == "partial_success":
        degradation_observed = (
            outcome.context.payload.get("collection_status") == "partial_success"
            or not isinstance(media_coverage, Mapping)
            or media_coverage.get("complete") is not True
            or any(status == "partial_success" for status in step_statuses.values())
            or media_status == "failed"
        )
        if not degradation_observed:
            raise ValueError("Amazon partial row lacks a convergent degradation signal.")
    fact_refs = result.get("fact_refs")
    if not isinstance(fact_refs, Mapping) or not {
        "product_id",
        "snapshot_id",
        "binding_id",
        "raw_capture_ids",
        "normalized_capture_ref",
    }.issubset(fact_refs):
        raise ValueError("Amazon row persistence result lacks governed fact references.")
    writeback = result.get("writeback")
    if not isinstance(writeback, Mapping) or writeback != {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": [expected_source_record_id],
    }:
        raise ValueError("Amazon row persistence writeback did not converge.")
    observability = result.get("observability")
    if not isinstance(observability, Mapping) or observability.get("final_status") != row_status:
        raise ValueError("Amazon row persistence observability status is invalid.")


def _amazon_observability(
    value: Any,
    *,
    payload: Mapping[str, Any],
    row_status: Any,
    media_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    stage_durations = _amazon_stage_durations(
        payload.get("stage_durations_ms"),
        allowed=("navigation", "parse", "artifact"),
    )
    stage_durations.update(
        _amazon_stage_durations(
            raw.get("stage_durations_ms"),
            allowed=("media", "fact", "feishu"),
        )
    )
    raw_capture_refs = payload.get("raw_capture_refs")
    media_source_refs = payload.get("media_source_refs")
    observation: dict[str, Any] = {
        "stage_durations_ms": stage_durations,
        "field_coverage": _amazon_field_coverage(payload.get("field_coverage")),
        "artifact_count": len(raw_capture_refs) if isinstance(raw_capture_refs, list) else 0,
        "media_observed_count": (
            len(media_source_refs) if isinstance(media_source_refs, list) else 0
        ),
        "media_materialized_count": _native_nonnegative_int(media_coverage.get("materialized")),
    }
    if row_status in _AMAZON_ROW_STATUSES:
        observation["final_status"] = row_status
    error_code = _amazon_error_code("amazon_product_row_persist", raw.get("error_code"))
    if error_code:
        observation["error_code"] = error_code
    provider_name = _amazon_provider_code(payload.get("browser_provider_name"))
    if provider_name:
        observation["browser_provider_name"] = provider_name
    return observation


def _api_error_text(outcome: Any) -> str:
    if outcome.context.handler_code != "amazon_product_row_persist":
        return outcome.error_text
    _, error_code = _api_runtime_error_codes(outcome)
    suffix = f" ({error_code})" if error_code else ""
    return f"Amazon row persistence failed{suffix}."


def _storage_envelope_without_worker_result(
    outcome: Any,
    *,
    projected_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if outcome.context.handler_code == "amazon_product_browser_fetch":
        return _amazon_storage_envelope(outcome, projected_result or {})
    stored = outcome.storage_result()
    result_fields = set(outcome.worker_result.result)
    return {
        key: value
        for key, value in stored.items()
        if key in {"handler_result", "supervisor", "child_runner"} and key not in result_fields
    }


def _browser_storage_result(
    outcome: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    if outcome.context.handler_code != "amazon_product_browser_fetch":
        return outcome.storage_result(), None
    raw_result = outcome.worker_result.result
    safe_result = _project_amazon_browser_result(outcome, raw_result)
    normalized_ref = raw_result.get("normalized_capture_ref")
    safe_normalized: dict[str, Any] = {}
    if normalized_ref is not None:
        safe_normalized, _ = _validated_amazon_capture_ref(outcome, normalized_ref)
        safe_result["normalized_capture_ref"] = safe_normalized

    safe_lists: dict[str, list[dict[str, Any]]] = {}
    indexed_artifact_refs: list[dict[str, Any]] = []
    for field in ("raw_capture_refs", "artifact_refs"):
        if field not in raw_result:
            continue
        safe_refs, index_refs = _validated_amazon_capture_refs(
            outcome,
            raw_result[field],
        )
        safe_lists[field] = safe_refs
        safe_result[field] = safe_refs
        if field == "artifact_refs":
            indexed_artifact_refs = index_refs

    raw_refs = safe_lists.get("raw_capture_refs")
    artifact_refs = safe_lists.get("artifact_refs")
    if raw_refs is not None and artifact_refs is not None and raw_refs != artifact_refs:
        raise ValueError("Amazon artifact_refs must exactly match raw_capture_refs.")
    if safe_normalized and (
        raw_refs is None or sum(ref == safe_normalized for ref in raw_refs) != 1
    ):
        raise ValueError(
            "Amazon normalized_capture_ref must appear exactly once in raw_capture_refs."
        )
    _validate_amazon_browser_failure_evidence(
        outcome,
        safe_result,
        raw_refs=raw_refs,
        artifact_refs=artifact_refs,
    )
    _validate_amazon_browser_success_result(
        outcome,
        safe_result,
        normalized_ref=safe_normalized,
        raw_refs=raw_refs,
        artifact_refs=artifact_refs,
    )

    stored = _storage_envelope_without_worker_result(
        outcome,
        projected_result=safe_result,
    )
    stored.update(safe_result)
    return stored, indexed_artifact_refs


def _validate_amazon_browser_success_result(
    outcome: Any,
    result: Mapping[str, Any],
    *,
    normalized_ref: Mapping[str, Any],
    raw_refs: list[dict[str, Any]] | None,
    artifact_refs: list[dict[str, Any]] | None,
) -> None:
    handler_status = _amazon_effective_handler_status(outcome, result)
    if handler_status not in {"success", "partial_success"}:
        return
    expected_asin = _amazon_asin(outcome.context.payload.get("requested_asin"))
    required_fields = (
        "marketplace_code",
        "requested_asin",
        "resolved_asin",
        "canonical_url",
        "collection_status",
        "field_coverage",
        "browser_target_digest",
    )
    if any(field not in result for field in required_fields):
        raise ValueError("Amazon browser success result is missing required compact fields.")
    if (
        result.get("marketplace_code") != "US"
        or not expected_asin
        or result.get("requested_asin") != expected_asin
        or result.get("canonical_url") != f"https://www.amazon.com/dp/{expected_asin}"
    ):
        raise ValueError("Amazon browser success result identity is invalid.")
    collection_status = result.get("collection_status")
    resolved_asin = result.get("resolved_asin")
    parent_redirect = (
        resolved_asin != expected_asin
        and result.get("parent_asin") == expected_asin
        and collection_status == "partial_success"
    )
    if resolved_asin != expected_asin and not parent_redirect:
        raise ValueError("Amazon browser resolved ASIN is unrelated to the request.")
    if handler_status == "partial_success":
        valid_status = collection_status == "partial_success"
    else:
        valid_status = collection_status in {"success", "unavailable"}
    if not valid_status:
        raise ValueError("Amazon handler and collection statuses do not converge.")
    if (
        not normalized_ref
        or raw_refs != [normalized_ref]
        or artifact_refs != raw_refs
    ):
        raise ValueError("Amazon browser success result lacks governed capture evidence.")


def _validate_amazon_browser_failure_evidence(
    outcome: Any,
    result: Mapping[str, Any],
    *,
    raw_refs: list[dict[str, Any]] | None,
    artifact_refs: list[dict[str, Any]] | None,
) -> None:
    if _amazon_effective_handler_status(outcome, result) in {
        "success",
        "partial_success",
    }:
        return
    _, error_code = _browser_runtime_error_codes(outcome)
    requires_evidence = result.get("collection_status") == "blocked" or error_code in {
        "access_blocked",
        "captcha_required",
    }
    if not requires_evidence:
        if raw_refs or artifact_refs:
            raise ValueError(
                "Amazon non-blocked browser failure must not persist capture evidence."
            )
        return
    if (
        not raw_refs
        or len(raw_refs) != 1
        or artifact_refs != raw_refs
        or raw_refs[0]["capture_kind"] != "screenshot"
    ):
        raise ValueError("Amazon blocked browser result lacks governed screenshot evidence.")


def _browser_storage_summary(
    outcome: Any,
    stored_result: Mapping[str, Any],
) -> dict[str, Any]:
    if outcome.context.handler_code != "amazon_product_browser_fetch":
        return outcome.storage_summary()
    summary: dict[str, Any] = {
        "handler_status": _amazon_effective_handler_status(outcome, stored_result),
        "supervisor_status": _amazon_supervisor_status(outcome.supervisor_status),
        "heartbeat_count": _native_nonnegative_int(outcome.heartbeat_count),
        "execution_mode": _amazon_execution_mode(outcome.execution_mode),
    }
    for field in (
        "marketplace_code",
        "requested_asin",
        "resolved_asin",
        "collection_status",
        "browser_target_digest",
        "browser_provider_name",
        "field_coverage",
        "stage_durations_ms",
    ):
        if field in stored_result:
            summary[field] = stored_result[field]
    refs = stored_result.get("artifact_refs")
    if isinstance(refs, list):
        summary["artifact_count"] = len(refs)
    progress_stage = _amazon_progress_stage(
        outcome.context.handler_code,
        outcome.progress_stage,
    )
    if progress_stage:
        summary["progress_stage"] = progress_stage
    error = outcome.error
    if error is not None:
        error_type, error_code = _browser_runtime_error_codes(outcome)
        summary.update(
            {
                "error_type": error_type,
                "error_code": error_code,
                "retryable": bool(error.retryable),
                "terminal_error": bool(error.terminal),
            }
        )
    elif outcome.worker_result.error is not None:
        error_type, error_code = _browser_runtime_error_codes(outcome)
        summary.update(
            {
                "error_type": error_type,
                "error_code": error_code,
                "retryable": bool(outcome.worker_result.error.retryable),
                "terminal_error": not bool(outcome.worker_result.error.retryable),
            }
        )
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _amazon_storage_envelope(
    outcome: Any,
    projected_result: Mapping[str, Any],
) -> dict[str, Any]:
    handler_result: dict[str, Any] = {
        "status": _amazon_effective_handler_status(outcome, projected_result),
        "handler_code": "amazon_product_browser_fetch",
        "request_id": _amazon_identifier(outcome.context.request_id),
        "job_id": _amazon_identifier(outcome.context.job_id),
        "contract_revision": _amazon_contract_revision(outcome.worker_result.contract_revision),
    }
    supervisor: dict[str, Any] = {
        "supervisor_status": _amazon_supervisor_status(outcome.supervisor_status),
        "execution_mode": _amazon_execution_mode(outcome.execution_mode),
        "worker_type": "browser_worker",
        "runtime_table": "task_execution",
        "request_id": _amazon_identifier(outcome.context.request_id),
        "job_id": _amazon_identifier(outcome.context.job_id),
        "handler_code": "amazon_product_browser_fetch",
        "started_at": _finite_nonnegative_number(outcome.started_at),
        "finished_at": _finite_nonnegative_number(outcome.finished_at),
        "duration_seconds": _finite_nonnegative_number(outcome.duration_seconds),
        "heartbeat_count": _native_nonnegative_int(outcome.heartbeat_count),
        "progress_stage": _amazon_progress_stage(
            outcome.context.handler_code,
            outcome.progress_stage,
        ),
        "failure_disposition": _amazon_failure_disposition(outcome.failure_disposition),
    }
    error = outcome.error
    if error is not None:
        error_type, error_code = _browser_runtime_error_codes(outcome)
        supervisor["error"] = {
            "error_type": error_type,
            "error_code": error_code,
            "retryable": bool(error.retryable),
            "terminal": bool(error.terminal),
        }
    return {
        "handler_result": {
            key: value for key, value in handler_result.items() if value not in (None, "")
        },
        "supervisor": {key: value for key, value in supervisor.items() if value not in (None, "")},
    }


def _project_amazon_browser_result(
    outcome: Any,
    raw_result: Mapping[str, Any],
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    expected_asin = _amazon_asin(outcome.context.payload.get("requested_asin"))
    if not expected_asin:
        raise ValueError("Amazon browser request identity is invalid.")
    requested_asin = _amazon_asin(raw_result.get("requested_asin"))
    resolved_asin = _amazon_asin(raw_result.get("resolved_asin"))
    parent_asin = _amazon_asin(raw_result.get("parent_asin"))
    collection_status = raw_result.get("collection_status")
    if "requested_asin" in raw_result and requested_asin != expected_asin:
        raise ValueError("Amazon browser requested ASIN does not match its runtime context.")
    if "resolved_asin" in raw_result and (
        not resolved_asin
        or (
            resolved_asin != expected_asin
            and not (
                parent_asin == expected_asin
                and collection_status == "partial_success"
            )
        )
    ):
        raise ValueError("Amazon browser resolved ASIN is unrelated to the request.")
    if "parent_asin" in raw_result and not parent_asin:
        raise ValueError("Amazon browser parent ASIN is invalid.")
    if raw_result.get("marketplace_code") == "US":
        safe["marketplace_code"] = "US"
    if requested_asin:
        safe["requested_asin"] = requested_asin
    if resolved_asin:
        safe["resolved_asin"] = resolved_asin
    if parent_asin:
        safe["parent_asin"] = parent_asin
    canonical_url = raw_result.get("canonical_url")
    expected_canonical_url = f"https://www.amazon.com/dp/{expected_asin}"
    if "canonical_url" in raw_result:
        if canonical_url != expected_canonical_url:
            raise ValueError("Amazon browser canonical URL does not match the request.")
        safe["canonical_url"] = expected_canonical_url
    if collection_status in _AMAZON_BROWSER_COLLECTION_STATUSES:
        safe["collection_status"] = collection_status
    field_coverage = _amazon_field_coverage(raw_result.get("field_coverage"))
    if field_coverage:
        safe["field_coverage"] = field_coverage
    media_refs, media_removed = _amazon_media_source_refs(
        outcome,
        raw_result.get("media_source_refs"),
    )
    if "media_source_refs" in raw_result:
        safe["media_source_refs"] = media_refs
    if media_removed and safe.get("collection_status") == "success":
        safe["collection_status"] = "partial_success"
    target_digest = raw_result.get("browser_target_digest")
    expected_target_digest = _amazon_browser_target_digest(outcome.context.resource_code)
    if not expected_target_digest or target_digest != expected_target_digest:
        raise ValueError("Amazon browser target digest does not match its resource lane.")
    safe["browser_target_digest"] = expected_target_digest
    provider_name = _amazon_provider_code(raw_result.get("browser_provider_name"))
    if provider_name:
        safe["browser_provider_name"] = provider_name
    stage_durations = _amazon_stage_durations(
        raw_result.get("stage_durations_ms"),
        allowed=("navigation", "parse", "artifact"),
    )
    if stage_durations:
        safe["stage_durations_ms"] = stage_durations
    return safe


def _amazon_media_source_refs(
    outcome: Any,
    value: Any,
) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], value is not None
    if len(value) > 100:
        raise ValueError("Amazon media_source_refs must be a bounded list.")
    expected_asin = _amazon_asin(outcome.context.payload.get("requested_asin"))
    refs: list[dict[str, Any]] = []
    removed = False
    coordinates: set[tuple[str, int]] = set()
    for item in value:
        raw = item if isinstance(item, Mapping) else {}
        source_url = normalize_amazon_media_url(raw.get("source_url"))
        media_role = raw.get("media_role")
        position = raw.get("position")
        valid_position = (
            type(position) is int
            and position >= 0
            and not (media_role == "main_image" and position != 0)
        )
        if (
            not source_url
            or raw.get("source_platform") != "amazon"
            or raw.get("marketplace_code") != "US"
            or _amazon_asin(raw.get("product_id")) != expected_asin
            or media_role not in {"main_image", "gallery_image"}
            or not valid_position
        ):
            removed = True
            continue
        coordinate = (media_role, position)
        if coordinate in coordinates:
            raise ValueError("Amazon media_source_refs contain a duplicate role/position.")
        coordinates.add(coordinate)
        refs.append(
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": expected_asin,
                "media_role": media_role,
                "position": position,
            }
        )
    return refs, removed


def _amazon_field_coverage(value: Any) -> dict[str, int | float]:
    raw = value if isinstance(value, Mapping) else {}
    total = _optional_native_nonnegative_int(raw.get("total"))
    if total is None:
        return {}
    observed = min(_native_nonnegative_int(raw.get("observed")), total)
    explicitly_unavailable = min(
        _native_nonnegative_int(raw.get("explicitly_unavailable")),
        max(total - observed, 0),
    )
    missing = max(total - observed - explicitly_unavailable, 0)
    covered = observed + explicitly_unavailable
    return {
        "total": total,
        "observed": observed,
        "explicitly_unavailable": explicitly_unavailable,
        "missing": missing,
        "percentage": round((covered / total) * 100.0, 2) if total else 0.0,
    }


def _amazon_stage_durations(
    value: Any,
    *,
    allowed: tuple[str, ...],
) -> dict[str, float]:
    raw = value if isinstance(value, Mapping) else {}
    durations: dict[str, float] = {}
    for stage_name in allowed:
        duration = raw.get(stage_name)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            continue
        normalized = float(duration)
        if math.isfinite(normalized) and normalized >= 0:
            durations[stage_name] = round(normalized, 3)
    return durations


def _amazon_asin(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    asin = value.strip().upper()
    return asin if _AMAZON_ASIN.fullmatch(asin) else ""


def _amazon_identifier(value: Any) -> str:
    return value if isinstance(value, str) and _AMAZON_IDENTIFIER.fullmatch(value) else ""


def _amazon_provider_code(value: Any) -> str:
    return value if value in _AMAZON_PROVIDER_CODES else ""


def _amazon_supervisor_status(value: Any) -> str:
    return value if value in _AMAZON_SUPERVISOR_STATUSES else "completed"


def _amazon_execution_mode(value: Any) -> str:
    return value if value in _AMAZON_EXECUTION_MODES else "inline"


def _amazon_failure_disposition(value: Any) -> str:
    return value if value in _AMAZON_FAILURE_DISPOSITIONS else "none"


def _amazon_progress_stage(handler_code: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    allowed = _AMAZON_PROGRESS_STAGES.get(handler_code, frozenset())
    return value if value in allowed else "handler_progress"


def _amazon_error_type(handler_code: str, value: Any) -> str:
    allowed = (
        _AMAZON_BROWSER_ERROR_TYPES
        if handler_code == "amazon_product_browser_fetch"
        else _AMAZON_PERSIST_ERROR_TYPES
    )
    return value if value in allowed else ""


def _amazon_error_code(handler_code: str, value: Any) -> str:
    allowed = (
        _AMAZON_BROWSER_ERROR_CODES
        if handler_code == "amazon_product_browser_fetch"
        else _AMAZON_PERSIST_ERROR_CODES
    )
    return value if value in allowed else ""


def _amazon_browser_target_digest(resource_code: Any) -> str:
    if not isinstance(resource_code, str) or not resource_code.startswith("browser:amazon:"):
        return ""
    digest = resource_code.removeprefix("browser:amazon:")
    return digest if _AMAZON_DIGEST.fullmatch(digest) else ""


def _amazon_contract_revision(value: Any) -> str:
    return "runtime_contract"


def _runtime_progress_stage(handler_code: str, value: Any) -> str:
    if handler_code in {"amazon_product_browser_fetch", "amazon_product_row_persist"}:
        return _amazon_progress_stage(handler_code, value) or "handler_progress"
    return str(value or "")


def _runtime_progress_message(handler_code: str, value: Any) -> str:
    if handler_code == "amazon_product_browser_fetch":
        return "Amazon browser collection progress updated."
    if handler_code == "amazon_product_row_persist":
        return "Amazon row persistence progress updated."
    return str(value or "")


def _amazon_runtime_response_projection(
    *,
    handler_code: str,
    summary: Any,
    result: Any,
    error_type: Any,
    error_code: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    safe_summary = dict(summary) if isinstance(summary, Mapping) else {}
    safe_result = dict(result) if isinstance(result, Mapping) else {}
    handler_value = safe_result.get("handler_result")
    handler = dict(handler_value) if isinstance(handler_value, Mapping) else {}
    supervisor_value = safe_result.get("supervisor")
    supervisor = dict(supervisor_value) if isinstance(supervisor_value, Mapping) else {}
    compact_result = {
        key: value
        for key, value in safe_result.items()
        if key not in {"handler_result", "supervisor"}
    }
    worker_result: dict[str, Any] = {
        "status": _amazon_handler_status(handler.get("status")),
        "handler_code": handler_code,
        "request_id": str(handler.get("request_id") or ""),
        "job_id": str(handler.get("job_id") or ""),
        "summary": safe_summary,
        "result": compact_result,
        "warnings": [],
        "next_action": {"type": "none", "payload": {}},
        "contract_revision": _amazon_contract_revision(handler.get("contract_revision")),
    }
    safe_error_type = _amazon_error_type(handler_code, error_type)
    safe_error_code = _amazon_error_code(handler_code, error_code)
    if safe_error_type or safe_error_code:
        if handler_code == "amazon_product_browser_fetch":
            safe_error_type = safe_error_type or "amazon_browser_failure"
            safe_error_code = safe_error_code or "amazon_browser_collection_failed"
            message = "Amazon browser collection failed."
        else:
            safe_error_type = safe_error_type or "amazon_row_persistence_failure"
            safe_error_code = safe_error_code or "amazon_row_persistence_failed"
            message = "Amazon row persistence failed."
        supervisor_error = supervisor.get("error")
        retryable = (
            bool(supervisor_error.get("retryable"))
            if isinstance(supervisor_error, Mapping)
            else bool(safe_summary.get("retryable"))
        )
        worker_result["error"] = {
            "error_type": safe_error_type,
            "error_code": safe_error_code,
            "message": message,
            "retryable": retryable,
            "fallback_allowed": False,
            "fallback_reason": "",
            "details": {},
        }
        supervisor["error"] = {
            "error_type": safe_error_type,
            "error_code": safe_error_code,
            "retryable": retryable,
            "terminal": bool(safe_summary.get("terminal_error")),
        }
    return worker_result, supervisor


def _amazon_runtime_response_error_payload(worker_result: Mapping[str, Any]) -> dict[str, Any]:
    error = worker_result.get("error")
    if not isinstance(error, Mapping):
        return {}
    return {
        "worker_error": str(error.get("message") or "Amazon worker failed."),
        "error_type": str(error.get("error_type") or ""),
        "error_code": str(error.get("error_code") or ""),
        "retryable": bool(error.get("retryable")),
        "terminal_error": not bool(error.get("retryable")),
    }


def _amazon_failure_codes(
    outcome: Any,
    *,
    fallback_type: str,
    fallback_code: str,
) -> tuple[str, str]:
    error = outcome.error or outcome.worker_result.error
    return (
        _amazon_error_type(outcome.context.handler_code, error.error_type)
        if error is not None
        else ""
    ) or fallback_type, (
        _amazon_error_code(outcome.context.handler_code, error.error_code)
        if error is not None
        else ""
    ) or fallback_code


def _api_runtime_error_codes(outcome: Any) -> tuple[str, str]:
    if outcome.context.handler_code == "amazon_product_row_persist":
        return _amazon_failure_codes(
            outcome,
            fallback_type="amazon_row_persistence_failure",
            fallback_code="amazon_row_persistence_failed",
        )
    error = outcome.error
    return (
        error.error_type if error is not None else "",
        error.error_code if error is not None else "",
    )


def _browser_runtime_error_codes(outcome: Any) -> tuple[str, str]:
    if outcome.context.handler_code == "amazon_product_browser_fetch":
        return _amazon_failure_codes(
            outcome,
            fallback_type="amazon_browser_failure",
            fallback_code="amazon_browser_collection_failed",
        )
    error = outcome.error
    return (
        error.error_type if error is not None else "",
        error.error_code if error is not None else "",
    )


def _amazon_handler_status(value: Any) -> str:
    return value if value in _AMAZON_STEP_STATUSES else "failed"


def _amazon_effective_handler_status(
    outcome: Any,
    projected_result: Mapping[str, Any],
) -> str:
    status = _amazon_handler_status(outcome.worker_result.status)
    if status == "success" and projected_result.get("collection_status") == "partial_success":
        return "partial_success"
    return status


def _native_nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _optional_native_nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _finite_nonnegative_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else 0.0


def _browser_error_text(outcome: Any) -> str:
    if outcome.context.handler_code != "amazon_product_browser_fetch":
        return outcome.error_text
    _, error_code = _browser_runtime_error_codes(outcome)
    suffix = f" ({error_code})" if error_code else ""
    return f"Amazon browser collection failed{suffix}."


def _validated_amazon_capture_refs(
    outcome: Any,
    value: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > len(_AMAZON_CAPTURE_POLICIES):
        raise ValueError("Amazon capture refs must be a bounded list.")
    safe_refs: list[dict[str, Any]] = []
    index_refs: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_coordinates: set[tuple[str, str]] = set()
    for item in value:
        safe_ref, index_ref = _validated_amazon_capture_ref(outcome, item)
        coordinate = (safe_ref["bucket"], safe_ref["object_key"])
        if safe_ref["capture_kind"] in seen_kinds or coordinate in seen_coordinates:
            raise ValueError("Amazon capture refs must use unique kinds and coordinates.")
        seen_kinds.add(safe_ref["capture_kind"])
        seen_coordinates.add(coordinate)
        safe_refs.append(safe_ref)
        index_refs.append(index_ref)
    return safe_refs, index_refs


def _validated_amazon_capture_ref(
    outcome: Any,
    value: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("Amazon capture refs must contain objects.")
    payload = outcome.context.payload
    requested_asin = str(payload.get("requested_asin") or "").strip().upper()
    stable_run_id = str(payload.get("run_id") or "").strip()
    expected_bucket = str(payload.get("artifact_bucket") or "").strip()
    prefix_value = payload.get("artifact_object_prefix")
    if not isinstance(prefix_value, str):
        raise ValueError("Amazon artifact_object_prefix snapshot is missing.")
    expected_prefix = prefix_value.strip("/")
    prefix_parts = expected_prefix.split("/") if expected_prefix else []
    if (
        not _AMAZON_IDENTIFIER.fullmatch(outcome.context.request_id)
        or not _AMAZON_IDENTIFIER.fullmatch(outcome.context.job_id)
        or not _AMAZON_ASIN.fullmatch(requested_asin)
        or not _AMAZON_DIGEST.fullmatch(stable_run_id)
        or not _AMAZON_BUCKET.fullmatch(expected_bucket)
        or prefix_value != expected_prefix
        or any(part in {"", ".", "..", "raw-captures"} for part in prefix_parts)
    ):
        raise ValueError("Amazon capture coordinate policy is invalid.")

    capture_kind = str(value.get("capture_kind") or "").strip()
    policy = _AMAZON_CAPTURE_POLICIES.get(capture_kind)
    content_digest = str(value.get("content_digest") or "").strip().lower()
    object_key = str(value.get("object_key") or "").strip()
    if (
        policy is None
        or value.get("bucket") != expected_bucket
        or value.get("request_id") != outcome.context.request_id
        or value.get("execution_id") != outcome.context.job_id
        or value.get("run_id") != stable_run_id
        or value.get("content_type") != policy[0]
        or value.get("sanitization_status") != policy[1]
        or not _AMAZON_DIGEST.fullmatch(content_digest)
        or not _AMAZON_OBJECT_PATH.fullmatch(object_key)
        or "%" in object_key
        or "\\" in object_key
    ):
        raise ValueError("Amazon capture ref provenance is invalid.")

    collected_at = _amazon_capture_timestamp(value.get("collected_at"))
    expected_parts = [
        *prefix_parts,
        "raw-captures",
        "amazon",
        "us",
        requested_asin,
        f"{collected_at.year:04d}",
        f"{collected_at.month:02d}",
        f"{collected_at.day:02d}",
        stable_run_id,
        content_digest,
        policy[2],
    ]
    if object_key.split("/") != expected_parts:
        raise ValueError("Amazon capture object_key is outside its bound coordinate.")

    safe_ref = {field: value[field] for field in _AMAZON_CAPTURE_RUNTIME_FIELDS if field in value}
    safe_ref["content_digest"] = content_digest
    safe_ref["collected_at"] = collected_at.isoformat().replace("+00:00", "Z")
    created_at_value = value.get("created_at")
    if created_at_value is not None:
        created_at = _amazon_capture_timestamp(created_at_value)
        safe_ref["created_at"] = created_at.isoformat().replace("+00:00", "Z")
    index_ref = {
        **safe_ref,
        "etag": "",
        "size": (value["size"] if type(value.get("size")) is int and value["size"] >= 0 else 0),
        "created_at_epoch": collected_at.timestamp(),
    }
    return safe_ref, index_ref


def _amazon_capture_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Amazon capture ref requires a collected_at timestamp.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Amazon capture timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Amazon capture timestamp must include a timezone.")
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Amazon capture timestamp is outside the supported range.") from exc


def _is_terminal_amazon_browser_failure(outcome: Any) -> bool:
    if outcome.context.handler_code != "amazon_product_browser_fetch":
        return False
    if outcome.error is not None:
        return outcome.error.terminal
    handler_error = outcome.worker_result.error
    return handler_error is not None and not handler_error.retryable


def _amazon_artifact_records(
    outcome: Any,
    artifact_refs: list[dict[str, Any]] | None,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    if not artifact_refs:
        return "", ()
    artifact_run_id = str(outcome.context.payload.get("run_id") or "").strip()
    if not artifact_run_id:
        raise ValueError("Amazon browser artifacts require a stable capture run_id.")
    records: list[dict[str, Any]] = []
    for ref in artifact_refs:
        if not isinstance(ref, dict):
            raise ValueError("Amazon browser artifact_refs must contain objects.")
        bucket = str(ref.get("bucket") or "").strip()
        object_key = str(ref.get("object_key") or "").strip()
        kind = str(ref.get("capture_kind") or "").strip()
        if not bucket or not object_key or not kind:
            raise ValueError("Amazon browser artifact ref is missing bucket, object_key, or kind.")
        seed = f"{artifact_run_id}:{bucket}:{object_key}".encode("utf-8")
        try:
            size = max(int(ref.get("size") or 0), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Amazon browser artifact size must be an integer.") from exc
        try:
            created_at = float(ref.get("created_at_epoch") or outcome.finished_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("Amazon browser artifact created_at_epoch is invalid.") from exc
        records.append(
            {
                "artifact_id": hashlib.sha256(seed).hexdigest(),
                "request_id": outcome.context.request_id,
                "execution_id": outcome.context.job_id,
                "run_id": artifact_run_id,
                "step_id": outcome.context.stage_code or outcome.context.handler_code,
                "kind": kind,
                "bucket": bucket,
                "object_key": object_key,
                "etag": "",
                "size": size,
                "content_type": str(ref.get("content_type") or "").strip(),
                "source_path": "",
                "metadata": {
                    "content_digest": str(ref.get("content_digest") or "").strip(),
                    "sanitization_status": str(ref.get("sanitization_status") or "").strip(),
                    "remote_uri": str(ref.get("remote_uri") or "").strip(),
                },
                "created_at": created_at,
            }
        )
    return artifact_run_id, tuple(records)


class AmazonRuntimeResultProjection:
    def project_storage(self, outcome: Any) -> RuntimeStorageProjection:
        handler_code = outcome.context.handler_code
        if handler_code == "amazon_product_row_persist":
            summary, result = _api_storage_payload(outcome)
            return RuntimeStorageProjection(summary=summary, result=result)
        if handler_code != "amazon_product_browser_fetch":
            raise ValueError(f"Unsupported Amazon runtime handler: {handler_code}")
        if outcome.worker_result.status not in {"success", "partial_success", "failed"}:
            raise ValueError("Amazon browser fetch returned an unsupported handler status.")
        result, artifact_refs = _browser_storage_result(outcome)
        summary = _browser_storage_summary(outcome, result)
        artifact_run_id, artifact_records = _amazon_artifact_records(
            outcome,
            artifact_refs,
        )
        return RuntimeStorageProjection(
            summary=summary,
            result=result,
            artifact_run_id=artifact_run_id,
            artifact_records=artifact_records,
        )

    def projection_failure(
        self,
        outcome: Any,
        error: Exception,
        *,
        phase: str,
    ) -> RuntimeFailureProjection:
        if phase == "artifact_index":
            projected = self.project_storage(outcome)
            summary = dict(projected.summary)
            summary.update(
                {
                    "error_type": "runtime_artifact_index_failure",
                    "error_code": "artifact_index_failed",
                    "retryable": True,
                    "terminal_error": False,
                }
            )
            return RuntimeFailureProjection(
                summary=summary,
                result=projected.result,
                error_text="Amazon artifact index update failed.",
                error_type="runtime_artifact_index_failure",
                error_code="artifact_index_failed",
            )
        if phase != "validation":
            raise ValueError(f"Unsupported Amazon projection failure phase: {phase}")
        if outcome.context.handler_code == "amazon_product_row_persist":
            summary = {
                "handler_status": "failed",
                "supervisor_status": _amazon_supervisor_status(outcome.supervisor_status),
                "error_type": "runtime_result_validation_failure",
                "error_code": "invalid_handler_result",
                "retryable": False,
                "terminal_error": True,
            }
            result = _amazon_api_storage_envelope(outcome)
            result["handler_result"]["status"] = "failed"
            return RuntimeFailureProjection(
                summary=summary,
                result=result,
                error_text=("Amazon row persistence returned an invalid compact result."),
                error_type="runtime_result_validation_failure",
                error_code="invalid_handler_result",
                dead_letter_reason="invalid_handler_result",
                force_terminal=True,
                terminal=True,
            )
        summary = _browser_storage_summary(outcome, {})
        summary.update(
            {
                "error_type": "runtime_artifact_validation_failure",
                "error_code": "artifact_validation_failed",
                "retryable": False,
                "terminal_error": True,
            }
        )
        return RuntimeFailureProjection(
            summary=summary,
            result=_storage_envelope_without_worker_result(outcome),
            error_text="Amazon browser result could not be safely projected.",
            error_type="runtime_artifact_validation_failure",
            error_code="artifact_validation_failed",
            dead_letter_reason="invalid_handler_result",
            force_terminal=True,
            terminal=True,
        )

    def failure_policy(self, outcome: Any) -> RuntimeFailureProjection:
        projected = self.project_storage(outcome)
        if outcome.context.handler_code == "amazon_product_row_persist":
            error_type, error_code = _api_runtime_error_codes(outcome)
            supervisor_terminal = bool(
                outcome.error is not None and outcome.error.terminal
            )
            handler_error = outcome.worker_result.error
            handler_terminal = bool(
                handler_error is not None and not handler_error.retryable
            )
            terminal = supervisor_terminal or handler_terminal
            return RuntimeFailureProjection(
                summary=projected.summary,
                result=projected.result,
                error_text=_api_error_text(outcome),
                error_type=error_type,
                error_code=error_code,
                dead_letter_reason=(
                    "supervisor_failed"
                    if supervisor_terminal
                    else "terminal_handler_failure" if handler_terminal else ""
                ),
                force_terminal=terminal,
                terminal=terminal,
            )
        error_type, error_code = _browser_runtime_error_codes(outcome)
        terminal = _is_terminal_amazon_browser_failure(outcome)
        return RuntimeFailureProjection(
            summary=projected.summary,
            result=projected.result,
            error_text=_browser_error_text(outcome),
            error_type=error_type,
            error_code=error_code,
            dead_letter_reason=(
                "terminal_handler_failure"
                if terminal
                else (
                    "supervisor_failed"
                    if outcome.error is not None and outcome.error.terminal
                    else ""
                )
            ),
            terminal=terminal,
        )

    def project_progress(
        self,
        handler_code: str,
        progress_stage: Any,
        message: Any,
    ) -> tuple[str, str]:
        return (
            _runtime_progress_stage(handler_code, progress_stage),
            _runtime_progress_message(handler_code, message),
        )

    def project_response(
        self,
        handler_code: str,
        summary: Any,
        result: Any,
        error_type: Any,
        error_code: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        worker_result, supervisor = _amazon_runtime_response_projection(
            handler_code=handler_code,
            summary=summary,
            result=result,
            error_type=error_type,
            error_code=error_code,
        )
        return (
            worker_result,
            supervisor,
            _amazon_runtime_response_error_payload(worker_result),
        )


AMAZON_RUNTIME_RESULT_PROJECTION = AmazonRuntimeResultProjection()


__all__ = [
    "AMAZON_RUNTIME_RESULT_PROJECTION",
    "AmazonRuntimeResultProjection",
]
