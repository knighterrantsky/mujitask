from __future__ import annotations






from automation_business_scaffold.contracts.handler.shared import (
    build_shop_key,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    extract_product_id,
    first_non_empty,
    new_fact_bundle,
)


from typing import Any


PRODUCT_STATUS_UNAVAILABLE = "off_shelf_or_region_unavailable"


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
    product_facts = compact_dict(
        {
            "collection_path": collection_path,
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
            "price_amount": first_non_empty(product_payload.get("price_amount"), raw.get("price_amount")),
            "price_currency": first_non_empty(product_payload.get("price_currency"), raw.get("price_currency")),
            "sales_count": first_non_empty(product_payload.get("sales_count"), raw.get("sales_count")),
            "rating_score": first_non_empty(product_payload.get("rating_score"), raw.get("rating_score")),
            "review_count": first_non_empty(product_payload.get("review_count"), raw.get("review_count")),
            "comment_count": first_non_empty(product_payload.get("comment_count"), raw.get("comment_count")),
            "availability_status": first_non_empty(
                product_payload.get("availability_status"),
                raw.get("availability_status"),
            ),
            "unavailable_message": first_non_empty(
                product_payload.get("unavailable_message"),
                raw.get("unavailable_message"),
            ),
        }
    )
    product_status = _product_status_from_availability(product_facts)
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
            "status": product_status,
            "facts": product_facts or {"collection_path": collection_path},
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


def _product_status_from_availability(product_facts: dict[str, Any]) -> str:
    availability_status = coerce_str(product_facts.get("availability_status")).strip().lower()
    if availability_status == "unavailable":
        return PRODUCT_STATUS_UNAVAILABLE
    return "active"


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
                        "local_path": first_non_empty(
                            raw_payload.get("main_image_local_path"),
                            coerce_mapping(raw_payload.get("product")).get("main_image_local_path"),
                        ),
                        "file_name": first_non_empty(
                            raw_payload.get("main_image_file_name"),
                            coerce_mapping(raw_payload.get("product")).get("main_image_file_name"),
                        ),
                        "mime_type": first_non_empty(
                            raw_payload.get("main_image_mime_type"),
                            coerce_mapping(raw_payload.get("product")).get("main_image_mime_type"),
                        ),
                        "source_platform": "tiktok",
                    },
                    fallback_product_id=product_id,
                )
            )
            break
    gallery_images = raw_payload.get("gallery_images") or coerce_mapping(raw_payload.get("product")).get("gallery_images")
    for entry in gallery_images if isinstance(gallery_images, list) else []:
        entry_map = coerce_mapping(entry)
        source_url = entry if isinstance(entry, str) else first_non_empty(
            entry_map.get("source_url"),
            entry_map.get("url"),
            entry_map.get("image_url"),
        )
        if not source_url:
            continue
        media_assets.append(
            _normalize_media_asset(
                {
                    "entity_type": "product",
                    "entity_external_id": product_id,
                    "media_role": first_non_empty(entry_map.get("media_role"), "product_gallery_image"),
                    "source_url": source_url,
                    "file_token": entry_map.get("file_token"),
                    "local_path": first_non_empty(entry_map.get("local_path"), entry_map.get("path")),
                    "file_name": entry_map.get("file_name"),
                    "mime_type": entry_map.get("mime_type"),
                    "source_platform": "tiktok",
                    "metadata": {
                        key: value
                        for key, value in entry_map.items()
                        if key
                        not in {
                            "media_role",
                            "source_url",
                            "url",
                            "image_url",
                            "file_token",
                            "local_path",
                            "path",
                            "file_name",
                            "mime_type",
                            "source_platform",
                        }
                    },
                },
                fallback_product_id=product_id,
            )
        )
    sku_images = raw_payload.get("sku_images") or coerce_mapping(raw_payload.get("product")).get("sku_images")
    for entry in sku_images if isinstance(sku_images, list) else []:
        entry_map = coerce_mapping(entry)
        source_url = first_non_empty(entry_map.get("source_url"), entry_map.get("url"), entry_map.get("image_url"))
        if not source_url:
            continue
        media_assets.append(
            _normalize_media_asset(
                {
                    "entity_type": "product",
                    "entity_external_id": product_id,
                    "media_role": first_non_empty(entry_map.get("media_role"), "product_sku_image"),
                    "source_url": source_url,
                    "file_token": entry_map.get("file_token"),
                    "local_path": first_non_empty(entry_map.get("local_path"), entry_map.get("path")),
                    "file_name": entry_map.get("file_name"),
                    "mime_type": entry_map.get("mime_type"),
                    "source_platform": "tiktok",
                    "metadata": {
                        key: value
                        for key, value in entry_map.items()
                        if key
                        not in {
                            "media_role",
                            "source_url",
                            "url",
                            "image_url",
                            "file_token",
                            "local_path",
                            "path",
                            "file_name",
                            "mime_type",
                            "source_platform",
                        }
                    },
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
