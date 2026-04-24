from __future__ import annotations

import tempfile
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    first_non_empty,
    new_fact_bundle,
    now_timestamp,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    create_store_from_settings,
    sync_artifact_specs,
)
from pathlib import Path
from typing import Any

HANDLER_CODE = "media_asset_sync"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


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


__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
