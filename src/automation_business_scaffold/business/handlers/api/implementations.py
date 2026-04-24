from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation_business_scaffold.business.feishu_common import (
    adapt_source_rows,
    build_feishu_client,
    classify_feishu_exception,
    execute_write_records,
    map_write_records,
    normalize_raw_rows,
    read_feishu_records,
    resolve_read_target,
    resolve_write_target,
    validate_read_schema,
    validate_write_schema,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    create_store_from_settings,
    sync_artifact_specs,
)
from automation_business_scaffold.infrastructure.fastmoss.fact_mappers import (
    extract_fastmoss_data,
    map_fastmoss_author_bundle,
    map_fastmoss_author_goods_list,
    map_fastmoss_author_video_list,
    map_fastmoss_goods_author,
    map_fastmoss_goods_base,
    map_fastmoss_goods_overview,
    map_fastmoss_goods_product_sku,
    map_fastmoss_goods_video,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

from .._shared import (
    build_error,
    build_creator_key,
    build_shop_key,
    bundle_entity_keys,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    extract_product_id,
    failed_result,
    fallback_required_result,
    first_non_empty,
    json_fingerprint,
    merge_fact_bundles,
    new_fact_bundle,
    normalize_product_identity,
    now_timestamp,
    partial_success_result,
    product_business_key,
    skipped_result,
    success_result,
)
from ..contract import HandlerContext, HandlerNextAction, HandlerResult

FASTMOSS_PRODUCT_SEARCH_ENDPOINT = "/api/goods/V2/search"
FASTMOSS_PRODUCT_DETAIL_URL_TEMPLATE = "https://www.fastmoss.com/zh/e-commerce/detail/{product_id}"
TIKTOK_PRODUCT_URL_TEMPLATE = "https://www.tiktok.com/view/product/{product_id}"


def feishu_table_read_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        target = resolve_read_target(payload)
        client = build_feishu_client(target)
        field_names = _list_text(payload.get("field_names"))
        read_policy = coerce_mapping(payload.get("read_policy"))
        if coerce_bool(read_policy.get("validate_schema")) or coerce_bool(payload.get("validate_schema")):
            validate_read_schema(client, target, field_names)
        raw_records, pagination = read_feishu_records(client, target, payload)
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


def _list_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return [coerce_str(item) for item in value if coerce_str(item)]
    if isinstance(value, tuple):
        return [coerce_str(item) for item in value if coerce_str(item)]
    text = coerce_str(value)
    return [text] if text else []


def tiktok_product_request_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    identity = normalize_product_identity(payload)
    fallback_allowed = coerce_bool(payload.get("fallback_allowed"), default=True)

    if coerce_bool(payload.get("force_failure")):
        error = build_error(
            error_type="request_failure",
            error_code="tiktok_request_forced_failure",
            message="TikTok request-first path was forced to fail by payload.",
            retryable=False,
            details={"product_identity": identity},
        )
        return failed_result(
            context,
            error=error,
            summary={"collection_path": "request", "product_business_key": product_business_key(identity)},
        )

    if coerce_bool(payload.get("force_fallback")):
        return _browser_fallback_result(
            context,
            identity=identity,
            fallback_reason=first_non_empty(payload.get("fallback_reason"), "forced_by_payload"),
            detail_message="TikTok request-first path requested browser fallback.",
        )

    normalized = coerce_mapping(payload.get("normalized_product_result"))
    if not normalized:
        raw_request_result = _resolve_inline_tiktok_payload(payload)
        normalized = _build_tiktok_normalized_product_result(
            raw_request_result,
            identity=identity,
            collection_path="request",
            source_endpoint="tiktok.product.request",
        )

    product = coerce_mapping(normalized.get("product"))
    product_id = first_non_empty(product.get("product_id"), identity.get("product_id"))
    product_url = first_non_empty(
        product.get("normalized_url"),
        product.get("product_url"),
        identity.get("normalized_product_url"),
        identity.get("product_url"),
    )
    if not product_id and not product_url:
        if fallback_allowed:
            return _browser_fallback_result(
                context,
                identity=identity,
                fallback_reason=first_non_empty(payload.get("fallback_reason"), "request_payload_missing_product_identity"),
                detail_message="TikTok request-first payload did not produce a stable product identity.",
            )
        error = build_error(
            error_type="request_failure",
            error_code="tiktok_request_missing_identity",
            message="TikTok request-first payload did not produce a stable product identity.",
            retryable=False,
            details={"product_identity": identity},
        )
        return failed_result(
            context,
            error=error,
            summary={"collection_path": "request", "product_business_key": product_business_key(identity)},
        )

    result = {
        "normalized_product_result": normalized,
        "fallback_required": False,
        "fallback_reason": "",
        "fallback_source_job_id": "",
    }
    summary = {
        "collection_path": "request",
        "product_id": product_id,
        "product_business_key": product_business_key(identity) or product_url,
        "media_asset_count": len(coerce_mapping_list(normalized.get("media_assets"))),
        "sku_count": len(coerce_mapping_list(normalized.get("product_skus"))),
    }
    return success_result(context, summary=summary, result=result)


def fastmoss_product_search_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    try:
        query = _resolve_fastmoss_product_search_query(payload)
        raw_pages, runtime_pagination, session_snapshot = _resolve_fastmoss_product_search_pages(
            payload,
            query=query,
        )
        normalized = _build_fastmoss_product_search_result(
            context,
            payload,
            query=query,
            raw_pages=raw_pages,
            runtime_pagination=runtime_pagination,
            session_snapshot=session_snapshot,
        )
    except ValueError as exc:
        error = build_error(
            error_type="configuration_error",
            error_code="fastmoss_search_invalid_payload",
            message=str(exc),
            retryable=False,
            details={"handler_code": context.handler_code},
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})
    except FastMossAuthError as exc:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_auth_required",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})
    except FastMossHTTPError as exc:
        error = build_error(
            error_type="transport_failure",
            error_code="fastmoss_http_failure",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(context, error=error, summary={"candidate_count": 0})

    candidates = coerce_mapping_list(normalized.get("candidates"))
    auth_state = coerce_mapping(normalized.get("auth_state"))
    summary = {
        "search_mode": query["search_mode"],
        "keyword": query["keyword"],
        "region": query["region"],
        "candidate_count": len(candidates),
        "raw_candidate_count": int(
            coerce_mapping(normalized.get("condition_summary")).get("raw_candidate_count", 0) or 0
        ),
        "degraded_preview": bool(auth_state.get("degraded_preview")),
        "source_code": coerce_str(auth_state.get("source_code")),
        "stop_reason": coerce_str(coerce_mapping(normalized.get("pagination")).get("stop_reason")),
    }
    warnings = tuple(str(item) for item in normalized.pop("warnings", []) if str(item))

    if auth_state.get("degraded_preview") and not query["degraded_preview_allowed"]:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_search_degraded_preview",
            message="FastMoss product search returned degraded preview results instead of deliverable data.",
            retryable=False,
            details={"auth_state": auth_state, "query": normalized.get("query", {})},
        )
        return failed_result(context, error=error, summary=summary, result=normalized, warnings=warnings)

    return success_result(context, summary=summary, result=normalized, warnings=warnings)


def fastmoss_product_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    identity = normalize_product_identity(payload)
    detail_level = first_non_empty(payload.get("detail_level"), "product")
    product_id = first_non_empty(identity.get("fastmoss_product_id"), identity.get("product_id"))
    required = coerce_bool(payload.get("required"), default=False)

    try:
        normalized_result = coerce_mapping(payload.get("product_fact_bundle"))
        if normalized_result:
            fact_bundle = merge_fact_bundles(normalized_result)
            raw_bundle = coerce_mapping(payload.get("fastmoss_bundle"))
            metrics_snapshot = coerce_mapping(payload.get("metrics_snapshot"))
            related_creators = coerce_mapping_list(payload.get("related_creators"))
        else:
            raw_bundle = _resolve_fastmoss_bundle(payload, product_id=product_id, detail_level=detail_level)
            if not raw_bundle:
                if required:
                    error = build_error(
                        error_type="source_missing",
                        error_code="fastmoss_payload_missing",
                        message="FastMoss payload or live session configuration was not provided.",
                        retryable=False,
                        details={"product_identity": identity},
                    )
                    return failed_result(
                        context,
                        error=error,
                        summary={"detail_level": detail_level, "product_business_key": product_business_key(identity)},
                    )
                return skipped_result(
                    context,
                    summary={"detail_level": detail_level, "product_business_key": product_business_key(identity)},
                    result={"product_fact_bundle": new_fact_bundle(), "related_creators": [], "metrics_snapshot": {}},
                    warnings=("FastMoss payload or live session configuration was not provided.",),
                )
            fact_bundle = _build_fastmoss_fact_bundle(raw_bundle, product_id=product_id)
            related_creators = _extract_related_creators(
                fact_bundle,
                source_context=coerce_mapping(payload.get("source_context")),
                relation_policy=coerce_mapping(payload.get("relation_policy")),
            )
            metrics_snapshot = _build_fastmoss_metrics_snapshot(raw_bundle, product_id=product_id)
    except FastMossAuthError as exc:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_auth_required",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "product_business_key": product_business_key(identity)},
        )
    except FastMossHTTPError as exc:
        error = build_error(
            error_type="transport_failure",
            error_code="fastmoss_http_failure",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "product_business_key": product_business_key(identity)},
        )

    summary = {
        "detail_level": detail_level,
        "product_business_key": product_business_key(identity),
        "entity_count": len(bundle_entity_keys(fact_bundle)),
        "related_creator_count": len(related_creators),
        "media_asset_count": len(coerce_mapping_list(fact_bundle.get("media_assets"))),
    }
    result = {
        "product_fact_bundle": fact_bundle,
        "related_creators": related_creators,
        "metrics_snapshot": metrics_snapshot,
    }
    return success_result(context, summary=summary, result=result)


def fastmoss_creator_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    creator_identity = _normalize_creator_identity(payload)
    detail_level = first_non_empty(payload.get("detail_level"), "profile_metrics")
    required = coerce_bool(payload.get("required"), default=False)

    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("fastmoss_creator_fetch", message="fastmoss creator fetch started")

    try:
        raw_bundle = _resolve_fastmoss_creator_bundle(payload, creator_identity=creator_identity)
        if not raw_bundle:
            if required:
                error = build_error(
                    error_type="source_missing",
                    error_code="fastmoss_creator_payload_missing",
                    message="FastMoss creator payload or live session configuration was not provided.",
                    retryable=False,
                    details={"creator_identity": creator_identity},
                )
                return failed_result(
                    context,
                    error=error,
                    summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
                )
            empty_bundle = new_fact_bundle()
            return skipped_result(
                context,
                summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
                result={
                    "entities": _contract_entities_from_fact_bundle(empty_bundle),
                    "relations": [],
                    "observations": [],
                    "media_refs": [],
                    "raw_response_refs": [],
                    "creator_fact_bundle": {},
                    "product_relations": [],
                    "fact_bundle": empty_bundle,
                },
                warnings=("FastMoss creator payload or live session configuration was not provided.",),
            )

        raw_bundle = _normalize_fastmoss_creator_bundle(raw_bundle)
        fact_bundle = _build_fastmoss_creator_fact_bundle(
            raw_bundle,
            creator_identity=creator_identity,
            payload=payload,
        )
    except FastMossAuthError as exc:
        error = build_error(
            error_type="auth_failure",
            error_code="fastmoss_auth_required",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
        )
    except FastMossHTTPError as exc:
        error = build_error(
            error_type="transport_failure",
            error_code="fastmoss_http_failure",
            message=str(exc),
            retryable=True,
            details=exc.to_dict(),
        )
        return failed_result(
            context,
            error=error,
            summary={"detail_level": detail_level, "creator_key": _creator_business_key(creator_identity)},
        )

    entities = _contract_entities_from_fact_bundle(fact_bundle)
    relations = _contract_relations_from_fact_bundle(fact_bundle)
    observations = _build_fastmoss_creator_observations(
        raw_bundle,
        fact_bundle=fact_bundle,
        payload=payload,
    )
    media_refs = _contract_media_refs_from_fact_bundle(fact_bundle)
    raw_response_refs = _raw_response_refs_from_fact_bundle(fact_bundle)
    creator_fact_bundle = _creator_compat_fact_bundle(
        fact_bundle,
        creator_identity=creator_identity,
        media_refs=media_refs,
    )
    product_relations = [
        relation for relation in relations if relation.get("relation_type") == "creator_promotes_product"
    ]
    quality = _creator_fetch_quality(raw_bundle, media_refs=media_refs)

    if callable(progress_callback):
        progress_callback("fastmoss_creator_mapped", message="fastmoss creator facts mapped")

    summary = {
        "detail_level": detail_level,
        "creator_key": first_non_empty(creator_fact_bundle.get("creator_key"), _creator_business_key(creator_identity)),
        "entity_count": len(bundle_entity_keys(fact_bundle)),
        "relation_count": len(relations),
        "observation_count": len(observations),
        "media_ref_count": len(media_refs),
    }
    result = {
        "entities": entities,
        "relations": relations,
        "observations": observations,
        "media_refs": media_refs,
        "raw_response_refs": raw_response_refs,
        "quality": quality,
        "creator_fact_bundle": creator_fact_bundle,
        "product_relations": product_relations,
        "fact_bundle": fact_bundle,
    }
    return success_result(context, summary=summary, result=result)


