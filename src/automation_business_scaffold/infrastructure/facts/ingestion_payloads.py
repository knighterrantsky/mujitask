from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

"""Ingestion payload normalization for contracts/facts/product-fact-collection.yaml."""


def media_assets_from_logical_images(
    images: Any,
    *,
    product_id: str,
    media_role: str,
    skip_source_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(images, list):
        return []
    skipped = {str(url).strip() for url in (skip_source_urls or set()) if str(url).strip()}
    media_assets: list[dict[str, Any]] = []
    for fallback_order, image in enumerate(images):
        if not isinstance(image, Mapping):
            continue
        source_url = first_non_empty(image.get("source_url"), image.get("url"), image.get("image_url"))
        if source_url and source_url in skipped:
            continue
        local_path = first_non_empty(image.get("local_path"), image.get("path"))
        object_key = first_non_empty(image.get("object_key"))
        if not (source_url or local_path or object_key):
            continue
        metadata = metadata_from_logical_image(image, fallback_order=fallback_order)
        media_assets.append(
            {
                "entity_type": "product",
                "entity_external_id": product_id,
                "media_role": first_non_empty(image.get("media_role")) or media_role,
                "source_url": source_url,
                "file_token": first_non_empty(image.get("file_token")),
                "local_path": local_path,
                "object_key": object_key,
                "file_name": first_non_empty(image.get("file_name")),
                "mime_type": first_non_empty(image.get("mime_type")),
                "source_platform": first_non_empty(image.get("source_platform")) or "tiktok",
                "bucket": first_non_empty(image.get("bucket")),
                "remote_uri": first_non_empty(image.get("remote_uri")),
                "metadata": metadata,
            }
        )
    return media_assets


def metadata_from_logical_image(image: Mapping[str, Any], *, fallback_order: int) -> dict[str, Any]:
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
            "object_key",
            "file_name",
            "mime_type",
            "source_platform",
            "bucket",
            "remote_uri",
        }
    }
    metadata.setdefault("display_order", fallback_order)
    return metadata


