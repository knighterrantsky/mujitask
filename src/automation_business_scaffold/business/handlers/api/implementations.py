from __future__ import annotations

import html
import json
import os
import re
import tempfile
from collections.abc import Mapping
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
            raw_bundle = _resolve_fastmoss_bundle(payload, product_id=product_id)
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
            related_creators = _extract_related_creators(fact_bundle)
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


def _resolve_fastmoss_bundle(payload: dict[str, Any], *, product_id: str) -> dict[str, Any]:
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
        return {
            "base": session.get_product_base(product_id),
            "overview": session.get_product_overview(product_id, d_type=d_type),
            "skus": session.get_product_skus(product_id, d_type=d_type),
            "sku_distribution": session.get_product_sku_distribution(product_id, d_type=d_type),
            "session_snapshot": session.cookie_snapshot(),
        }


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
        fact_bundle["product_metric_snapshots"].extend(
            _build_fastmoss_product_metric_snapshots(overview, product_id=product_id)
        )
        fact_bundle["product_daily_metrics"].extend(_build_fastmoss_daily_metrics(overview, product_id=product_id))
        fact_bundle["product_distribution_snapshots"].extend(
            _build_fastmoss_distribution_snapshots(overview, product_id=product_id)
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
            _build_fastmoss_sku_metric_snapshots(skus, coerce_mapping(raw_bundle.get("sku_distribution")), product_id=product_id)
        )
    if related_creators:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_author(related_creators, product_id=product_id))
    if videos:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_video(videos, product_id=product_id))

    return merge_fact_bundles(fact_bundle)


def _extract_related_creators(fact_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    creators = []
    for creator in coerce_mapping_list(fact_bundle.get("creators")):
        creators.append(
            compact_dict(
                {
                    "creator_key": first_non_empty(
                        creator.get("creator_key"),
                        creator.get("creator_id"),
                        creator.get("uid"),
                        creator.get("unique_id"),
                    ),
                    "creator_id": creator.get("creator_id"),
                    "uid": creator.get("uid"),
                    "unique_id": creator.get("unique_id"),
                    "nickname": creator.get("nickname"),
                }
            )
        )
    return creators


def _build_fastmoss_metrics_snapshot(raw_bundle: dict[str, Any], *, product_id: str) -> dict[str, Any]:
    overview = coerce_mapping(raw_bundle.get("overview"))
    return compact_dict(
        {
            "product_id": product_id,
            "window_days": overview.get("d_type"),
            "overview": coerce_mapping(overview.get("overview")),
            "chart_points": len(coerce_mapping_list(overview.get("chart_list"))),
            "session_snapshot": coerce_mapping(raw_bundle.get("session_snapshot")),
        }
    )


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