def media_asset_sync_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    asset_refs = coerce_mapping_list(payload.get("asset_refs"))
    if not asset_refs:
        return skipped_result(
            context,
            summary={"asset_count": 0, "synced_count": 0},
            result={"synced_assets": [], "artifact_refs": [], "media_fact_bundle": new_fact_bundle()},
        )

    artifact_settings = _resolve_artifact_settings(payload)
    artifact_store = create_store_from_settings(artifact_settings)
    artifact_root = Path(first_non_empty(payload.get("artifact_root"), tempfile.gettempdir()))
    artifact_bucket = first_non_empty(payload.get("artifact_bucket"), artifact_settings.get("artifact_bucket"), "runtime-artifacts")
    artifact_object_prefix = first_non_empty(
        payload.get("artifact_object_prefix"),
        artifact_settings.get("artifact_object_prefix"),
    )
    run_id = first_non_empty(payload.get("run_id"), context.metadata.get("run_id"), context.job_id)
    created_at = now_timestamp()

    specs: list[ArtifactFileSpec] = []
    local_assets_by_path: dict[str, dict[str, Any]] = {}
    synced_assets: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, asset in enumerate(asset_refs):
        normalized_asset = _normalize_media_asset(asset, fallback_product_id=payload.get("product_id"))
        local_path = Path(coerce_str(normalized_asset.get("local_path"))).expanduser()
        if local_path.exists() and local_path.is_file():
            specs.append(
                ArtifactFileSpec(
                    kind=first_non_empty(normalized_asset.get("media_role"), "asset_file"),
                    step_id=context.handler_code,
                    relative_name=f"assets/{index:03d}_{local_path.name}",
                    path=local_path,
                    content_type=coerce_str(normalized_asset.get("mime_type")),
                    metadata={
                        "entity_type": coerce_str(normalized_asset.get("entity_type")),
                        "entity_external_id": coerce_str(normalized_asset.get("entity_external_id")),
                        "media_role": coerce_str(normalized_asset.get("media_role")),
                    },
                )
            )
            local_assets_by_path[str(local_path.resolve())] = normalized_asset
            continue
        if coerce_str(normalized_asset.get("local_path")):
            warnings.append(f"Local asset path not found: {normalized_asset.get('local_path')}")
        normalized_asset["sync_state"] = "referenced"
        synced_assets.append(normalized_asset)

    artifact_refs: list[dict[str, Any]] = []
    if specs:
        records, artifact_uri_prefix = sync_artifact_specs(
            run_id=run_id,
            request_id=context.request_id,
            execution_id=context.job_id,
            artifact_root=artifact_root,
            artifact_bucket=artifact_bucket,
            artifact_object_prefix=artifact_object_prefix,
            specs=specs,
            artifact_store=artifact_store,
            created_at=created_at,
        )
        for record in records:
            base_asset = local_assets_by_path.get(record.source_path, {})
            synced_asset = dict(base_asset)
            synced_asset.update(
                {
                    "sync_state": "uploaded" if artifact_store is not None else "linked_local",
                    "bucket": record.bucket,
                    "object_key": record.object_key,
                    "remote_uri": record.metadata.get("remote_uri", ""),
                    "mime_type": record.content_type,
                    "source_path": record.source_path,
                    "artifact_id": record.artifact_id,
                    "artifact_uri_prefix": artifact_uri_prefix,
                }
            )
            synced_assets.append(compact_dict(synced_asset))
            artifact_refs.append(record.to_dict())

    media_bundle = new_fact_bundle()
    media_bundle["media_assets"] = synced_assets
    summary = {
        "asset_count": len(asset_refs),
        "synced_count": len(synced_assets),
        "artifact_count": len(artifact_refs),
        "artifact_store_provider": getattr(artifact_store, "provider_code", "local") if artifact_store else "local",
    }
    result = {
        "synced_assets": synced_assets,
        "artifact_refs": artifact_refs,
        "media_fact_bundle": media_bundle,
    }
    if warnings:
        return success_result(context, summary=summary, result=result, warnings=tuple(warnings))
    return success_result(context, summary=summary, result=result)


def fact_bundle_upsert_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    merged_bundle = merge_fact_bundles(
        coerce_mapping(payload.get("fact_bundle")),
        coerce_mapping(payload.get("fact_bundle_patch")),
        coerce_mapping(payload.get("media_fact_bundle")),
        coerce_mapping(payload.get("product_fact_bundle")),
        coerce_mapping(coerce_mapping(payload.get("normalized_product_result")).get("fact_bundle")),
    )

    entity_keys = bundle_entity_keys(merged_bundle)
    if not entity_keys:
        return skipped_result(
            context,
            summary={"entity_count": 0, "persistence_mode": "skipped"},
            result={"upserted_entities": [], "upserted_relations": [], "observation_refs": []},
            warnings=("Fact bundle was empty; nothing to persist.",),
        )

    persistence_mode = "dry_run"
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
    )
    if fact_db_url:
        persistence_mode = "database"

    try:
        if persistence_mode == "database":
            persisted = _persist_fact_bundle(merged_bundle, fact_db_url=fact_db_url)
        else:
            persisted = _plan_fact_bundle_upsert(merged_bundle)
    except Exception as exc:  # pragma: no cover - defensive boundary for worker loop
        error = build_error(
            error_type="persistence_failure",
            error_code="fact_bundle_upsert_failed",
            message=str(exc),
            retryable=True,
            details={"persistence_mode": persistence_mode},
        )
        return failed_result(
            context,
            error=error,
            summary={"entity_count": len(entity_keys), "persistence_mode": persistence_mode},
        )

    result = {
        "upserted_entities": persisted["upserted_entities"],
        "upserted_relations": persisted["upserted_relations"],
        "observation_refs": persisted["observation_refs"],
        "persisted_counts": persisted["persisted_counts"],
        "fact_bundle": merged_bundle,
        "persistence_mode": persistence_mode,
    }
    summary = {
        "entity_count": len(persisted["upserted_entities"]),
        "relation_count": len(persisted["upserted_relations"]),
        "observation_count": len(persisted["observation_refs"]),
        "persistence_mode": persistence_mode,
    }
    return success_result(context, summary=summary, result=result)


def _resolve_inline_tiktok_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "request_result",
        "raw_request_result",
        "tiktok_request_result",
        "mock_response",
        "source_payload",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate
    source_context = coerce_mapping(payload.get("source_context"))
    for key in ("request_result", "raw_request_result", "product"):
        candidate = coerce_mapping(source_context.get(key))
        if candidate:
            return candidate
    return {}


def _build_tiktok_normalized_product_result(
    raw_payload: dict[str, Any],
    *,
    identity: dict[str, Any],
    collection_path: str,
    source_endpoint: str,
) -> dict[str, Any]:
    raw = dict(raw_payload)
    product_payload = coerce_mapping(raw.get("product")) or raw
    shop_payload = coerce_mapping(raw.get("shop"))
    product_url = first_non_empty(
        product_payload.get("normalized_url"),
        product_payload.get("product_url"),
        raw.get("normalized_product_url"),
        raw.get("product_url"),
        raw.get("source_url"),
        identity.get("normalized_product_url"),
        identity.get("product_url"),
    )
    product_id = first_non_empty(
        identity.get("product_id"),
        product_payload.get("product_id"),
        raw.get("product_id"),
        extract_product_id(product_url),
    )
    shop_name = first_non_empty(
        shop_payload.get("shop_name"),
        shop_payload.get("name"),
        product_payload.get("shop_name"),
        product_payload.get("seller_name"),
        raw.get("shop_name"),
    )
    shop_url = first_non_empty(shop_payload.get("shop_url"), product_payload.get("shop_url"), raw.get("shop_url"))
    product = compact_dict(
        {
            "product_id": product_id,
            "product_url": product_url,
            "normalized_url": first_non_empty(identity.get("normalized_product_url"), product_url),
            "title": first_non_empty(product_payload.get("title"), raw.get("title")),
            "holiday": first_non_empty(product_payload.get("holiday"), raw.get("holiday")),
            "seller_name": shop_name,
            "shop_name": shop_name,
            "shop_url": shop_url,
            "source_platform": "tiktok",
            "facts": {"collection_path": collection_path},
        }
    )
    shop = compact_dict(
        {
            "shop_key": build_shop_key(
                shop_id=first_non_empty(shop_payload.get("shop_id"), shop_payload.get("seller_id"), raw.get("shop_id")),
                shop_name=shop_name,
            ),
            "shop_id": first_non_empty(shop_payload.get("shop_id"), shop_payload.get("seller_id"), raw.get("shop_id")),
            "shop_name": shop_name,
            "shop_url": shop_url,
            "source_platform": "tiktok",
            "facts": {"collection_path": collection_path},
        }
    )
    product_skus = _normalize_product_skus(raw, product_id=product_id)
    media_assets = _normalize_tiktok_media_assets(raw, product=product)

    fact_bundle = new_fact_bundle()
    if product:
        fact_bundle["products"].append(product)
    if shop and (coerce_str(shop.get("shop_id")) or coerce_str(shop.get("shop_name"))):
        fact_bundle["shops"].append(shop)
    if product and shop and first_non_empty(shop.get("shop_id"), shop.get("shop_name")):
        fact_bundle["relations"]["product_shops"].append(
            compact_dict(
                {
                    "product_id": product.get("product_id"),
                    "shop_id": shop.get("shop_id"),
                    "shop_name": shop.get("shop_name"),
                    "shop_key": first_non_empty(
                        shop.get("shop_key"),
                        build_shop_key(shop_id=shop.get("shop_id"), shop_name=shop.get("shop_name")),
                    ),
                    "relation_role": "seller",
                    "source_platform": "tiktok",
                }
            )
        )
    fact_bundle["product_skus"] = product_skus
    fact_bundle["media_assets"] = media_assets
    if raw:
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "tiktok",
                "source_endpoint": source_endpoint,
                "request_url": product_url,
                "request_params": compact_dict({"product_id": product_id}),
                "response_payload": raw,
                "status_code": 200,
            }
        )

    return {
        "product_identity": compact_dict(
            {
                "product_id": product_id,
                "product_url": product_url,
                "normalized_product_url": first_non_empty(identity.get("normalized_product_url"), product_url),
            }
        ),
        "collection_path": collection_path,
        "product": product,
        "product_skus": product_skus,
        "media_assets": media_assets,
        "fact_bundle": fact_bundle,
        "artifact_refs": coerce_mapping_list(raw.get("artifact_refs")),
        "logical_fields": compact_dict(
            {
                "title": product.get("title"),
                "shop_name": shop_name,
                "shop_url": shop_url,
                "main_image_url": first_non_empty(
                    product_payload.get("main_image_url"),
                    product_payload.get("img"),
                    product_payload.get("image_url"),
                    raw.get("main_image_url"),
                ),
                "price_text": first_non_empty(
                    product_payload.get("price_text"),
                    product_payload.get("real_price"),
                    product_payload.get("price"),
                    raw.get("price_text"),
                    raw.get("real_price"),
                    raw.get("price"),
                ),
            }
        ),
    }


def _normalize_product_skus(raw_payload: dict[str, Any], *, product_id: str) -> list[dict[str, Any]]:
    items = coerce_mapping_list(raw_payload.get("sku_list")) or coerce_mapping_list(raw_payload.get("skus"))
    normalized: list[dict[str, Any]] = []
    for item in items:
        sku_id = first_non_empty(item.get("sku_id"), item.get("id"))
        sku_name = first_non_empty(item.get("sku_name"), item.get("name"), sku_id)
        normalized.append(
            compact_dict(
                {
                    "product_id": product_id,
                    "sku_id": sku_id,
                    "sku_name": sku_name,
                    "spec_name": first_non_empty(item.get("spec_name"), item.get("spec")),
                    "price_text": first_non_empty(item.get("price_text"), item.get("real_price"), item.get("price")),
                    "stock_count": item.get("stock_count", item.get("stock")),
                    "facts": {"raw": item},
                }
            )
        )
    return normalized


def _normalize_tiktok_media_assets(raw_payload: dict[str, Any], *, product: dict[str, Any]) -> list[dict[str, Any]]:
    product_id = first_non_empty(product.get("product_id"))
    media_assets: list[dict[str, Any]] = []
    for media_role, field_name in (
        ("product_main_image", "main_image_url"),
        ("product_main_image", "image_url"),
        ("product_main_image", "img"),
    ):
        source_url = first_non_empty(raw_payload.get(field_name), coerce_mapping(raw_payload.get("product")).get(field_name))
        if source_url:
            media_assets.append(
                _normalize_media_asset(
                    {
                        "entity_type": "product",
                        "entity_external_id": product_id,
                        "media_role": media_role,
                        "source_url": source_url,
                        "source_platform": "tiktok",
                    },
                    fallback_product_id=product_id,
                )
            )
            break
    gallery_images = raw_payload.get("gallery_images") or coerce_mapping(raw_payload.get("product")).get("gallery_images")
    for entry in gallery_images if isinstance(gallery_images, list) else []:
        source_url = entry if isinstance(entry, str) else first_non_empty(coerce_mapping(entry).get("source_url"), coerce_mapping(entry).get("url"))
        if not source_url:
            continue
        media_assets.append(
            _normalize_media_asset(
                {
                    "entity_type": "product",
                    "entity_external_id": product_id,
                    "media_role": "product_gallery_image",
                    "source_url": source_url,
                    "source_platform": "tiktok",
                },
                fallback_product_id=product_id,
            )
        )
    screenshot_fields = ("product_page_screenshot_local_path", "product_page_screenshot_object_key")
    if any(coerce_str(raw_payload.get(name)) for name in screenshot_fields):
        media_assets.append(
            _normalize_media_asset(
                {
                    "entity_type": "product",
                    "entity_external_id": product_id,
                    "media_role": "product_page_screenshot",
                    "local_path": raw_payload.get("product_page_screenshot_local_path"),
                    "object_key": raw_payload.get("product_page_screenshot_object_key"),
                    "file_name": raw_payload.get("product_page_screenshot_file_name"),
                    "mime_type": raw_payload.get("product_page_screenshot_mime_type"),
                    "source_platform": "tiktok",
                },
                fallback_product_id=product_id,
            )
        )
    return media_assets


def _browser_fallback_result(
    context: HandlerContext,
    *,
    identity: dict[str, Any],
    fallback_reason: str,
    detail_message: str,
) -> HandlerResult:
    error = build_error(
        error_type="fallback_required",
        error_code="tiktok_browser_fallback_required",
        message=detail_message,
        retryable=False,
        fallback_allowed=True,
        fallback_reason=fallback_reason,
        details={"product_identity": identity},
    )
    next_action = HandlerNextAction(
        type="enqueue_browser_fallback",
        payload=compact_dict(
            {
                "product_identity": identity,
                "normalized_product_url": identity.get("normalized_product_url"),
                "fallback_source_job_id": context.job_id,
            }
        ),
    )
    result = {
        "fallback_required": True,
        "fallback_reason": fallback_reason,
        "fallback_source_job_id": context.job_id,
    }
    summary = {
        "collection_path": "request",
        "product_business_key": product_business_key(identity),
        "fallback_required": True,
    }
    return fallback_required_result(
        context,
        error=error,
        summary=summary,
        result=result,
        next_action=next_action,
    )