def product_metric_payload(logical_payload: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in (
        "rating_score",
        "review_count",
        "comment_count",
        "sales_count",
        "price_amount",
        "price_currency",
        "price_text",
        "source_url",
        "normalized_url",
    ):
        value = logical_payload.get(key)
        if has_observable_value(value):
            metrics[key] = value
    return metrics


def fastmoss_snapshot_metric_payload(fastmoss_payload: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key in (
        "rating_score",
        "review_count",
        "comment_count",
        "sales_count",
        "sold_count",
        "sales_7d",
        "sales_28d",
        "sale_amount",
        "fastmoss_price_amount",
        "price_amount",
        "price_currency",
        "price_text",
        "source_url",
        "normalized_url",
    ):
        value = fastmoss_payload.get(key)
        if has_observable_value(value):
            metrics[key] = value
    if "price_amount" not in metrics and has_observable_value(metrics.get("fastmoss_price_amount")):
        metrics["price_amount"] = metrics["fastmoss_price_amount"]
    if "sales_count" not in metrics:
        sales_count = first_non_empty(
            metrics.get("sold_count"),
            metrics.get("sales_7d"),
            metrics.get("sales_28d"),
        )
        if sales_count:
            metrics["sales_count"] = sales_count
    return metrics


def has_observable_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def tiktok_product_skus_from_logical_payload(
    logical_payload: Mapping[str, Any],
    fastmoss_payload: Mapping[str, Any],
    *,
    product_id: str,
) -> list[dict[str, Any]]:
    tiktok_skus = list_of_mappings(logical_payload.get("skus"))
    if not tiktok_skus:
        tiktok_skus = skus_from_single_option_group(logical_payload.get("sku_options"), product_id=product_id)
    if not tiktok_skus:
        return []

    fastmoss_index = fastmoss_sku_reference_index(
        fastmoss_payload.get("skus"),
        fastmoss_payload.get("sku_distribution"),
    )
    normalized_skus: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sku in tiktok_skus:
        matched_fastmoss = match_fastmoss_sku_reference(sku, fastmoss_index)
        sku_id = first_non_empty(
            sku.get("sku_id"),
            sku.get("id"),
            matched_fastmoss.get("sku_id"),
            matched_fastmoss.get("id"),
        )
        sku_name = first_non_empty(
            sku.get("sku_name"),
            sku.get("name"),
            join_property_values(sku.get("properties")),
            matched_fastmoss.get("sku_name"),
            matched_fastmoss.get("name"),
            sku_id,
        )
        if not (sku_id or sku_name):
            continue
        spec_name = first_non_empty(
            sku.get("spec_name"),
            join_property_pairs(sku.get("properties")),
            sku_name,
        )
        sku_key = f"{product_id}:{sku_id or sku_name}"
        if sku_key in seen:
            continue
        seen.add(sku_key)
        facts = {
            "source_platform": "tiktok",
            "properties": list_of_mappings(sku.get("properties")),
            "sku_property_keys": [
                key for key in list_of_texts(sku.get("sku_property_keys")) if key
            ],
        }
        normalized_skus.append(
            {
                "product_id": product_id,
                "sku_id": sku_id,
                "sku_name": sku_name,
                "spec_name": spec_name,
                "price_text": first_non_empty(sku.get("price_text")),
                "stock_count": sku.get("stock_count") if sku.get("stock_count") not in (None, "") else 0,
                "facts": facts,
            }
        )
    return normalized_skus


def skus_from_single_option_group(value: Any, *, product_id: str) -> list[dict[str, Any]]:
    options = list_of_mappings(value)
    if len(options) != 1:
        return []
    option_name = first_non_empty(options[0].get("name"))
    if not option_name:
        return []
    skus: list[dict[str, Any]] = []
    for option_value in list_of_mappings(options[0].get("values")):
        value_name = first_non_empty(option_value.get("value"))
        if not value_name:
            continue
        skus.append(
            {
                "product_id": product_id,
                "sku_name": value_name,
                "spec_name": f"{option_name}: {value_name}",
                "properties": [
                    {
                        "name": option_name,
                        "value": value_name,
                        "value_id": first_non_empty(option_value.get("value_id")),
                        "sku_property_key": first_non_empty(option_value.get("sku_property_key"))
                        or f"{option_name}:{value_name}",
                    }
                ],
                "sku_property_keys": [
                    first_non_empty(option_value.get("sku_property_key")) or f"{option_name}:{value_name}"
                ],
            }
        )
    return skus


def fastmoss_sku_reference_index(*payloads: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        data = payload if isinstance(payload, Mapping) else {}
        for row in extract_rows_from_mapping(data, "sku_list", "list"):
            rows.append(row)
            for key in fastmoss_sku_reference_keys(row):
                normalized = normalize_lookup_key(key)
                if normalized:
                    index.setdefault(normalized, dict(row))
    if len(rows) == 1:
        index.setdefault("__single__", dict(rows[0]))
    return index


def match_fastmoss_sku_reference(
    tiktok_sku: Mapping[str, Any],
    fastmoss_index: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    for key in tiktok_sku_reference_keys(tiktok_sku):
        matched = fastmoss_index.get(normalize_lookup_key(key))
        if matched:
            return matched
    return fastmoss_index.get("__single__", {})


def tiktok_sku_reference_keys(sku: Mapping[str, Any]) -> list[str]:
    keys = [
        first_non_empty(sku.get("sku_id"), sku.get("id")),
        first_non_empty(sku.get("sku_name"), sku.get("name")),
        first_non_empty(sku.get("spec_name")),
    ]
    for prop in list_of_mappings(sku.get("properties")):
        keys.extend(
            [
                first_non_empty(prop.get("value")),
                first_non_empty(prop.get("sku_property_key")),
            ]
        )
    return [key for key in keys if key]


def fastmoss_sku_reference_keys(row: Mapping[str, Any]) -> list[str]:
    keys = [
        first_non_empty(row.get("sku_id"), row.get("id")),
        first_non_empty(row.get("sku_name"), row.get("name")),
    ]
    for prop in list_of_mappings(row.get("sku_sale_props") or row.get("props")):
        prop_name = first_non_empty(prop.get("prop_name"), prop.get("name"))
        prop_value = first_non_empty(prop.get("prop_value"), prop.get("value_name"), prop.get("value"))
        prop_value_id = first_non_empty(prop.get("prop_value_id"), prop.get("value_id"))
        if prop_value_id:
            keys.append(prop_value_id)
        if prop_value:
            keys.append(prop_value)
        if prop_name and prop_value:
            keys.append(f"{prop_name}: {prop_value}")
            keys.append(f"{prop_name}:{prop_value}")
    return [key for key in keys if key]


def extract_rows_from_mapping(mapping: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        rows = mapping.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def join_property_values(value: Any) -> str:
    return " / ".join(
        first_non_empty(prop.get("value"))
        for prop in list_of_mappings(value)
        if first_non_empty(prop.get("value"))
    )


def join_property_pairs(value: Any) -> str:
    return " / ".join(
        f"{first_non_empty(prop.get('name'))}: {first_non_empty(prop.get('value'))}"
        for prop in list_of_mappings(value)
        if first_non_empty(prop.get("name")) and first_non_empty(prop.get("value"))
    )


def list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def list_of_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [first_non_empty(item) for item in value]


def normalize_lookup_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def empty_ingestion_payload() -> dict[str, list[dict[str, Any]]]:
    return {
        "fact_entities": [],
        "fact_relations": [],
        "fact_media_assets": [],
        "fact_metric_observations": [],
        "raw_api_responses": [],
    }


def append_dict(target: list[dict[str, Any]], value: Mapping[str, Any] | None) -> None:
    if isinstance(value, Mapping) and value:
        target.append(dict(value))


def execution_ids(execution: Any | None) -> dict[str, str]:
    if execution is None:
        return {"request_id": "", "execution_id": "", "run_id": ""}
    return {
        "request_id": first_non_empty(getattr(execution, "request_id", "")),
        "execution_id": first_non_empty(getattr(execution, "execution_id", "")),
        "run_id": first_non_empty(getattr(execution, "run_id", "")),
    }


def facts_from_spec(spec: Mapping[str, Any], *, source_endpoint: str = "") -> dict[str, Any]:
    facts = spec.get("facts")
    if isinstance(facts, Mapping):
        payload = dict(facts)
    else:
        payload = {
            key: value
            for key, value in spec.items()
            if key
            not in {
                "metadata",
                "shop",
                "entity_type",
                "entity_external_id",
                "media_role",
                "source_url",
                "file_token",
                "local_path",
                "path",
                "object_key",
                "file_name",
                "mime_type",
            }
        }
    if source_endpoint:
        payload.setdefault("source_endpoint", source_endpoint)
    return payload


def product_status_from_spec(spec: Mapping[str, Any]) -> str:
    status = first_non_empty(spec.get("status"))
    if status:
        return status
    facts = spec.get("facts")
    fact_payload = facts if isinstance(facts, Mapping) else spec
    if first_non_empty(fact_payload.get("availability_status")).lower() == "unavailable":
        return "off_shelf_or_region_unavailable"
    return "active"


def metadata_from_relation(spec: Mapping[str, Any]) -> dict[str, Any]:
    metadata = spec.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def entity_identity(entity: Mapping[str, Any]) -> tuple[str, str]:
    if entity.get("shop_key"):
        return "shop", str(entity["shop_key"])
    if entity.get("creator_key"):
        return "creator", str(entity["creator_key"])
    if entity.get("video_key"):
        return "video", str(entity["video_key"])
    if entity.get("product_id") and entity.get("id"):
        return "product", str(entity["product_id"])
    return "", ""


def video_key(video_id: Any) -> str:
    value = first_non_empty(video_id)
    return f"video:{value}" if value else ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def infer_mime_type(path_value: Any) -> str:
    path_text = first_non_empty(path_value)
    if not path_text:
        return ""
    suffix = Path(path_text).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return ""
