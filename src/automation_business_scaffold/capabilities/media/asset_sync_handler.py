from __future__ import annotations

import hashlib
import mimetypes
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    failed_result,
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
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore
from automation_business_scaffold.infrastructure.rate_limit import RequestPacer, resolve_api_request_pacer_config
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
    sync_referenced_files = _sync_referenced_files_enabled(payload, artifact_settings)

    specs: list[ArtifactFileSpec] = []
    local_assets_by_path: dict[str, dict[str, Any]] = {}
    synced_assets: list[dict[str, Any]] = []
    synced_assets_by_ref: dict[str, dict[str, Any]] = {}
    deferred_reuse_assets: list[tuple[dict[str, Any], str]] = []
    warnings: list[str] = []
    fact_store = _create_fact_store(payload, warnings=warnings)
    request_pacer = RequestPacer(resolve_api_request_pacer_config(payload, provider="media"))

    for index, asset in enumerate(asset_refs):
        normalized_asset = _normalize_media_asset(asset, fallback_product_id=payload.get("product_id"))
        asset_ref_key = _asset_ref_key(normalized_asset)
        if asset_ref_key and asset_ref_key in synced_assets_by_ref:
            existing_asset = synced_assets_by_ref[asset_ref_key]
            if existing_asset.get("sync_state") == "pending_upload":
                deferred_reuse_assets.append((normalized_asset, asset_ref_key))
            else:
                synced_assets.append(_reused_in_run_media_asset(normalized_asset, existing_asset))
            continue
        cached_asset = _find_reusable_media_asset(fact_store, normalized_asset)
        if cached_asset:
            reused_asset = _reused_media_asset(normalized_asset, cached_asset)
            synced_assets.append(reused_asset)
            if asset_ref_key:
                synced_assets_by_ref[asset_ref_key] = reused_asset
            continue
        local_path = Path(coerce_str(normalized_asset.get("local_path"))).expanduser()
        if local_path.exists() and local_path.is_file():
            _append_artifact_spec(
                specs,
                local_assets_by_path,
                normalized_asset,
                index=index,
                handler_code=context.handler_code,
                local_path=local_path,
            )
            if asset_ref_key:
                pending_asset = dict(normalized_asset)
                pending_asset["sync_state"] = "pending_upload"
                synced_assets_by_ref[asset_ref_key] = pending_asset
            continue
        if coerce_str(normalized_asset.get("local_path")):
            warnings.append(f"Local asset path not found: {normalized_asset.get('local_path')}")
        if sync_referenced_files and coerce_str(normalized_asset.get("source_url")):
            try:
                downloaded_asset = _download_referenced_asset(
                    normalized_asset,
                    payload=payload,
                    artifact_root=artifact_root,
                    index=index,
                    request_pacer=request_pacer,
                )
                downloaded_path = Path(coerce_str(downloaded_asset.get("local_path"))).expanduser()
                _append_artifact_spec(
                    specs,
                    local_assets_by_path,
                    downloaded_asset,
                    index=index,
                    handler_code=context.handler_code,
                    local_path=downloaded_path,
                )
                if asset_ref_key:
                    pending_asset = dict(downloaded_asset)
                    pending_asset["sync_state"] = "pending_upload"
                    synced_assets_by_ref[asset_ref_key] = pending_asset
                continue
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Referenced asset download failed: {normalized_asset.get('source_url')} ({exc})")
        normalized_asset["sync_state"] = "referenced"
        synced_assets.append(normalized_asset)
        if asset_ref_key:
            synced_assets_by_ref[asset_ref_key] = normalized_asset

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
            synced_asset = compact_dict(synced_asset)
            synced_assets.append(synced_asset)
            for asset_ref_key in {*_asset_ref_keys(base_asset), *_asset_ref_keys(synced_asset)}:
                if asset_ref_key:
                    synced_assets_by_ref[asset_ref_key] = synced_asset
            artifact_refs.append(record.to_dict())
        for duplicate_asset, asset_ref_key in deferred_reuse_assets:
            existing_asset = synced_assets_by_ref.get(asset_ref_key, {})
            if existing_asset:
                synced_assets.append(_reused_in_run_media_asset(duplicate_asset, existing_asset))

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
    if _coerce_bool(payload.get("require_materialized_assets")):
        referenced_assets = [asset for asset in synced_assets if asset.get("sync_state") == "referenced"]
        if referenced_assets:
            return failed_result(
                context,
                error=build_error(
                    error_type="media_sync_failed",
                    error_code="media_asset_materialization_failed",
                    message="Media asset sync requires every referenced fact media asset to be materialized.",
                    retryable=True,
                    details={"referenced_count": len(referenced_assets)},
                ),
                summary=summary,
                result=result,
                warnings=tuple(warnings),
            )
    if warnings:
        return success_result(context, summary=summary, result=result, warnings=tuple(warnings))
    return success_result(context, summary=summary, result=result)