def _resolve_fastmoss_product_search_query(payload: dict[str, Any]) -> dict[str, Any]:
    filters = coerce_mapping(payload.get("filters"))
    fastmoss_settings = _resolve_fastmoss_search_settings(payload)
    search_mode = first_non_empty(payload.get("search_mode"), "keyword")
    if search_mode != "keyword":
        raise ValueError(f"Unsupported FastMoss product search_mode: {search_mode}")

    keyword = first_non_empty(
        payload.get("keyword"),
        payload.get("search_query"),
        payload.get("search_keyword"),
        payload.get("words"),
    )
    if not keyword:
        raise ValueError("FastMoss product keyword/search_query is required.")

    output_conditions = coerce_mapping(payload.get("output_conditions"))
    legacy_condition_context = coerce_mapping(payload.get("condition_context"))
    if legacy_condition_context:
        output_conditions = {**legacy_condition_context, **output_conditions}
    max_candidates = _positive_int(
        output_conditions.get("max_candidates"),
        _positive_int(payload.get("limit"), 20),
    )
    if max_candidates > 0:
        output_conditions["max_candidates"] = max_candidates
    sales_7d_threshold = _positive_int(payload.get("sales_7d_threshold"), 0)
    if sales_7d_threshold > 0:
        business_conditions = coerce_mapping(output_conditions.get("business_conditions"))
        business_conditions.setdefault("min_day7_sold_count", sales_7d_threshold)
        output_conditions["business_conditions"] = business_conditions

    sort = coerce_mapping(payload.get("sort"))
    pagination = coerce_mapping(payload.get("pagination"))
    session_policy = coerce_mapping(payload.get("session_policy"))
    raw_capture_policy = coerce_mapping(payload.get("raw_capture_policy"))
    page = _positive_int(pagination.get("page"), _positive_int(payload.get("page"), 1)) or 1
    page_size = _positive_int(
        first_non_empty(pagination.get("page_size"), pagination.get("pagesize")),
        _positive_int(payload.get("page_size"), 10),
    ) or 10
    max_pages = _positive_int(pagination.get("max_pages"), _positive_int(payload.get("max_pages"), 50)) or 50
    require_login = coerce_bool(session_policy.get("require_login"), default=True)
    degraded_preview_allowed = coerce_bool(
        session_policy.get("degraded_preview_allowed"),
        default=False,
    )

    extra_params, filter_warnings = _fastmoss_search_extra_params(filters)
    region = first_non_empty(
        payload.get("region"),
        filters.get("region"),
        filters.get("country_code"),
        fastmoss_settings.get("region"),
        "US",
    )
    source_order = first_non_empty(
        sort.get("source_order"),
        payload.get("source_order"),
        payload.get("order"),
        _source_order_from_sort(sort),
        "2,2",
    )
    raw_capture_policy.setdefault("store_raw_response", True)

    return {
        "search_mode": search_mode,
        "keyword": keyword,
        "region": region,
        "filters": filters,
        "sort": sort,
        "source_order": source_order,
        "page": page,
        "page_size": page_size,
        "max_pages": max_pages,
        "stop_when_no_new_product": coerce_bool(
            pagination.get("stop_when_no_new_product"),
            default=True,
        ),
        "max_candidates": max_candidates,
        "output_conditions": output_conditions,
        "session_policy": session_policy,
        "raw_capture_policy": raw_capture_policy,
        "require_login": require_login,
        "degraded_preview_allowed": degraded_preview_allowed,
        "fastmoss_settings": fastmoss_settings,
        "extra_params": extra_params,
        "warnings": filter_warnings,
    }


def _resolve_fastmoss_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = coerce_mapping(payload.get("fastmoss"))
    phone_env = first_non_empty(
        settings.get("phone_env"),
        settings.get("fastmoss_phone_env"),
        payload.get("fastmoss_phone_env"),
    )
    password_env = first_non_empty(
        settings.get("password_env"),
        settings.get("fastmoss_password_env"),
        payload.get("fastmoss_password_env"),
    )
    browser_cookies = settings.get("browser_cookies", payload.get("browser_cookies"))
    return {
        "phone": first_non_empty(
            settings.get("phone"),
            payload.get("fastmoss_phone"),
            _env_value(phone_env),
        ),
        "password": first_non_empty(
            settings.get("password"),
            payload.get("fastmoss_password"),
            _env_value(password_env),
        ),
        "phone_env": phone_env,
        "password_env": password_env,
        "base_url": first_non_empty(settings.get("base_url"), payload.get("fastmoss_base_url"), "https://www.fastmoss.com"),
        "region": first_non_empty(settings.get("region"), payload.get("region"), "US"),
        "timeout": settings.get("timeout", payload.get("fastmoss_timeout", 30.0)),
        "browser_cookies": browser_cookies if isinstance(browser_cookies, list) else [],
        "live_fetch": settings.get("live_fetch", payload.get("fastmoss_live_fetch", True)),
        "ensure_logged_in": settings.get("ensure_logged_in", payload.get("ensure_fastmoss_logged_in", None)),
    }


