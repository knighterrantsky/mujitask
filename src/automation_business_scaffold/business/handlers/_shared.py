from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from typing import Any

from .contract import HandlerContext, HandlerError, HandlerNextAction, HandlerResult

CONTRACT_REVISION = "phase2"

_PRODUCT_ID_PATTERNS = (
    re.compile(r"/product/(\d+)", re.IGNORECASE),
    re.compile(r"[?&]product_id=(\d+)", re.IGNORECASE),
    re.compile(r"\b(\d{8,})\b"),
)

_RELATION_KEYS = (
    "product_shops",
    "creator_products",
    "creator_videos",
    "video_products",
    "shop_creators",
)


def coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = coerce_str(value).lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def coerce_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = coerce_str(value)
        if text:
            return text
    return ""


def compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            compacted[key] = text
            continue
        if isinstance(value, dict):
            nested = compact_dict(dict(value))
            if nested:
                compacted[key] = nested
            continue
        if isinstance(value, list):
            if value:
                compacted[key] = value
            continue
        compacted[key] = value
    return compacted


def extract_product_id(*candidates: Any) -> str:
    for candidate in candidates:
        text = coerce_str(candidate)
        if not text:
            continue
        for pattern in _PRODUCT_ID_PATTERNS:
            matched = pattern.search(text)
            if matched is not None:
                return matched.group(1)
    return ""


def normalize_product_identity(payload: dict[str, Any]) -> dict[str, Any]:
    product_identity = coerce_mapping(payload.get("product_identity"))
    source_context = coerce_mapping(payload.get("source_context"))
    source_identity = coerce_mapping(source_context.get("product_identity"))

    product_url = first_non_empty(
        product_identity.get("product_url"),
        product_identity.get("normalized_product_url"),
        payload.get("normalized_product_url"),
        payload.get("product_url"),
        payload.get("source_url"),
        source_identity.get("product_url"),
        source_context.get("product_url"),
        source_context.get("source_url"),
    )
    normalized_product_url = first_non_empty(
        product_identity.get("normalized_product_url"),
        payload.get("normalized_product_url"),
        source_identity.get("normalized_product_url"),
        source_context.get("normalized_product_url"),
        product_url,
    )
    product_id = first_non_empty(
        product_identity.get("product_id"),
        payload.get("product_id"),
        source_identity.get("product_id"),
        source_context.get("product_id"),
        extract_product_id(product_url, normalized_product_url),
    )
    fastmoss_product_id = first_non_empty(
        product_identity.get("fastmoss_product_id"),
        payload.get("fastmoss_product_id"),
        source_identity.get("fastmoss_product_id"),
        source_context.get("fastmoss_product_id"),
        product_id,
    )

    return compact_dict(
        {
            "product_id": product_id,
            "product_url": product_url,
            "normalized_product_url": normalized_product_url,
            "fastmoss_product_id": fastmoss_product_id,
        }
    )


def product_business_key(identity: dict[str, Any]) -> str:
    return first_non_empty(
        identity.get("product_id"),
        identity.get("normalized_product_url"),
        identity.get("product_url"),
        identity.get("fastmoss_product_id"),
    )


def new_fact_bundle() -> dict[str, Any]:
    return {
        "products": [],
        "product_skus": [],
        "shops": [],
        "creators": [],
        "videos": [],
        "media_assets": [],
        "raw_api_responses": [],
        "raw_entity_links": [],
        "product_metric_snapshots": [],
        "product_daily_metrics": [],
        "product_distribution_snapshots": [],
        "product_sku_metric_snapshots": [],
        "relations": {key: [] for key in _RELATION_KEYS},
    }


def merge_fact_bundles(*bundles: dict[str, Any]) -> dict[str, Any]:
    merged = new_fact_bundle()
    for bundle in bundles:
        current = coerce_mapping(bundle)
        for key in (
            "products",
            "product_skus",
            "shops",
            "creators",
            "videos",
            "media_assets",
            "raw_api_responses",
            "raw_entity_links",
            "product_metric_snapshots",
            "product_daily_metrics",
            "product_distribution_snapshots",
            "product_sku_metric_snapshots",
        ):
            for item in coerce_mapping_list(current.get(key)):
                _append_unique(merged[key], item, collection=key, key=_bundle_item_key(key, item))
        relations = coerce_mapping(current.get("relations"))
        for relation_key in _RELATION_KEYS:
            for item in coerce_mapping_list(relations.get(relation_key)):
                _append_unique(
                    merged["relations"][relation_key],
                    item,
                    collection=relation_key,
                    key=_bundle_item_key(relation_key, item),
                )
    return merged


