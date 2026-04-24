from __future__ import annotations

from automation_business_scaffold.capabilities.fact_sources.tiktok.product_normalization import (
    _build_tiktok_normalized_product_result as _shared_build_tiktok_normalized_product_result,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerNextAction,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    build_shop_key,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    extract_product_id,
    failed_result,
    fallback_required_result,
    first_non_empty,
    new_fact_bundle,
    normalize_product_identity,
    product_business_key,
    success_result,
)
from typing import Any

HANDLER_CODE = "tiktok_product_request_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


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
        if not raw_request_result and fallback_allowed:
            return _browser_fallback_result(
                context,
                identity=identity,
                fallback_reason=first_non_empty(payload.get("fallback_reason"), "request_payload_missing_product_detail"),
                detail_message="TikTok request-first payload did not include product detail data.",
            )
        normalized = _shared_build_tiktok_normalized_product_result(
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


__all__ = ["CONTRACT", "HANDLER_CODE", "tiktok_product_request_fetch_handler"]
