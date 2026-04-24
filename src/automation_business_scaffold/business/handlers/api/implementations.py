from __future__ import annotations

import tempfile
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
