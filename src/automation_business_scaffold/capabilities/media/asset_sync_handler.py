from __future__ import annotations

from contextlib import suppress
from html import unescape
import hashlib
import mimetypes
import re
import tempfile
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

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
    partial_success_result,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_sync import (
    ArtifactFileSpec,
    create_store_from_settings,
    sync_artifact_specs,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    join_object_key,
    normalize_artifact_store_provider,
)
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import AmazonFactStore
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore
from automation_business_scaffold.infrastructure.rate_limit import (
    RequestPacer,
    resolve_api_request_pacer_config,
)
from pathlib import Path
from typing import Any

HANDLER_CODE = "media_asset_sync"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]
_TK_MEDIA_CACHE_PLATFORMS = frozenset({"fastmoss", "tiktok"})
_AMAZON_US_MEDIA_DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024
_AMAZON_US_MEDIA_ALLOWED_HOST_SUFFIXES = (
    "media-amazon.com",
    "ssl-images-amazon.com",
)
_AMAZON_MEDIA_UNSAFE_PATH = re.compile(
    r"(?:[<>{}\\\x00-\x1f\x7f]|(?:(?<![A-Za-z0-9])(?:authorization|bearer|cookie|"
    r"token|access[_-]?token|api[_-]?key|secret[_-]?key|session[_-]?secret|password|"
    r"credential)(?![A-Za-z0-9])|(?:authorization|bearer|cookie|token|access[_-]?token|"
    r"api[_-]?key|secret[_-]?key|session[_-]?secret|password|credential)(?=[=:])))",
    re.IGNORECASE,
)
_AMAZON_IMAGE_TRANSFORM_SEGMENT = re.compile(
    r"\._(?:AC|SL|SX|SY|SR|US|UL|UX|UY|QL|UF|CR|FM|AA|SS|SC|PK|PI|PA)"
    r"[A-Za-z0-9,+.-]*(?:_[A-Za-z0-9,+.-]+)*_\."
    r"(?P<extension>jpe?g|png|webp|gif|avif)$",
    re.IGNORECASE,
)
_AMAZON_CALLER_MATERIALIZED_FIELDS = (
    "local_path",
    "object_key",
    "source_path",
    "bucket",
    "remote_uri",
    "file_token",
)


class _AmazonMediaNotModified(Exception):
    pass


