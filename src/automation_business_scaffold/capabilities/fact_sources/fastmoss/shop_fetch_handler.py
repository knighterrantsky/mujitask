from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.fastmoss.creator_fetch_handler import (
    _coerce_positive_int,
    _contract_entities_from_fact_bundle,
    _contract_entity_key,
    _contract_media_refs_from_fact_bundle,
    _contract_relations_from_fact_bundle,
    _iter_metric_values,
    _shop_entity_ref,
    _strip_key_prefix,
    _utc_now_iso,
)
from automation_business_scaffold.contracts.handler.allowlist import API_HANDLER_CONTRACTS
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    build_shop_key,
    coerce_bool,
    coerce_mapping,
    compact_dict,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    new_fact_bundle,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.fastmoss.fact_mappers import (
    map_fastmoss_shop_author,
    map_fastmoss_shop_base,
    map_fastmoss_shop_goods,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)

HANDLER_CODE = "fastmoss_shop_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def fastmoss_shop_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    shop_identity = _normalize_shop_identity(payload)
    detail_level = first_non_empty(payload.get("detail_level"), "shop_metrics")
    required = coerce_bool(payload.get("required"), default=False)
    progress_callback = context.metadata.get("progress_callback")

    if callable(progress_callback):
        progress_callback("fastmoss_shop_fetch", message="fastmoss shop fetch started")

    try:
        fact_bundle_payload = coerce_mapping(payload.get("shop_fact_bundle")) or coerce_mapping(
            payload.get("fact_bundle")
        )
        if fact_bundle_payload:
            raw_bundle: dict[str, Any] = {}
            fact_bundle = merge_fact_bundles(fact_bundle_payload)
        else:
            raw_bundle = _resolve_fastmoss_shop_bundle(payload, shop_identity=shop_identity)
            if not raw_bundle:
                if required:
                    error = build_error(
                        error_type="source_missing",
                        error_code="fastmoss_shop_payload_missing",
                        message="FastMoss shop payload or live session configuration was not provided.",
                        retryable=False,
                        details={"shop_identity": shop_identity},
                    )
                    return failed_result(
                        context,
                        error=error,
                        summary={"detail_level": detail_level, "shop_key": _shop_business_key(shop_identity)},
                    )
                empty_bundle = new_fact_bundle()
                return skipped_result(
                    context,
                    summary={"detail_level": detail_level, "shop_key": _shop_business_key(shop_identity)},
                    result=_result_payload(
                        fact_bundle=empty_bundle,
                        raw_bundle={},
                        payload=payload,
                        shop_identity=shop_identity,
                    ),
                    warnings=("FastMoss shop payload or live session configuration was not provided.",),
                )
            raw_bundle = _normalize_fastmoss_shop_bundle(raw_bundle)
            fact_bundle = _build_fastmoss_shop_fact_bundle(
                raw_bundle,
                shop_identity=shop_identity,
                payload=payload,
            )
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
            summary={"detail_level": detail_level, "shop_key": _shop_business_key(shop_identity)},
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
            summary={"detail_level": detail_level, "shop_key": _shop_business_key(shop_identity)},
        )

    if callable(progress_callback):
        progress_callback("fastmoss_shop_mapped", message="fastmoss shop facts mapped")

    result = _result_payload(
        fact_bundle=fact_bundle,
        raw_bundle=raw_bundle,
        payload=payload,
        shop_identity=shop_identity,
    )
    summary = {
        "detail_level": detail_level,
        "shop_key": _shop_business_key(shop_identity),
        "entity_count": sum(len(items) for items in result["entities"].values()),
        "relation_count": len(result["relations"]),
        "observation_count": len(result["observations"]),
        "media_ref_count": len(result["media_refs"]),
    }
    return success_result(context, summary=summary, result=result)


def _normalize_shop_identity(payload: dict[str, Any]) -> dict[str, Any]:
    shop_identity = coerce_mapping(payload.get("shop_identity"))
    source_context = coerce_mapping(payload.get("source_context"))
    source_shop = coerce_mapping(source_context.get("shop"))
    seller_id = first_non_empty(
        shop_identity.get("seller_id"),
        shop_identity.get("shop_id"),
        payload.get("seller_id"),
        payload.get("shop_id"),
        source_shop.get("seller_id"),
        source_shop.get("shop_id"),
        source_context.get("seller_id"),
        source_context.get("shop_id"),
    )
    shop_name = first_non_empty(
        shop_identity.get("shop_name"),
        payload.get("shop_name"),
        source_shop.get("shop_name"),
        source_shop.get("name"),
        source_context.get("shop_name"),
    )
    return compact_dict({"seller_id": seller_id, "shop_id": seller_id, "shop_name": shop_name})


def _shop_business_key(identity: Mapping[str, Any]) -> str:
    return build_shop_key(
        shop_id=first_non_empty(identity.get("shop_id"), identity.get("seller_id")),
        shop_name=first_non_empty(identity.get("shop_name")),
    )


