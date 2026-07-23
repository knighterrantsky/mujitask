from __future__ import annotations

import math
import re
from collections.abc import Mapping
from time import perf_counter
from typing import Any

from automation_business_scaffold.capabilities.browser.amazon.product_page import (
    InvalidASINError,
    normalize_amazon_media_url,
    normalize_asin,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    failed_result,
    partial_success_result,
    success_result,
)
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    build_projection_write_payload,
)
from automation_business_scaffold.domains.amazon.projections.feishu_product_projection import (
    AMAZON_PRODUCT_FEISHU_WRITE_FIELDS,
)


_MATERIALIZED_MEDIA_STATES = {"uploaded", "reused", "reused_in_run"}
_COLLECTION_STATUSES = {"success", "partial_success", "unavailable"}
_MEDIA_ROLES = {"main_image", "gallery_image"}
_MAX_AMAZON_MEDIA_DOWNLOAD_BYTES = 25 * 1024 * 1024
_MEDIA_SOURCE_REF_FIELDS = {
    "source_url",
    "source_platform",
    "marketplace_code",
    "product_id",
    "media_role",
    "position",
}
_BROWSER_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BROWSER_STAGE_NAMES = ("navigation", "parse", "artifact")


def run_amazon_product_row_persist_flow(context: HandlerContext) -> HandlerResult:
    try:
        inputs = _validate_inputs(context.payload)
    except (InvalidASINError, TypeError, ValueError) as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="invalid_amazon_persist_payload",
                message=str(exc),
                retryable=False,
            ),
            summary={"row_status": "failed"},
        )

    step_statuses: dict[str, str] = {}
    warnings: list[str] = []
    media_result: HandlerResult | None = None
    materialized_assets: list[dict[str, Any]] = []
    media_failed = False

    if inputs["media_source_refs"]:
        media_context = _child_context(
            context,
            handler_code="media_asset_sync",
            step_code="media_asset_sync",
            payload={
                "asset_refs": inputs["media_source_refs"],
                "product_id": inputs["requested_asin"],
                "source_platform": "amazon",
                "marketplace_code": "US",
                "run_id": inputs["run_id"],
                "sync_referenced_files": True,
                "require_object_storage": True,
                "require_materialized_assets": True,
                "media_download_max_bytes": _MAX_AMAZON_MEDIA_DOWNLOAD_BYTES,
                "media_download_allowed_host_suffixes": [
                    "media-amazon.com",
                    "ssl-images-amazon.com",
                ],
            },
        )
        media_started_at = perf_counter()
        try:
            media_result = api_handler_callable("media_asset_sync")(media_context)
        except Exception:  # noqa: BLE001 - preserve product facts across media transport failures.
            media_result = HandlerResult.failed(
                media_context,
                error=build_error(
                    error_type="media_sync_failed",
                    error_code="media_asset_materialization_failed",
                    message="Amazon media materialization raised an unexpected transport error.",
                    retryable=True,
                ),
            )
        inputs["stage_durations_ms"]["media"] = _elapsed_ms(media_started_at)
        step_statuses["media_asset_sync"] = media_result.status
        materialized_assets = _materialized_assets(media_result.result.get("synced_assets"))
        inputs["media_materialized_count"] = len(materialized_assets)
        if media_result.status == "failed":
            if not _is_partial_media_failure(media_result.error):
                return _failed_child_result(
                    context,
                    inputs=inputs,
                    step_statuses=step_statuses,
                    failed_step="media_asset_sync",
                    child_error=media_result.error,
                )
            media_failed = True
            warnings.append("Some Amazon product media could not be materialized.")
        elif media_result.status not in {"success", "partial_success", "skipped"}:
            return _invalid_child_status(
                context,
                inputs=inputs,
                step_statuses=step_statuses,
                failed_step="media_asset_sync",
                child_status=media_result.status,
            )
    else:
        step_statuses["media_asset_sync"] = "skipped"

    fact_context = _child_context(
        context,
        handler_code="amazon_product_fact_upsert",
        step_code="amazon_product_fact_upsert",
        payload={
            "source_record_id": inputs["source_record_id"],
            "source_table_ref": inputs["source_table_identity"],
            "requested_asin": inputs["requested_asin"],
            "run_id": inputs["run_id"],
            "normalized_capture_ref": inputs["normalized_capture_ref"],
            "raw_capture_refs": inputs["raw_capture_refs"],
            "materialized_media_assets": materialized_assets,
        },
        metadata={"include_transient_projection_facts": True},
    )
    fact_started_at = perf_counter()
    fact_result = api_handler_callable("amazon_product_fact_upsert")(fact_context)
    inputs["stage_durations_ms"]["fact"] = _elapsed_ms(fact_started_at)
    step_statuses["amazon_product_fact_upsert"] = fact_result.status
    if fact_result.status == "failed":
        return _failed_child_result(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="amazon_product_fact_upsert",
            child_error=fact_result.error,
        )
    if fact_result.status not in {"success", "partial_success"}:
        return _invalid_child_status(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="amazon_product_fact_upsert",
            child_status=fact_result.status,
        )

    try:
        fact_refs = _fact_refs(
            fact_result.result,
            expected_normalized_capture_ref=inputs["normalized_capture_ref"],
        )
    except ValueError as exc:
        return _failed_child_result(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="amazon_product_fact_upsert",
            child_error=build_error(
                error_type="contract_error",
                error_code="amazon_fact_reference_mismatch",
                message=str(exc),
                retryable=False,
            ),
        )
    media_coverage = _media_coverage(
        fact_result,
        expected_count=len(inputs["media_source_refs"]),
        materialized_count=len(materialized_assets),
    )
    projection_facts = _mapping(fact_result.result.get("projection_facts"))
    if not projection_facts:
        return failed_result(
            context,
            error=build_error(
                error_type="contract_error",
                error_code="amazon_projection_facts_missing",
                message="Amazon fact persistence did not return transient projection facts.",
                retryable=False,
            ),
            summary=_summary(
                inputs,
                "failed",
                step_statuses,
                media_coverage,
                error_code="amazon_projection_facts_missing",
            ),
            result=_compact_result(
                inputs,
                row_status="failed",
                step_statuses=step_statuses,
                fact_refs=fact_refs,
                media_coverage=media_coverage,
                error_code="amazon_projection_facts_missing",
            ),
        )
    if (
        projection_facts.get("source_record_id") != inputs["source_record_id"]
        or projection_facts.get("requested_asin") != inputs["requested_asin"]
    ):
        return failed_result(
            context,
            error=build_error(
                error_type="contract_error",
                error_code="amazon_projection_identity_mismatch",
                message="Amazon projection facts do not match the source row identity.",
                retryable=False,
            ),
            summary=_summary(
                inputs,
                "failed",
                step_statuses,
                media_coverage,
                error_code="amazon_projection_identity_mismatch",
            ),
            result=_compact_result(
                inputs,
                row_status="failed",
                step_statuses=step_statuses,
                fact_refs=fact_refs,
                media_coverage=media_coverage,
                error_code="amazon_projection_identity_mismatch",
            ),
        )

    if media_failed and _has_retry_remaining(context):
        return failed_result(
            context,
            error=build_error(
                error_type="media_sync_failed",
                error_code="media_sync_failed",
                message="Amazon media materialization is incomplete; retrying the same run.",
                retryable=True,
                details={"media_coverage": media_coverage},
            ),
            summary=_summary(
                inputs,
                "failed",
                step_statuses,
                media_coverage,
                error_code="media_sync_failed",
            ),
            result=_compact_result(
                inputs,
                row_status="failed",
                step_statuses=step_statuses,
                fact_refs=fact_refs,
                media_coverage=media_coverage,
                error_code="media_sync_failed",
            ),
            warnings=warnings,
        )

    row_status = _row_status(
        projection_facts.get("collection_status"),
        fact_status=fact_result.status,
        media_failed=media_failed,
        media_coverage=media_coverage,
    )
    if row_status == "partial_success":
        projection_facts = {**projection_facts, "collection_status": "partial_success"}

    projection_record = {
        "source_record_id": inputs["source_record_id"],
        "projection_facts": projection_facts,
        "materialized_media_assets": materialized_assets,
        "media_coverage": media_coverage,
    }
    write_payload = build_projection_write_payload(
        stage_code=context.stage_code or "persist_amazon_product_detail",
        request_id=context.request_id,
        target_table_ref=inputs["table_ref"],
        records=[projection_record],
        mapper_code="amazon_product_projection_mapper",
        write_mode="update",
        request_payload=inputs["request_payload"],
        source_record_id=inputs["source_record_id"],
        business_entity_key=f"amazon:US:{inputs['requested_asin']}",
    )
    write_payload["feishu_table"] = {
        "app_token": inputs["source_table_identity"]["base_id"],
        "table_id": inputs["source_table_identity"]["table_id"],
        **inputs["feishu_credential_refs"],
    }
    write_payload["write_policy"] = {
        "ignore_missing_fields": True,
        "field_allowlist": list(AMAZON_PRODUCT_FEISHU_WRITE_FIELDS),
    }
    write_context = _child_context(
        context,
        handler_code="feishu_table_write",
        step_code="feishu_table_write",
        payload=write_payload,
    )
    feishu_started_at = perf_counter()
    write_result = api_handler_callable("feishu_table_write")(write_context)
    inputs["stage_durations_ms"]["feishu"] = _elapsed_ms(feishu_started_at)
    step_statuses["feishu_table_write"] = write_result.status
    if write_result.status == "failed":
        return _failed_child_result(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="feishu_table_write",
            child_error=write_result.error,
            fact_refs=fact_refs,
            media_coverage=media_coverage,
        )
    if write_result.status not in {"success", "partial_success"}:
        return _invalid_child_status(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="feishu_table_write",
            child_status=write_result.status,
            fact_refs=fact_refs,
            media_coverage=media_coverage,
        )
    if write_result.status == "partial_success":
        row_status = "partial_success"
        warnings.append("Feishu writeback completed only partially.")

    if not _raw_writeback_converged(
        write_result.result,
        source_record_id=inputs["source_record_id"],
    ):
        step_statuses["feishu_table_write"] = "failed"
        return _failed_child_result(
            context,
            inputs=inputs,
            step_statuses=step_statuses,
            failed_step="feishu_table_write",
            child_error=build_error(
                error_type="contract_error",
                error_code="feishu_writeback_not_converged",
                message="Feishu writeback did not update exactly the requested source record.",
                retryable=False,
            ),
            fact_refs=fact_refs,
            media_coverage=media_coverage,
        )
    writeback = _writeback_summary(write_result)
    result = _compact_result(
        inputs,
        row_status=row_status,
        step_statuses=step_statuses,
        fact_refs=fact_refs,
        media_coverage=media_coverage,
        writeback=writeback,
    )
    summary = _summary(inputs, row_status, step_statuses, media_coverage)
    summary["writeback_written_count"] = writeback.get("written_count", 0)
    if row_status == "partial_success":
        return partial_success_result(
            context,
            summary=summary,
            result=result,
            warnings=tuple(dict.fromkeys(warnings)),
        )
    return success_result(context, summary=summary, result=result, warnings=warnings)