class _BoundedRedirectResponse:
    def __init__(self, response: Any, *, max_bytes: int) -> None:
        self._response = response
        self._max_bytes = max_bytes
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        read_limit = self._max_bytes - self._bytes_read + 1
        if size >= 0:
            read_limit = min(size, read_limit)
        content = self._response.read(read_limit)
        self._bytes_read += len(content)
        if self._bytes_read > self._max_bytes:
            with suppress(Exception):
                self.close()
            raise ValueError("redirect response exceeds media_download_max_bytes")
        return content

    def close(self) -> None:
        self._response.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class _GovernedMediaRedirectHandler(HTTPRedirectHandler):
    def __init__(
        self,
        allowed_host_suffixes: tuple[str, ...],
        *,
        max_bytes: int = 0,
    ) -> None:
        super().__init__()
        self._allowed_host_suffixes = allowed_host_suffixes
        self._max_bytes = max_bytes

    def http_error_302(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> Any:
        bounded_fp = (
            _BoundedRedirectResponse(fp, max_bytes=self._max_bytes) if self._max_bytes > 0 else fp
        )
        try:
            return super().http_error_302(req, bounded_fp, code, msg, headers)
        finally:
            if bounded_fp is not None:
                with suppress(Exception):
                    bounded_fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        try:
            _require_governed_media_url(
                newurl,
                allowed_host_suffixes=self._allowed_host_suffixes,
            )
        except ValueError:
            if fp is not None:
                with suppress(Exception):
                    fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def media_asset_sync_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    asset_refs = coerce_mapping_list(payload.get("asset_refs"))
    if not asset_refs:
        return skipped_result(
            context,
            summary={"asset_count": 0, "synced_count": 0},
            result={
                "synced_assets": [],
                "artifact_refs": [],
                "media_fact_bundle": new_fact_bundle(),
            },
        )

    try:
        normalized_asset_refs = [
            _normalize_media_asset(
                asset,
                fallback_product_id=payload.get("product_id"),
                fallback_source_platform=payload.get("source_platform"),
                fallback_marketplace_code=payload.get("marketplace_code"),
            )
            for asset in asset_refs
        ]
        normalized_asset_refs, rejected_url_warnings = _govern_amazon_media_source_urls(
            normalized_asset_refs,
            payload=payload,
        )
    except ValueError as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="invalid_media_asset_input",
                message=str(exc),
                retryable=False,
                details={"asset_count": len(asset_refs)},
            ),
            summary={
                "asset_count": len(asset_refs),
                "synced_count": 0,
                "artifact_count": 0,
            },
            result={
                "synced_assets": [],
                "artifact_refs": [],
                "media_fact_bundle": new_fact_bundle(),
            },
        )
    rejected_count = len(rejected_url_warnings)
    if not normalized_asset_refs:
        media_bundle = new_fact_bundle()
        media_bundle["media_assets"] = []
        return partial_success_result(
            context,
            summary={
                "asset_count": len(asset_refs),
                "synced_count": 0,
                "artifact_count": 0,
                "rejected_count": rejected_count,
            },
            result={
                "synced_assets": [],
                "artifact_refs": [],
                "media_fact_bundle": media_bundle,
            },
            warnings=tuple(rejected_url_warnings),
        )

    artifact_settings = _resolve_artifact_settings(payload)
    strict_storage_required = _requires_object_storage(payload, artifact_settings)
    storage_error = _object_storage_requirement_error(
        payload=payload,
        artifact_settings=artifact_settings,
        asset_count=len(asset_refs),
    )
    if storage_error:
        return failed_result(
            context,
            error=storage_error,
            summary={
                "asset_count": len(asset_refs),
                "synced_count": 0,
                "artifact_count": 0,
                "artifact_store_provider": normalize_artifact_store_provider(
                    artifact_settings.get("artifact_store_provider")
                ),
            },
            result={
                "synced_assets": [],
                "artifact_refs": [],
                "media_fact_bundle": new_fact_bundle(),
            },
        )
    try:
        artifact_store = create_store_from_settings(artifact_settings)
    except Exception as exc:  # noqa: BLE001 - strict formal workflows must fail before local fallback.
        if strict_storage_required:
            return failed_result(
                context,
                error=build_error(
                    error_type="persistence_configuration_invalid",
                    error_code="object_storage_configuration_invalid",
                    message=str(exc),
                    retryable=False,
                    details={
                        "artifact_store_provider": normalize_artifact_store_provider(
                            artifact_settings.get("artifact_store_provider")
                        )
                    },
                ),
                summary={
                    "asset_count": len(asset_refs),
                    "synced_count": 0,
                    "artifact_count": 0,
                    "artifact_store_provider": normalize_artifact_store_provider(
                        artifact_settings.get("artifact_store_provider")
                    ),
                },
                result={
                    "synced_assets": [],
                    "artifact_refs": [],
                    "media_fact_bundle": new_fact_bundle(),
                },
            )
        raise
    artifact_root = Path(first_non_empty(payload.get("artifact_root"), tempfile.gettempdir()))
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
    created_at = now_timestamp()
    sync_referenced_files = _sync_referenced_files_enabled(payload, artifact_settings)

    specs: list[ArtifactFileSpec] = []
    local_assets_by_path: dict[str, dict[str, Any]] = {}
    synced_assets: list[dict[str, Any]] = []
    synced_assets_by_ref: dict[str, dict[str, Any]] = {}
    deferred_reuse_assets: list[tuple[dict[str, Any], str]] = []
    warnings = list(rejected_url_warnings)
    fact_stores = _create_fact_store(
        payload,
        asset_refs=normalized_asset_refs,
        warnings=warnings,
    )
    request_pacer = RequestPacer(resolve_api_request_pacer_config(payload, provider="media"))

    for index, normalized_asset in enumerate(normalized_asset_refs):
        asset_ref_key = _asset_ref_key(normalized_asset)
        if asset_ref_key and asset_ref_key in synced_assets_by_ref:
            existing_asset = synced_assets_by_ref[asset_ref_key]
            if existing_asset.get("sync_state") == "pending_upload":
                deferred_reuse_assets.append((normalized_asset, asset_ref_key))
            else:
                synced_assets.append(_reused_in_run_media_asset(normalized_asset, existing_asset))
            continue
        cached_asset = _find_reusable_media_asset(
            fact_stores,
            normalized_asset,
            artifact_object_prefix=artifact_object_prefix,
        )
        if cached_asset and not _is_amazon_product_media(normalized_asset):
            reused_asset = _reused_media_asset(normalized_asset, cached_asset)
            synced_assets.append(reused_asset)
            if asset_ref_key:
                synced_assets_by_ref[asset_ref_key] = reused_asset
            continue
        if cached_asset:
            cached_asset = _validated_amazon_cached_asset(
                cached_asset,
                artifact_store=artifact_store,
                artifact_bucket=artifact_bucket,
                artifact_object_prefix=artifact_object_prefix,
            )
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
                    conditional_cache=cached_asset,
                )
                if cached_asset and _download_matches_cached_asset(
                    downloaded_asset,
                    cached_asset,
                ):
                    reused_asset = _reused_media_asset(
                        normalized_asset,
                        cached_asset,
                        metadata=coerce_mapping(downloaded_asset.get("metadata")),
                    )
                    synced_assets.append(reused_asset)
                    if asset_ref_key:
                        synced_assets_by_ref[asset_ref_key] = reused_asset
                    continue
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
            except _AmazonMediaNotModified:
                reused_asset = _reused_media_asset(normalized_asset, cached_asset)
                synced_assets.append(reused_asset)
                if asset_ref_key:
                    synced_assets_by_ref[asset_ref_key] = reused_asset
                continue
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"Referenced asset download failed: {normalized_asset.get('source_url')} ({exc})"
                )
        normalized_asset["sync_state"] = "referenced"
        synced_assets.append(normalized_asset)
        if asset_ref_key:
            synced_assets_by_ref[asset_ref_key] = normalized_asset

    for fact_store in (fact_stores or {}).values():
        close = getattr(fact_store, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

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
        "rejected_count": rejected_count,
        "artifact_store_provider": getattr(artifact_store, "provider_code", "local")
        if artifact_store
        else "local",
    }
    result = {
        "synced_assets": synced_assets,
        "artifact_refs": artifact_refs,
        "media_fact_bundle": media_bundle,
    }
    if strict_storage_required or _coerce_bool(payload.get("require_materialized_assets")):
        referenced_assets = [
            asset for asset in synced_assets if asset.get("sync_state") == "referenced"
        ]
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
    if rejected_count:
        return partial_success_result(
            context,
            summary=summary,
            result=result,
            warnings=tuple(warnings),
        )
    if warnings:
        return success_result(context, summary=summary, result=result, warnings=tuple(warnings))
    return success_result(context, summary=summary, result=result)