def _resolve_fastmoss_product_search_pages(
    payload: dict[str, Any],
    *,
    query: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    raw_pages = _inline_fastmoss_search_pages(payload, first_page=int(query["page"]))
    if raw_pages:
        return raw_pages, _pagination_runtime_from_raw_pages(raw_pages, query=query), {}

    fastmoss_settings = coerce_mapping(query.get("fastmoss_settings"))
    live_fetch = coerce_bool(fastmoss_settings.get("live_fetch"), default=True)
    if not live_fetch:
        raise ValueError("FastMoss live_fetch is disabled and no raw search response was provided.")

    cookies = fastmoss_settings.get("browser_cookies") if isinstance(fastmoss_settings.get("browser_cookies"), list) else []
    phone = first_non_empty(fastmoss_settings.get("phone"))
    password = first_non_empty(fastmoss_settings.get("password"))
    if query["require_login"] and not cookies and not (phone and password):
        raise ValueError("FastMoss product search requires credentials or browser_cookies.")

    session = FastMossHTTPSession(
        phone=phone,
        password=password,
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(query.get("region"), fastmoss_settings.get("region"), "US"),
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
    )
    with session:
        if cookies:
            session.replace_browser_cookies(cookies)
        ensure_logged_in = coerce_bool(
            fastmoss_settings.get("ensure_logged_in"),
            default=bool(query["require_login"] or cookies or phone),
        )
        if ensure_logged_in:
            session.ensure_logged_in()

        return _fetch_fastmoss_search_pages(session, query=query), {}, session.cookie_snapshot()


def _fetch_fastmoss_search_pages(
    session: FastMossHTTPSession,
    *,
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_pages: list[dict[str, Any]] = []
    seen_product_keys: set[str] = set()
    page = int(query["page"])
    stop_reason = "max_pages"
    for _ in range(max(int(query["max_pages"]), 1)):
        raw = session.search_products(
            query["keyword"],
            page=page,
            pagesize=int(query["page_size"]),
            region=query["region"],
            order=query["source_order"],
            extra_params=coerce_mapping(query.get("extra_params")),
            check_auth=False,
        )
        raw_pages.append({"page": page, "response": raw})
        rows = _fastmoss_search_rows(raw)
        if not rows:
            stop_reason = "empty_page"
            break

        page_keys = {
            _fastmoss_product_row_key(row)
            for row in rows
            if _fastmoss_product_row_key(row)
        }
        new_keys = {key for key in page_keys if key not in seen_product_keys}
        seen_product_keys.update(new_keys)
        auth_state = _fastmoss_auth_state_from_payloads([raw], session_snapshot={})
        if auth_state.get("degraded_preview"):
            stop_reason = "degraded_preview"
            break
        if query["stop_when_no_new_product"] and not new_keys:
            stop_reason = "no_new_product"
            break
        if int(query["max_candidates"]) > 0 and len(seen_product_keys) >= int(query["max_candidates"]):
            stop_reason = "max_candidates"
            break

        total = _positive_int(
            first_non_empty(
                coerce_mapping(raw.get("data")).get("total"),
                coerce_mapping(raw.get("data")).get("total_cnt"),
            ),
            0,
        )
        if total > 0 and page * int(query["page_size"]) >= total:
            stop_reason = "total_reached"
            break
        page += 1

    if raw_pages:
        raw_pages[-1]["stop_reason"] = stop_reason
    return raw_pages


def _build_fastmoss_product_search_result(
    context: HandlerContext,
    payload: dict[str, Any],
    *,
    query: dict[str, Any],
    raw_pages: list[dict[str, Any]],
    runtime_pagination: dict[str, Any],
    session_snapshot: dict[str, Any],
) -> dict[str, Any]:
    raw_response_ref, artifact_refs, capture_warnings = _capture_fastmoss_search_raw_response(
        context,
        payload,
        raw_pages=raw_pages,
        query=query,
    )
    output_conditions = coerce_mapping(query.get("output_conditions"))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected_count = 0
    deduped_count = 0
    raw_candidate_count = 0
    deferred_conditions: dict[str, Any] = {}
    max_candidates = int(query["max_candidates"])

    for page_record in raw_pages:
        page_number = _positive_int(page_record.get("page"), int(query["page"])) or int(query["page"])
        for row_index, row in enumerate(_fastmoss_search_rows(coerce_mapping(page_record.get("response"))), start=1):
            raw_candidate_count += 1
            candidate = _normalize_fastmoss_search_candidate(
                row,
                query=query,
                page_number=page_number,
                raw_index=raw_candidate_count,
                raw_response_ref=raw_response_ref,
            )
            matched, deferred, condition_allowed = _evaluate_fastmoss_output_conditions(
                candidate,
                output_conditions,
            )
            candidate["matched_conditions"] = matched
            candidate["deferred_conditions"] = deferred
            deferred_conditions.update(deferred)
            candidate["quality_score"] = _fastmoss_candidate_quality_score(candidate, output_conditions)
            if not condition_allowed or not _fastmoss_candidate_allowed(candidate, output_conditions):
                rejected_count += 1
                continue
            dedupe_key = _fastmoss_candidate_dedupe_key(candidate, output_conditions)
            if dedupe_key in seen:
                deduped_count += 1
                continue
            seen.add(dedupe_key)
            candidate["rank"] = len(candidates) + 1
            candidate["search_rank"] = candidate["rank"]
            candidates.append(candidate)
            if max_candidates > 0 and len(candidates) >= max_candidates:
                break
        if max_candidates > 0 and len(candidates) >= max_candidates:
            break

    auth_state = _fastmoss_auth_state_from_payloads(
        [coerce_mapping(page.get("response")) for page in raw_pages],
        session_snapshot=session_snapshot,
    )
    pagination = _build_fastmoss_search_pagination(
        raw_pages,
        query=query,
        runtime_pagination=runtime_pagination,
        accepted_count=len(candidates),
    )
    condition_summary = {
        "applied": compact_dict(
            {
                "business_conditions": coerce_mapping(output_conditions.get("business_conditions")),
                "required_fields": output_conditions.get("required_fields"),
                "min_quality_score": output_conditions.get("min_quality_score"),
                "dedupe_by": output_conditions.get("dedupe_by"),
            }
        ),
        "deferred": deferred_conditions,
        "raw_candidate_count": raw_candidate_count,
        "accepted_count": len(candidates),
        "rejected_count": rejected_count,
        "deduped_count": deduped_count,
    }
    condition_context = dict(coerce_mapping(payload.get("condition_context")) or output_conditions)
    condition_context["condition_summary"] = condition_summary
    result = {
        "query": {
            "search_mode": query["search_mode"],
            "keyword": query["keyword"],
            "region": query["region"],
            "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
            "source_order": query["source_order"],
            "page": query["page"],
            "page_size": query["page_size"],
        },
        "candidates": candidates,
        "condition_summary": condition_summary,
        "condition_context": condition_context,
        "pagination": pagination,
        "auth_state": auth_state,
        "raw_response_ref": raw_response_ref,
        "artifact_refs": artifact_refs,
        "warnings": [*query.get("warnings", []), *capture_warnings],
    }
    return result


def _inline_fastmoss_search_pages(payload: dict[str, Any], *, first_page: int) -> list[dict[str, Any]]:
    for key in (
        "fastmoss_search_response",
        "product_search_response",
        "search_response",
        "mock_fastmoss_search_response",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return [{"page": first_page, "response": candidate}]

    for key in (
        "fastmoss_search_pages",
        "product_search_pages",
        "search_pages",
        "mock_fastmoss_search_pages",
    ):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        pages: list[dict[str, Any]] = []
        for index, item in enumerate(value, start=0):
            record = coerce_mapping(item)
            if not record:
                continue
            response = coerce_mapping(record.get("response")) or coerce_mapping(record.get("payload")) or record
            pages.append(
                {
                    "page": _positive_int(record.get("page"), first_page + index) or first_page + index,
                    "response": response,
                    "stop_reason": coerce_str(record.get("stop_reason")),
                }
            )
        if pages:
            return pages
    return []


def _capture_fastmoss_search_raw_response(
    context: HandlerContext,
    payload: dict[str, Any],
    *,
    raw_pages: list[dict[str, Any]],
    query: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    raw_capture_policy = coerce_mapping(query.get("raw_capture_policy"))
    if not coerce_bool(raw_capture_policy.get("store_raw_response"), default=True):
        return "", [], []

    artifact_settings = _resolve_artifact_settings(payload)
    artifact_store = create_store_from_settings(artifact_settings)
    artifact_root = Path(
        first_non_empty(
            payload.get("artifact_root"),
            payload.get("execution_control_artifact_root"),
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
    run_id = first_non_empty(payload.get("run_id"), context.metadata.get("run_id"), context.job_id)
    relative_name = "artifacts/fastmoss_product_search/raw_response.json"
    raw_path = artifact_root / "runs" / run_id / relative_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(
            {
                "query": {
                    "keyword": query["keyword"],
                    "region": query["region"],
                    "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                    "source_order": query["source_order"],
                },
                "pages": raw_pages,
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
                kind="fastmoss_product_search_raw_json",
                step_id=context.handler_code,
                relative_name=relative_name,
                path=raw_path,
                content_type="application/json",
                metadata={
                    "source_platform": "fastmoss",
                    "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
                },
            )
        ],
        artifact_store=artifact_store,
        created_at=now_timestamp(),
    )
    if not records:
        return raw_path.resolve().as_uri(), [], []
    record = records[0]
    raw_response_ref = first_non_empty(
        record.metadata.get("remote_uri"),
        record.metadata.get("local_uri"),
        Path(record.source_path).resolve().as_uri(),
    )
    return raw_response_ref, [record.to_dict() for record in records], []


def _normalize_fastmoss_search_candidate(
    row: dict[str, Any],
    *,
    query: dict[str, Any],
    page_number: int,
    raw_index: int,
    raw_response_ref: str,
) -> dict[str, Any]:
    product_id = first_non_empty(
        row.get("product_id"),
        row.get("id"),
        extract_product_id(row.get("detail_url"), row.get("product_url")),
    )
    normalized_product_url = first_non_empty(
        row.get("normalized_product_url"),
        row.get("product_url"),
        row.get("tiktok_product_url"),
        _tiktok_product_url(product_id),
    )
    title_raw = first_non_empty(row.get("title"), row.get("product_title"), row.get("name"))
    title = _strip_html(title_raw)
    shop_info = coerce_mapping(row.get("shop_info"))
    currency = first_non_empty(
        row.get("currency"),
        shop_info.get("currency"),
        coerce_mapping(coerce_mapping(query.get("filters")).get("price_range")).get("currency"),
        "USD",
    )
    candidate = {
        "source": "fastmoss",
        "source_endpoint": FASTMOSS_PRODUCT_SEARCH_ENDPOINT,
        "product_id": product_id,
        "normalized_product_url": normalized_product_url,
        "product_url": normalized_product_url,
        "fastmoss_product_url": _fastmoss_product_detail_url(product_id),
        "detail_url": first_non_empty(row.get("detail_url")),
        "title": title,
        "title_raw": title_raw,
        "image_url": first_non_empty(row.get("img"), row.get("image_url"), row.get("cover")),
        "shop": {
            "seller_id": first_non_empty(
                shop_info.get("seller_id"),
                shop_info.get("shop_id"),
                row.get("seller_id"),
            ),
            "shop_name": first_non_empty(row.get("shop_name"), shop_info.get("shop_name"), shop_info.get("name")),
            "raw": shop_info,
        },
        "price": {
            "amount": _parse_number(row.get("price")),
            "currency": currency,
            "display": first_non_empty(row.get("price"), row.get("price_show")),
        },
        "original_price": {
            "amount": _parse_number(row.get("ori_price")),
            "currency": currency,
            "display": first_non_empty(row.get("ori_price"), row.get("original_price_show")),
        },
        "commission": {
            "rate": _parse_rate(first_non_empty(row.get("crate"), row.get("commission_rate"))),
            "display": first_non_empty(row.get("crate_show"), row.get("commission_rate_show")),
        },
        "metrics": _fastmoss_product_search_metrics(row),
        "trend": _fastmoss_product_search_trend(row),
        "dedupe_keys": {
            "product_id": product_id,
            "normalized_product_url": normalized_product_url,
        },
        "matched_conditions": {},
        "deferred_conditions": {},
        "quality_score": 1.0,
        "raw_item_ref": f"{raw_response_ref}#page-{page_number}/product_list/{raw_index}" if raw_response_ref else "",
        "page": page_number,
        "raw_index": raw_index,
    }
    return candidate


def _fastmoss_product_search_metrics(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sold_count": _parse_number(row.get("sold_count")),
        "sale_amount": _parse_number(row.get("sale_amount")),
        "yday_sold_count": _parse_number(row.get("yday_sold_count")),
        "day7_sold_count": _parse_number(row.get("day7_sold_count")),
        "day14_sold_count": _parse_number(row.get("day14_sold_count")),
        "day28_sold_count": _parse_number(row.get("day28_sold_count")),
        "relate_author_count": _parse_number(row.get("relate_author_count")),
        "relate_video_count": _parse_number(row.get("relate_video_count")),
        "relate_live_count": _parse_number(row.get("relate_live_count")),
        "product_rating": _parse_number(row.get("product_rating")),
    }


def _fastmoss_product_search_trend(row: dict[str, Any]) -> list[dict[str, Any]]:
    trend: list[dict[str, Any]] = []
    for item in coerce_mapping_list(row.get("trend")):
        trend.append(
            compact_dict(
                {
                    "date": first_non_empty(item.get("date"), item.get("dt")),
                    "inc_sold_count": _parse_number(item.get("inc_sold_count")),
                    "inc_sale_amount": _parse_number(item.get("inc_sale_amount")),
                    "region": item.get("region"),
                    "region_name": item.get("region_name"),
                }
            )
        )
    return trend


def _evaluate_fastmoss_output_conditions(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, Any], bool]:
    business_conditions = coerce_mapping(output_conditions.get("business_conditions"))
    matched: dict[str, bool] = {}
    deferred: dict[str, Any] = {}
    for key, expected in business_conditions.items():
        threshold = _parse_number(expected)
        if key == "min_day7_sold_count":
            matched[key] = _number_at_least(candidate["metrics"].get("day7_sold_count"), threshold)
        elif key == "min_sold_count":
            matched[key] = _number_at_least(candidate["metrics"].get("sold_count"), threshold)
        elif key == "min_sale_amount":
            matched[key] = _number_at_least(candidate["metrics"].get("sale_amount"), threshold)
        elif key == "min_product_rating":
            matched[key] = _number_at_least(candidate["metrics"].get("product_rating"), threshold)
        elif key == "min_relate_author_count":
            matched[key] = _number_at_least(candidate["metrics"].get("relate_author_count"), threshold)
        elif key == "max_price_amount":
            matched[key] = _number_at_most(candidate["price"].get("amount"), threshold)
        elif key == "min_commission_rate":
            matched[key] = _number_at_least(candidate["commission"].get("rate"), threshold)
        else:
            deferred[key] = expected
    return matched, deferred, all(matched.values())


def _fastmoss_candidate_allowed(candidate: dict[str, Any], output_conditions: dict[str, Any]) -> bool:
    allowed_ids = {coerce_str(item) for item in output_conditions.get("allowed_product_ids") or [] if coerce_str(item)}
    excluded_ids = {coerce_str(item) for item in output_conditions.get("exclude_product_ids") or [] if coerce_str(item)}
    product_id = coerce_str(candidate.get("product_id"))
    if allowed_ids and product_id not in allowed_ids:
        return False
    if excluded_ids and product_id in excluded_ids:
        return False
    if coerce_bool(output_conditions.get("require_product_url"), default=False) and not coerce_str(
        candidate.get("normalized_product_url")
    ):
        return False
    min_quality_score = _parse_number(output_conditions.get("min_quality_score"))
    if min_quality_score is not None and float(candidate.get("quality_score") or 0.0) < float(min_quality_score):
        return False
    return True


def _fastmoss_candidate_quality_score(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> float:
    required_fields = [coerce_str(item) for item in output_conditions.get("required_fields") or [] if coerce_str(item)]
    if not required_fields:
        return 1.0
    present_count = sum(1 for field_name in required_fields if _candidate_field_value(candidate, field_name) not in ("", None))
    return round(present_count / len(required_fields), 4)


def _fastmoss_candidate_dedupe_key(
    candidate: dict[str, Any],
    output_conditions: dict[str, Any],
) -> str:
    dedupe_fields = [coerce_str(item) for item in output_conditions.get("dedupe_by") or [] if coerce_str(item)]
    if not dedupe_fields:
        dedupe_fields = ["product_id", "normalized_product_url"]
    parts = []
    for field_name in dedupe_fields:
        value = _candidate_field_value(candidate, field_name)
        if value not in ("", None):
            parts.append(f"{field_name}:{value}")
    return "|".join(parts) or json_fingerprint(candidate)


def _candidate_field_value(candidate: Mapping[str, Any], field_name: str) -> Any:
    current: Any = candidate
    for part in coerce_str(field_name).split("."):
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part)
    return current


def _fastmoss_search_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = coerce_mapping(payload.get("data"))
    return (
        coerce_mapping_list(data.get("product_list"))
        or coerce_mapping_list(data.get("list"))
        or coerce_mapping_list(data.get("goods_list"))
    )


def _fastmoss_product_row_key(row: dict[str, Any]) -> str:
    return first_non_empty(row.get("product_id"), row.get("id"), row.get("detail_url"), row.get("title"))


def _fastmoss_auth_state_from_payloads(
    payloads: list[dict[str, Any]],
    *,
    session_snapshot: dict[str, Any],
) -> dict[str, Any]:
    source_code = ""
    source_msg = ""
    is_login = bool(session_snapshot.get("has_fd_tk"))
    degraded_preview = False
    for payload in payloads:
        source_code = first_non_empty(payload.get("code"), source_code)
        source_msg = first_non_empty(payload.get("msg"), source_msg)
        ext = coerce_mapping(payload.get("ext"))
        if ext.get("is_login") in {1, "1", True}:
            is_login = True
        if ext.get("is_login") in {0, "0", False}:
            degraded_preview = True
        if coerce_str(payload.get("code")) == "MAG_AUTH_3001":
            degraded_preview = True
    return compact_dict(
        {
            "is_login": is_login,
            "degraded_preview": degraded_preview,
            "source_code": source_code,
            "source_msg": source_msg,
            "session_snapshot": session_snapshot,
        }
    )


def _build_fastmoss_search_pagination(
    raw_pages: list[dict[str, Any]],
    *,
    query: dict[str, Any],
    runtime_pagination: dict[str, Any],
    accepted_count: int,
) -> dict[str, Any]:
    last_record = raw_pages[-1] if raw_pages else {}
    last_payload = coerce_mapping(last_record.get("response"))
    data = coerce_mapping(last_payload.get("data"))
    total = _positive_int(
        first_non_empty(data.get("total"), data.get("total_cnt"), data.get("result_cnt")),
        0,
    )
    last_page = _positive_int(last_record.get("page"), int(query["page"])) or int(query["page"])
    stop_reason = first_non_empty(
        runtime_pagination.get("stop_reason"),
        last_record.get("stop_reason"),
        "completed",
    )
    has_more = False
    if total > 0:
        has_more = last_page * int(query["page_size"]) < total
    if stop_reason in {"empty_page", "no_new_product", "degraded_preview", "max_candidates"}:
        has_more = False
    return {
        "page": int(query["page"]),
        "page_size": int(query["page_size"]),
        "total": total,
        "has_more": has_more,
        "next_page": last_page + 1 if has_more else None,
        "stop_reason": stop_reason,
        "accepted_count": accepted_count,
        "fetched_pages": len(raw_pages),
    }


def _pagination_runtime_from_raw_pages(
    raw_pages: list[dict[str, Any]],
    *,
    query: dict[str, Any],
) -> dict[str, Any]:
    if not raw_pages:
        return {"stop_reason": "empty_page"}
    stop_reason = first_non_empty(raw_pages[-1].get("stop_reason"))
    if stop_reason:
        return {"stop_reason": stop_reason}
    last_rows = _fastmoss_search_rows(coerce_mapping(raw_pages[-1].get("response")))
    if not last_rows:
        return {"stop_reason": "empty_page"}
    if len(raw_pages) >= int(query["max_pages"]):
        return {"stop_reason": "max_pages"}
    return {"stop_reason": "inline_response"}


def _fastmoss_search_extra_params(filters: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    extra_params: dict[str, Any] = {}
    warnings: list[str] = []
    for source in (coerce_mapping(filters.get("extra")), coerce_mapping(filters.get("source_params"))):
        for key, value in source.items():
            normalized_key = coerce_str(key)
            if not normalized_key or normalized_key in {
                "page",
                "pagesize",
                "page_size",
                "order",
                "region",
                "words",
                "_time",
                "cnonce",
                "fm-sign",
            }:
                continue
            if isinstance(value, (str, int, float, bool)):
                extra_params[normalized_key] = value
    ignored_filter_keys = sorted(
        key
        for key in filters
        if key not in {"country_code", "region", "extra", "source_params"}
        and filters.get(key) not in (None, "", [], {})
    )
    if ignored_filter_keys:
        warnings.append(f"FastMoss search ignored unsupported input filters: {', '.join(ignored_filter_keys)}.")
    return extra_params, warnings


def _source_order_from_sort(sort: dict[str, Any]) -> str:
    field = coerce_str(sort.get("field"))
    direction = coerce_str(sort.get("direction")).lower()
    if field == "day7_sold_count" and direction in {"", "desc", "descending"}:
        return "2,2"
    return ""


def _strip_html(value: Any) -> str:
    text = html.unescape(coerce_str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = coerce_str(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*([kKmMwW万亿]?)", text)
    if match is None:
        return None
    number = float(match.group(1))
    suffix = match.group(2).lower()
    if suffix == "k":
        number *= 1_000
    elif suffix in {"m", "w", "万"}:
        number *= 1_000_000 if suffix == "m" else 10_000
    elif suffix == "亿":
        number *= 100_000_000
    return int(number) if number.is_integer() else number


def _parse_rate(value: Any) -> float | None:
    text = coerce_str(value)
    number = _parse_number(text)
    if number is None:
        return None
    rate = float(number)
    if "%" in text or rate > 1:
        rate /= 100
    return round(rate, 6)


def _number_at_least(value: Any, threshold: Any) -> bool:
    value_number = _parse_number(value)
    threshold_number = _parse_number(threshold)
    if value_number is None or threshold_number is None:
        return False
    return float(value_number) >= float(threshold_number)


def _number_at_most(value: Any, threshold: Any) -> bool:
    value_number = _parse_number(value)
    threshold_number = _parse_number(threshold)
    if value_number is None or threshold_number is None:
        return False
    return float(value_number) <= float(threshold_number)


def _positive_int(value: Any, default: int) -> int:
    number = _parse_number(value)
    if number is None:
        return default
    try:
        integer = int(number)
    except (TypeError, ValueError):
        return default
    return integer if integer > 0 else default


def _env_value(env_name: str) -> str:
    name = coerce_str(env_name)
    if not name:
        return ""
    return coerce_str(os.environ.get(name))


def _tiktok_product_url(product_id: str) -> str:
    return TIKTOK_PRODUCT_URL_TEMPLATE.format(product_id=product_id) if product_id else ""


def _fastmoss_product_detail_url(product_id: str) -> str:
    return FASTMOSS_PRODUCT_DETAIL_URL_TEMPLATE.format(product_id=product_id) if product_id else ""


def _resolve_fastmoss_bundle(payload: dict[str, Any], *, product_id: str, detail_level: str = "") -> dict[str, Any]:
    for key in ("fastmoss_bundle", "fastmoss_result", "mock_fastmoss_bundle"):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate

    fastmoss_settings = coerce_mapping(payload.get("fastmoss"))
    live_fetch = coerce_bool(fastmoss_settings.get("live_fetch"), default=bool(product_id and fastmoss_settings))
    if not live_fetch or not product_id:
        return {}

    session = FastMossHTTPSession(
        phone=first_non_empty(fastmoss_settings.get("phone")),
        password=first_non_empty(fastmoss_settings.get("password")),
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(fastmoss_settings.get("region"), "US"),
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
    )
    with session:
        cookies = fastmoss_settings.get("browser_cookies")
        if isinstance(cookies, list):
            session.replace_browser_cookies(cookies)
        if coerce_bool(fastmoss_settings.get("ensure_logged_in"), default=bool(cookies or fastmoss_settings.get("phone"))):
            session.ensure_logged_in()
        d_type = int(fastmoss_settings.get("window_days", 28) or 28)
        bundle = {
            "base": session.get_product_base(product_id),
            "overview": session.get_product_overview(product_id, d_type=d_type),
            "skus": session.get_product_skus(product_id, d_type=d_type),
            "sku_distribution": session.get_product_sku_distribution(product_id, d_type=d_type),
            "session_snapshot": session.cookie_snapshot(),
        }
        if _product_fetch_includes_related_creators(payload, detail_level=detail_level):
            author_plan = coerce_mapping(payload.get("author_list_plan")) or coerce_mapping(
                fastmoss_settings.get("author_list")
            )
            bundle["related_creators"] = session.list_product_authors(
                product_id,
                page=_coerce_positive_int(author_plan.get("page"), default=1),
                pagesize=_coerce_positive_int(author_plan.get("page_size") or author_plan.get("pagesize"), default=10),
                order=first_non_empty(author_plan.get("order"), "2,2"),
                ecommerce_type=first_non_empty(author_plan.get("ecommerce_type"), "all"),
            )
        return bundle


def _product_fetch_includes_related_creators(payload: Mapping[str, Any], *, detail_level: str) -> bool:
    normalized = first_non_empty(detail_level, payload.get("detail_level")).lower()
    return any(token in normalized for token in ("related_creator", "author", "creator"))


def _build_fastmoss_fact_bundle(raw_bundle: dict[str, Any], *, product_id: str) -> dict[str, Any]:
    fact_bundle = new_fact_bundle()
    base = coerce_mapping(raw_bundle.get("base"))
    overview = coerce_mapping(raw_bundle.get("overview"))
    skus = coerce_mapping(raw_bundle.get("skus"))
    related_creators = coerce_mapping(raw_bundle.get("related_creators")) or coerce_mapping(raw_bundle.get("authors"))
    videos = coerce_mapping(raw_bundle.get("videos"))

    if base:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_base(base, product_id=product_id))
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": "goods.base",
                "request_url": "",
                "request_params": {"product_id": product_id},
                "response_payload": base,
                "status_code": 200,
            }
        )
    if overview:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_overview(overview, product_id=product_id))
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": "goods.overview",
                "request_url": "",
                "request_params": {"product_id": product_id},
                "response_payload": overview,
                "status_code": 200,
            }
        )
        overview_data = extract_fastmoss_data(overview)
        fact_bundle["product_metric_snapshots"].extend(
            _build_fastmoss_product_metric_snapshots(overview_data, product_id=product_id)
        )
        fact_bundle["product_daily_metrics"].extend(_build_fastmoss_daily_metrics(overview_data, product_id=product_id))
        fact_bundle["product_distribution_snapshots"].extend(
            _build_fastmoss_distribution_snapshots(overview_data, product_id=product_id)
        )
    if skus:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_product_sku(skus, product_id=product_id))
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": "goods.skus",
                "request_url": "",
                "request_params": {"product_id": product_id},
                "response_payload": skus,
                "status_code": 200,
            }
        )
        fact_bundle["product_sku_metric_snapshots"].extend(
            _build_fastmoss_sku_metric_snapshots(
                extract_fastmoss_data(skus),
                extract_fastmoss_data(coerce_mapping(raw_bundle.get("sku_distribution"))),
                product_id=product_id,
            )
        )
    if related_creators:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_author(related_creators, product_id=product_id))
    if videos:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_video(videos, product_id=product_id))

    return merge_fact_bundles(fact_bundle)