def _create_fact_store(payload: dict[str, Any], *, warnings: list[str]) -> TKFactStore | None:
    request_payload = coerce_mapping(payload.get("request_payload"))
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
        request_payload.get("db_url"),
        payload.get("execution_control_db_url"),
        request_payload.get("execution_control_db_url"),
    )
    if not fact_db_url:
        return None
    try:
        return TKFactStore(db_url=fact_db_url)
    except Exception as exc:  # noqa: BLE001 - media sync can proceed without cache lookup.
        warnings.append(f"Media asset cache lookup disabled: {exc}")
        return None


def _find_reusable_media_asset(fact_store: TKFactStore | None, asset: dict[str, Any]) -> dict[str, Any]:
    if fact_store is None:
        return {}
    cached = fact_store.find_media_asset(
        source_url=coerce_str(asset.get("source_url")),
        file_token=coerce_str(asset.get("file_token")),
        local_path=coerce_str(asset.get("local_path")),
        object_key=coerce_str(asset.get("object_key")),
    )
    if not cached:
        return {}
    if coerce_str(cached.get("object_key")) or coerce_str(cached.get("remote_uri")):
        return cached
    return {}


def _reused_media_asset(asset: dict[str, Any], cached: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            **asset,
            "sync_state": "reused",
            "asset_id": cached.get("asset_id"),
            "asset_key": cached.get("asset_key"),
            "source_url": first_non_empty(asset.get("source_url"), cached.get("source_url")),
            "file_token": first_non_empty(cached.get("file_token"), asset.get("file_token")),
            "local_path": first_non_empty(cached.get("local_path"), asset.get("local_path")),
            "object_key": first_non_empty(cached.get("object_key"), asset.get("object_key")),
            "file_name": first_non_empty(cached.get("file_name"), asset.get("file_name")),
            "mime_type": first_non_empty(cached.get("mime_type"), asset.get("mime_type")),
            "source_platform": first_non_empty(asset.get("source_platform"), cached.get("source_platform")),
        }
    )


def _reused_in_run_media_asset(asset: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {
            **asset,
            "sync_state": "reused_in_run",
            "asset_id": existing.get("asset_id"),
            "asset_key": existing.get("asset_key"),
            "source_url": first_non_empty(asset.get("source_url"), existing.get("source_url")),
            "file_token": first_non_empty(asset.get("file_token"), existing.get("file_token")),
            "local_path": first_non_empty(existing.get("local_path"), asset.get("local_path")),
            "source_path": first_non_empty(existing.get("source_path"), asset.get("source_path")),
            "object_key": existing.get("object_key"),
            "bucket": existing.get("bucket"),
            "remote_uri": existing.get("remote_uri"),
            "file_name": first_non_empty(existing.get("file_name"), asset.get("file_name")),
            "mime_type": first_non_empty(existing.get("mime_type"), asset.get("mime_type")),
            "source_platform": first_non_empty(asset.get("source_platform"), existing.get("source_platform")),
            "metadata": asset.get("metadata"),
        }
    )


def _asset_ref_key(asset: dict[str, Any]) -> str:
    keys = _asset_ref_keys(asset)
    return keys[0] if keys else ""