def _resolve_fastmoss_shop_bundle(
    payload: dict[str, Any],
    *,
    shop_identity: Mapping[str, Any],
) -> dict[str, Any]:
    for key in ("fastmoss_shop_bundle", "shop_bundle", "mock_fastmoss_shop_bundle"):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate

    endpoint_bundle: dict[str, Any] = {}
    for source_key, target_key in (
        ("base", "base"),
        ("base_info", "base"),
        ("goods", "goods"),
        ("goods_list", "goods"),
        ("authors", "authors"),
        ("author_list", "authors"),
    ):
        candidate = coerce_mapping(payload.get(source_key))
        if candidate:
            endpoint_bundle[target_key] = candidate
    if endpoint_bundle:
        return endpoint_bundle

    fastmoss_settings = coerce_mapping(payload.get("fastmoss"))
    seller_id = first_non_empty(shop_identity.get("seller_id"), shop_identity.get("shop_id"))
    live_fetch = coerce_bool(fastmoss_settings.get("live_fetch"), default=bool(seller_id and fastmoss_settings))
    if not live_fetch or not seller_id:
        return {}

    with _build_fastmoss_session(fastmoss_settings) as session:
        _prepare_fastmoss_session(session, fastmoss_settings)
        return _fetch_live_fastmoss_shop_bundle(session, seller_id=seller_id, payload=payload)


def _normalize_fastmoss_shop_bundle(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw_bundle)
    for key in ("base", "goods", "authors"):
        payload = coerce_mapping(raw_bundle.get(key))
        data = coerce_mapping(payload.get("data"))
        if data:
            normalized[key] = data
    return normalized


def _build_fastmoss_shop_fact_bundle(
    raw_bundle: dict[str, Any],
    *,
    shop_identity: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    seller_id = first_non_empty(shop_identity.get("seller_id"), shop_identity.get("shop_id"))
    shop_name = first_non_empty(shop_identity.get("shop_name"))
    fact_bundle = new_fact_bundle()
    base = coerce_mapping(raw_bundle.get("base"))
    goods = coerce_mapping(raw_bundle.get("goods"))
    authors = coerce_mapping(raw_bundle.get("authors"))
    if base:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_shop_base(base, seller_id=seller_id))
    if goods:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_shop_goods(goods, seller_id=seller_id, shop_name=shop_name),
        )
    if authors:
        fact_bundle = merge_fact_bundles(
            fact_bundle,
            map_fastmoss_shop_author(authors, seller_id=seller_id, shop_name=shop_name),
        )
    _append_fastmoss_shop_raw_responses(fact_bundle, raw_bundle, shop_identity=shop_identity)
    return merge_fact_bundles(fact_bundle)