def _extract_related_creators(
    fact_bundle: dict[str, Any],
    *,
    source_context: Mapping[str, Any] | None = None,
    relation_policy: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    source_context_payload = coerce_mapping(source_context)
    relation_policy_payload = coerce_mapping(relation_policy)
    relation_by_creator = _creator_product_relation_index(fact_bundle)
    creators = []
    for creator in coerce_mapping_list(fact_bundle.get("creators")):
        creator_key = first_non_empty(
            creator.get("creator_key"),
            creator.get("creator_id"),
            creator.get("uid"),
            creator.get("unique_id"),
        )
        relation = relation_by_creator.get(creator_key, {})
        raw = coerce_mapping(coerce_mapping(relation.get("metadata")).get("raw"))
        facts = coerce_mapping(creator.get("facts"))
        metrics = _metric_fields(
            raw,
            coerce_mapping(facts.get("raw")),
            coerce_mapping(facts.get("base_info")),
            coerce_mapping(facts.get("author_index")),
        )
        matched_conditions = _creator_candidate_matched_conditions(
            metrics,
            relation_policy=relation_policy_payload,
        )
        if matched_conditions and not all(matched_conditions.values()):
            continue
        uid = first_non_empty(creator.get("uid"), raw.get("uid"), raw.get("author_uid"))
        unique_id = first_non_empty(creator.get("unique_id"), raw.get("unique_id"), raw.get("author_unique_id"))
        creator_id = first_non_empty(creator.get("creator_id"), unique_id, uid)
        creators.append(
            compact_dict(
                {
                    "creator_key": creator_key,
                    "creator_id": creator_id,
                    "creator_identity": compact_dict(
                        {
                            "creator_id": creator_id,
                            "uid": uid,
                            "unique_id": unique_id,
                            "profile_url": _fastmoss_creator_profile_url(uid, unique_id),
                        }
                    ),
                    "uid": uid,
                    "unique_id": unique_id,
                    "nickname": creator.get("nickname"),
                    "display_name": creator.get("nickname"),
                    "metrics": metrics,
                    "matched_conditions": matched_conditions,
                    "source_context": {
                        **source_context_payload,
                        "matched_product_sold_count": first_non_empty(metrics.get("sold_count")),
                    },
                }
            )
        )
    return creators


def _creator_product_relation_index(fact_bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    relations = coerce_mapping(fact_bundle.get("relations"))
    for relation in coerce_mapping_list(relations.get("creator_products")):
        creator_key = first_non_empty(
            relation.get("creator_key"),
            relation.get("creator_id"),
            relation.get("uid"),
            relation.get("unique_id"),
        )
        if creator_key and creator_key not in index:
            index[creator_key] = relation
    return index


def _creator_candidate_matched_conditions(
    metrics: Mapping[str, Any],
    *,
    relation_policy: Mapping[str, Any],
) -> dict[str, bool]:
    conditions: dict[str, bool] = {}
    sold_threshold = relation_policy.get("creator_sold_count_min")
    follower_threshold = relation_policy.get("creator_follower_count_min")
    if sold_threshold not in (None, ""):
        conditions["creator_sold_count_min"] = _number_at_least(metrics.get("sold_count"), sold_threshold)
    if follower_threshold not in (None, ""):
        conditions["creator_follower_count_min"] = _number_at_least(metrics.get("follower_count"), follower_threshold)
    return conditions


def _fastmoss_creator_profile_url(uid: Any, unique_id: Any = "") -> str:
    ref = first_non_empty(uid, unique_id)
    return f"https://www.fastmoss.com/zh/influencer/detail/{ref}" if ref else ""


def _build_fastmoss_metrics_snapshot(raw_bundle: dict[str, Any], *, product_id: str) -> dict[str, Any]:
    overview = extract_fastmoss_data(coerce_mapping(raw_bundle.get("overview")))
    return compact_dict(
        {
            "product_id": product_id,
            "window_days": overview.get("d_type"),
            "overview": coerce_mapping(overview.get("overview")),
            "chart_points": len(coerce_mapping_list(overview.get("chart_list"))),
            "session_snapshot": coerce_mapping(raw_bundle.get("session_snapshot")),
        }
    )


def _normalize_creator_identity(payload: dict[str, Any]) -> dict[str, Any]:
    creator_identity = coerce_mapping(payload.get("creator_identity"))
    source_context = coerce_mapping(payload.get("source_context"))
    creator_candidate = coerce_mapping(source_context.get("creator_candidate"))
    profile_url = first_non_empty(
        creator_identity.get("profile_url"),
        payload.get("profile_url"),
        creator_candidate.get("profile_url"),
        creator_candidate.get("author_url"),
    )
    uid = first_non_empty(
        creator_identity.get("uid"),
        payload.get("uid"),
        creator_candidate.get("uid"),
        creator_candidate.get("author_uid"),
        _extract_fastmoss_influencer_uid(profile_url),
    )
    unique_id = first_non_empty(
        creator_identity.get("unique_id"),
        payload.get("unique_id"),
        creator_candidate.get("unique_id"),
        creator_candidate.get("author_unique_id"),
    )
    creator_id = first_non_empty(
        creator_identity.get("creator_id"),
        payload.get("creator_id"),
        creator_candidate.get("creator_id"),
        creator_candidate.get("influencer_id"),
        unique_id,
        uid,
    )
    return compact_dict(
        {
            "creator_id": creator_id,
            "uid": uid,
            "unique_id": unique_id,
            "nickname": first_non_empty(
                creator_identity.get("nickname"),
                creator_identity.get("display_name"),
                payload.get("nickname"),
                creator_candidate.get("nickname"),
                creator_candidate.get("display_name"),
            ),
            "profile_url": profile_url,
        }
    )


def _creator_business_key(identity: Mapping[str, Any]) -> str:
    return build_creator_key(
        creator_id=first_non_empty(identity.get("creator_id")),
        uid=first_non_empty(identity.get("uid")),
        unique_id=first_non_empty(identity.get("unique_id")),
    )


def _extract_fastmoss_influencer_uid(profile_url: Any) -> str:
    text = coerce_str(profile_url)
    marker = "/influencer/detail/"
    if marker not in text:
        return ""
    return text.split(marker, 1)[-1].split("?", 1)[0].split("/", 1)[0]


def _resolve_fastmoss_creator_bundle(
    payload: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
) -> dict[str, Any]:
    inline_bundle = _resolve_inline_fastmoss_creator_bundle(payload, creator_identity=creator_identity)
    if inline_bundle:
        return inline_bundle

    fastmoss_settings = coerce_mapping(payload.get("fastmoss"))
    live_fetch = coerce_bool(
        fastmoss_settings.get("live_fetch"),
        default=bool(fastmoss_settings and _creator_business_key(creator_identity)),
    )
    if not live_fetch:
        return {}

    session_policy = coerce_mapping(payload.get("session_policy"))
    session = FastMossHTTPSession(
        phone=first_non_empty(fastmoss_settings.get("phone")),
        password=first_non_empty(fastmoss_settings.get("password")),
        base_url=first_non_empty(fastmoss_settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(
            payload.get("region"),
            fastmoss_settings.get("region"),
            "US",
        ),
        timeout=float(fastmoss_settings.get("timeout", 30.0) or 30.0),
    )
    with session:
        cookies = fastmoss_settings.get("browser_cookies")
        if isinstance(cookies, list):
            session.replace_browser_cookies(cookies)
        require_login = coerce_bool(
            session_policy.get("require_login"),
            default=coerce_bool(
                fastmoss_settings.get("ensure_logged_in"),
                default=bool(cookies or fastmoss_settings.get("phone")),
            ),
        )
        if require_login:
            session.ensure_logged_in()

        creator_id = first_non_empty(creator_identity.get("creator_id"))
        uid_candidate = first_non_empty(creator_identity.get("uid"))
        if not uid_candidate and creator_id.isdigit():
            uid_candidate = creator_id
        uid = session.resolve_author_uid(
            uid=uid_candidate,
            unique_id=first_non_empty(creator_identity.get("unique_id")),
        )
        return _fetch_live_fastmoss_creator_bundle(session, uid=uid, payload=payload)


def _resolve_inline_fastmoss_creator_bundle(
    payload: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
) -> dict[str, Any]:
    for key in (
        "fastmoss_creator_bundle",
        "creator_bundle",
        "author_bundle",
        "fastmoss_author_bundle",
        "mock_fastmoss_creator_bundle",
        "mock_author_bundle",
        "fastmoss_bundle",
        "mock_fastmoss_bundle",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate

    endpoint_bundle: dict[str, Any] = {}
    for key in (
        "base_info",
        "author_index",
        "stat_info",
        "cargo_summary",
        "author_contact",
        "shop_list",
        "goods_list",
        "video_list",
    ):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            endpoint_bundle[key] = candidate
    if endpoint_bundle:
        endpoint_bundle.setdefault("uid", first_non_empty(creator_identity.get("uid")))
        endpoint_bundle.setdefault("unique_id", first_non_empty(creator_identity.get("unique_id")))
    return endpoint_bundle


def _normalize_fastmoss_creator_bundle(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw_bundle)
    for key in (
        "base_info",
        "author_index",
        "stat_info",
        "cargo_summary",
        "author_contact",
        "shop_list",
        "goods_list",
        "video_list",
    ):
        payload = coerce_mapping(raw_bundle.get(key))
        data = coerce_mapping(payload.get("data"))
        if data:
            normalized[key] = data
    return normalized


def _fetch_live_fastmoss_creator_bundle(
    session: FastMossHTTPSession,
    *,
    uid: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    endpoints = _creator_fetch_endpoints(payload)
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    date_type = first_non_empty(fetch_plan.get("date_type"), fetch_plan.get("d_type"), 28)
    region = first_non_empty(payload.get("region"), session.default_region)
    bundle: dict[str, Any] = {"uid": uid}

    if "base_info" in endpoints:
        bundle["base_info"] = session.get_author_base_info(uid)
        bundle["unique_id"] = first_non_empty(bundle["base_info"].get("unique_id"), payload.get("unique_id"))
    if "author_index" in endpoints:
        bundle["author_index"] = session.get_author_index(uid)
    if "stat_info" in endpoints:
        bundle["stat_info"] = session.get_author_stat_info(uid)
    if "cargo_summary" in endpoints:
        bundle["cargo_summary"] = session.get_author_cargo_summary(uid)
    if "contact" in endpoints or "author_contact" in endpoints:
        bundle["author_contact"] = session.get_author_contact(uid)
    if "shop_list" in endpoints:
        shop_plan = coerce_mapping(fetch_plan.get("shop_list"))
        bundle["shop_list"] = session.get_author_shop_list(
            uid,
            page=_coerce_positive_int(shop_plan.get("page"), default=1),
            page_size=_coerce_positive_int(shop_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(shop_plan.get("order"), "sold_count,2"),
        )
    if "goods_list" in endpoints:
        goods_plan = coerce_mapping(fetch_plan.get("goods_list"))
        bundle["goods_list"] = session.list_author_goods(
            uid,
            page=_coerce_positive_int(goods_plan.get("page"), default=1),
            page_size=_coerce_positive_int(goods_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(goods_plan.get("order"), "sold_count,2"),
            date_type=first_non_empty(goods_plan.get("date_type"), date_type),
        )
    if "video_list" in endpoints:
        video_plan = coerce_mapping(fetch_plan.get("video_list"))
        bundle["video_list"] = session.get_author_video_list(
            uid,
            page=_coerce_positive_int(video_plan.get("page"), default=1),
            page_size=_coerce_positive_int(video_plan.get("page_size"), default=5),
            region=region,
            order=first_non_empty(video_plan.get("order"), "sold_count,2"),
            date_type=first_non_empty(video_plan.get("date_type"), date_type),
        )
    bundle["session_snapshot"] = session.cookie_snapshot()
    return bundle


def _creator_fetch_endpoints(payload: dict[str, Any]) -> set[str]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = {
        coerce_str(item)
        for item in _coerce_sequence(fetch_plan.get("endpoints"))
        if coerce_str(item)
    }
    if endpoints:
        return endpoints

    detail_level = first_non_empty(payload.get("detail_level"), "default").lower()
    endpoints = {"base_info", "author_index", "cargo_summary", "contact", "shop_list"}
    if "stat" in detail_level:
        endpoints.add("stat_info")
    if "goods" in detail_level or "product" in detail_level:
        endpoints.add("goods_list")
    if "video" in detail_level:
        endpoints.add("video_list")
    return endpoints


def _build_fastmoss_creator_fact_bundle(
    raw_bundle: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    source_context = coerce_mapping(payload.get("source_context"))
    relation_policy = coerce_mapping(payload.get("relation_policy"))
    uid = first_non_empty(raw_bundle.get("uid"), creator_identity.get("uid"), creator_identity.get("creator_id"))
    unique_id = first_non_empty(raw_bundle.get("unique_id"), creator_identity.get("unique_id"))
    creator_id = first_non_empty(creator_identity.get("creator_id"), unique_id, uid)
    source_product_id = _source_product_id_for_creator_relation(source_context, relation_policy)

    fact_bundle = merge_fact_bundles(
        new_fact_bundle(),
        map_fastmoss_author_bundle(
            raw_bundle,
            source_product_id=source_product_id,
            source_key=first_non_empty(source_context.get("source_record_id")),
            target_record_id=first_non_empty(source_context.get("target_record_id")),
            table_url=first_non_empty(source_context.get("source_table_ref"), source_context.get("table_url")),
        ),
    )
    video_list = coerce_mapping(raw_bundle.get("video_list"))
    if video_list:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_author_video_list(
                video_list,
                uid=uid,
                creator_id=creator_id,
                unique_id=unique_id,
            ),
        )
    goods_list = coerce_mapping(raw_bundle.get("goods_list"))
    if goods_list:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_author_goods_list(
                goods_list,
                uid=uid,
                creator_id=creator_id,
                unique_id=unique_id,
            ),
        )

    stat_info = coerce_mapping(raw_bundle.get("stat_info"))
    if stat_info:
        creator_items = fact_bundle.get("creators")
        if isinstance(creator_items, list):
            for creator in creator_items:
                if not isinstance(creator, dict):
                    continue
                facts = coerce_mapping(creator.get("facts"))
                facts["stat_info"] = stat_info
                creator["facts"] = facts

    _enrich_creator_source_product_relation(
        fact_bundle,
        source_context=source_context,
        source_product_id=source_product_id,
    )
    _append_fastmoss_creator_raw_responses(
        fact_bundle,
        raw_bundle,
        uid=uid,
        unique_id=unique_id,
    )
    return merge_fact_bundles(fact_bundle)


def _enrich_creator_source_product_relation(
    fact_bundle: dict[str, Any],
    *,
    source_context: Mapping[str, Any],
    source_product_id: str,
) -> None:
    if not source_product_id:
        return
    relations = coerce_mapping(fact_bundle.get("relations"))
    creator_products = relations.get("creator_products")
    if not isinstance(creator_products, list):
        return
    matched_sold_count = _metric_number(source_context.get("matched_product_sold_count"))
    for relation in creator_products:
        if not isinstance(relation, dict):
            continue
        if first_non_empty(relation.get("product_id")) != source_product_id:
            continue
        relation["source_record_id"] = first_non_empty(relation.get("source_record_id"), source_context.get("source_record_id"))
        relation["holiday_name"] = first_non_empty(relation.get("holiday_name"), source_context.get("holiday"))
        if matched_sold_count is not None and _metric_number(relation.get("sold_count")) in (None, 0):
            relation["sold_count"] = matched_sold_count
        metadata = coerce_mapping(relation.get("metadata"))
        raw = coerce_mapping(metadata.get("raw"))
        raw.update(
            compact_dict(
                {
                    "source_record_id": source_context.get("source_record_id"),
                    "holiday": source_context.get("holiday"),
                    "matched_product_sold_count": source_context.get("matched_product_sold_count"),
                }
            )
        )
        metadata["raw"] = raw
        relation["metadata"] = metadata


def _source_product_id_for_creator_relation(
    source_context: dict[str, Any],
    relation_policy: dict[str, Any],
) -> str:
    product_id = first_non_empty(source_context.get("product_id"), source_context.get("fastmoss_product_id"))
    include_relation = coerce_bool(
        relation_policy.get("include_source_product_relation"),
        default=bool(product_id),
    )
    if not include_relation:
        return ""
    minimum = _metric_number(relation_policy.get("min_source_product_sold_count"))
    observed = _metric_number(source_context.get("matched_product_sold_count"))
    if minimum is not None and observed is not None and observed < minimum:
        return ""
    return product_id


def _append_fastmoss_creator_raw_responses(
    fact_bundle: dict[str, Any],
    raw_bundle: dict[str, Any],
    *,
    uid: str,
    unique_id: str,
) -> None:
    endpoint_by_key = {
        "base_info": "author.base_info",
        "author_index": "author.index",
        "stat_info": "author.stat_info",
        "cargo_summary": "author.cargo_summary",
        "author_contact": "author.contact",
        "shop_list": "author.shop_list",
        "goods_list": "author.goods_list",
        "video_list": "author.video_list",
    }
    for key, endpoint in endpoint_by_key.items():
        response_payload = coerce_mapping(raw_bundle.get(key))
        if not response_payload:
            continue
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": endpoint,
                "request_url": "",
                "request_params": compact_dict({"uid": uid, "unique_id": unique_id}),
                "response_payload": response_payload,
                "status_code": 200,
            }
        )


def _contract_entities_from_fact_bundle(fact_bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "creators": [
            _contract_creator_entity(creator)
            for creator in coerce_mapping_list(fact_bundle.get("creators"))
        ],
        "products": [
            _contract_product_entity(product)
            for product in coerce_mapping_list(fact_bundle.get("products"))
        ],
        "shops": [
            _contract_shop_entity(shop)
            for shop in coerce_mapping_list(fact_bundle.get("shops"))
        ],
        "videos": [
            _contract_video_entity(video)
            for video in coerce_mapping_list(fact_bundle.get("videos"))
        ],
    }


def _contract_creator_entity(creator: dict[str, Any]) -> dict[str, Any]:
    facts = coerce_mapping(creator.get("facts"))
    base_info = coerce_mapping(facts.get("base_info"))
    author_index = coerce_mapping(facts.get("author_index"))
    stat_info = coerce_mapping(facts.get("stat_info"))
    cargo_summary = coerce_mapping(facts.get("cargo_summary"))
    author_contact = coerce_mapping(facts.get("author_contact"))
    metrics = _metric_fields(base_info, author_index, stat_info, cargo_summary)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("creator", _creator_entity_ref(creator)),
            "creator_id": first_non_empty(creator.get("creator_id"), creator.get("unique_id")),
            "uid": creator.get("uid"),
            "unique_id": creator.get("unique_id"),
            "nickname": creator.get("nickname"),
            "avatar_url": first_non_empty(base_info.get("avatar"), base_info.get("avatar_url")),
            "region": first_non_empty(creator.get("country_region"), base_info.get("region")),
            "profile_url": creator.get("profile_url"),
            "metrics": metrics,
            "contact": _contract_contact(author_contact),
            "source_platform": creator.get("source_platform"),
        }
    )


def _contract_product_entity(product: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(product.get("product_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("product", product_id),
            "product_id": product_id,
            "title": product.get("title"),
            "image_url": first_non_empty(product.get("image_url"), product.get("cover_url")),
            "source_platform": product.get("source_platform"),
        }
    )


def _contract_shop_entity(shop: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_entity_ref(shop)
    return compact_dict(
        {
            "entity_key": _contract_entity_key("shop", shop_ref),
            "seller_id": first_non_empty(shop.get("seller_id"), shop.get("shop_id")),
            "shop_id": first_non_empty(shop.get("shop_id"), shop.get("seller_id")),
            "shop_name": shop.get("shop_name"),
            "source_platform": shop.get("source_platform"),
        }
    )


def _contract_video_entity(video: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(video.get("video_id"))
    return compact_dict(
        {
            "entity_key": _contract_entity_key("video", video_id),
            "video_id": video_id,
            "title": video.get("title"),
            "cover_url": video.get("cover_url"),
            "video_url": video.get("video_url"),
            "source_platform": video.get("source_platform"),
        }
    )


def _contract_relations_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    relations = coerce_mapping(fact_bundle.get("relations"))
    result: list[dict[str, Any]] = []
    for relation in coerce_mapping_list(relations.get("creator_products")):
        result.append(_contract_creator_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("creator_videos")):
        result.append(_contract_creator_video_relation(relation))
    for relation in coerce_mapping_list(relations.get("video_products")):
        result.append(_contract_video_product_relation(relation))
    for relation in coerce_mapping_list(relations.get("shop_creators")):
        result.append(_contract_shop_creator_relation(relation))
    for relation in coerce_mapping_list(relations.get("product_shops")):
        result.append(_contract_product_shop_relation(relation))
    return [relation for relation in result if relation]


def _contract_creator_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    raw = _relation_raw(relation)
    creator_ref = _creator_ref_from_relation(relation)
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"creator_product:{creator_ref}:{product_id}",
            "relation_type": "creator_promotes_product",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "metrics": _relation_metric_fields(raw, relation),
            "source_context": _relation_source_context(relation, raw),
        }
    )


def _contract_creator_video_relation(relation: dict[str, Any]) -> dict[str, Any]:
    creator_ref = _creator_ref_from_relation(relation)
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    return compact_dict(
        {
            "relation_key": f"creator_video:{creator_ref}:{video_id}",
            "relation_type": "creator_published_video",
            "from_entity_key": _contract_entity_key("creator", creator_ref),
            "to_entity_key": _contract_entity_key("video", video_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_video_product_relation(relation: dict[str, Any]) -> dict[str, Any]:
    video_id = first_non_empty(relation.get("video_id"), _strip_key_prefix(relation.get("video_key")))
    product_id = first_non_empty(relation.get("product_id"))
    return compact_dict(
        {
            "relation_key": f"video_product:{video_id}:{product_id}",
            "relation_type": "video_mounts_product",
            "from_entity_key": _contract_entity_key("video", video_id),
            "to_entity_key": _contract_entity_key("product", product_id),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_shop_creator_relation(relation: dict[str, Any]) -> dict[str, Any]:
    shop_ref = _shop_ref_from_relation(relation)
    creator_ref = _creator_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"shop_creator:{shop_ref}:{creator_ref}",
            "relation_type": "shop_collaborates_with_creator",
            "from_entity_key": _contract_entity_key("shop", shop_ref),
            "to_entity_key": _contract_entity_key("creator", creator_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_product_shop_relation(relation: dict[str, Any]) -> dict[str, Any]:
    product_id = first_non_empty(relation.get("product_id"))
    shop_ref = _shop_ref_from_relation(relation)
    return compact_dict(
        {
            "relation_key": f"product_shop:{product_id}:{shop_ref}",
            "relation_type": "product_sold_by_shop",
            "from_entity_key": _contract_entity_key("product", product_id),
            "to_entity_key": _contract_entity_key("shop", shop_ref),
            "source": first_non_empty(relation.get("source_platform"), "fastmoss"),
            "source_context": _relation_source_context(relation, _relation_raw(relation)),
        }
    )


def _contract_media_refs_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    media_refs: list[dict[str, Any]] = []
    for asset in coerce_mapping_list(fact_bundle.get("media_assets")):
        entity_type = first_non_empty(asset.get("entity_type"))
        entity_ref = _strip_key_prefix(asset.get("entity_external_id"))
        media_refs.append(
            compact_dict(
                {
                    "entity_key": _contract_entity_key(entity_type, entity_ref),
                    "media_type": first_non_empty(asset.get("media_role"), "media"),
                    "source_url": asset.get("source_url"),
                    "source_platform": asset.get("source_platform"),
                }
            )
        )
    return media_refs


def _raw_response_refs_from_fact_bundle(fact_bundle: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for raw_response in coerce_mapping_list(fact_bundle.get("raw_api_responses")):
        endpoint = first_non_empty(raw_response.get("source_endpoint"))
        request_params = coerce_mapping(raw_response.get("request_params"))
        uid = first_non_empty(request_params.get("uid"), request_params.get("unique_id"), "unknown")
        if endpoint:
            refs.append(f"fastmoss://creator/{uid}/{endpoint}")
    return refs


def _build_fastmoss_creator_observations(
    raw_bundle: dict[str, Any],
    *,
    fact_bundle: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    creators = coerce_mapping_list(fact_bundle.get("creators"))
    creator_ref = _creator_entity_ref(creators[0]) if creators else _creator_business_key(
        _normalize_creator_identity(payload)
    )
    entity_key = _contract_entity_key("creator", _strip_key_prefix(creator_ref))
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    window_days = _coerce_positive_int(fetch_plan.get("date_type"), default=0)
    observed_at = first_non_empty(payload.get("observed_at"), _utc_now_iso())
    observations: list[dict[str, Any]] = []
    endpoint_by_key = {
        "base_info": "author.base_info",
        "author_index": "author.index",
        "stat_info": "author.stat_info",
        "cargo_summary": "author.cargo_summary",
    }
    for bundle_key, endpoint in endpoint_by_key.items():
        for metric_name, metric_value in _iter_metric_values(coerce_mapping(raw_bundle.get(bundle_key))):
            observations.append(
                compact_dict(
                    {
                        "entity_key": entity_key,
                        "metric_name": metric_name,
                        "metric_value": metric_value,
                        "window_days": window_days if bundle_key != "base_info" else 0,
                        "observed_at": observed_at,
                        "source": "fastmoss",
                        "source_endpoint": endpoint,
                    }
                )
            )
    return observations


def _creator_compat_fact_bundle(
    fact_bundle: dict[str, Any],
    *,
    creator_identity: dict[str, Any],
    media_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    creators = coerce_mapping_list(fact_bundle.get("creators"))
    creator = dict(creators[0]) if creators else dict(creator_identity)
    contract_creator = _contract_creator_entity(creator) if creators else {}
    creator_id = first_non_empty(
        creator.get("creator_id"),
        creator_identity.get("creator_id"),
        creator.get("unique_id"),
        creator.get("uid"),
    )
    nickname = first_non_empty(creator.get("nickname"), creator_identity.get("nickname"))
    avatar_url = first_non_empty(
        contract_creator.get("avatar_url"),
        _first_media_ref_url(media_refs, entity_type="creator"),
    )
    return compact_dict(
        {
            "entity_key": first_non_empty(
                contract_creator.get("entity_key"),
                _contract_entity_key("creator", creator_id),
            ),
            "creator_key": build_creator_key(
                creator_id=creator_id,
                uid=first_non_empty(creator.get("uid"), creator_identity.get("uid")),
                unique_id=first_non_empty(creator.get("unique_id"), creator_identity.get("unique_id")),
            ),
            "creator_id": creator_id,
            "uid": first_non_empty(creator.get("uid"), creator_identity.get("uid")),
            "unique_id": first_non_empty(creator.get("unique_id"), creator_identity.get("unique_id")),
            "nickname": nickname,
            "display_name": nickname,
            "profile_url": first_non_empty(creator.get("profile_url"), creator_identity.get("profile_url")),
            "avatar_url": avatar_url,
            "metrics": coerce_mapping(contract_creator.get("metrics")),
            "contact": coerce_mapping(contract_creator.get("contact")),
            "facts": coerce_mapping(creator.get("facts")),
        }
    )


def _creator_fetch_quality(
    raw_bundle: dict[str, Any],
    *,
    media_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    contact = _contract_contact(coerce_mapping(raw_bundle.get("author_contact")))
    missing_optional_fields: list[str] = []
    if not contact.get("available"):
        missing_optional_fields.append("contact.normalized_text")
    if not _first_media_ref_url(media_refs, entity_type="creator"):
        missing_optional_fields.append("creator.avatar_url")
    return {
        "contact_available": bool(contact.get("available")),
        "degraded_preview": _has_fastmoss_auth_preview(raw_bundle),
        "missing_optional_fields": missing_optional_fields,
    }


def _contract_contact(payload: dict[str, Any]) -> dict[str, Any]:
    contact_text = first_non_empty(
        payload.get("email"),
        payload.get("mail"),
        payload.get("whatsapp"),
        payload.get("phone"),
        payload.get("contact"),
        payload.get("contact_info"),
        payload.get("raw"),
    )
    return compact_dict(
        {
            "raw": contact_text,
            "normalized_text": contact_text,
            "available": bool(contact_text),
        }
    )


def _metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for payload in payloads:
        for metric_name, metric_value in _iter_metric_values(payload):
            metrics[metric_name] = metric_value
    return metrics


def _relation_metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for field_name in ("sold_count", "sale_amount", "commission_rate"):
        for payload in payloads:
            metric_value = _metric_number(payload.get(field_name))
            if metric_value is not None:
                metrics[field_name] = metric_value
                break
    return metrics


def _iter_metric_values(payload: Mapping[str, Any], *, prefix: str = "") -> list[tuple[str, Any]]:
    metrics: list[tuple[str, Any]] = []
    ignored_keys = {
        "id",
        "uid",
        "unique_id",
        "creator_id",
        "product_id",
        "video_id",
        "shop_id",
        "seller_id",
        "nickname",
        "name",
        "avatar",
        "avatar_url",
        "region",
        "country_region",
        "update_at",
    }
    for key, value in payload.items():
        key_text = coerce_str(key)
        if not key_text or key_text in ignored_keys:
            continue
        metric_name = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, Mapping):
            metrics.extend(_iter_metric_values(value, prefix=metric_name))
            continue
        metric_value = _metric_number(value)
        if metric_value is not None:
            metrics.append((metric_name, metric_value))
    return metrics


def _metric_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = coerce_str(value)
    if not text:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _relation_raw(relation: dict[str, Any]) -> dict[str, Any]:
    return coerce_mapping(coerce_mapping(relation.get("metadata")).get("raw"))


def _relation_source_context(relation: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            "source_record_id": first_non_empty(relation.get("source_record_id"), raw.get("source_record_id")),
            "target_record_id": relation.get("target_record_id"),
            "holiday": first_non_empty(relation.get("holiday_name"), raw.get("holiday")),
            "table_url": coerce_mapping(relation.get("metadata")).get("table_url"),
        }
    )


def _creator_entity_ref(creator: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            creator.get("creator_key"),
            creator.get("creator_id"),
            creator.get("uid"),
            creator.get("unique_id"),
        )
    )


def _shop_entity_ref(shop: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            shop.get("shop_key"),
            shop.get("shop_id"),
            shop.get("seller_id"),
            shop.get("shop_name"),
        )
    )


def _creator_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("creator_key"),
            relation.get("creator_id"),
            relation.get("uid"),
            relation.get("unique_id"),
        )
    )


def _shop_ref_from_relation(relation: Mapping[str, Any]) -> str:
    return _strip_key_prefix(
        first_non_empty(
            relation.get("shop_key"),
            relation.get("shop_id"),
            relation.get("seller_id"),
            relation.get("shop_name"),
        )
    )


def _contract_entity_key(entity_type: str, ref: Any) -> str:
    normalized_type = coerce_str(entity_type)
    normalized_ref = _strip_key_prefix(ref)
    if not normalized_type or not normalized_ref:
        return ""
    return f"fastmoss_{normalized_type}:{normalized_ref}"


def _strip_key_prefix(value: Any) -> str:
    text = first_non_empty(value)
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _first_media_ref_url(media_refs: list[dict[str, Any]], *, entity_type: str) -> str:
    prefix = f"fastmoss_{entity_type}:"
    for media_ref in media_refs:
        if coerce_str(media_ref.get("entity_key")).startswith(prefix):
            return first_non_empty(media_ref.get("source_url"))
    return ""


def _has_fastmoss_auth_preview(raw_bundle: dict[str, Any]) -> bool:
    for value in raw_bundle.values():
        payload = coerce_mapping(value)
        code = coerce_str(payload.get("code"))
        if code.startswith("MAG_AUTH_"):
            return True
    return False


def _coerce_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_fastmoss_product_metric_snapshots(raw_overview: dict[str, Any], *, product_id: str) -> list[dict[str, Any]]:
    overview = coerce_mapping(raw_overview.get("overview"))
    chart_list = coerce_mapping_list(raw_overview.get("chart_list"))
    if not overview and not chart_list:
        return []
    return [
        compact_dict(
            {
                "product_id": product_id,
                "source_platform": "fastmoss",
                "source_endpoint": "goods.overview",
                "window_days": raw_overview.get("d_type"),
                "window_start": chart_list[0].get("dt") if chart_list else "",
                "window_end": chart_list[-1].get("dt") if chart_list else "",
                "payload": {"overview": overview, "chart_list": chart_list},
            }
        )
    ]


def _build_fastmoss_daily_metrics(raw_overview: dict[str, Any], *, product_id: str) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for item in coerce_mapping_list(raw_overview.get("chart_list")):
        metrics.append(
            compact_dict(
                {
                    "product_id": product_id,
                    "metric_date": first_non_empty(item.get("dt"), item.get("date")),
                    "source_platform": "fastmoss",
                    "sold_count": item.get("inc_sold_count", item.get("sold_count")),
                    "sale_amount": item.get("inc_sale_amount", item.get("sale_amount")),
                    "price_amount": item.get("price", item.get("real_price_value")),
                    "currency": first_non_empty(item.get("currency"), raw_overview.get("currency")),
                    "payload": item,
                }
            )
        )
    return metrics


def _build_fastmoss_distribution_snapshots(raw_overview: dict[str, Any], *, product_id: str) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    window_days = raw_overview.get("d_type")
    distributions = (
        ("channel_distribution", "channel"),
        ("content_distribution", "content"),
        ("ads_distribution", "ads"),
    )
    for field_name, prefix in distributions:
        distribution = coerce_mapping(raw_overview.get(field_name))
        for metric_key, value_key, amount_key in (
            ("units_sold", "sold_count", "metric_amount"),
            ("gmv", "metric_value", "sale_amount"),
        ):
            metric_payload = coerce_mapping(distribution.get(metric_key))
            for item in coerce_mapping_list(metric_payload.get("list")):
                source_key = first_non_empty(item.get("source"), item.get("category"))
                snapshots.append(
                    compact_dict(
                        {
                            "product_id": product_id,
                            "distribution_type": f"{prefix}_{metric_key}",
                            "source_key": source_key,
                            "source_name": source_key,
                            "source_platform": "fastmoss",
                            "window_days": window_days,
                            "metric_value": item.get(value_key, item.get("sold_count"), item.get("propotion")),
                            "metric_amount": item.get(amount_key, item.get("sale_amount")),
                            "payload": item,
                        }
                    )
                )
    return snapshots


def _build_fastmoss_sku_metric_snapshots(
    raw_skus: dict[str, Any],
    raw_sku_distribution: dict[str, Any],
    *,
    product_id: str,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    sku_list = coerce_mapping_list(raw_skus.get("sku_list")) or coerce_mapping_list(raw_sku_distribution.get("sku_list"))
    window_days = first_non_empty(raw_skus.get("d_type"), raw_sku_distribution.get("d_type"))
    for item in sku_list:
        sku_id = first_non_empty(item.get("sku_id"), item.get("id"))
        sku_name = first_non_empty(item.get("sku_name"), item.get("name"), sku_id)
        snapshots.append(
            compact_dict(
                {
                    "product_id": product_id,
                    "sku_id": sku_id,
                    "sku_name": sku_name,
                    "sku_key": f"{product_id}:{first_non_empty(sku_id, sku_name)}",
                    "source_platform": "fastmoss",
                    "window_days": window_days,
                    "sold_count": item.get("sold_count"),
                    "sale_amount": item.get("sale_amount"),
                    "stock_count": item.get("stock"),
                    "payload": item,
                }
            )
        )
    return snapshots


def _resolve_artifact_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = coerce_mapping(payload.get("artifact_store"))
    if settings:
        return settings
    return compact_dict(
        {
            "artifact_store_provider": payload.get("artifact_store_provider"),
            "artifact_bucket": payload.get("artifact_bucket"),
            "artifact_object_prefix": payload.get("artifact_object_prefix"),
            "minio_endpoint": payload.get("minio_endpoint"),
            "minio_access_key": payload.get("minio_access_key"),
            "minio_secret_key": payload.get("minio_secret_key"),
            "minio_secure": payload.get("minio_secure"),
            "minio_region": payload.get("minio_region"),
            "minio_create_bucket": payload.get("minio_create_bucket"),
        }
    )


def _normalize_media_asset(asset: dict[str, Any], *, fallback_product_id: str = "") -> dict[str, Any]:
    entity_external_id = first_non_empty(asset.get("entity_external_id"), asset.get("product_id"), fallback_product_id)
    return compact_dict(
        {
            "entity_type": first_non_empty(asset.get("entity_type"), "product"),
            "entity_external_id": entity_external_id,
            "media_role": first_non_empty(asset.get("media_role"), "asset"),
            "source_url": asset.get("source_url"),
            "file_token": asset.get("file_token"),
            "local_path": asset.get("local_path"),
            "object_key": asset.get("object_key"),
            "file_name": asset.get("file_name"),
            "mime_type": asset.get("mime_type"),
            "bucket": asset.get("bucket"),
            "remote_uri": asset.get("remote_uri"),
            "source_platform": first_non_empty(asset.get("source_platform"), "tiktok"),
            "metadata": coerce_mapping(asset.get("metadata")),
        }
    )


def _plan_fact_bundle_upsert(fact_bundle: dict[str, Any]) -> dict[str, Any]:
    upserted_entities = bundle_entity_keys(fact_bundle)
    upserted_relations: list[str] = []
    for relation_key, items in coerce_mapping(fact_bundle.get("relations")).items():
        for item in coerce_mapping_list(items):
            upserted_relations.append(f"{relation_key}:{first_non_empty(item.get('relation_key'), item.get('product_id'), item.get('video_key'), item.get('shop_key'), item.get('creator_key'))}")
    observation_refs = []
    observation_refs.extend(
        f"product_metric:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_metric_snapshots")), start=1)
    )
    observation_refs.extend(
        f"product_daily:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_daily_metrics")), start=1)
    )
    observation_refs.extend(
        f"distribution:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_distribution_snapshots")), start=1)
    )
    observation_refs.extend(
        f"sku_metric:{index}" for index, _ in enumerate(coerce_mapping_list(fact_bundle.get("product_sku_metric_snapshots")), start=1)
    )
    return {
        "upserted_entities": upserted_entities,
        "upserted_relations": upserted_relations,
        "observation_refs": observation_refs,
        "persisted_counts": {
            "products": len(coerce_mapping_list(fact_bundle.get("products"))),
            "product_skus": len(coerce_mapping_list(fact_bundle.get("product_skus"))),
            "shops": len(coerce_mapping_list(fact_bundle.get("shops"))),
            "creators": len(coerce_mapping_list(fact_bundle.get("creators"))),
            "videos": len(coerce_mapping_list(fact_bundle.get("videos"))),
            "media_assets": len(coerce_mapping_list(fact_bundle.get("media_assets"))),
            "relations": len(upserted_relations),
            "observations": len(observation_refs),
        },
    }


