from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests

from automation_business_scaffold.business.flows.tiktok_product_flow import (
    download_tiktok_product_main_image,
    fetch_tiktok_product_record,
    fetch_tiktok_product_record_via_browser,
    normalize_tiktok_product_url,
)
from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.infrastructure.artifacts.artifact_store import (
    create_artifact_store,
    join_object_key,
)
from automation_business_scaffold.infrastructure.fastmoss.cookie_cache import (
    attach_fastmoss_cookie_cache,
    build_fastmoss_cookie_cache_context,
    save_fastmoss_cookie_cache_from_session,
)
from automation_business_scaffold.infrastructure.fastmoss.fact_mappers import (
    map_fastmoss_goods_base,
    map_fastmoss_goods_overview,
    map_fastmoss_goods_product_sku,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import FastMossHTTPSession
from automation_business_scaffold.infrastructure.fastmoss.visualization_renderer import (
    FastMossVisualizationRenderer,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_ingestion_service import TKFactIngestionService
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
from automation_business_scaffold.models import TikTokProductRecord

INGESTION_KEYS = (
    "fact_entities",
    "fact_relations",
    "fact_media_assets",
    "fact_metric_observations",
    "raw_api_responses",
)


def fetch_tiktok_product_via_request(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_tiktok_request_settings(params)
    product = fetch_tiktok_product_record(
        settings["product_url"],
        timeout=int(settings["timeout_seconds"]),
    )
    if settings["download_image"]:
        product = download_tiktok_product_main_image(
            product,
            download_dir=settings["image_download_dir"],
            timeout=int(settings["timeout_seconds"]),
        )
    item = _tiktok_item(product)
    return {
        "summary": {"total": 1, "counts": {"fetched": 1}},
        "item": item,
        "items": [item],
        "product": product.to_dict(),
        "product_id": product.product_id,
        "normalized_url": product.normalized_url,
        "settings": {
            "timeout_seconds": settings["timeout_seconds"],
            "download_image": settings["download_image"],
            "image_download_dir": settings["image_download_dir"],
        },
    }


def fetch_tiktok_product_via_browser(params: dict[str, Any]) -> dict[str, Any]:
    settings = _build_tiktok_browser_fetch_settings(params)
    product = fetch_tiktok_product_record_via_browser(
        settings["product_url"],
        profile_ref=settings["profile_ref"],
        workspace_id=settings["workspace_id"],
        profile_id=settings["profile_id"],
        provider_name=settings["provider_name"],
        timeout_ms=int(settings["timeout_ms"]),
        capture_page_screenshot=settings["capture_page_screenshot"],
        trace_id=settings["trace_id"],
    )
    item = _tiktok_item(product)
    item["fetch_source"] = "browser"
    return {
        "summary": {"total": 1, "counts": {"fetched": 1}},
        "item": item,
        "items": [item],
        "product": product.to_dict(),
        "product_id": product.product_id,
        "normalized_url": product.normalized_url,
        "fetch_source": "browser",
        "settings": {
            "timeout_ms": settings["timeout_ms"],
            "profile_ref": settings["profile_ref"],
            "workspace_id": settings["workspace_id"],
            "profile_id": settings["profile_id"],
            "provider_name": settings["provider_name"],
            "capture_page_screenshot": settings["capture_page_screenshot"],
        },
    }


def fetch_fastmoss_product_by_sku(params: dict[str, Any], *, product_id: str = "") -> dict[str, Any]:
    settings = _build_fastmoss_settings(params, product_id=product_id)
    product_id = settings["product_id"]
    with FastMossHTTPSession(
        phone=settings["fastmoss_phone"],
        password=settings["fastmoss_password"],
        default_region=settings["fastmoss_region"],
        timeout=float(settings["timeout_seconds"]),
        request_delay_range=(
            float(settings["request_delay_min_seconds"]),
            float(settings["request_delay_max_seconds"]),
        ),
    ) as fastmoss:
        cookie_cache_status: dict[str, Any] = {"enabled": False, "reason": "disabled"}
        cookie_cache_store = _create_cookie_cache_store(params) if settings["cookie_cache_enabled"] else None
        cookie_cache_context = build_fastmoss_cookie_cache_context(
            base_url=fastmoss.base_url,
            account_key=settings["fastmoss_phone"],
            region=settings["fastmoss_region"],
            namespace=settings["cookie_cache_namespace"],
        )
        cookie_cache_status = attach_fastmoss_cookie_cache(
            fastmoss,
            store=cookie_cache_store,
            account_key=settings["fastmoss_phone"],
            region=settings["fastmoss_region"],
            namespace=settings["cookie_cache_namespace"],
            enabled=settings["cookie_cache_enabled"],
            force_refresh=settings["cookie_cache_force_refresh"],
            ttl_seconds=float(settings["cookie_cache_ttl_seconds"]),
        )
        if settings["ensure_login"]:
            fastmoss.ensure_logged_in()
            if cookie_cache_store is not None and cookie_cache_context.get("enabled"):
                cookie_cache_status = save_fastmoss_cookie_cache_from_session(
                    fastmoss,
                    store=cookie_cache_store,
                    context=cookie_cache_context,
                    ttl_seconds=float(settings["cookie_cache_ttl_seconds"]),
                )
        base_payload = fastmoss.get_product_base(product_id)
        overview_payload = _with_default_d_type(
            fastmoss.get_product_overview(
                product_id,
                d_type=settings["overview_d_type"],
            ),
            settings["overview_d_type"],
        )
        skus_payload = _with_default_d_type(
            fastmoss.get_product_skus(
                product_id,
                d_type=settings["sku_d_type"],
            ),
            settings["sku_d_type"],
        )
        sku_distribution_payload = _with_default_d_type(
            fastmoss.get_product_sku_distribution(
                product_id,
                d_type=settings["sku_d_type"],
            ),
            settings["sku_d_type"],
        )

    item = {
        "product_id": product_id,
        "status": "fetched",
        "base": base_payload,
        "overview": overview_payload,
        "skus": skus_payload,
        "sku_distribution": sku_distribution_payload,
    }
    return {
        "summary": {"total": 4, "counts": {"fetched": 4}},
        "item": item,
        "items": [item],
        "product_id": product_id,
        "fastmoss": {
            "base": base_payload,
            "overview": overview_payload,
            "skus": skus_payload,
            "sku_distribution": sku_distribution_payload,
        },
        "settings": {
            "fastmoss_region": settings["fastmoss_region"],
            "overview_d_type": settings["overview_d_type"],
            "sku_d_type": settings["sku_d_type"],
            "ensure_login": settings["ensure_login"],
            "cookie_cache": cookie_cache_status,
        },
    }


def upload_product_media_assets(
    params: dict[str, Any],
    *,
    tiktok_payload: Mapping[str, Any],
    fastmoss_payload: Mapping[str, Any],
) -> dict[str, Any]:
    settings = _build_media_upload_settings(params)
    artifact_store = create_artifact_store(settings)
    if str(settings["artifact_store_provider"]).strip().lower() != "minio" or artifact_store is None:
        raise ValueError("Product media upload requires execution_control_artifact_store_provider=minio.")

    product_id = _first_non_empty(
        params.get("sku_id"),
        params.get("product_id"),
        fastmoss_payload.get("product_id"),
        tiktok_payload.get("product_id"),
    )
    candidates = _collect_product_media_assets(
        params,
        tiktok_payload=tiktok_payload,
        fastmoss_payload=fastmoss_payload,
        product_id=product_id,
    )
    uploaded_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    with requests.Session() as session:
        for candidate in candidates:
            try:
                prepared = _ensure_product_media_local_file(
                    candidate,
                    product_id=product_id,
                    settings=settings,
                    session=session,
                )
                local_path = Path(str(prepared["local_path"])).expanduser()
                content_type = str(prepared.get("mime_type") or "").strip() or _guess_mime_type(local_path)
                object_key = _build_product_media_object_key(
                    product_id=product_id,
                    media_spec=prepared,
                    local_path=local_path,
                    artifact_object_prefix=str(settings.get("artifact_object_prefix", "") or ""),
                )
                uploaded = artifact_store.upload_file(
                    bucket=str(settings["artifact_bucket"]),
                    object_key=object_key,
                    local_path=local_path,
                    content_type=content_type,
                    metadata={
                        "product_id": product_id,
                        "entity_type": str(prepared.get("entity_type") or ""),
                        "entity_external_id": str(prepared.get("entity_external_id") or ""),
                        "media_role": str(prepared.get("media_role") or ""),
                        "source_url": str(prepared.get("source_url") or ""),
                    },
                )
                uploaded_items.append(
                    {
                        **prepared,
                        "bucket": uploaded.bucket,
                        "object_key": uploaded.object_key,
                        "remote_uri": uploaded.uri,
                        "etag": uploaded.etag,
                        "size": uploaded.size,
                        "mime_type": uploaded.content_type or content_type,
                        "upload_status": "uploaded",
                    }
                )
            except Exception as exc:
                failed = {**candidate, "upload_status": "failed", "error": str(exc)}
                failed_items.append(failed)

    if failed_items and not settings["allow_media_upload_failures"]:
        summary = ", ".join(str(item.get("source_url") or item.get("local_path") or "") for item in failed_items[:3])
        raise RuntimeError(f"Failed to upload product media assets to MinIO: {summary}")

    return {
        "summary": {
            "total": len(candidates),
            "counts": {
                "uploaded": len(uploaded_items),
                "failed": len(failed_items),
            },
        },
        "item": {
            "product_id": product_id,
            "status": "uploaded" if not failed_items else "partial_uploaded",
            "uploaded_media_count": len(uploaded_items),
            "failed_media_count": len(failed_items),
        },
        "items": uploaded_items,
        "failed_items": failed_items,
        "product_id": product_id,
        "uploaded_media_assets": uploaded_items,
        "settings": {
            "artifact_store_provider": settings["artifact_store_provider"],
            "artifact_bucket": settings["artifact_bucket"],
            "artifact_object_prefix": settings["artifact_object_prefix"],
            "media_download_dir": settings["media_download_dir"],
        },
    }


def persist_tiktok_fastmoss_product_facts(
    params: dict[str, Any],
    *,
    tiktok_payload: Mapping[str, Any],
    fastmoss_payload: Mapping[str, Any],
    media_upload_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    product_payload = _as_mapping(tiktok_payload.get("product"))
    product = TikTokProductRecord.from_dict(dict(product_payload))
    product_item = _as_mapping(tiktok_payload.get("item")) or _tiktok_item(product)
    product_id = _first_non_empty(
        params.get("sku_id"),
        params.get("product_id"),
        fastmoss_payload.get("product_id"),
        product.product_id,
        product_item.get("product_id"),
    )
    product_logical_fields = product.to_dict()
    if product_id:
        product_item = {**dict(product_item), "product_id": product_id}
        product_logical_fields["product_id"] = product_id
    uploaded_media_assets = _uploaded_media_assets(media_upload_payload or {})
    product_logical_fields = _apply_uploaded_tiktok_media_to_logical_fields(
        product_logical_fields,
        uploaded_media_assets,
    )
    tiktok_skus = _logical_tiktok_skus(product_logical_fields)
    fastmoss_data = _as_mapping(fastmoss_payload.get("fastmoss"))
    store = _create_store(params)
    ingestion = TKFactIngestionService(runtime_store=store)
    persisted = _empty_persisted_payload()

    tiktok_persisted = ingestion.ingest_tiktok_product_request(
        logical_fields=product_logical_fields,
        source_item=product_item,
        fastmoss_snapshot={
            "product_id": product_id,
            "base": dict(_as_mapping(fastmoss_data.get("base"))),
            "overview": dict(_as_mapping(fastmoss_data.get("overview"))),
            "skus": dict(_as_mapping(fastmoss_data.get("skus"))),
            "sku_distribution": dict(_as_mapping(fastmoss_data.get("sku_distribution"))),
        },
        source_endpoint="tiktok.product.http_request",
    )
    _merge_persisted(persisted, tiktok_persisted)

    for source_endpoint, raw_payload, mapper in (
        ("fastmoss.goods.v3.base", _as_mapping(fastmoss_data.get("base")), map_fastmoss_goods_base),
        ("fastmoss.goods.v3.overview", _as_mapping(fastmoss_data.get("overview")), map_fastmoss_goods_overview),
        ("fastmoss.goods.v3.productSku", _as_mapping(fastmoss_data.get("skus")), map_fastmoss_goods_product_sku),
        (
            "fastmoss.goods.productSku",
            _as_mapping(fastmoss_data.get("sku_distribution")),
            map_fastmoss_goods_product_sku,
        ),
    ):
        if not raw_payload:
            continue
        mapped = mapper({"data": dict(raw_payload)}, product_id=product_id)
        if mapper is map_fastmoss_goods_product_sku:
            mapped = {
                **mapped,
                "product_skus": _prefer_tiktok_product_sku_text(
                    mapped["product_skus"],
                    tiktok_skus,
                ),
            }
        media_assets = _apply_uploaded_media_to_assets(
            mapped["media_assets"],
            uploaded_media_assets,
        )
        fastmoss_persisted = ingestion.ingest_api_response(
            source_platform="fastmoss",
            source_endpoint=source_endpoint,
            request_params={"product_id": product_id},
            response_payload=dict(raw_payload),
            products=mapped["products"],
            product_skus=mapped["product_skus"],
            shops=mapped["shops"],
            creators=mapped["creators"],
            videos=mapped["videos"],
            media_assets=media_assets,
            product_metric_snapshots=_fastmoss_product_metric_snapshots(
                source_endpoint=source_endpoint,
                raw_payload=raw_payload,
                product_id=product_id,
            ),
            product_daily_metrics=_fastmoss_product_daily_metrics(
                source_endpoint=source_endpoint,
                raw_payload=raw_payload,
                product_id=product_id,
            ),
            product_distribution_snapshots=_fastmoss_product_distribution_snapshots(
                source_endpoint=source_endpoint,
                raw_payload=raw_payload,
                product_id=product_id,
            ),
            product_sku_metric_snapshots=_fastmoss_product_sku_metric_snapshots(
                source_endpoint=source_endpoint,
                raw_payload=raw_payload,
                product_id=product_id,
                tiktok_skus=tiktok_skus,
            ),
            relations=mapped["relations"],
            raw_entity_links=mapped["raw_entity_links"],
        )
        _merge_persisted(persisted, fastmoss_persisted)

    item = {
        "product_id": product_id,
        "normalized_url": product.normalized_url,
        "status": "persisted",
        **persisted,
    }
    return {
        "summary": {
            "total": 1,
            "counts": {"persisted": 1},
            "fact_entity_count": len(persisted["fact_entities"]),
            "fact_relation_count": len(persisted["fact_relations"]),
            "fact_media_asset_count": len(persisted["fact_media_assets"]),
            "fact_metric_observation_count": len(persisted["fact_metric_observations"]),
            "raw_api_response_count": len(persisted["raw_api_responses"]),
        },
        "item": item,
        "items": [item],
        "product_id": product_id,
        **persisted,
    }


def render_fastmoss_product_visualizations(
    params: dict[str, Any],
    *,
    fastmoss_payload: Mapping[str, Any],
) -> dict[str, Any]:
    settings = _build_fastmoss_visualization_settings(params, fastmoss_payload=fastmoss_payload)
    fastmoss_data = _as_mapping(fastmoss_payload.get("fastmoss"))
    result = FastMossVisualizationRenderer(
        node_binary=settings["node_binary"],
        renderer_package_json=settings["renderer_package_json"],
        timeout_seconds=float(settings["timeout_seconds"]),
    ).render_product_charts(
        product_id=settings["product_id"],
        overview_payload=_as_mapping(fastmoss_data.get("overview")),
        product_sku_payload=_fastmoss_sku_visualization_payload(fastmoss_data),
        output_dir=settings["output_dir"],
    )
    item = {
        "product_id": settings["product_id"],
        "status": "rendered",
        **result.to_dict(),
    }
    return {
        "summary": {
            "total": len(result.files),
            "counts": {"rendered": len(result.files)},
        },
        "item": item,
        "items": [{"chart_name": key, "local_path": str(value)} for key, value in result.files.items()],
        "product_id": settings["product_id"],
        "visualizations": result.to_dict(),
        "files": {key: str(value) for key, value in result.files.items()},
    }


def run_tiktok_fastmoss_product_ingest(params: dict[str, Any]) -> dict[str, Any]:
    tiktok_payload = _tiktok_payload_override(params) or fetch_tiktok_product_via_request(params)
    fastmoss_payload = fetch_fastmoss_product_by_sku(
        params,
        product_id=str(tiktok_payload.get("product_id") or ""),
    )
    visualization_payload: dict[str, Any] = {}
    if _read_bool(params, "fastmoss_visualization_enabled", False):
        visualization_payload = render_fastmoss_product_visualizations(
            params,
            fastmoss_payload=fastmoss_payload,
        )
    media_upload_payload = upload_product_media_assets(
        params,
        tiktok_payload=tiktok_payload,
        fastmoss_payload=fastmoss_payload,
    )
    persisted_payload = persist_tiktok_fastmoss_product_facts(
        params,
        tiktok_payload=tiktok_payload,
        fastmoss_payload=fastmoss_payload,
        media_upload_payload=media_upload_payload,
    )
    return {
        "summary": persisted_payload["summary"],
        "item": persisted_payload["item"],
        "items": persisted_payload["items"],
        "tiktok": tiktok_payload,
        "fastmoss": fastmoss_payload,
        "visualizations": visualization_payload,
        "media_upload": media_upload_payload,
        "persisted": persisted_payload,
        "product_id": persisted_payload.get("product_id", ""),
    }


def _tiktok_payload_override(params: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("tiktok_payload", "tiktok_product_payload"):
        payload = params.get(key)
        if _looks_like_tiktok_product_payload(payload):
            return dict(payload)  # type: ignore[arg-type]

    browser_result = params.get("tiktok_browser_fallback_result")
    if _looks_like_tiktok_product_payload(browser_result):
        return dict(browser_result)  # type: ignore[arg-type]
    if isinstance(browser_result, Mapping):
        nested = browser_result.get("tiktok")
        if _looks_like_tiktok_product_payload(nested):
            return dict(nested)  # type: ignore[arg-type]
    return {}


def _looks_like_tiktok_product_payload(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    product = value.get("product")
    item = value.get("item")
    return isinstance(product, Mapping) and isinstance(item, Mapping)


def _build_tiktok_request_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    product_url = _first_non_empty(params.get("product_url"), params.get("source_url"), params.get("url"))
    if not product_url:
        raise ValueError("product_url is required")
    return {
        "product_url": normalize_tiktok_product_url(product_url),
        "timeout_seconds": _read_int(params, "request_timeout_seconds", 30),
        "download_image": _read_bool(params, "download_image", True),
        "image_download_dir": _first_non_empty(
            params.get("image_download_dir"),
            "runtime/downloads/tiktok_product_images",
        ),
    }


def _build_tiktok_browser_fetch_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    request_settings = _build_tiktok_request_settings(params)
    timeout_ms = _read_int(params, "browser_timeout_ms", 0)
    if timeout_ms <= 0:
        timeout_ms = _read_int(params, "tiktok_browser_timeout_ms", 0)
    if timeout_ms <= 0:
        timeout_ms = int(request_settings["timeout_seconds"]) * 1000
    profile_ref = _first_non_empty(
        params.get("tiktok_browser_profile_ref"),
        params.get("browser_profile_ref"),
        params.get("profile_ref"),
        os.environ.get("BROWSER_PROFILE_REF"),
        "roxy-tiktok",
    )
    provider_name = _first_non_empty(
        params.get("tiktok_browser_provider_name"),
        params.get("browser_provider_name"),
        params.get("provider_name"),
        os.environ.get("BROWSER_PROVIDER_NAME"),
    )
    profile_id = _first_non_empty(
        params.get("tiktok_browser_profile_id"),
        params.get("browser_profile_id"),
        os.environ.get("BROWSER_PROFILE_ID"),
    )
    workspace_id = _optional_int(
        _first_non_empty(
            params.get("tiktok_browser_workspace_id"),
            params.get("browser_workspace_id"),
            os.environ.get("BROWSER_WORKSPACE_ID"),
        )
    )
    if provider_name and profile_id:
        profile_ref = ""
    return {
        "product_url": request_settings["product_url"],
        "timeout_ms": timeout_ms,
        "profile_ref": profile_ref,
        "workspace_id": workspace_id,
        "profile_id": profile_id,
        "provider_name": provider_name,
        "capture_page_screenshot": _read_bool(params, "capture_page_screenshot", True),
        "trace_id": _first_non_empty(params.get("trace_id"), params.get("request_id")),
    }


def _build_fastmoss_settings(params: Mapping[str, Any], *, product_id: str = "") -> dict[str, Any]:
    normalized_product_id = _first_non_empty(params.get("sku_id"), params.get("product_id"), product_id)
    if not normalized_product_id:
        raise ValueError("sku_id/product_id is required for FastMoss product API fetch")
    phone = _resolve_secret_param(params, "fastmoss_phone", "fastmoss_phone_env")
    password = _resolve_secret_param(params, "fastmoss_password", "fastmoss_password_env")
    ensure_login = _read_bool(params, "fastmoss_ensure_login", False)
    return {
        "product_id": normalized_product_id,
        "fastmoss_phone": phone,
        "fastmoss_password": password,
        "fastmoss_region": _first_non_empty(params.get("fastmoss_region"), "US"),
        "timeout_seconds": _read_int(params, "fastmoss_timeout_seconds", 30),
        "request_delay_min_seconds": _read_float(params, "fastmoss_request_delay_min_seconds", 0.0),
        "request_delay_max_seconds": _read_float(params, "fastmoss_request_delay_max_seconds", 0.0),
        "overview_d_type": _first_non_empty(params.get("fastmoss_overview_d_type"), params.get("d_type"), "28"),
        "sku_d_type": _first_non_empty(params.get("fastmoss_sku_d_type"), params.get("d_type"), "28"),
        "ensure_login": ensure_login,
        "cookie_cache_enabled": _read_bool_with_env(
            params,
            "fastmoss_cookie_cache_enabled",
            "FASTMOSS_COOKIE_CACHE_ENABLED",
            True,
        ),
        "cookie_cache_force_refresh": _read_bool(params, "fastmoss_cookie_cache_force_refresh", False),
        "cookie_cache_ttl_seconds": _read_float_with_env(
            params,
            "fastmoss_cookie_cache_ttl_seconds",
            "FASTMOSS_COOKIE_CACHE_TTL_SECONDS",
            43200.0,
        ),
        "cookie_cache_namespace": _first_non_empty(
            params.get("fastmoss_cookie_cache_namespace"),
            os.environ.get("FASTMOSS_COOKIE_CACHE_NAMESPACE"),
        ),
    }


def _create_store(params: Mapping[str, Any]) -> RuntimeStore:
    defaults = get_execution_control_defaults()
    return RuntimeStore(db_url=_first_non_empty(params.get("execution_control_db_url"), defaults.db_url))


def _create_cookie_cache_store(params: Mapping[str, Any]) -> RuntimeStore | None:
    defaults = get_execution_control_defaults()
    db_url = _first_non_empty(params.get("execution_control_db_url"), defaults.db_url)
    if not db_url:
        return None
    return RuntimeStore(db_url=db_url)


def _build_media_upload_settings(params: Mapping[str, Any]) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    return {
        "artifact_store_provider": str(
            params.get("execution_control_artifact_store_provider")
            or params.get("artifact_store_provider")
            or defaults.artifact_store_provider
        ).strip().lower()
        or "local",
        "artifact_bucket": _first_non_empty(
            params.get("execution_control_artifact_bucket"),
            params.get("artifact_bucket"),
            defaults.artifact_bucket,
        ),
        "artifact_object_prefix": _first_non_empty(
            params.get("execution_control_artifact_object_prefix"),
            params.get("artifact_object_prefix"),
            defaults.artifact_object_prefix,
        ),
        "minio_endpoint": _first_non_empty(
            params.get("execution_control_minio_endpoint"),
            params.get("minio_endpoint"),
            defaults.minio_endpoint,
        ),
        "minio_access_key": _first_non_empty(
            params.get("execution_control_minio_access_key"),
            params.get("minio_access_key"),
            defaults.minio_access_key,
        ),
        "minio_secret_key": _first_non_empty(
            params.get("execution_control_minio_secret_key"),
            params.get("minio_secret_key"),
            defaults.minio_secret_key,
        ),
        "minio_region": _first_non_empty(
            params.get("execution_control_minio_region"),
            params.get("minio_region"),
            defaults.minio_region,
        ),
        "minio_secure": _read_bool(params, "execution_control_minio_secure", defaults.minio_secure),
        "minio_create_bucket": _read_bool(
            params,
            "execution_control_minio_create_bucket",
            defaults.minio_create_bucket,
        ),
        "media_download_dir": _first_non_empty(
            params.get("media_download_dir"),
            params.get("image_download_dir"),
            "runtime/downloads/product_media_assets",
        ),
        "timeout_seconds": _read_int(params, "media_download_timeout_seconds", 30),
        "allow_media_upload_failures": _read_bool(params, "allow_media_upload_failures", False),
    }


def _build_fastmoss_visualization_settings(
    params: Mapping[str, Any],
    *,
    fastmoss_payload: Mapping[str, Any],
) -> dict[str, Any]:
    product_id = _first_non_empty(
        params.get("sku_id"),
        params.get("product_id"),
        fastmoss_payload.get("product_id"),
    )
    if not product_id:
        raise ValueError("product_id is required for FastMoss visualization rendering")
    output_dir = _first_non_empty(
        params.get("fastmoss_visualization_output_dir"),
        params.get("visualization_output_dir"),
    )
    return {
        "product_id": product_id,
        "output_dir": output_dir or None,
        "node_binary": _first_non_empty(params.get("fastmoss_visualization_node_binary")) or None,
        "renderer_package_json": _first_non_empty(
            params.get("fastmoss_visualization_renderer_package_json"),
        )
        or None,
        "timeout_seconds": _read_float(params, "fastmoss_visualization_timeout_seconds", 60.0),
    }


def _tiktok_item(product: TikTokProductRecord) -> dict[str, Any]:
    return {
        "product_id": product.product_id,
        "source_url": product.source_url,
        "resolved_url": product.resolved_url,
        "normalized_url": product.normalized_url,
        "status": "fetched",
        "logical_fields": product.to_dict(),
        "main_image_local_path": product.main_image_local_path,
        "main_image_file_name": product.main_image_file_name,
        "main_image_mime_type": product.main_image_mime_type,
    }


def _collect_product_media_assets(
    params: Mapping[str, Any],
    *,
    tiktok_payload: Mapping[str, Any],
    fastmoss_payload: Mapping[str, Any],
    product_id: str,
) -> list[dict[str, Any]]:
    del params
    candidates: list[dict[str, Any]] = []
    tiktok_product = _as_mapping(tiktok_payload.get("product"))
    tiktok_item = _as_mapping(tiktok_payload.get("item"))
    tiktok_logical_fields = _as_mapping(tiktok_item.get("logical_fields"))
    tiktok_source_url = _first_non_empty(
        tiktok_product.get("main_image_url"),
        tiktok_item.get("main_image_url"),
        tiktok_logical_fields.get("main_image_url"),
    )
    tiktok_local_path = _first_non_empty(
        tiktok_product.get("main_image_local_path"),
        tiktok_item.get("main_image_local_path"),
        tiktok_logical_fields.get("main_image_local_path"),
    )
    tiktok_main_file_token = _tiktok_main_image_file_token(
        tiktok_product,
        tiktok_logical_fields,
        source_url=tiktok_source_url,
    )
    if tiktok_source_url or tiktok_local_path:
        candidates.append(
            {
                "entity_type": "product",
                "entity_external_id": product_id or _first_non_empty(tiktok_product.get("product_id")),
                "media_role": "product_main_image",
                "source_url": tiktok_source_url,
                "file_token": tiktok_main_file_token,
                "local_path": tiktok_local_path,
                "file_name": _first_non_empty(
                    tiktok_product.get("main_image_file_name"),
                    tiktok_item.get("main_image_file_name"),
                ),
                "mime_type": _first_non_empty(
                    tiktok_product.get("main_image_mime_type"),
                    tiktok_item.get("main_image_mime_type"),
                ),
                "source_platform": "tiktok",
            }
        )
    candidates.extend(
        _tiktok_logical_media_candidates(
            tiktok_product.get("gallery_images") or tiktok_logical_fields.get("gallery_images"),
            product_id=product_id or _first_non_empty(tiktok_product.get("product_id")),
            media_role="product_gallery_image",
            skip_source_urls={tiktok_source_url} if tiktok_source_url else set(),
        )
    )
    candidates.extend(
        _tiktok_logical_media_candidates(
            tiktok_product.get("sku_images") or tiktok_logical_fields.get("sku_images"),
            product_id=product_id or _first_non_empty(tiktok_product.get("product_id")),
            media_role="product_sku_image",
        )
    )

    fastmoss_data = _as_mapping(fastmoss_payload.get("fastmoss"))
    for source_endpoint, raw_payload, mapper in (
        ("fastmoss.goods.v3.base", _as_mapping(fastmoss_data.get("base")), map_fastmoss_goods_base),
        ("fastmoss.goods.v3.overview", _as_mapping(fastmoss_data.get("overview")), map_fastmoss_goods_overview),
        ("fastmoss.goods.v3.productSku", _as_mapping(fastmoss_data.get("skus")), map_fastmoss_goods_product_sku),
    ):
        if not raw_payload:
            continue
        mapped = mapper({"data": dict(raw_payload)}, product_id=product_id)
        for media_asset in mapped["media_assets"]:
            if _first_non_empty(media_asset.get("entity_type")) != "product":
                continue
            candidates.append(
                {
                    **dict(media_asset),
                    "source_endpoint": source_endpoint,
                    "entity_external_id": _first_non_empty(
                        media_asset.get("entity_external_id"),
                        product_id,
                    ),
                }
            )
    return _dedupe_media_assets(candidates)


def _tiktok_logical_media_candidates(
    images: Any,
    *,
    product_id: str,
    media_role: str,
    skip_source_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(images, list):
        return []
    skipped = {str(url).strip() for url in (skip_source_urls or set()) if str(url).strip()}
    candidates: list[dict[str, Any]] = []
    for fallback_order, image in enumerate(images):
        if not isinstance(image, Mapping):
            continue
        source_url = _first_non_empty(image.get("source_url"), image.get("url"), image.get("image_url"))
        if source_url and source_url in skipped:
            continue
        local_path = _first_non_empty(image.get("local_path"), image.get("path"))
        if not (source_url or local_path):
            continue
        candidates.append(
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": _first_non_empty(image.get("media_role")) or media_role,
                "source_url": source_url,
                "file_token": _stable_tiktok_media_file_token(image),
                "local_path": local_path,
                "file_name": _first_non_empty(image.get("file_name")),
                "mime_type": _first_non_empty(image.get("mime_type")),
                "source_platform": "tiktok",
                "metadata": _logical_media_metadata(image, fallback_order=fallback_order),
            }
        )
    return candidates


def _tiktok_main_image_file_token(
    tiktok_product: Mapping[str, Any],
    tiktok_logical_fields: Mapping[str, Any],
    *,
    source_url: str,
) -> str:
    for image in _logical_image_list(tiktok_product.get("gallery_images")):
        if _first_non_empty(image.get("source_url"), image.get("url"), image.get("image_url")) == source_url:
            return _stable_tiktok_media_file_token(image)
    for image in _logical_image_list(tiktok_logical_fields.get("gallery_images")):
        if _first_non_empty(image.get("source_url"), image.get("url"), image.get("image_url")) == source_url:
            return _stable_tiktok_media_file_token(image)
    for image in (
        *_logical_image_list(tiktok_product.get("gallery_images")),
        *_logical_image_list(tiktok_logical_fields.get("gallery_images")),
    ):
        if _coerce_int(image.get("display_order"), default=-1) == 0:
            return _stable_tiktok_media_file_token(image)
    return ""


def _logical_image_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _stable_tiktok_media_file_token(image: Mapping[str, Any]) -> str:
    existing = _first_non_empty(image.get("file_token"))
    if existing:
        return existing
    uri = _first_non_empty(image.get("uri"))
    return f"tiktok_uri:{uri}" if uri else ""


def _coerce_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _media_stable_identity(media_spec: Mapping[str, Any]) -> str:
    return _first_non_empty(
        media_spec.get("file_token"),
        _as_mapping(media_spec.get("metadata")).get("file_token"),
        _as_mapping(media_spec.get("metadata")).get("uri"),
        media_spec.get("source_url"),
        media_spec.get("local_path"),
    )


def _logical_media_metadata(image: Mapping[str, Any], *, fallback_order: int) -> dict[str, Any]:
    metadata = {
        key: value
        for key, value in image.items()
        if key
        not in {
            "media_role",
            "source_url",
            "url",
            "image_url",
            "local_path",
            "path",
            "file_token",
            "file_name",
            "mime_type",
            "source_platform",
            "bucket",
            "remote_uri",
            "object_key",
        }
    }
    metadata.setdefault("display_order", fallback_order)
    return metadata


def _dedupe_media_assets(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            _first_non_empty(candidate.get("entity_type")),
            _first_non_empty(candidate.get("entity_external_id")),
            _first_non_empty(candidate.get("media_role")),
            _media_stable_identity(candidate),
        )
        if not key[-1] or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _ensure_product_media_local_file(
    media_spec: Mapping[str, Any],
    *,
    product_id: str,
    settings: Mapping[str, Any],
    session: requests.Session,
) -> dict[str, Any]:
    prepared = dict(media_spec)
    local_path = Path(str(prepared.get("local_path") or "")).expanduser()
    if str(local_path) != "." and local_path.exists() and local_path.is_file():
        prepared["local_path"] = str(local_path)
        prepared["file_name"] = _first_non_empty(prepared.get("file_name"), local_path.name)
        prepared["mime_type"] = _first_non_empty(prepared.get("mime_type"), _guess_mime_type(local_path))
        return prepared

    source_url = _first_non_empty(prepared.get("source_url"))
    if not source_url:
        raise ValueError("Product media asset must have either local_path or source_url.")

    response = session.get(source_url, timeout=int(settings.get("timeout_seconds") or 30))
    response.raise_for_status()
    content = response.content
    if not content:
        raise ValueError(f"Downloaded product media is empty: {source_url}")
    content_type = str(response.headers.get("Content-Type", "") or "")
    suffix = _guess_media_suffix(source_url, content_type)
    stable_identity = _media_stable_identity(prepared) or source_url
    digest = hashlib.sha1(stable_identity.encode("utf-8")).hexdigest()[:16]
    role = _safe_object_segment(_first_non_empty(prepared.get("media_role"), "product_image"))
    file_name = _first_non_empty(prepared.get("file_name"), f"{product_id}-{role}-{digest}{suffix}")
    target_dir = Path(str(settings.get("media_download_dir") or "runtime/downloads/product_media_assets")) / (
        product_id or "unknown-product"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / Path(file_name).name
    target_path.write_bytes(content)

    prepared["local_path"] = str(target_path)
    prepared["file_name"] = target_path.name
    prepared["mime_type"] = _normalize_content_type(content_type, suffix)
    return prepared


def _build_product_media_object_key(
    *,
    product_id: str,
    media_spec: Mapping[str, Any],
    local_path: Path,
    artifact_object_prefix: str,
) -> str:
    role = _safe_object_segment(_first_non_empty(media_spec.get("media_role"), "product_image"))
    source = _media_stable_identity(media_spec) or str(local_path)
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    file_name = Path(_first_non_empty(media_spec.get("file_name"), local_path.name)).name
    object_name = f"{role}-{digest}-{file_name}"
    return join_object_key(
        artifact_object_prefix,
        f"product-media/{_safe_object_segment(product_id or 'unknown-product')}/{object_name}",
    )


def _uploaded_media_assets(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = payload.get("uploaded_media_assets") or payload.get("items")
    if not isinstance(values, list):
        return []
    return [dict(value) for value in values if isinstance(value, Mapping)]


def _tiktok_main_image_upload_fields(uploaded_media_assets: list[dict[str, Any]]) -> dict[str, Any]:
    for media_asset in uploaded_media_assets:
        if _first_non_empty(media_asset.get("source_platform")) != "tiktok":
            continue
        if _first_non_empty(media_asset.get("media_role")) != "product_main_image":
            continue
        return {
            "main_image_object_key": _first_non_empty(media_asset.get("object_key")),
            "main_image_bucket": _first_non_empty(media_asset.get("bucket")),
            "main_image_remote_uri": _first_non_empty(media_asset.get("remote_uri")),
            "main_image_file_token": _first_non_empty(media_asset.get("file_token")),
        }
    return {}


def _apply_uploaded_tiktok_media_to_logical_fields(
    logical_fields: Mapping[str, Any],
    uploaded_media_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    updated = dict(logical_fields)
    updated.update(_tiktok_main_image_upload_fields(uploaded_media_assets))
    uploaded_by_role_and_url = {
        (
            _first_non_empty(media_asset.get("media_role")),
            _first_non_empty(media_asset.get("source_url")),
        ): media_asset
        for media_asset in uploaded_media_assets
        if _first_non_empty(media_asset.get("source_platform")) == "tiktok"
        and _first_non_empty(media_asset.get("source_url"))
    }
    uploaded_by_role_and_token = {
        (
            _first_non_empty(media_asset.get("media_role")),
            _first_non_empty(media_asset.get("file_token")),
        ): media_asset
        for media_asset in uploaded_media_assets
        if _first_non_empty(media_asset.get("source_platform")) == "tiktok"
        and _first_non_empty(media_asset.get("file_token"))
    }
    for logical_key, media_role in (
        ("gallery_images", "product_gallery_image"),
        ("sku_images", "product_sku_image"),
    ):
        images = updated.get(logical_key)
        if not isinstance(images, list):
            continue
        decorated_images: list[dict[str, Any]] = []
        for image in images:
            if not isinstance(image, Mapping):
                continue
            source_url = _first_non_empty(image.get("source_url"), image.get("url"), image.get("image_url"))
            file_token = _stable_tiktok_media_file_token(image)
            uploaded = uploaded_by_role_and_token.get((media_role, file_token)) or uploaded_by_role_and_url.get(
                (media_role, source_url)
            )
            decorated = dict(image)
            if uploaded:
                decorated.update(
                    {
                        "object_key": _first_non_empty(uploaded.get("object_key")),
                        "bucket": _first_non_empty(uploaded.get("bucket")),
                        "remote_uri": _first_non_empty(uploaded.get("remote_uri")),
                        "file_token": _first_non_empty(uploaded.get("file_token"), file_token),
                        "local_path": _first_non_empty(uploaded.get("local_path")),
                        "file_name": _first_non_empty(uploaded.get("file_name")),
                        "mime_type": _first_non_empty(uploaded.get("mime_type")),
                        "source_platform": "tiktok",
                    }
                )
            decorated_images.append(decorated)
        updated[logical_key] = decorated_images
    return updated


def _apply_uploaded_media_to_assets(
    media_assets: list[dict[str, Any]],
    uploaded_media_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    uploaded_by_key = {
        _media_match_key(media_asset): media_asset
        for media_asset in uploaded_media_assets
        if _media_match_key(media_asset)
    }
    decorated: list[dict[str, Any]] = []
    for media_asset in media_assets:
        uploaded = uploaded_by_key.get(_media_match_key(media_asset))
        decorated.append({**dict(media_asset), **uploaded} if uploaded else dict(media_asset))
    return decorated


def _fastmoss_product_metric_snapshots(
    *,
    source_endpoint: str,
    raw_payload: Mapping[str, Any],
    product_id: str,
) -> list[dict[str, Any]]:
    if not product_id:
        return []
    if source_endpoint == "fastmoss.goods.v3.base":
        product = _as_mapping(raw_payload.get("product"))
        if not product:
            return []
        payload = _compact_mapping(
            {
                "rating_score": _first_present(
                    product.get("product_rating"),
                    product.get("rating_score"),
                    product.get("rating"),
                ),
                "review_count": _first_present(
                    product.get("review_count"),
                    product.get("has_review"),
                ),
                "comment_count": _first_present(
                    product.get("review_count"),
                    product.get("has_review"),
                ),
                "sales_count": _first_present(product.get("sold_count")),
                "sale_amount": product.get("sale_amount"),
                "price_amount": _first_non_empty(
                    product.get("floor_price"),
                    product.get("real_price"),
                    product.get("format_price"),
                ),
                "price_currency": _first_non_empty(product.get("currency"), product.get("currency_unit")),
                "price_text": _first_non_empty(product.get("real_price"), product.get("format_price")),
                "source_url": _first_non_empty(product.get("detail_url")),
            }
        )
        window_days = 0
        observation_reason = "fastmoss_base_ingest"
    elif source_endpoint == "fastmoss.goods.v3.overview":
        overview = _as_mapping(raw_payload.get("overview")) or _as_mapping(raw_payload)
        if not overview:
            return []
        window_days = _coerce_int(
            _first_present(raw_payload.get("d_type"), overview.get("d_type")),
            default=28,
        )
        payload = _compact_mapping(
            {
                **{
                    key: value
                    for key, value in overview.items()
                    if key not in {"product_id", "id"}
                },
                "d_type": window_days,
                "sales_count": _first_present(
                    overview.get("sold_count"),
                    overview.get("sales_count"),
                    overview.get("sales_7d"),
                    overview.get("sales_28d"),
                ),
                "sale_amount": _first_present(
                    overview.get("sale_amount"),
                    overview.get("gmv"),
                    overview.get("revenue"),
                ),
                "avg_sales_count": _first_present(
                    overview.get("avg_sold_count"),
                    overview.get("avg_sales_count"),
                ),
                "avg_sale_amount": _first_present(
                    overview.get("avg_sale_amount"),
                    overview.get("avg_gmv"),
                ),
            }
        )
        observation_reason = "fastmoss_overview_ingest"
    else:
        return []
    if not payload:
        return []
    return [
        {
            "product_id": product_id,
            "source_platform": "fastmoss",
            "source_endpoint": source_endpoint,
            "window_days": window_days,
            "observation_reason": observation_reason,
            "payload": payload,
        }
    ]


def _fastmoss_product_daily_metrics(
    *,
    source_endpoint: str,
    raw_payload: Mapping[str, Any],
    product_id: str,
) -> list[dict[str, Any]]:
    if source_endpoint != "fastmoss.goods.v3.overview" or not product_id:
        return []
    overview = _as_mapping(raw_payload.get("overview"))
    window_days = _coerce_int(
        _first_present(raw_payload.get("d_type"), overview.get("d_type")),
        default=28,
    )
    metrics: list[dict[str, Any]] = []
    for row in _extract_rows(raw_payload, "chart_list", "trend", "trends"):
        metric_date = _first_non_empty(row.get("dt"), row.get("date"), row.get("day"))
        if not metric_date:
            continue
        sold_count = _first_present(row.get("inc_sold_count"), row.get("sold_count"))
        sale_amount = _first_present(row.get("inc_sale_amount"), row.get("sale_amount"))
        price_amount = _first_present(row.get("price"), row.get("price_amount"))
        payload = _compact_mapping(
            {
                **dict(row),
                "source_endpoint": source_endpoint,
                "window_days": window_days,
                "sold_count": sold_count,
                "sale_amount": sale_amount,
                "price_amount": price_amount,
            }
        )
        metrics.append(
            {
                "product_id": product_id,
                "metric_date": metric_date,
                "source_platform": "fastmoss",
                "source_endpoint": source_endpoint,
                "sold_count": sold_count,
                "sale_amount": sale_amount,
                "price_amount": price_amount,
                "currency": _first_non_empty(row.get("currency")),
                "payload": payload,
            }
        )
    return metrics


def _fastmoss_product_distribution_snapshots(
    *,
    source_endpoint: str,
    raw_payload: Mapping[str, Any],
    product_id: str,
) -> list[dict[str, Any]]:
    if source_endpoint != "fastmoss.goods.v3.overview" or not product_id:
        return []
    overview = _as_mapping(raw_payload.get("overview"))
    window_days = _coerce_int(
        _first_present(raw_payload.get("d_type"), overview.get("d_type")),
        default=28,
    )
    snapshots: list[dict[str, Any]] = []
    for distribution_type, payload_key, key_field in (
        ("channel", "channel_distribution", "source"),
        ("content", "content_distribution", "category"),
        ("ads", "ads_distribution", "category"),
    ):
        distribution = _as_mapping(raw_payload.get(payload_key))
        units = _as_mapping(distribution.get("units_sold"))
        gmv = _as_mapping(distribution.get("gmv"))
        merged: dict[str, dict[str, Any]] = {}
        for row in _extract_rows(units, "list"):
            source_key = _distribution_source_key(row, preferred_key=key_field)
            if not source_key:
                continue
            item = merged.setdefault(source_key, {})
            item.update(
                {
                    "sold_count": _first_present(row.get("sold_count")),
                    "sold_count_show": _first_non_empty(row.get("sold_count_show")),
                    "sold_proportion": _first_present(
                        row.get("propotion"),
                        row.get("proportion"),
                        row.get("percent"),
                    ),
                }
            )
        for row in _extract_rows(gmv, "list"):
            source_key = _distribution_source_key(row, preferred_key=key_field)
            if not source_key:
                continue
            item = merged.setdefault(source_key, {})
            item.update(
                {
                    "sale_amount": _first_present(row.get("sale_amount")),
                    "sale_amount_show": _first_non_empty(row.get("sale_amount_show")),
                    "gmv_proportion": _first_present(
                        row.get("propotion"),
                        row.get("proportion"),
                        row.get("percent"),
                    ),
                    "currency": _first_non_empty(row.get("currency")),
                }
            )
        for source_key, values in merged.items():
            payload = _compact_mapping(
                {
                    "distribution_type": distribution_type,
                    "source_key": source_key,
                    "source_name": _fastmoss_distribution_source_name(source_key),
                    "window_days": window_days,
                    "total_sold_count": _first_present(units.get("total_count")),
                    "total_sale_amount": _first_present(gmv.get("total_count")),
                    "source_endpoint": source_endpoint,
                    **values,
                }
            )
            snapshots.append(
                {
                    "product_id": product_id,
                    "distribution_type": distribution_type,
                    "source_key": source_key,
                    "source_name": _fastmoss_distribution_source_name(source_key),
                    "source_platform": "fastmoss",
                    "source_endpoint": source_endpoint,
                    "window_days": window_days,
                    "metric_value": values.get("sold_count"),
                    "metric_amount": values.get("sale_amount"),
                    "observation_reason": "fastmoss_overview_distribution_ingest",
                    "payload": payload,
                }
            )
    return snapshots


def _fastmoss_sku_visualization_payload(fastmoss_data: Mapping[str, Any]) -> dict[str, Any]:
    sku_distribution = _as_mapping(fastmoss_data.get("sku_distribution"))
    if any(key in sku_distribution for key in ("sku_units_sold", "sku_gmv", "sku_stock", "best_sku")):
        return sku_distribution
    return _as_mapping(fastmoss_data.get("skus"))


def _logical_tiktok_skus(logical_fields: Mapping[str, Any]) -> list[dict[str, Any]]:
    skus = _list_of_mappings(logical_fields.get("skus"))
    if skus:
        return skus
    options = _list_of_mappings(logical_fields.get("sku_options"))
    if len(options) != 1:
        return []
    option_name = _first_non_empty(options[0].get("name"))
    if not option_name:
        return []
    derived: list[dict[str, Any]] = []
    for option_value in _list_of_mappings(options[0].get("values")):
        value_name = _first_non_empty(option_value.get("value"))
        if not value_name:
            continue
        property_pair = {
            "name": option_name,
            "value": value_name,
            "value_id": _first_non_empty(option_value.get("value_id")),
            "sku_property_key": _first_non_empty(option_value.get("sku_property_key")) or f"{option_name}:{value_name}",
        }
        derived.append(
            {
                "sku_name": value_name,
                "spec_name": f"{option_name}: {value_name}",
                "properties": [property_pair],
                "sku_property_keys": [property_pair["sku_property_key"]],
                "source_platform": "tiktok",
            }
        )
    return derived


def _prefer_tiktok_product_sku_text(
    fastmoss_product_skus: list[dict[str, Any]],
    tiktok_skus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not tiktok_skus:
        return fastmoss_product_skus
    tiktok_index = _tiktok_sku_reference_index(tiktok_skus)
    merged: list[dict[str, Any]] = []
    for sku_spec in fastmoss_product_skus:
        matched_tiktok = _match_tiktok_sku_reference(sku_spec, tiktok_index)
        if not matched_tiktok:
            merged.append(dict(sku_spec))
            continue
        facts = _as_mapping(sku_spec.get("facts"))
        facts.update(
            {
                "tiktok_sku_name": _first_non_empty(matched_tiktok.get("sku_name"), matched_tiktok.get("name")),
                "tiktok_spec_name": _first_non_empty(matched_tiktok.get("spec_name")),
                "tiktok_properties": _list_of_mappings(matched_tiktok.get("properties")),
            }
        )
        merged.append(
            {
                **dict(sku_spec),
                "sku_name": _first_non_empty(
                    matched_tiktok.get("sku_name"),
                    matched_tiktok.get("name"),
                    sku_spec.get("sku_name"),
                ),
                "spec_name": _first_non_empty(
                    matched_tiktok.get("spec_name"),
                    sku_spec.get("spec_name"),
                ),
                "facts": facts,
            }
        )
    return merged


def _tiktok_sku_reference_index(tiktok_skus: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for sku in tiktok_skus:
        for key in _sku_reference_keys_from_spec(sku):
            normalized = _normalize_lookup_key(key)
            if normalized:
                index.setdefault(normalized, dict(sku))
    if len(tiktok_skus) == 1:
        index.setdefault("__single__", dict(tiktok_skus[0]))
    return index


def _match_tiktok_sku_reference(
    sku_spec: Mapping[str, Any],
    tiktok_index: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    for key in _sku_reference_keys_from_spec(sku_spec):
        matched = tiktok_index.get(_normalize_lookup_key(key))
        if matched:
            return matched
    return tiktok_index.get("__single__", {})


def _sku_reference_keys_from_spec(sku_spec: Mapping[str, Any]) -> list[str]:
    keys = [
        _first_non_empty(sku_spec.get("sku_id"), sku_spec.get("id")),
        _first_non_empty(sku_spec.get("sku_name"), sku_spec.get("name")),
        _first_non_empty(sku_spec.get("spec_name")),
    ]
    for prop in _list_of_mappings(sku_spec.get("properties")):
        prop_name = _first_non_empty(prop.get("name"), prop.get("prop_name"))
        prop_value = _first_non_empty(prop.get("value"), prop.get("prop_value"), prop.get("value_name"))
        keys.append(prop_value)
        if prop_name and prop_value:
            keys.append(f"{prop_name}: {prop_value}")
            keys.append(f"{prop_name}:{prop_value}")
    for key in _list_of_texts(sku_spec.get("sku_property_keys")):
        keys.append(key)
    props = sku_spec.get("sku_sale_props") or sku_spec.get("props")
    for prop in _list_of_mappings(props):
        prop_name = _first_non_empty(prop.get("prop_name"), prop.get("name"))
        prop_value = _first_non_empty(prop.get("prop_value"), prop.get("value_name"), prop.get("value"))
        keys.append(prop_value)
        if prop_name and prop_value:
            keys.append(f"{prop_name}: {prop_value}")
            keys.append(f"{prop_name}:{prop_value}")
    return [key for key in keys if key]


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_of_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_first_non_empty(item) for item in value]


def _fastmoss_product_sku_metric_snapshots(
    *,
    source_endpoint: str,
    raw_payload: Mapping[str, Any],
    product_id: str,
    tiktok_skus: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not product_id:
        return []
    if source_endpoint == "fastmoss.goods.productSku":
        return _fastmoss_product_sku_distribution_metric_snapshots(
            source_endpoint=source_endpoint,
            raw_payload=raw_payload,
            product_id=product_id,
            tiktok_skus=tiktok_skus or [],
        )
    if source_endpoint != "fastmoss.goods.v3.productSku":
        return []
    window_days = _coerce_int(raw_payload.get("d_type"), default=28)
    tiktok_index = _tiktok_sku_reference_index(tiktok_skus or [])
    snapshots: list[dict[str, Any]] = []
    for row in _extract_rows(raw_payload, "sku_list", "list"):
        matched_tiktok = _match_tiktok_sku_reference(row, tiktok_index)
        sku_id = _first_non_empty(row.get("sku_id"), row.get("id"))
        spec_name = _join_spec_values(row.get("sku_sale_props") or row.get("props"))
        sku_name = _first_non_empty(
            matched_tiktok.get("sku_name"),
            matched_tiktok.get("name"),
            row.get("sku_name"),
            row.get("name"),
            spec_name,
            sku_id,
        )
        spec_name = _first_non_empty(matched_tiktok.get("spec_name"), spec_name)
        if not (sku_id or sku_name):
            continue
        row_product_id = _first_non_empty(row.get("product_id"), product_id)
        price_text = _first_non_empty(
            row.get("real_price"),
            row.get("format_price"),
            row.get("price"),
            row.get("sale_price"),
        )
        price_amount = _first_present(
            row.get("real_price_value"),
            row.get("price_amount"),
            row.get("floor_price"),
            row.get("sale_price_value"),
        )
        stock_count = _first_present(row.get("stock"), row.get("stock_count"))
        sold_count = _first_present(row.get("sold_count"), row.get("sales_count"))
        sale_amount = _first_present(row.get("sale_amount"), row.get("sku_sale_amount"))
        payload = _compact_mapping(
            {
                "sku_id": sku_id,
                "sku_name": sku_name,
                "spec_name": spec_name,
                "price_text": price_text,
                "price_amount": price_amount,
                "price_currency": _first_non_empty(row.get("currency"), row.get("currency_unit")),
                "stock_count": stock_count,
                "sold_count": sold_count,
                "sales_count": sold_count,
                "sale_amount": sale_amount,
            }
        )
        if not payload:
            continue
        snapshots.append(
            {
                "product_id": row_product_id,
                "sku_key": f"{row_product_id}:{sku_id or sku_name}",
                "sku_id": sku_id,
                "sku_name": sku_name,
                "source_platform": "fastmoss",
                "source_endpoint": source_endpoint,
                "window_days": window_days,
                "sold_count": sold_count,
                "sale_amount": sale_amount,
                "stock_count": stock_count,
                "observation_reason": "fastmoss_product_sku_ingest",
                "payload": payload,
            }
        )
    return snapshots


def _fastmoss_product_sku_distribution_metric_snapshots(
    *,
    source_endpoint: str,
    raw_payload: Mapping[str, Any],
    product_id: str,
    tiktok_skus: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    window_days = _coerce_int(raw_payload.get("d_type"), default=28)
    sku_index = _sku_reference_index(raw_payload)
    tiktok_index = _tiktok_sku_reference_index(tiktok_skus or [])
    units_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    gmv_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    stock_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for dimension, group_value in _as_mapping(raw_payload.get("sku_units_sold")).items():
        group = _as_mapping(group_value)
        for row in _extract_rows(group, "list"):
            source = _distribution_source_key(row, preferred_key="source")
            if source:
                units_by_key[(str(dimension), source)] = {**dict(row), "total_count": group.get("total_count")}
    for dimension, group_value in _as_mapping(raw_payload.get("sku_gmv")).items():
        group = _as_mapping(group_value)
        for row in _extract_rows(group, "list"):
            source = _distribution_source_key(row, preferred_key="source")
            if source:
                gmv_by_key[(str(dimension), source)] = {**dict(row), "total_count": group.get("total_count")}
    for dimension, group_value in _as_mapping(raw_payload.get("sku_stock")).items():
        group = _as_mapping(group_value)
        for row in _extract_rows(group, "list"):
            source = _distribution_source_key(row, preferred_key="source")
            if source:
                stock_by_key[(str(dimension), source)] = {**dict(row), "total_count": group.get("total_count")}
    snapshots: list[dict[str, Any]] = []
    best_sku = _as_mapping(raw_payload.get("best_sku"))
    total_sold_by_dimension = _sum_metric_by_dimension(units_by_key, "sold_count")
    total_sale_by_dimension = _sum_metric_by_dimension(gmv_by_key, "sale_amount")
    total_stock_by_dimension = _sum_metric_by_dimension(stock_by_key, "sold_count")
    for dimension, source in sorted({*units_by_key.keys(), *gmv_by_key.keys(), *stock_by_key.keys()}):
        unit_row = units_by_key.get((dimension, source), {})
        gmv_row = gmv_by_key.get((dimension, source), {})
        stock_row = stock_by_key.get((dimension, source), {})
        sku_reference = sku_index.get(_normalize_lookup_key(source), {})
        matched_tiktok = _match_tiktok_sku_reference(
            {
                **sku_reference,
                "sku_name": source,
                "spec_name": f"{dimension}: {source}",
                "sku_sale_props": [{"prop_name": dimension, "prop_value": source}],
            },
            tiktok_index,
        )
        sku_id = _first_non_empty(sku_reference.get("sku_id"), sku_reference.get("id"))
        sku_name = _first_non_empty(
            matched_tiktok.get("sku_name"),
            matched_tiktok.get("name"),
            source,
            sku_reference.get("sku_name"),
            sku_reference.get("name"),
        )
        sku_key = f"{product_id}:{sku_id}" if sku_id else f"{product_id}:{dimension}:{sku_name}"
        sold_count = _first_present(unit_row.get("sold_count"))
        sale_amount = _first_present(gmv_row.get("sale_amount"))
        stock_count = _first_present(stock_row.get("sold_count"), sku_reference.get("stock"), sku_reference.get("stock_count"))
        payload = _compact_mapping(
            {
                "dimension": dimension,
                "sku_source": source,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "spec_name": _first_non_empty(matched_tiktok.get("spec_name"), f"{dimension}: {source}"),
                "sku_key": sku_key,
                "window_days": window_days,
                "sold_count": sold_count,
                "sales_count": sold_count,
                "sold_count_show": _first_non_empty(unit_row.get("sold_count_show")),
                "sold_proportion": _first_present(
                    unit_row.get("propotion"),
                    unit_row.get("proportion"),
                    unit_row.get("percent"),
                ),
                "sale_amount": sale_amount,
                "sale_amount_show": _first_non_empty(gmv_row.get("sale_amount_show")),
                "gmv_proportion": _first_present(
                    gmv_row.get("propotion"),
                    gmv_row.get("proportion"),
                    gmv_row.get("percent"),
                ),
                "stock_count": stock_count,
                "stock_count_show": _first_non_empty(stock_row.get("sold_count_show")),
                "stock_proportion": _first_present(
                    stock_row.get("propotion"),
                    stock_row.get("proportion"),
                    stock_row.get("percent"),
                ),
                "total_sold_count": total_sold_by_dimension.get(dimension),
                "total_sale_amount": total_sale_by_dimension.get(dimension),
                "total_stock_count": total_stock_by_dimension.get(dimension),
                "raw_units_total_count": _first_present(unit_row.get("total_count")),
                "raw_gmv_total_count": _first_present(gmv_row.get("total_count")),
                "raw_stock_total_count": _first_present(stock_row.get("total_count")),
                "currency": _first_non_empty(gmv_row.get("currency"), best_sku.get("currency")),
                "price_text": _first_non_empty(sku_reference.get("real_price"), best_sku.get("price")),
                "price_amount": _first_present(sku_reference.get("real_price_value")),
                "is_best_sku": _normalize_lookup_key(best_sku.get("sku_value")) == _normalize_lookup_key(source),
                "source_endpoint": source_endpoint,
            }
        )
        snapshots.append(
            {
                "product_id": product_id,
                "sku_key": sku_key,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "source_platform": "fastmoss",
                "source_endpoint": source_endpoint,
                "window_days": window_days,
                "sold_count": sold_count,
                "sale_amount": sale_amount,
                "stock_count": stock_count,
                "observation_reason": "fastmoss_product_sku_distribution_ingest",
                "payload": payload,
            }
        )
    return snapshots


def _sum_metric_by_dimension(
    rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    metric_key: str,
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for (dimension, _source), row in rows_by_key.items():
        totals[dimension] = totals.get(dimension, 0.0) + _coerce_float(row.get(metric_key))
    return totals


def _extract_rows(mapping: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        rows = mapping.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _join_spec_values(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                parts.append(_first_non_empty(item.get("value_name"), item.get("name"), item.get("value")))
            else:
                parts.append(_first_non_empty(item))
        return " / ".join(part for part in parts if part)
    return _first_non_empty(value)


def _distribution_source_key(row: Mapping[str, Any], *, preferred_key: str) -> str:
    return _first_non_empty(
        row.get(preferred_key),
        row.get("source"),
        row.get("category"),
        row.get("name"),
        row.get("sku_name"),
    )


def _fastmoss_distribution_source_name(source_key: str) -> str:
    return {
        "video.name": "短视频",
        "live.name": "直播",
        "common.goods.product_card": "商品卡",
        "common.goods.affiliate": "达人联盟",
        "common.goods.shop_account": "店铺账号",
        "common.goods.adTraffic": "广告",
        "common.goods.otherTraffic": "非广告流量",
    }.get(source_key, source_key)


def _sku_reference_index(raw_payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in _extract_rows(raw_payload, "sku_list", "list"):
        for key in _sku_reference_keys(row):
            normalized = _normalize_lookup_key(key)
            if normalized:
                index.setdefault(normalized, dict(row))
    return index


def _sku_reference_keys(row: Mapping[str, Any]) -> list[str]:
    keys = [
        _first_non_empty(row.get("sku_id"), row.get("id")),
        _first_non_empty(row.get("sku_name"), row.get("name")),
    ]
    props = row.get("sku_sale_props") or row.get("props")
    if isinstance(props, list):
        for prop in props:
            if isinstance(prop, Mapping):
                keys.append(_first_non_empty(prop.get("value_name"), prop.get("prop_value"), prop.get("name"), prop.get("value")))
            else:
                keys.append(_first_non_empty(prop))
    return [key for key in keys if key]


def _normalize_lookup_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def _compact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and (not isinstance(value, str) or bool(value.strip()))
    }


def _media_match_key(media_asset: Mapping[str, Any]) -> tuple[str, str, str, str] | tuple[()]:
    identity = _media_stable_identity(media_asset)
    if not identity:
        return ()
    return (
        _first_non_empty(media_asset.get("source_platform")),
        _first_non_empty(media_asset.get("entity_type")),
        _first_non_empty(media_asset.get("entity_external_id")),
        identity,
    )


def _guess_media_suffix(media_url: str, content_type: str) -> str:
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    guessed_from_type = mimetypes.guess_extension(normalized_content_type, strict=False)
    if guessed_from_type:
        return ".jpg" if guessed_from_type == ".jpe" else guessed_from_type
    parsed_suffix = Path(urlparse(media_url).path).suffix.lower()
    return parsed_suffix or ".jpg"


def _normalize_content_type(content_type: str, file_suffix: str) -> str:
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_content_type:
        return normalized_content_type
    return mimetypes.guess_type(f"image{file_suffix}", strict=False)[0] or "application/octet-stream"


def _guess_mime_type(path: Path) -> str:
    return mimetypes.guess_type(str(path), strict=False)[0] or "application/octet-stream"


def _safe_object_segment(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return normalized or "unnamed"


def _empty_persisted_payload() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in INGESTION_KEYS}


def _merge_persisted(target: dict[str, list[dict[str, Any]]], source: Mapping[str, Any]) -> None:
    for key in INGESTION_KEYS:
        values = source.get(key)
        if not isinstance(values, list):
            continue
        target[key].extend(dict(value) for value in values if isinstance(value, Mapping))


def _resolve_secret_param(params: Mapping[str, Any], value_key: str, env_key: str) -> str:
    direct = _first_non_empty(params.get(value_key))
    if direct:
        return direct
    env_name = _first_non_empty(params.get(env_key))
    if not env_name:
        return ""
    return str(os.environ.get(env_name, "") or "").strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _with_default_d_type(payload: Mapping[str, Any], d_type: Any) -> dict[str, Any]:
    data = dict(payload or {})
    data.setdefault("d_type", d_type)
    return data


def _read_bool(params: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = params.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_bool_with_env(params: Mapping[str, Any], key: str, env_key: str, default: bool) -> bool:
    if key in params:
        return _read_bool(params, key, default)
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_int(params: Mapping[str, Any], key: str, default: int) -> int:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_float(params: Mapping[str, Any], key: str, default: float) -> float:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_float_with_env(params: Mapping[str, Any], key: str, env_key: str, default: float) -> float:
    if key in params:
        return _read_float(params, key, default)
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