def _fetch_live_fastmoss_shop_bundle(
    session: FastMossHTTPSession,
    *,
    seller_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = _shop_fetch_endpoints(payload)
    d_type = first_non_empty(fetch_plan.get("d_type"), fetch_plan.get("date_type"), 28)
    bundle: dict[str, Any] = {}
    if "base" in endpoints:
        bundle["base"] = session.get_shop_base(seller_id)
    if "goods" in endpoints:
        goods_plan = coerce_mapping(fetch_plan.get("goods"))
        bundle["goods"] = session.list_shop_goods(
            seller_id,
            page=_coerce_positive_int(goods_plan.get("page"), default=1),
            page_size=_coerce_positive_int(goods_plan.get("page_size"), default=10),
            d_type=first_non_empty(goods_plan.get("d_type"), goods_plan.get("date_type"), d_type),
            order=first_non_empty(goods_plan.get("order"), "sold_count,2"),
        )
    if "authors" in endpoints:
        author_plan = coerce_mapping(fetch_plan.get("authors"))
        bundle["authors"] = session.list_shop_authors(
            seller_id,
            page=_coerce_positive_int(author_plan.get("page"), default=1),
            page_size=_coerce_positive_int(author_plan.get("page_size"), default=10),
            d_type=first_non_empty(author_plan.get("d_type"), author_plan.get("date_type"), d_type),
            author_product_type=first_non_empty(author_plan.get("author_product_type"), 3),
            order=first_non_empty(author_plan.get("order"), "sold_count,2"),
        )
    bundle["session_snapshot"] = session.cookie_snapshot()
    return bundle


def _shop_fetch_endpoints(payload: Mapping[str, Any]) -> set[str]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = {
        first_non_empty(item)
        for item in fetch_plan.get("endpoints", [])
        if first_non_empty(item)
    } if isinstance(fetch_plan.get("endpoints"), list) else set()
    if endpoints:
        return endpoints
    detail_level = first_non_empty(payload.get("detail_level"), "default").lower()
    endpoints = {"base", "goods"}
    if "author" in detail_level or "creator" in detail_level:
        endpoints.add("authors")
    return endpoints


def _build_fastmoss_session(settings: Mapping[str, Any]) -> FastMossHTTPSession:
    return FastMossHTTPSession(
        phone=first_non_empty(settings.get("phone")),
        password=first_non_empty(settings.get("password")),
        base_url=first_non_empty(settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(settings.get("region"), "US"),
        timeout=float(settings.get("timeout", 30.0) or 30.0),
    )


def _prepare_fastmoss_session(session: FastMossHTTPSession, settings: Mapping[str, Any]) -> None:
    cookies = settings.get("browser_cookies")
    if isinstance(cookies, list):
        session.replace_browser_cookies(cookies)
    if coerce_bool(settings.get("ensure_logged_in"), default=bool(cookies or settings.get("phone"))):
        session.ensure_logged_in()


def _append_fastmoss_shop_raw_responses(
    fact_bundle: dict[str, Any],
    raw_bundle: Mapping[str, Any],
    *,
    shop_identity: Mapping[str, Any],
) -> None:
    endpoint_by_key = {
        "base": "shop.base",
        "goods": "shop.goods",
        "authors": "shop.authors",
    }
    request_params = compact_dict(
        {
            "seller_id": first_non_empty(shop_identity.get("seller_id"), shop_identity.get("shop_id")),
            "shop_name": shop_identity.get("shop_name"),
        }
    )
    for key, endpoint in endpoint_by_key.items():
        response_payload = coerce_mapping(raw_bundle.get(key))
        if not response_payload:
            continue
        fact_bundle["raw_api_responses"].append(
            {
                "source_platform": "fastmoss",
                "source_endpoint": endpoint,
                "request_url": "",
                "request_params": request_params,
                "response_payload": response_payload,
                "status_code": 200,
            }
        )


def _result_payload(
    *,
    fact_bundle: dict[str, Any],
    raw_bundle: Mapping[str, Any],
    payload: Mapping[str, Any],
    shop_identity: Mapping[str, Any],
) -> dict[str, Any]:
    media_refs = _contract_media_refs_from_fact_bundle(fact_bundle)
    return {
        "entities": _contract_entities_from_fact_bundle(fact_bundle),
        "relations": _contract_relations_from_fact_bundle(fact_bundle),
        "observations": _build_fastmoss_shop_observations(
            raw_bundle,
            fact_bundle=fact_bundle,
            payload=payload,
            shop_identity=shop_identity,
        ),
        "media_refs": media_refs,
        "raw_response_refs": _raw_response_refs_from_shop_bundle(fact_bundle, shop_identity=shop_identity),
        "quality": _shop_fetch_quality(fact_bundle, shop_identity=shop_identity),
        "shop_fact_bundle": fact_bundle,
        "fact_bundle": fact_bundle,
    }


def _build_fastmoss_shop_observations(
    raw_bundle: Mapping[str, Any],
    *,
    fact_bundle: dict[str, Any],
    payload: Mapping[str, Any],
    shop_identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    shops = fact_bundle.get("shops")
    shop_ref = _shop_entity_ref(shops[0]) if isinstance(shops, list) and shops else _shop_business_key(shop_identity)
    entity_key = _contract_entity_key("shop", _strip_key_prefix(shop_ref))
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    window_days = _coerce_positive_int(first_non_empty(fetch_plan.get("d_type"), fetch_plan.get("date_type")), default=0)
    observed_at = first_non_empty(payload.get("observed_at"), _utc_now_iso())
    observations: list[dict[str, Any]] = []
    for metric_name, metric_value in _iter_metric_values(coerce_mapping(raw_bundle.get("base"))):
        observations.append(
            compact_dict(
                {
                    "entity_key": entity_key,
                    "metric_name": metric_name,
                    "metric_value": metric_value,
                    "window_days": window_days,
                    "observed_at": observed_at,
                    "source": "fastmoss",
                    "source_endpoint": "shop.base",
                }
            )
        )
    return observations


def _raw_response_refs_from_shop_bundle(
    fact_bundle: dict[str, Any],
    *,
    shop_identity: Mapping[str, Any],
) -> list[str]:
    refs: list[str] = []
    fallback_ref = _strip_key_prefix(_shop_business_key(shop_identity)) or "unknown"
    for raw_response in fact_bundle.get("raw_api_responses", []):
        if not isinstance(raw_response, dict):
            continue
        endpoint = first_non_empty(raw_response.get("source_endpoint"))
        params = coerce_mapping(raw_response.get("request_params"))
        shop_ref = first_non_empty(params.get("seller_id"), params.get("shop_id"), params.get("shop_name"), fallback_ref)
        if endpoint:
            refs.append(f"fastmoss://shop/{shop_ref}/{endpoint}")
    return refs


def _shop_fetch_quality(
    fact_bundle: dict[str, Any],
    *,
    shop_identity: Mapping[str, Any],
) -> dict[str, Any]:
    shops = fact_bundle.get("shops")
    first_shop = shops[0] if isinstance(shops, list) and shops and isinstance(shops[0], dict) else {}
    return {
        "shop_resolved": bool(_shop_entity_ref(first_shop) or _shop_business_key(shop_identity)),
        "product_count": len(fact_bundle.get("products", [])) if isinstance(fact_bundle.get("products"), list) else 0,
        "creator_count": len(fact_bundle.get("creators", [])) if isinstance(fact_bundle.get("creators"), list) else 0,
    }


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_shop_fetch_handler"]