def _create_fact_store(
    payload: dict[str, Any],
    *,
    asset_refs: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    platforms = {
        coerce_str(asset.get("source_platform")).lower() for asset in asset_refs
    }
    if not (platforms & (_TK_MEDIA_CACHE_PLATFORMS | {"amazon"})):
        return {}
    request_payload = coerce_mapping(payload.get("request_payload"))
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
        request_payload.get("db_url"),
        get_execution_control_defaults().fact_db_url,
    )
    if not fact_db_url:
        return {}
    stores: dict[str, Any] = {}
    for platform_group, store_type in (
        ("tk", TKFactStore),
        ("amazon", AmazonFactStore),
    ):
        if platform_group == "tk" and not platforms.intersection(_TK_MEDIA_CACHE_PLATFORMS):
            continue
        if platform_group == "amazon" and "amazon" not in platforms:
            continue
        try:
            stores[platform_group] = store_type(db_url=fact_db_url)
        except Exception as exc:  # noqa: BLE001 - media sync can proceed without cache lookup.
            warnings.append(f"{platform_group.title()} media asset cache lookup disabled: {exc}")
    return stores


def _find_reusable_media_asset(
    fact_stores: dict[str, Any] | None,
    asset: dict[str, Any],
    *,
    artifact_object_prefix: str,
) -> dict[str, Any]:
    source_platform = coerce_str(asset.get("source_platform")).lower()
    if not fact_stores:
        return {}
    if source_platform in _TK_MEDIA_CACHE_PLATFORMS:
        fact_store = fact_stores.get("tk")
        if fact_store is None:
            return {}
        try:
            cached = fact_store.find_media_asset(
                source_url=coerce_str(asset.get("source_url")),
                file_token=coerce_str(asset.get("file_token")),
                local_path=coerce_str(asset.get("local_path")),
                object_key=coerce_str(asset.get("object_key")),
            )
        except Exception:  # noqa: BLE001 - cache lookup failure falls back to materialization.
            fact_stores.pop("tk", None)
            close = getattr(fact_store, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            return {}
    elif _is_amazon_product_media(asset):
        fact_store = fact_stores.get("amazon")
        if fact_store is None:
            return {}
        try:
            cached = fact_store.find_media_asset(
                source_url=coerce_str(asset.get("source_url")),
                object_key_prefix=_amazon_media_cache_object_prefix(
                    artifact_object_prefix=artifact_object_prefix,
                ),
            )
        except Exception:  # noqa: BLE001 - cache lookup failure falls back to materialization.
            fact_stores.pop("amazon", None)
            close = getattr(fact_store, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
            return {}
    else:
        return {}
    if not cached:
        return {}
    if coerce_str(cached.get("object_key")) or coerce_str(cached.get("remote_uri")):
        return cached
    return {}


def _reused_media_asset(
    asset: dict[str, Any],
    cached: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compact_dict(
        {
            **asset,
            "sync_state": "reused",
            "asset_id": cached.get("asset_id"),
            "asset_key": cached.get("asset_key"),
            "source_url": first_non_empty(asset.get("source_url"), cached.get("source_url")),
            "file_token": first_non_empty(cached.get("file_token"), asset.get("file_token")),
            "local_path": first_non_empty(cached.get("local_path"), asset.get("local_path")),
            "bucket": cached.get("bucket"),
            "object_key": first_non_empty(cached.get("object_key"), asset.get("object_key")),
            "remote_uri": cached.get("remote_uri"),
            "file_name": first_non_empty(cached.get("file_name"), asset.get("file_name")),
            "mime_type": first_non_empty(cached.get("mime_type"), asset.get("mime_type")),
            "content_digest": cached.get("content_digest"),
            "size_bytes": cached.get("size_bytes"),
            "source_platform": first_non_empty(
                asset.get("source_platform"), cached.get("source_platform")
            ),
            "metadata": metadata if metadata is not None else cached.get("metadata"),
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
            "content_digest": existing.get("content_digest"),
            "size_bytes": existing.get("size_bytes"),
            "source_platform": first_non_empty(
                asset.get("source_platform"), existing.get("source_platform")
            ),
            "metadata": asset.get("metadata"),
        }
    )


def _asset_ref_key(asset: dict[str, Any]) -> str:
    keys = _asset_ref_keys(asset)
    return keys[0] if keys else ""


def _asset_ref_keys(asset: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    source_platform = coerce_str(asset.get("source_platform")).lower() or "legacy"
    duplicate_scope = source_platform
    if source_platform == "amazon":
        duplicate_scope = (
            f"{source_platform}:{coerce_str(asset.get('media_role')).lower() or 'asset'}"
        )
    for prefix, value in (
        ("file_token", asset.get("file_token")),
        ("object_key", asset.get("object_key")),
        ("local_path", asset.get("local_path")),
        ("source_path", asset.get("source_path")),
        ("source_url", asset.get("source_url")),
    ):
        text = coerce_str(value)
        if text:
            keys.append(f"{duplicate_scope}:{prefix}:{text}")
    return keys


def _resolve_artifact_settings(payload: dict[str, Any]) -> dict[str, Any]:
    request_payload = coerce_mapping(payload.get("request_payload"))
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
    for source in (
        coerce_mapping(request_payload.get("artifact_store")),
        coerce_mapping(payload.get("artifact_store")),
    ):
        if not source:
            continue
        for source_key, target_key in (
            ("provider", "artifact_store_provider"),
            ("artifact_store_provider", "artifact_store_provider"),
            ("bucket", "artifact_bucket"),
            ("artifact_bucket", "artifact_bucket"),
            ("object_prefix", "artifact_object_prefix"),
            ("artifact_object_prefix", "artifact_object_prefix"),
            ("artifact_root", "artifact_root"),
        ):
            value = source.get(source_key)
            if value not in (None, ""):
                settings[target_key] = value
    explicit_settings = compact_dict(
        {
            "artifact_store_provider": first_non_empty(
                payload.get("artifact_store_provider"),
                payload.get("execution_control_artifact_store_provider"),
                request_payload.get("artifact_store_provider"),
                request_payload.get("execution_control_artifact_store_provider"),
            ),
            "artifact_bucket": first_non_empty(
                payload.get("artifact_bucket"),
                payload.get("execution_control_artifact_bucket"),
                request_payload.get("artifact_bucket"),
                request_payload.get("execution_control_artifact_bucket"),
            ),
            "artifact_object_prefix": first_non_empty(
                payload.get("artifact_object_prefix"),
                payload.get("execution_control_artifact_object_prefix"),
                request_payload.get("artifact_object_prefix"),
                request_payload.get("execution_control_artifact_object_prefix"),
            ),
            "minio_endpoint": first_non_empty(
                payload.get("minio_endpoint"),
                payload.get("execution_control_minio_endpoint"),
                request_payload.get("minio_endpoint"),
                request_payload.get("execution_control_minio_endpoint"),
            ),
            "minio_access_key": first_non_empty(
                payload.get("minio_access_key"),
                payload.get("execution_control_minio_access_key"),
                request_payload.get("minio_access_key"),
                request_payload.get("execution_control_minio_access_key"),
            ),
            "minio_secret_key": first_non_empty(
                payload.get("minio_secret_key"),
                payload.get("execution_control_minio_secret_key"),
                request_payload.get("minio_secret_key"),
                request_payload.get("execution_control_minio_secret_key"),
            ),
            "minio_secure": first_non_empty(
                payload.get("minio_secure"),
                payload.get("execution_control_minio_secure"),
                request_payload.get("minio_secure"),
                request_payload.get("execution_control_minio_secure"),
            ),
            "minio_region": first_non_empty(
                payload.get("minio_region"),
                payload.get("execution_control_minio_region"),
                request_payload.get("minio_region"),
                request_payload.get("execution_control_minio_region"),
            ),
            "minio_create_bucket": first_non_empty(
                payload.get("minio_create_bucket"),
                payload.get("execution_control_minio_create_bucket"),
                request_payload.get("minio_create_bucket"),
                request_payload.get("execution_control_minio_create_bucket"),
            ),
        }
    )
    if explicit_settings:
        settings.update(explicit_settings)
    return compact_dict(settings)


def _requires_object_storage(payload: dict[str, Any], artifact_settings: dict[str, Any]) -> bool:
    request_payload = coerce_mapping(payload.get("request_payload"))
    for source in (payload, request_payload, artifact_settings):
        for key in ("require_object_storage", "requires_object_storage", "strict_object_storage"):
            if key in source and source.get(key) not in (None, ""):
                return _coerce_bool(source.get(key))
    return False


def _object_storage_requirement_error(
    *,
    payload: dict[str, Any],
    artifact_settings: dict[str, Any],
    asset_count: int,
) -> Any | None:
    if not _requires_object_storage(payload, artifact_settings):
        return None
    provider = normalize_artifact_store_provider(artifact_settings.get("artifact_store_provider"))
    missing: list[str] = []
    if provider == "local":
        missing.append("object storage provider")
    if not first_non_empty(
        payload.get("artifact_bucket"), artifact_settings.get("artifact_bucket")
    ):
        missing.append("artifact bucket")
    if provider == "minio":
        for key, label in (
            ("minio_endpoint", "MinIO/S3 endpoint"),
            ("minio_access_key", "MinIO/S3 access key"),
            ("minio_secret_key", "MinIO/S3 secret key"),
        ):
            if not first_non_empty(payload.get(key), artifact_settings.get(key)):
                missing.append(label)
    if not missing:
        return None
    return build_error(
        error_type="persistence_configuration_missing",
        error_code="object_storage_required",
        message=(
            "media_asset_sync requires object storage persistence, but required "
            f"configuration is missing: {', '.join(missing)}."
        ),
        retryable=False,
        details={
            "asset_count": asset_count,
            "artifact_store_provider": provider,
            "missing_required_config": missing,
        },
    )


def _normalize_media_asset(
    asset: dict[str, Any],
    *,
    fallback_product_id: str = "",
    fallback_source_platform: str = "",
    fallback_marketplace_code: str = "",
) -> dict[str, Any]:
    source_platform, marketplace_code = _resolve_media_asset_identity(
        asset,
        fallback_source_platform=fallback_source_platform,
        fallback_marketplace_code=fallback_marketplace_code,
    )
    if source_platform == "amazon":
        injected_fields = [
            field
            for field in _AMAZON_CALLER_MATERIALIZED_FIELDS
            if asset.get(field) not in (None, "", [], {})
        ]
        if injected_fields:
            raise ValueError(
                "Amazon media assets cannot provide caller-materialized fields: "
                + ", ".join(injected_fields)
            )
    entity_key_type, entity_key_external_id = _entity_parts_from_key(asset.get("entity_key"))
    entity_type = first_non_empty(asset.get("entity_type"), entity_key_type, "product")
    entity_external_id = first_non_empty(
        asset.get("entity_external_id"),
        asset.get("product_id"),
        entity_key_external_id if entity_type == entity_key_type else "",
        fallback_product_id,
    )
    return compact_dict(
        {
            "entity_key": asset.get("entity_key"),
            "entity_type": entity_type,
            "entity_external_id": entity_external_id,
            "product_id": first_non_empty(
                asset.get("product_id"), entity_external_id if entity_type == "product" else ""
            ),
            "media_role": first_non_empty(
                asset.get("media_role"), asset.get("media_type"), "asset"
            ),
            "source_url": asset.get("source_url"),
            "file_token": asset.get("file_token"),
            "local_path": asset.get("local_path"),
            "object_key": asset.get("object_key"),
            "file_name": asset.get("file_name"),
            "mime_type": asset.get("mime_type"),
            "bucket": asset.get("bucket"),
            "remote_uri": asset.get("remote_uri"),
            "source_platform": source_platform,
            "marketplace_code": marketplace_code,
            "position": asset.get("position"),
            "metadata": coerce_mapping(asset.get("metadata")),
        }
    )


def _resolve_media_asset_identity(
    asset: dict[str, Any],
    *,
    fallback_source_platform: Any,
    fallback_marketplace_code: Any,
) -> tuple[str, str]:
    payload_source_platform = coerce_str(fallback_source_platform).lower()
    asset_source_platform = coerce_str(asset.get("source_platform")).lower()
    payload_marketplace_code = coerce_str(fallback_marketplace_code).upper()
    asset_marketplace_code = coerce_str(asset.get("marketplace_code")).upper()
    amazon_identity = "amazon" in {
        payload_source_platform,
        asset_source_platform,
    }
    if amazon_identity:
        if (
            payload_source_platform
            and asset_source_platform
            and payload_source_platform != asset_source_platform
        ):
            raise ValueError("payload and asset source_platform must not conflict for Amazon media")
        if (
            payload_marketplace_code
            and asset_marketplace_code
            and payload_marketplace_code != asset_marketplace_code
        ):
            raise ValueError(
                "payload and asset marketplace_code must not conflict for Amazon media"
            )

    source_platform = first_non_empty(
        asset_source_platform,
        payload_source_platform,
        "tiktok",
    )
    marketplace_code = first_non_empty(
        asset_marketplace_code,
        payload_marketplace_code,
    )
    if source_platform == "amazon" and marketplace_code != "US":
        raise ValueError("Amazon media assets require marketplace_code=US")
    return source_platform, marketplace_code


def _govern_amazon_media_source_urls(
    asset_refs: list[dict[str, Any]],
    *,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    governed_assets: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, asset in enumerate(asset_refs):
        if not _is_amazon_us_media(asset):
            governed_assets.append(asset)
            continue
        _, allowed_host_suffixes = _resolve_media_download_policy(
            asset,
            payload=payload,
        )
        try:
            _require_governed_media_url(
                asset.get("source_url"),
                allowed_host_suffixes=allowed_host_suffixes,
                allow_amazon_derivative=True,
            )
            parsed_source_url = urlparse(coerce_str(asset.get("source_url")))
            original_path = _AMAZON_IMAGE_TRANSFORM_SEGMENT.sub(
                lambda match: f".{match.group('extension').lower()}",
                parsed_source_url.path,
            )
            original_url = parsed_source_url._replace(
                path=original_path,
                query="",
                fragment="",
            ).geturl()
            _require_governed_media_url(
                original_url,
                allowed_host_suffixes=allowed_host_suffixes,
            )
        except ValueError:
            warnings.append(f"Amazon media asset at index {index} was rejected by URL policy.")
            continue
        governed_asset = dict(asset)
        governed_asset["source_url"] = original_url
        governed_assets.append(governed_asset)
    return governed_assets, warnings


def _entity_parts_from_key(value: Any) -> tuple[str, str]:
    text = coerce_str(value)
    if not text or ":" not in text:
        return "", ""
    entity_type, entity_external_id = text.split(":", 1)
    if entity_type not in {"product", "creator", "video", "shop"}:
        return "", ""
    return entity_type, entity_external_id


def _append_artifact_spec(
    specs: list[ArtifactFileSpec],
    local_assets_by_path: dict[str, dict[str, Any]],
    asset: dict[str, Any],
    *,
    index: int,
    handler_code: str,
    local_path: Path,
) -> None:
    resolved_path = local_path.resolve()
    stored_asset = dict(asset)
    explicit_object_key = ""
    if _is_amazon_product_media(asset):
        content_digest = _sha256_of_file(resolved_path)
        stored_asset.update(
            {
                "asset_key": f"content_sha256:{content_digest}",
                "content_digest": content_digest,
                "size_bytes": resolved_path.stat().st_size,
            }
        )
        explicit_object_key = _amazon_product_media_object_key(
            stored_asset,
            local_path=resolved_path,
        )
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
                "source_platform": coerce_str(asset.get("source_platform")),
                "marketplace_code": coerce_str(asset.get("marketplace_code")),
                "position": asset.get("position"),
                "content_digest": stored_asset.get("content_digest"),
            },
            object_key=explicit_object_key,
        )
    )
    local_assets_by_path[str(resolved_path)] = compact_dict(stored_asset)


def _is_amazon_product_media(asset: dict[str, Any]) -> bool:
    return (
        _is_amazon_us_media(asset)
        and coerce_str(asset.get("entity_type")) == "product"
        and bool(coerce_str(asset.get("entity_external_id")))
    )


def _is_amazon_us_media(asset: dict[str, Any]) -> bool:
    return (
        coerce_str(asset.get("source_platform")).lower() == "amazon"
        and coerce_str(asset.get("marketplace_code")).upper() == "US"
    )


def _amazon_product_media_object_key(
    asset: dict[str, Any],
    *,
    local_path: Path,
) -> str:
    suffix = local_path.suffix.lower() or ".bin"
    return (
        "product-media/amazon/us/"
        f"{_safe_segment(asset.get('entity_external_id')).upper()}/"
        f"{_safe_segment(asset.get('media_role')).lower()}/"
        f"{coerce_str(asset.get('content_digest')).lower()}{suffix}"
    )


def _amazon_media_cache_object_prefix(*, artifact_object_prefix: str) -> str:
    return join_object_key(
        artifact_object_prefix,
        "product-media/amazon/us/",
    )


def _validated_amazon_cached_asset(
    cached: dict[str, Any],
    *,
    artifact_store: Any,
    artifact_bucket: str,
    artifact_object_prefix: str,
) -> dict[str, Any]:
    if artifact_store is None:
        return {}
    bucket = coerce_str(cached.get("bucket"))
    object_key = coerce_str(cached.get("object_key"))
    remote_uri = coerce_str(cached.get("remote_uri"))
    content_digest = coerce_str(cached.get("content_digest")).lower()
    size_bytes = _coerce_int(cached.get("size_bytes"), default=0)
    required_values = (
        cached.get("asset_id"),
        cached.get("asset_key"),
        cached.get("source_url"),
        bucket,
        object_key,
        remote_uri,
        content_digest,
        cached.get("mime_type"),
    )
    expected_prefix = _amazon_media_cache_object_prefix(
        artifact_object_prefix=artifact_object_prefix
    )
    if (
        not all(coerce_str(value) for value in required_values)
        or bucket != artifact_bucket
        or not object_key.startswith(expected_prefix)
        or size_bytes <= 0
        or size_bytes > _AMAZON_US_MEDIA_DOWNLOAD_MAX_BYTES
        or len(content_digest) != 64
    ):
        return {}
    try:
        bytes.fromhex(content_digest)
        expected_uri = artifact_store.build_uri(bucket=bucket, object_key=object_key)
        if remote_uri != expected_uri:
            return {}
        stored_bytes = artifact_store.read_bytes(
            bucket=bucket,
            object_key=object_key,
            max_bytes=size_bytes + 1,
        )
    except Exception:  # noqa: BLE001 - an invalid cache entry falls back to a source download.
        return {}
    if len(stored_bytes) != size_bytes:
        return {}
    if hashlib.sha256(stored_bytes).hexdigest() != content_digest:
        return {}
    return cached


def _download_matches_cached_asset(
    downloaded: dict[str, Any],
    cached: dict[str, Any],
) -> bool:
    local_path = Path(coerce_str(downloaded.get("local_path"))).expanduser()
    if not local_path.is_file():
        return False
    return (
        local_path.stat().st_size == _coerce_int(cached.get("size_bytes"), default=0)
        and _sha256_of_file(local_path) == coerce_str(cached.get("content_digest")).lower()
    )


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sync_referenced_files_enabled(
    payload: dict[str, Any], artifact_settings: dict[str, Any]
) -> bool:
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
    conditional_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_url = coerce_str(asset.get("source_url"))
    timeout_seconds = _coerce_int(
        first_non_empty(
            payload.get("media_download_timeout_seconds"), payload.get("download_timeout_seconds")
        ),
        default=30,
    )
    max_bytes, allowed_host_suffixes = _resolve_media_download_policy(
        asset,
        payload=payload,
    )
    if allowed_host_suffixes:
        _require_governed_media_url(
            source_url,
            allowed_host_suffixes=allowed_host_suffixes,
        )
    request_headers = {"User-Agent": "Mozilla/5.0"}
    cache_metadata = coerce_mapping((conditional_cache or {}).get("metadata"))
    source_etag = coerce_str(cache_metadata.get("source_etag"))
    source_last_modified = coerce_str(cache_metadata.get("source_last_modified"))
    if source_etag:
        request_headers["If-None-Match"] = source_etag
    if source_last_modified:
        request_headers["If-Modified-Since"] = source_last_modified
    request = Request(source_url, headers=request_headers)
    request_pacer.wait_before_request("media:download")
    try:
        try:
            response_context = (
                build_opener(
                    _GovernedMediaRedirectHandler(
                        allowed_host_suffixes,
                        max_bytes=max_bytes,
                    )
                ).open(
                    request,
                    timeout=timeout_seconds,
                )
                if allowed_host_suffixes
                else urlopen(  # noqa: S310 - governed by caller policy.
                    request,
                    timeout=timeout_seconds,
                )
            )
        except HTTPError as exc:
            if exc.code == 304 and (source_etag or source_last_modified):
                raise _AmazonMediaNotModified from None
            raise
        with response_context as response:
            if allowed_host_suffixes:
                response_url = getattr(response, "geturl", lambda: source_url)()
                _require_governed_media_url(
                    response_url,
                    allowed_host_suffixes=allowed_host_suffixes,
                )
            content = response.read(max_bytes + 1) if max_bytes else response.read()
            content_type = coerce_str(response.headers.get("Content-Type"))
            response_etag = coerce_str(response.headers.get("ETag"))
            response_last_modified = coerce_str(response.headers.get("Last-Modified"))
    finally:
        request_pacer.mark_request_finished("media:download")
    if not content:
        raise ValueError("downloaded asset is empty")
    if max_bytes and len(content) > max_bytes:
        raise ValueError("downloaded asset exceeds media_download_max_bytes")

    suffix = _guess_media_suffix(source_url, content_type)
    file_name = _safe_file_name(
        first_non_empty(
            asset.get("file_name"),
            f"{_safe_segment(first_non_empty(asset.get('entity_external_id'), payload.get('product_id'), 'product'))}-"
            f"{_safe_segment(first_non_empty(asset.get('media_role'), 'asset'))}-"
            f"{index:03d}-{hashlib.sha1(source_url.encode('utf-8')).hexdigest()[:12]}{suffix}",
        )
    )
    product_id = _safe_segment(
        first_non_empty(
            asset.get("entity_external_id"),
            asset.get("product_id"),
            _entity_parts_from_key(asset.get("entity_key"))[1],
            payload.get("product_id"),
            "unknown-product",
        )
    )
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
    downloaded["mime_type"] = first_non_empty(
        asset.get("mime_type"), _normalize_content_type(content_type, suffix)
    )
    downloaded["metadata"] = compact_dict(
        {
            **coerce_mapping(asset.get("metadata")),
            "source_etag": response_etag,
            "source_last_modified": response_last_modified,
        }
    )
    return downloaded


def _guess_media_suffix(source_url: str, content_type: str) -> str:
    guessed = (
        mimetypes.guess_extension(str(content_type).split(";")[0].strip()) if content_type else ""
    )
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
    return (
        "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in name).strip(
            "-"
        )
        or "asset.bin"
    )


def _safe_segment(value: Any) -> str:
    text = coerce_str(value)
    return (
        "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in text).strip(
            "-"
        )
        or "unknown"
    )


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _media_download_host_suffixes(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("media_download_allowed_host_suffixes must be a list of DNS suffixes")
    suffixes: list[str] = []
    for item in value:
        suffix = coerce_str(item).lower().lstrip(".")
        if (
            not suffix
            or suffix.startswith("-")
            or suffix.endswith(("-", "."))
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in suffix
            )
        ):
            raise ValueError("media_download_allowed_host_suffixes contains an invalid DNS suffix")
        if suffix not in suffixes:
            suffixes.append(suffix)
    if not suffixes:
        raise ValueError("media_download_allowed_host_suffixes must not be empty")
    return tuple(suffixes)