def _asset_ref_keys(asset: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for prefix, value in (
        ("file_token", asset.get("file_token")),
        ("object_key", asset.get("object_key")),
        ("local_path", asset.get("local_path")),
        ("source_path", asset.get("source_path")),
        ("source_url", asset.get("source_url")),
    ):
        text = coerce_str(value)
        if text:
            keys.append(f"{prefix}:{text}")
    return keys


def _resolve_artifact_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = coerce_mapping(payload.get("artifact_store"))
    if settings:
        return settings
    explicit_settings = compact_dict(
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
    if explicit_settings:
        return explicit_settings
    defaults = get_execution_control_defaults()
    return compact_dict(
        {
            "artifact_store_provider": defaults.artifact_store_provider,
            "artifact_bucket": defaults.artifact_bucket,
            "artifact_object_prefix": defaults.artifact_object_prefix,
            "minio_endpoint": defaults.minio_endpoint,
            "minio_access_key": defaults.minio_access_key,
            "minio_secret_key": defaults.minio_secret_key,
            "minio_secure": defaults.minio_secure,
            "minio_region": defaults.minio_region,
            "minio_create_bucket": defaults.minio_create_bucket,
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


def _append_artifact_spec(
    specs: list[ArtifactFileSpec],
    local_assets_by_path: dict[str, dict[str, Any]],
    asset: dict[str, Any],
    *,
    index: int,
    handler_code: str,
    local_path: Path,
) -> None:
    specs.append(
        ArtifactFileSpec(
            kind=first_non_empty(asset.get("media_role"), "asset_file"),
            step_id=handler_code,
            relative_name=f"assets/{index:03d}_{local_path.name}",
            path=local_path,
            content_type=coerce_str(asset.get("mime_type")),
            metadata={
                "entity_type": coerce_str(asset.get("entity_type")),
                "entity_external_id": coerce_str(asset.get("entity_external_id")),
                "media_role": coerce_str(asset.get("media_role")),
            },
        )
    )
    local_assets_by_path[str(local_path.resolve())] = asset


def _sync_referenced_files_enabled(payload: dict[str, Any], artifact_settings: dict[str, Any]) -> bool:
    for source in (payload, artifact_settings):
        value = source.get("sync_referenced_files") if isinstance(source, dict) else None
        if value not in (None, ""):
            return _coerce_bool(value)
    return get_execution_control_defaults().sync_referenced_files


def _download_referenced_asset(
    asset: dict[str, Any],
    *,
    payload: dict[str, Any],
    artifact_root: Path,
    index: int,
    request_pacer: RequestPacer,
) -> dict[str, Any]:
    source_url = coerce_str(asset.get("source_url"))
    timeout_seconds = _coerce_int(
        first_non_empty(payload.get("media_download_timeout_seconds"), payload.get("download_timeout_seconds")),
        default=30,
    )
    request = Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
    request_pacer.wait_before_request("media:download")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URLs come from trusted workflow fetches.
            content = response.read()
            content_type = coerce_str(response.headers.get("Content-Type"))
    finally:
        request_pacer.mark_request_finished("media:download")
    if not content:
        raise ValueError("downloaded asset is empty")

    suffix = _guess_media_suffix(source_url, content_type)
    file_name = _safe_file_name(
        first_non_empty(
            asset.get("file_name"),
            f"{_safe_segment(first_non_empty(asset.get('entity_external_id'), payload.get('product_id'), 'product'))}-"
            f"{_safe_segment(first_non_empty(asset.get('media_role'), 'asset'))}-"
            f"{index:03d}-{hashlib.sha1(source_url.encode('utf-8')).hexdigest()[:12]}{suffix}",
        )
    )
    product_id = _safe_segment(first_non_empty(asset.get("entity_external_id"), payload.get("product_id"), "unknown-product"))
    download_root = Path(
        first_non_empty(
            payload.get("media_download_dir"),
            payload.get("image_download_dir"),
            artifact_root / "downloaded_media",
        )
    ).expanduser()
    target_dir = download_root / product_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / file_name
    target_path.write_bytes(content)

    downloaded = dict(asset)
    downloaded["local_path"] = str(target_path)
    downloaded["file_name"] = target_path.name
    downloaded["mime_type"] = first_non_empty(asset.get("mime_type"), _normalize_content_type(content_type, suffix))
    return downloaded


def _guess_media_suffix(source_url: str, content_type: str) -> str:
    guessed = mimetypes.guess_extension(str(content_type).split(";")[0].strip()) if content_type else ""
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".mp4", ".mov"}:
        return suffix
    return ".bin"


def _normalize_content_type(content_type: str, suffix: str) -> str:
    normalized = str(content_type or "").split(";")[0].strip().lower()
    if normalized:
        return normalized
    return mimetypes.types_map.get(suffix.lower(), "application/octet-stream")


def _safe_file_name(value: str) -> str:
    name = Path(str(value or "").strip()).name
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in name).strip("-") or "asset.bin"


def _safe_segment(value: Any) -> str:
    text = coerce_str(value)
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in text).strip("-") or "unknown"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