def _validate_inputs(raw_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(raw_payload)
    requested_asin = normalize_asin(payload.get("requested_asin"))
    table_ref = _required_text(payload.get("table_ref"), "table_ref")
    source_record_id = _required_text(payload.get("source_record_id"), "source_record_id")
    run_id = _required_text(payload.get("run_id"), "run_id")
    collection_status = _required_text(payload.get("collection_status"), "collection_status")
    if collection_status not in _COLLECTION_STATUSES:
        raise ValueError("collection_status must be success, partial_success, or unavailable.")
    raw_source_table_identity = _mapping(payload.get("source_table_identity"))
    source_table_identity = {
        "base_id": _required_text(
            raw_source_table_identity.get("base_id"),
            "source_table_identity.base_id",
        ),
        "table_id": _required_text(
            raw_source_table_identity.get("table_id"),
            "source_table_identity.table_id",
        ),
    }
    normalized_capture_ref = _mapping(payload.get("normalized_capture_ref"))
    if not normalized_capture_ref:
        raise ValueError("normalized_capture_ref is required.")
    raw_capture_refs = _mapping_list(payload.get("raw_capture_refs"))
    if raw_capture_refs != [normalized_capture_ref]:
        raise ValueError(
            "raw_capture_refs must contain exactly the normalized_capture_ref."
        )
    media_source_refs = _mapping_list(payload.get("media_source_refs"))
    normalized_media_source_refs: list[dict[str, Any]] = []
    seen_media_coordinates: set[tuple[str, int]] = set()
    for item in media_source_refs:
        unexpected_fields = sorted(set(item) - _MEDIA_SOURCE_REF_FIELDS)
        if unexpected_fields:
            raise ValueError(
                "Amazon media_source_ref contains unsupported fields: "
                + ", ".join(unexpected_fields)
            )
        if item.get("source_platform") != "amazon":
            raise ValueError("Every media_source_ref must use source_platform=amazon.")
        if item.get("marketplace_code") != "US":
            raise ValueError("Every media_source_ref must use marketplace_code=US.")
        if normalize_asin(item.get("product_id")) != requested_asin:
            raise ValueError("Every media_source_ref must match requested_asin.")
        media_role = _required_text(item.get("media_role"), "media_source_ref.media_role")
        if media_role not in _MEDIA_ROLES:
            raise ValueError("Every media_source_ref must use an approved Amazon media_role.")
        if type(item.get("position")) is not int or item["position"] < 0:
            raise ValueError("Every media_source_ref must have a non-negative integer position.")
        media_coordinate = (media_role, item["position"])
        if media_coordinate in seen_media_coordinates:
            raise ValueError("Amazon media_source_refs must use unique role/position mappings.")
        seen_media_coordinates.add(media_coordinate)
        source_url = normalize_amazon_media_url(item.get("source_url"))
        if not source_url:
            raise ValueError("Every media_source_ref must use an approved HTTPS Amazon CDN URL.")
        normalized_media_source_refs.append(
            {
                "source_url": source_url,
                "source_platform": "amazon",
                "marketplace_code": "US",
                "product_id": requested_asin,
                "media_role": media_role,
                "position": item["position"],
            }
        )
    media_source_refs = normalized_media_source_refs
    raw_request_payload = _mapping(payload.get("request_payload"))
    feishu_credential_refs = _feishu_credential_refs(
        table_ref=table_ref,
        source_table_identity=raw_source_table_identity,
        request_payload=raw_request_payload,
    )
    request_payload = {
        key: raw_request_payload[key]
        for key in ("table_ref", "source_record_id")
        if key in raw_request_payload
    }
    browser_provider_name = _optional_browser_provider_name(payload.get("browser_provider_name"))
    return {
        "table_ref": table_ref,
        "source_record_id": source_record_id,
        "source_table_identity": source_table_identity,
        "feishu_credential_refs": feishu_credential_refs,
        "requested_asin": requested_asin,
        "resolved_asin": _text(payload.get("resolved_asin")),
        "run_id": run_id,
        "collection_status": collection_status,
        "normalized_capture_ref": normalized_capture_ref,
        "raw_capture_refs": raw_capture_refs,
        "media_source_refs": media_source_refs,
        "field_coverage": _field_coverage(payload.get("field_coverage")),
        "browser_provider_name": browser_provider_name,
        "stage_durations_ms": _stage_durations(
            payload.get("stage_durations_ms"),
            allowed_stages=_BROWSER_STAGE_NAMES,
        ),
        "media_materialized_count": 0,
        "request_payload": request_payload,
    }


def _feishu_credential_refs(
    *,
    table_ref: str,
    source_table_identity: Mapping[str, Any],
    request_payload: Mapping[str, Any],
) -> dict[str, str]:
    table_refs = _mapping(request_payload.get("table_refs"))
    configured_table = _mapping(table_refs.get(table_ref))
    refs: dict[str, str] = {}
    for key in ("access_token_env", "access_token_ref"):
        value = _text(
            source_table_identity.get(key) or configured_table.get(key) or request_payload.get(key)
        )
        if value:
            refs[key] = value
    return refs


def _child_context(
    parent: HandlerContext,
    *,
    handler_code: str,
    step_code: str,
    payload: dict[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> HandlerContext:
    child_metadata = dict(parent.metadata)
    child_metadata.update(dict(metadata or {}))
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
        item_code=parent.item_code,
        business_key=parent.business_key,
        dedupe_key=(
            f"{parent.dedupe_key}:{step_code}"
            if parent.dedupe_key
            else f"{parent.job_id}:{step_code}"
        ),
        resource_code=parent.resource_code,
        worker_id=parent.worker_id,
        attempt_count=parent.attempt_count,
        max_attempts=parent.max_attempts,
        metadata=child_metadata,
    )


def _failed_child_result(
    context: HandlerContext,
    *,
    inputs: Mapping[str, Any],
    step_statuses: Mapping[str, str],
    failed_step: str,
    child_error: HandlerError | None,
    fact_refs: Mapping[str, Any] | None = None,
    media_coverage: Mapping[str, Any] | None = None,
) -> HandlerResult:
    error = child_error or build_error(
        error_type="child_handler_failure",
        error_code=f"{failed_step}_failed",
        message=f"{failed_step} failed without a structured error.",
        retryable=False,
    )
    result = _compact_result(
        inputs,
        row_status="failed",
        step_statuses=step_statuses,
        fact_refs=fact_refs,
        media_coverage=media_coverage,
        error_code=error.error_code,
    )
    result["failed_step"] = failed_step
    return failed_result(
        context,
        error=error,
        summary=_summary(
            inputs,
            "failed",
            step_statuses,
            media_coverage,
            error_code=error.error_code,
        ),
        result=result,
    )


def _invalid_child_status(
    context: HandlerContext,
    *,
    inputs: Mapping[str, Any],
    step_statuses: Mapping[str, str],
    failed_step: str,
    child_status: str,
    fact_refs: Mapping[str, Any] | None = None,
    media_coverage: Mapping[str, Any] | None = None,
) -> HandlerResult:
    return _failed_child_result(
        context,
        inputs=inputs,
        step_statuses=step_statuses,
        failed_step=failed_step,
        child_error=build_error(
            error_type="contract_error",
            error_code="invalid_child_handler_status",
            message=f"{failed_step} returned unsupported status {child_status!r}.",
            retryable=False,
        ),
        fact_refs=fact_refs,
        media_coverage=media_coverage,
    )


def _is_partial_media_failure(error: HandlerError | None) -> bool:
    return error is not None and error.error_code == "media_asset_materialization_failed"


def _has_retry_remaining(context: HandlerContext) -> bool:
    return context.max_attempts > 0 and context.attempt_count < context.max_attempts


def _materialized_assets(raw_assets: Any) -> list[dict[str, Any]]:
    return (
        [
            dict(item)
            for item in raw_assets
            if isinstance(item, Mapping)
            and item.get("sync_state") in _MATERIALIZED_MEDIA_STATES
            and _text(item.get("bucket"))
            and _text(item.get("object_key"))
        ]
        if isinstance(raw_assets, list)
        else []
    )


def _fact_refs(
    result: Mapping[str, Any],
    *,
    expected_normalized_capture_ref: Mapping[str, Any],
) -> dict[str, Any]:
    refs = {
        key: result[key]
        for key in ("product_id", "snapshot_id", "binding_id")
        if isinstance(result.get(key), str) and result[key]
    }
    raw_capture_ids = result.get("raw_capture_ids")
    if isinstance(raw_capture_ids, list) and all(
        isinstance(item, str) and item for item in raw_capture_ids
    ):
        refs["raw_capture_ids"] = list(raw_capture_ids)
    capture_fields = {
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
    }
    raw_normalized = _mapping(result.get("normalized_capture_ref"))
    expected_normalized = {
        key: expected_normalized_capture_ref[key]
        for key in capture_fields
        if key in expected_normalized_capture_ref
    }
    normalized = {
        key: raw_normalized[key]
        for key in capture_fields
        if key in raw_normalized
    }
    if raw_normalized and normalized != expected_normalized:
        raise ValueError(
            "Fact normalized_capture_ref must match the validated browser capture."
        )
    if normalized:
        refs["normalized_capture_ref"] = normalized
    return refs


def _media_coverage(
    _fact_result: HandlerResult,
    *,
    expected_count: int,
    materialized_count: int,
) -> dict[str, Any]:
    expected = max(expected_count, 0)
    materialized = min(max(materialized_count, 0), expected)
    missing_count = expected - materialized
    return {
        "expected": expected,
        "materialized": materialized,
        "missing": missing_count,
        "complete": missing_count == 0,
    }


def _row_status(
    collection_status: Any,
    *,
    fact_status: str,
    media_failed: bool,
    media_coverage: Mapping[str, Any],
) -> str:
    status = _text(collection_status)
    if status == "unavailable":
        return "unavailable"
    if (
        status == "partial_success"
        or fact_status == "partial_success"
        or media_failed
        or not bool(media_coverage.get("complete", True))
    ):
        return "partial_success"
    return "success"


def _writeback_summary(result: HandlerResult) -> dict[str, Any]:
    payload = result.result
    return {
        "written_count": (
            payload["written_count"] if type(payload.get("written_count")) is int else -1
        ),
        "skipped_count": (
            payload["skipped_count"] if type(payload.get("skipped_count")) is int else -1
        ),
        "failed_count": (
            payload["failed_count"] if type(payload.get("failed_count")) is int else -1
        ),
        "target_record_ids": [
            _text(item) for item in payload.get("target_record_ids", []) if _text(item)
        ]
        if isinstance(payload.get("target_record_ids"), list)
        else [],
    }


def _raw_writeback_converged(
    value: Mapping[str, Any],
    *,
    source_record_id: str,
) -> bool:
    return (
        type(value.get("written_count")) is int
        and type(value.get("skipped_count")) is int
        and type(value.get("failed_count")) is int
        and value.get("written_count") == 1
        and value.get("skipped_count") == 0
        and value.get("failed_count") == 0
        and value.get("target_record_ids") == [source_record_id]
    )


def _compact_result(
    inputs: Mapping[str, Any],
    *,
    row_status: str,
    step_statuses: Mapping[str, str],
    fact_refs: Mapping[str, Any] | None = None,
    media_coverage: Mapping[str, Any] | None = None,
    writeback: Mapping[str, Any] | None = None,
    error_code: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "row_status": row_status,
        "source_record_id": inputs["source_record_id"],
        "requested_asin": inputs["requested_asin"],
        "run_id": inputs["run_id"],
        "step_statuses": dict(step_statuses),
        "observability": _row_observability(
            inputs,
            final_status=row_status,
            media_coverage=media_coverage,
            error_code=error_code,
        ),
    }
    if inputs.get("resolved_asin"):
        result["resolved_asin"] = inputs["resolved_asin"]
    if fact_refs:
        result["fact_refs"] = dict(fact_refs)
    if media_coverage:
        result["media_coverage"] = dict(media_coverage)
    if writeback:
        result["writeback"] = dict(writeback)
    return result


def _summary(
    inputs: Mapping[str, Any],
    row_status: str,
    step_statuses: Mapping[str, str],
    media_coverage: Mapping[str, Any] | None = None,
    *,
    error_code: str = "",
) -> dict[str, Any]:
    return {
        "row_status": row_status,
        "source_record_id": inputs["source_record_id"],
        "requested_asin": inputs["requested_asin"],
        "run_id": inputs["run_id"],
        "step_statuses": dict(step_statuses),
        "media_expected_count": int((media_coverage or {}).get("expected") or 0),
        "media_materialized_count": int((media_coverage or {}).get("materialized") or 0),
        "observability": _row_observability(
            inputs,
            final_status=row_status,
            media_coverage=media_coverage,
            error_code=error_code,
        ),
    }


def _row_observability(
    inputs: Mapping[str, Any],
    *,
    final_status: str,
    media_coverage: Mapping[str, Any] | None,
    error_code: str,
) -> dict[str, Any]:
    coverage = _field_coverage(inputs.get("field_coverage"))
    stage_durations = _stage_durations(
        inputs.get("stage_durations_ms"),
        allowed_stages=(
            "navigation",
            "parse",
            "artifact",
            "media",
            "fact",
            "feishu",
        ),
    )
    materialized_count = int(
        (media_coverage or {}).get("materialized") or inputs.get("media_materialized_count") or 0
    )
    observation: dict[str, Any] = {
        "stage_durations_ms": stage_durations,
        "field_coverage": coverage,
        "artifact_count": len(_mapping_list(inputs.get("raw_capture_refs"))),
        "media_observed_count": len(_mapping_list(inputs.get("media_source_refs"))),
        "media_materialized_count": materialized_count,
        "final_status": final_status,
        "error_code": _safe_error_code(error_code),
    }
    provider_name = _optional_browser_provider_name(inputs.get("browser_provider_name"))
    if provider_name:
        observation["browser_provider_name"] = provider_name
    return observation


def _optional_browser_provider_name(value: Any) -> str:
    provider_name = _text(value)
    if provider_name and not _BROWSER_PROVIDER_NAME.fullmatch(provider_name):
        raise ValueError("browser_provider_name must be a non-sensitive provider code.")
    return provider_name


def _field_coverage(value: Any) -> dict[str, int | float]:
    raw = _mapping(value)
    coverage: dict[str, int | float] = {}
    for key in ("total", "observed", "explicitly_unavailable", "missing"):
        item = raw.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            continue
        coverage[key] = item
    percentage = raw.get("percentage")
    if (
        not isinstance(percentage, bool)
        and isinstance(percentage, (int, float))
        and math.isfinite(float(percentage))
        and 0 <= float(percentage) <= 100
    ):
        coverage["percentage"] = round(float(percentage), 2)
    return coverage


def _stage_durations(
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


def _elapsed_ms(started_at: float) -> float:
    return round(max(perf_counter() - started_at, 0.0) * 1_000.0, 3)


def _safe_error_code(value: Any) -> str:
    error_code = _text(value)
    return error_code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", error_code) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _required_text(value: Any, name: str) -> str:
    item = _text(value)
    if not item:
        raise ValueError(f"{name} is required.")
    return item


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


__all__ = ["run_amazon_product_row_persist_flow"]