def _resolve_media_download_policy(
    asset: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> tuple[int, tuple[str, ...]]:
    raw_max_bytes = payload.get("media_download_max_bytes")
    max_bytes = 0
    if raw_max_bytes not in (None, ""):
        max_bytes = _coerce_int(raw_max_bytes, default=0)
        if max_bytes <= 0:
            raise ValueError("media_download_max_bytes must be a positive integer")
    allowed_host_suffixes = _media_download_host_suffixes(
        payload.get("media_download_allowed_host_suffixes")
    )
    if not _is_amazon_us_media(asset):
        return max_bytes, allowed_host_suffixes

    max_bytes = min(
        max_bytes or _AMAZON_US_MEDIA_DOWNLOAD_MAX_BYTES,
        _AMAZON_US_MEDIA_DOWNLOAD_MAX_BYTES,
    )
    if not allowed_host_suffixes:
        return max_bytes, _AMAZON_US_MEDIA_ALLOWED_HOST_SUFFIXES

    effective_host_suffixes: list[str] = []
    for required_suffix in _AMAZON_US_MEDIA_ALLOWED_HOST_SUFFIXES:
        for caller_suffix in allowed_host_suffixes:
            effective_suffix = ""
            if caller_suffix == required_suffix or caller_suffix.endswith(f".{required_suffix}"):
                effective_suffix = caller_suffix
            elif required_suffix.endswith(f".{caller_suffix}"):
                effective_suffix = required_suffix
            if effective_suffix and effective_suffix not in effective_host_suffixes:
                effective_host_suffixes.append(effective_suffix)
    if not effective_host_suffixes:
        raise ValueError(
            "media_download_allowed_host_suffixes cannot allow hosts outside the Amazon CDN policy"
        )
    return max_bytes, tuple(effective_host_suffixes)


def _require_governed_media_url(
    value: Any,
    *,
    allowed_host_suffixes: tuple[str, ...],
    allow_amazon_derivative: bool = False,
) -> None:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("media download requires a governed HTTPS media host")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("media download requires a governed HTTPS media host") from exc
    hostname = (parsed.hostname or "").lower()
    decoded_path = parsed.path
    for _ in range(8):
        next_path = unescape(unquote(decoded_path))
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        if unescape(unquote(decoded_path)) != decoded_path:
            raise ValueError("media download requires a governed HTTPS media host")
    allowed_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}") for suffix in allowed_host_suffixes
    )
    amazon_media_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _AMAZON_US_MEDIA_ALLOWED_HOST_SUFFIXES
    )
    if (
        parsed.scheme.lower() != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or any(character.isspace() for character in decoded_path)
        or _AMAZON_MEDIA_UNSAFE_PATH.search(decoded_path)
        or (
            amazon_media_host
            and not allow_amazon_derivative
            and _AMAZON_IMAGE_TRANSFORM_SEGMENT.search(decoded_path)
        )
    ):
        raise ValueError("media download requires a governed HTTPS media host")


__all__ = ["CONTRACT", "HANDLER_CODE", "media_asset_sync_handler"]
