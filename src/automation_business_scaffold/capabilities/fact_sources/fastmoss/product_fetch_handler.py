from __future__ import annotations

import re
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    bundle_entity_keys,
    coerce_bool,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    compact_dict,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    new_fact_bundle,
    normalize_product_identity,
    product_business_key,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.fastmoss.fact_mappers import (
    extract_fastmoss_data,
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
from collections.abc import Mapping
from typing import Any

HANDLER_CODE = "fastmoss_product_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


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
            raw_bundle = _resolve_fastmoss_bundle(payload, product_id=product_id, detail_level=detail_level)
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
            related_creators = _extract_related_creators(
                fact_bundle,
                source_context=coerce_mapping(payload.get("source_context")),
                relation_policy=coerce_mapping(payload.get("relation_policy")),
            )
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


def _number_at_least(value: Any, threshold: Any) -> bool:
    value_number = _parse_number(value)
    threshold_number = _parse_number(threshold)
    if value_number is None or threshold_number is None:
        return False
    return float(value_number) >= float(threshold_number)


def _resolve_fastmoss_bundle(payload: dict[str, Any], *, product_id: str, detail_level: str = "") -> dict[str, Any]:
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
        bundle = {
            "base": session.get_product_base(product_id),
            "overview": session.get_product_overview(product_id, d_type=d_type),
            "skus": session.get_product_skus(product_id, d_type=d_type),
            "sku_distribution": session.get_product_sku_distribution(product_id, d_type=d_type),
            "session_snapshot": session.cookie_snapshot(),
        }
        if _product_fetch_includes_related_creators(payload, detail_level=detail_level):
            author_plan = coerce_mapping(payload.get("author_list_plan")) or coerce_mapping(
                fastmoss_settings.get("author_list")
            )
            bundle["related_creators"] = session.list_product_authors(
                product_id,
                page=_coerce_positive_int(author_plan.get("page"), default=1),
                pagesize=_coerce_positive_int(author_plan.get("page_size") or author_plan.get("pagesize"), default=10),
                order=first_non_empty(author_plan.get("order"), "2,2"),
                ecommerce_type=first_non_empty(author_plan.get("ecommerce_type"), "all"),
            )
        return bundle


def _product_fetch_includes_related_creators(payload: Mapping[str, Any], *, detail_level: str) -> bool:
    normalized = first_non_empty(detail_level, payload.get("detail_level")).lower()
    return any(token in normalized for token in ("related_creator", "author", "creator"))


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
        overview_data = extract_fastmoss_data(overview)
        fact_bundle["product_metric_snapshots"].extend(
            _build_fastmoss_product_metric_snapshots(overview_data, product_id=product_id)
        )
        fact_bundle["product_daily_metrics"].extend(_build_fastmoss_daily_metrics(overview_data, product_id=product_id))
        fact_bundle["product_distribution_snapshots"].extend(
            _build_fastmoss_distribution_snapshots(overview_data, product_id=product_id)
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
            _build_fastmoss_sku_metric_snapshots(
                extract_fastmoss_data(skus),
                extract_fastmoss_data(coerce_mapping(raw_bundle.get("sku_distribution"))),
                product_id=product_id,
            )
        )
    if related_creators:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_author(related_creators, product_id=product_id))
    if videos:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_goods_video(videos, product_id=product_id))

    return merge_fact_bundles(fact_bundle)