def bundle_entity_keys(bundle: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for product in coerce_mapping_list(bundle.get("products")):
        _append_unique_key(keys, f"product:{first_non_empty(product.get('product_id'))}")
    for shop in coerce_mapping_list(bundle.get("shops")):
        _append_unique_key(
            keys,
            f"shop:{first_non_empty(shop.get('shop_key'), shop.get('shop_id'), shop.get('shop_name'))}",
        )
    for creator in coerce_mapping_list(bundle.get("creators")):
        _append_unique_key(
            keys,
            f"creator:{first_non_empty(creator.get('creator_key'), creator.get('creator_id'), creator.get('uid'), creator.get('unique_id'))}",
        )
    for video in coerce_mapping_list(bundle.get("videos")):
        _append_unique_key(keys, f"video:{first_non_empty(video.get('video_key'), video.get('video_id'))}")
    for asset in coerce_mapping_list(bundle.get("media_assets")):
        _append_unique_key(
            keys,
            f"asset:{first_non_empty(asset.get('asset_key'), asset.get('object_key'), asset.get('source_url'), asset.get('local_path'))}",
        )
    return [key for key in keys if not key.endswith(":")]


def build_error(
    *,
    error_type: str,
    error_code: str,
    message: str,
    retryable: bool,
    fallback_allowed: bool = False,
    fallback_reason: str = "",
    details: dict[str, Any] | None = None,
) -> HandlerError:
    return HandlerError(
        error_type=error_type,
        error_code=error_code,
        message=message,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        fallback_reason=fallback_reason,
        details=details or {},
    )


def success_result(
    context: HandlerContext,
    *,
    summary: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
    next_action: HandlerNextAction | None = None,
) -> HandlerResult:
    return replace(
        HandlerResult.success(
            context,
            summary=summary,
            result=result,
            warnings=warnings,
            next_action=next_action,
        ),
        contract_revision=CONTRACT_REVISION,
    )


def skipped_result(
    context: HandlerContext,
    *,
    summary: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> HandlerResult:
    return replace(
        HandlerResult.skipped(context, summary=summary, result=result, warnings=warnings),
        contract_revision=CONTRACT_REVISION,
    )


def partial_success_result(
    context: HandlerContext,
    *,
    summary: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
    next_action: HandlerNextAction | None = None,
) -> HandlerResult:
    return replace(
        HandlerResult.partial_success(
            context,
            summary=summary,
            result=result,
            warnings=warnings,
            next_action=next_action,
        ),
        contract_revision=CONTRACT_REVISION,
    )


def failed_result(
    context: HandlerContext,
    *,
    error: HandlerError,
    summary: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> HandlerResult:
    return replace(
        HandlerResult.failed(
            context,
            error=error,
            summary=summary,
            result=result,
            warnings=warnings,
        ),
        contract_revision=CONTRACT_REVISION,
    )


def fallback_required_result(
    context: HandlerContext,
    *,
    error: HandlerError,
    summary: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    warnings: tuple[str, ...] | list[str] = (),
    next_action: HandlerNextAction | None = None,
) -> HandlerResult:
    return replace(
        HandlerResult.fallback_required(
            context,
            error=error,
            summary=summary,
            result=result,
            warnings=warnings,
            next_action=next_action,
        ),
        contract_revision=CONTRACT_REVISION,
    )


def json_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def now_timestamp() -> float:
    return time.time()


def build_shop_key(*, shop_id: str = "", shop_name: str = "") -> str:
    normalized_shop_id = coerce_str(shop_id)
    normalized_shop_name = coerce_str(shop_name)
    if normalized_shop_id:
        return f"shop_id:{normalized_shop_id}"
    if normalized_shop_name:
        return f"shop_name:{normalized_shop_name}"
    return ""


def build_creator_key(*, creator_id: str = "", uid: str = "", unique_id: str = "") -> str:
    normalized_creator_id = coerce_str(creator_id)
    normalized_uid = coerce_str(uid)
    normalized_unique_id = coerce_str(unique_id)
    if normalized_creator_id:
        return f"creator_id:{normalized_creator_id}"
    if normalized_uid:
        return f"uid:{normalized_uid}"
    if normalized_unique_id:
        return f"unique_id:{normalized_unique_id}"
    return ""


def _append_unique(
    target: list[dict[str, Any]],
    item: dict[str, Any],
    *,
    collection: str,
    key: str,
) -> None:
    normalized = compact_dict(dict(item))
    if not normalized:
        return
    item_key = key or json_fingerprint(normalized)
    for index, existing in enumerate(target):
        if _bundle_item_key(collection, existing) == item_key:
            merged = dict(existing)
            merged.update(normalized)
            if isinstance(existing.get("facts"), dict) or isinstance(normalized.get("facts"), dict):
                merged["facts"] = {
                    **coerce_mapping(existing.get("facts")),
                    **coerce_mapping(normalized.get("facts")),
                }
            target[index] = compact_dict(merged)
            return
    target.append(normalized)


def _append_unique_key(target: list[str], item: str) -> None:
    text = coerce_str(item)
    if text and text not in target:
        target.append(text)


def _bundle_item_key(collection: str, item: dict[str, Any]) -> str:
    record = coerce_mapping(item)
    if collection in {"products", "product_shops"}:
        return first_non_empty(record.get("product_id"), record.get("relation_key"))
    if collection == "product_skus":
        return first_non_empty(
            record.get("sku_key"),
            f"{coerce_str(record.get('product_id'))}:{first_non_empty(record.get('sku_id'), record.get('sku_name'))}",
        )
    if collection == "shops":
        return first_non_empty(record.get("shop_key"), record.get("shop_id"), record.get("shop_name"))
    if collection == "creators":
        return first_non_empty(
            record.get("creator_key"),
            record.get("creator_id"),
            record.get("uid"),
            record.get("unique_id"),
            record.get("relation_key"),
        )
    if collection == "creator_products":
        creator_key = first_non_empty(
            record.get("creator_key"),
            record.get("creator_id"),
            record.get("uid"),
            record.get("unique_id"),
        )
        product_id = coerce_str(record.get("product_id"))
        if creator_key or product_id:
            return f"{creator_key}:{product_id}"
        return first_non_empty(record.get("relation_key"))
    if collection == "creator_videos":
        creator_key = first_non_empty(
            record.get("creator_key"),
            record.get("creator_id"),
            record.get("uid"),
            record.get("unique_id"),
        )
        video_key = first_non_empty(record.get("video_key"), record.get("video_id"))
        if creator_key or video_key:
            return f"{creator_key}:{video_key}"
        return first_non_empty(record.get("relation_key"))
    if collection in {"videos", "video_products"}:
        return first_non_empty(record.get("video_key"), record.get("video_id"), record.get("relation_key"))
    if collection == "shop_creators":
        return first_non_empty(record.get("relation_key"))
    if collection == "media_assets":
        return first_non_empty(
            record.get("asset_key"),
            record.get("object_key"),
            record.get("source_url"),
            record.get("local_path"),
            record.get("file_token"),
        )
    if collection == "raw_api_responses":
        request_url = coerce_str(record.get("request_url"))
        endpoint = first_non_empty(record.get("source_endpoint"), record.get("source_platform"))
        if request_url or endpoint:
            return f"{endpoint}:{request_url}:{json_fingerprint(coerce_mapping(record.get('request_params')))}"
    if collection == "raw_entity_links":
        return first_non_empty(
            record.get("raw_link_id"),
            f"{coerce_str(record.get('raw_response_id'))}:{coerce_str(record.get('entity_type'))}:{coerce_str(record.get('entity_external_id'))}:{coerce_str(record.get('link_role'))}",
        )
    if collection == "product_metric_snapshots":
        return (
            f"{coerce_str(record.get('product_id'))}:"
            f"{coerce_str(record.get('source_platform'))}:"
            f"{coerce_str(record.get('source_endpoint'))}:"
            f"{coerce_str(record.get('window_days'))}"
        )
    if collection == "product_daily_metrics":
        return (
            f"{coerce_str(record.get('product_id'))}:"
            f"{coerce_str(record.get('metric_date'))}:"
            f"{coerce_str(record.get('source_platform'))}"
        )
    if collection == "product_distribution_snapshots":
        return (
            f"{coerce_str(record.get('product_id'))}:"
            f"{coerce_str(record.get('distribution_type'))}:"
            f"{coerce_str(record.get('source_key'))}:"
            f"{coerce_str(record.get('window_days'))}"
        )
    if collection == "product_sku_metric_snapshots":
        return (
            f"{coerce_str(record.get('product_id'))}:"
            f"{first_non_empty(record.get('sku_key'), record.get('sku_id'), record.get('sku_name'))}:"
            f"{coerce_str(record.get('window_days'))}"
        )
    return json_fingerprint(record)