def _persist_fact_bundle(fact_bundle: dict[str, Any], *, fact_db_url: str) -> dict[str, Any]:
    store = TKFactStore(db_url=fact_db_url)
    upserted_entities: list[str] = []
    upserted_relations: list[str] = []
    observation_refs: list[str] = []
    asset_id_by_key: dict[str, str] = {}

    for product in coerce_mapping_list(fact_bundle.get("products")):
        row = store.upsert_product(
            product_id=coerce_str(product.get("product_id")),
            product_url=coerce_str(product.get("product_url")),
            normalized_url=coerce_str(product.get("normalized_url")),
            title=coerce_str(product.get("title")),
            holiday=coerce_str(product.get("holiday")),
            seller_name=coerce_str(first_non_empty(product.get("seller_name"), product.get("shop_name"))),
            source_platform=coerce_str(product.get("source_platform")),
            facts=coerce_mapping(product.get("facts")),
        )
        if row:
            upserted_entities.append(f"product:{row.get('product_id')}")

    for sku in coerce_mapping_list(fact_bundle.get("product_skus")):
        row = store.upsert_product_sku(
            product_id=coerce_str(sku.get("product_id")),
            sku_id=coerce_str(sku.get("sku_id")),
            sku_name=coerce_str(sku.get("sku_name")),
            spec_name=coerce_str(sku.get("spec_name")),
            price_text=coerce_str(sku.get("price_text")),
            stock_count=sku.get("stock_count"),
            facts=coerce_mapping(sku.get("facts")),
        )
        if row:
            upserted_entities.append(f"product_sku:{row.get('sku_key')}")

    for shop in coerce_mapping_list(fact_bundle.get("shops")):
        row = store.upsert_shop(
            shop_id=coerce_str(shop.get("shop_id")),
            shop_name=coerce_str(shop.get("shop_name")),
            shop_url=coerce_str(shop.get("shop_url")),
            source_platform=coerce_str(shop.get("source_platform")),
            facts=coerce_mapping(shop.get("facts")),
        )
        if row:
            upserted_entities.append(f"shop:{row.get('shop_key')}")

    for creator in coerce_mapping_list(fact_bundle.get("creators")):
        row = store.upsert_creator(
            creator_id=coerce_str(creator.get("creator_id")),
            uid=coerce_str(creator.get("uid")),
            unique_id=coerce_str(creator.get("unique_id")),
            nickname=coerce_str(creator.get("nickname")),
            profile_url=coerce_str(creator.get("profile_url")),
            source_platform=coerce_str(creator.get("source_platform")),
            facts=coerce_mapping(creator.get("facts")),
        )
        if row:
            upserted_entities.append(f"creator:{row.get('creator_key')}")

    for video in coerce_mapping_list(fact_bundle.get("videos")):
        row = store.upsert_video(
            video_id=coerce_str(video.get("video_id")),
            creator_key=coerce_str(video.get("creator_key")),
            product_id=coerce_str(video.get("product_id")),
            title=coerce_str(video.get("title")),
            video_url=coerce_str(video.get("video_url")),
            cover_url=coerce_str(video.get("cover_url")),
            source_platform=coerce_str(video.get("source_platform")),
            facts=coerce_mapping(video.get("facts")),
        )
        if row:
            upserted_entities.append(f"video:{row.get('video_key')}")

    for asset in coerce_mapping_list(fact_bundle.get("media_assets")):
        row = store.upsert_media_asset(
            source_url=coerce_str(asset.get("source_url")),
            file_token=coerce_str(asset.get("file_token")),
            local_path=coerce_str(first_non_empty(asset.get("source_path"), asset.get("local_path"))),
            object_key=coerce_str(asset.get("object_key")),
            file_name=coerce_str(asset.get("file_name")),
            mime_type=coerce_str(asset.get("mime_type")),
            source_platform=coerce_str(asset.get("source_platform")),
            metadata=coerce_mapping(asset.get("metadata")),
        )
        if row:
            asset_key = first_non_empty(row.get("asset_key"))
            asset_id_by_key[asset_key] = coerce_str(row.get("asset_id"))
            upserted_entities.append(f"asset:{asset_key}")
            entity_type = coerce_str(asset.get("entity_type"))
            entity_external_id = coerce_str(asset.get("entity_external_id"))
            media_role = coerce_str(asset.get("media_role"))
            if entity_type and entity_external_id and media_role:
                linked = store.link_media_asset(
                    entity_type=entity_type,
                    entity_external_id=entity_external_id,
                    asset_id=coerce_str(row.get("asset_id")),
                    media_role=media_role,
                    metadata=coerce_mapping(asset.get("metadata")),
                )
                if linked:
                    upserted_relations.append(f"entity_media_asset:{linked.get('relation_key')}")

    relations = coerce_mapping(fact_bundle.get("relations"))
    for relation in coerce_mapping_list(relations.get("product_shops")):
        row = store.upsert_product_shop_relation(
            product_id=coerce_str(relation.get("product_id")),
            shop_key=first_non_empty(
                relation.get("shop_key"),
                build_shop_key(shop_id=coerce_str(relation.get("shop_id")), shop_name=coerce_str(relation.get("shop_name"))),
            ),
            shop_id=coerce_str(relation.get("shop_id")),
            shop_name=coerce_str(relation.get("shop_name")),
            relation_role=coerce_str(first_non_empty(relation.get("relation_role"), "seller")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"product_shop:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("creator_products")):
        row = store.upsert_creator_product_relation(
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            product_id=coerce_str(relation.get("product_id")),
            creator_id=coerce_str(relation.get("creator_id")),
            source_record_id=coerce_str(relation.get("source_record_id")),
            target_record_id=coerce_str(relation.get("target_record_id")),
            holiday_name=coerce_str(relation.get("holiday_name")),
            sold_count=relation.get("sold_count"),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"creator_product:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("creator_videos")):
        row = store.upsert_creator_video_relation(
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            video_key=first_non_empty(relation.get("video_key"), f"video:{coerce_str(relation.get('video_id'))}"),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"creator_video:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("video_products")):
        row = store.upsert_video_product_relation(
            video_key=first_non_empty(relation.get("video_key"), f"video:{coerce_str(relation.get('video_id'))}"),
            product_id=coerce_str(relation.get("product_id")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"video_product:{row.get('relation_key')}")

    for relation in coerce_mapping_list(relations.get("shop_creators")):
        row = store.upsert_shop_creator_relation(
            shop_key=first_non_empty(
                relation.get("shop_key"),
                build_shop_key(shop_id=coerce_str(relation.get("shop_id")), shop_name=coerce_str(relation.get("shop_name"))),
            ),
            creator_key=first_non_empty(
                relation.get("creator_key"),
                build_creator_key(
                    creator_id=coerce_str(relation.get("creator_id")),
                    uid=coerce_str(relation.get("uid")),
                    unique_id=coerce_str(relation.get("unique_id")),
                ),
            ),
            shop_name=coerce_str(relation.get("shop_name")),
            creator_id=coerce_str(relation.get("creator_id")),
            source_platform=coerce_str(relation.get("source_platform")),
            metadata=coerce_mapping(relation.get("metadata")),
        )
        if row:
            upserted_relations.append(f"shop_creator:{row.get('relation_key')}")

    raw_id_by_key: dict[str, str] = {}
    for raw_response in coerce_mapping_list(fact_bundle.get("raw_api_responses")):
        row = store.record_raw_api_response(
            source_platform=coerce_str(raw_response.get("source_platform")),
            source_endpoint=coerce_str(raw_response.get("source_endpoint")),
            request_url=coerce_str(raw_response.get("request_url")),
            request_params=coerce_mapping(raw_response.get("request_params")),
            response_payload=coerce_mapping(raw_response.get("response_payload")),
            status_code=int(raw_response.get("status_code", 0) or 0),
        )
        if row:
            raw_key = f"{row.get('source_platform')}:{row.get('source_endpoint')}:{row.get('request_url')}"
            raw_id_by_key[raw_key] = coerce_str(row.get("raw_response_id"))

    for observation in coerce_mapping_list(fact_bundle.get("product_metric_snapshots")):
        latest = store.upsert_product_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            source_platform=coerce_str(observation.get("source_platform")),
            source_endpoint=coerce_str(observation.get("source_endpoint")),
            window_days=int(observation.get("window_days", 0) or 0),
            window_start=coerce_str(observation.get("window_start")),
            window_end=coerce_str(observation.get("window_end")),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            source_platform=coerce_str(observation.get("source_platform")),
            source_endpoint=coerce_str(observation.get("source_endpoint")),
            window_days=int(observation.get("window_days", 0) or 0),
            window_start=coerce_str(observation.get("window_start")),
            window_end=coerce_str(observation.get("window_end")),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"product_window_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"product_window_observation:{observed.get('observation_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_daily_metrics")):
        row = store.upsert_product_daily_metric(
            product_id=coerce_str(observation.get("product_id")),
            metric_date=coerce_str(observation.get("metric_date")),
            source_platform=coerce_str(observation.get("source_platform")),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            price_amount=observation.get("price_amount"),
            currency=coerce_str(observation.get("currency")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if row:
            observation_refs.append(f"product_daily:{row.get('metric_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_distribution_snapshots")):
        latest = store.upsert_product_distribution_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            distribution_type=coerce_str(observation.get("distribution_type")),
            source_key=coerce_str(observation.get("source_key")),
            source_name=coerce_str(observation.get("source_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            metric_value=observation.get("metric_value"),
            metric_amount=observation.get("metric_amount"),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_distribution_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            distribution_type=coerce_str(observation.get("distribution_type")),
            source_key=coerce_str(observation.get("source_key")),
            source_name=coerce_str(observation.get("source_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            metric_value=observation.get("metric_value"),
            metric_amount=observation.get("metric_amount"),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"distribution_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"distribution_observation:{observed.get('observation_id')}")

    for observation in coerce_mapping_list(fact_bundle.get("product_sku_metric_snapshots")):
        latest = store.upsert_product_sku_window_latest(
            product_id=coerce_str(observation.get("product_id")),
            sku_key=coerce_str(observation.get("sku_key")),
            sku_id=coerce_str(observation.get("sku_id")),
            sku_name=coerce_str(observation.get("sku_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            stock_count=observation.get("stock_count"),
            payload=coerce_mapping(observation.get("payload")),
        )
        observed = store.record_product_sku_window_observation(
            product_id=coerce_str(observation.get("product_id")),
            sku_key=coerce_str(observation.get("sku_key")),
            sku_id=coerce_str(observation.get("sku_id")),
            sku_name=coerce_str(observation.get("sku_name")),
            source_platform=coerce_str(observation.get("source_platform")),
            window_days=int(observation.get("window_days", 0) or 0),
            sold_count=observation.get("sold_count"),
            sale_amount=observation.get("sale_amount"),
            stock_count=observation.get("stock_count"),
            observation_reason=coerce_str(first_non_empty(observation.get("observation_reason"), "handler_upsert")),
            payload=coerce_mapping(observation.get("payload")),
        )
        if latest:
            observation_refs.append(f"sku_latest:{latest.get('latest_id')}")
        if observed:
            observation_refs.append(f"sku_observation:{observed.get('observation_id')}")

    persisted_counts = {
        "products": sum(1 for key in upserted_entities if key.startswith("product:")),
        "product_skus": sum(1 for key in upserted_entities if key.startswith("product_sku:")),
        "shops": sum(1 for key in upserted_entities if key.startswith("shop:")),
        "creators": sum(1 for key in upserted_entities if key.startswith("creator:")),
        "videos": sum(1 for key in upserted_entities if key.startswith("video:")),
        "media_assets": sum(1 for key in upserted_entities if key.startswith("asset:")),
        "relations": len(upserted_relations),
        "observations": len(observation_refs),
    }
    return {
        "upserted_entities": upserted_entities,
        "upserted_relations": upserted_relations,
        "observation_refs": observation_refs,
        "persisted_counts": persisted_counts,
    }