def _extract_related_creators(
    fact_bundle: dict[str, Any],
    *,
    source_context: Mapping[str, Any] | None = None,
    relation_policy: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    source_context_payload = coerce_mapping(source_context)
    relation_policy_payload = coerce_mapping(relation_policy)
    relation_by_creator = _creator_product_relation_index(fact_bundle)
    creators = []
    for creator in coerce_mapping_list(fact_bundle.get("creators")):
        creator_key = first_non_empty(
            creator.get("creator_key"),
            creator.get("creator_id"),
            creator.get("uid"),
            creator.get("unique_id"),
        )
        relation = relation_by_creator.get(creator_key, {})
        raw = coerce_mapping(coerce_mapping(relation.get("metadata")).get("raw"))
        facts = coerce_mapping(creator.get("facts"))
        metrics = _metric_fields(
            raw,
            coerce_mapping(facts.get("raw")),
            coerce_mapping(facts.get("base_info")),
            coerce_mapping(facts.get("author_index")),
        )
        matched_conditions = _creator_candidate_matched_conditions(
            metrics,
            relation_policy=relation_policy_payload,
        )
        if matched_conditions and not all(matched_conditions.values()):
            continue
        uid = first_non_empty(creator.get("uid"), raw.get("uid"), raw.get("author_uid"))
        unique_id = first_non_empty(creator.get("unique_id"), raw.get("unique_id"), raw.get("author_unique_id"))
        creator_id = first_non_empty(creator.get("creator_id"), unique_id, uid)
        creators.append(
            compact_dict(
                {
                    "creator_key": creator_key,
                    "creator_id": creator_id,
                    "creator_identity": compact_dict(
                        {
                            "creator_id": creator_id,
                            "uid": uid,
                            "unique_id": unique_id,
                            "profile_url": _fastmoss_creator_profile_url(uid, unique_id),
                        }
                    ),
                    "uid": uid,
                    "unique_id": unique_id,
                    "nickname": creator.get("nickname"),
                    "display_name": creator.get("nickname"),
                    "metrics": metrics,
                    "matched_conditions": matched_conditions,
                    "source_context": {
                        **source_context_payload,
                        "matched_product_sold_count": first_non_empty(metrics.get("sold_count")),
                    },
                }
            )
        )
    return creators


def _creator_product_relation_index(fact_bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    relations = coerce_mapping(fact_bundle.get("relations"))
    for relation in coerce_mapping_list(relations.get("creator_products")):
        creator_key = first_non_empty(
            relation.get("creator_key"),
            relation.get("creator_id"),
            relation.get("uid"),
            relation.get("unique_id"),
        )
        if creator_key and creator_key not in index:
            index[creator_key] = relation
    return index


def _creator_candidate_matched_conditions(
    metrics: Mapping[str, Any],
    *,
    relation_policy: Mapping[str, Any],
) -> dict[str, bool]:
    conditions: dict[str, bool] = {}
    sold_threshold = relation_policy.get("creator_sold_count_min")
    follower_threshold = relation_policy.get("creator_follower_count_min")
    if sold_threshold not in (None, ""):
        conditions["creator_sold_count_min"] = _number_at_least(metrics.get("sold_count"), sold_threshold)
    if follower_threshold not in (None, ""):
        conditions["creator_follower_count_min"] = _number_at_least(metrics.get("follower_count"), follower_threshold)
    return conditions


def _fastmoss_creator_profile_url(uid: Any, unique_id: Any = "") -> str:
    ref = first_non_empty(uid, unique_id)
    return f"https://www.fastmoss.com/zh/influencer/detail/{ref}" if ref else ""


def _build_fastmoss_metrics_snapshot(raw_bundle: dict[str, Any], *, product_id: str) -> dict[str, Any]:
    overview = extract_fastmoss_data(coerce_mapping(raw_bundle.get("overview")))
    return compact_dict(
        {
            "product_id": product_id,
            "window_days": overview.get("d_type"),
            "overview": coerce_mapping(overview.get("overview")),
            "chart_points": len(coerce_mapping_list(overview.get("chart_list"))),
            "session_snapshot": coerce_mapping(raw_bundle.get("session_snapshot")),
        }
    )


def _metric_fields(*payloads: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for payload in payloads:
        for metric_name, metric_value in _iter_metric_values(payload):
            metrics[metric_name] = metric_value
    return metrics


def _iter_metric_values(payload: Mapping[str, Any], *, prefix: str = "") -> list[tuple[str, Any]]:
    metrics: list[tuple[str, Any]] = []
    ignored_keys = {
        "id",
        "uid",
        "unique_id",
        "creator_id",
        "product_id",
        "video_id",
        "shop_id",
        "seller_id",
        "nickname",
        "name",
        "avatar",
        "avatar_url",
        "region",
        "country_region",
        "update_at",
    }
    for key, value in payload.items():
        key_text = coerce_str(key)
        if not key_text or key_text in ignored_keys:
            continue
        metric_name = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, Mapping):
            metrics.extend(_iter_metric_values(value, prefix=metric_name))
            continue
        metric_value = _metric_number(value)
        if metric_value is not None:
            metrics.append((metric_name, metric_value))
    return metrics


def _metric_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = coerce_str(value)
    if not text:
        return None
    normalized = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_product_fetch_handler"]
