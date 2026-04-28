from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.fastmoss.contract_mapping import (
    _coerce_positive_int,
    _contract_entities_from_fact_bundle,
    _contract_entity_key,
    _contract_media_refs_from_fact_bundle,
    _contract_relations_from_fact_bundle,
    _iter_metric_values,
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
    map_fastmoss_video_goods,
    map_fastmoss_video_overview,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.rate_limit import resolve_api_request_delay_range

HANDLER_CODE = "fastmoss_video_fetch"
CONTRACT = API_HANDLER_CONTRACTS[HANDLER_CODE]


def fastmoss_video_fetch_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    video_identity = _normalize_video_identity(payload)
    detail_level = first_non_empty(payload.get("detail_level"), "video_metrics")
    required = coerce_bool(payload.get("required"), default=False)
    progress_callback = context.metadata.get("progress_callback")

    if callable(progress_callback):
        progress_callback("fastmoss_video_fetch", message="fastmoss video fetch started")

    try:
        fact_bundle_payload = coerce_mapping(payload.get("video_fact_bundle")) or coerce_mapping(
            payload.get("fact_bundle")
        )
        if fact_bundle_payload:
            raw_bundle: dict[str, Any] = {}
            fact_bundle = merge_fact_bundles(fact_bundle_payload)
        else:
            raw_bundle = _resolve_fastmoss_video_bundle(payload, video_identity=video_identity)
            if not raw_bundle:
                if required:
                    error = build_error(
                        error_type="source_missing",
                        error_code="fastmoss_video_payload_missing",
                        message="FastMoss video payload or live session configuration was not provided.",
                        retryable=False,
                        details={"video_identity": video_identity},
                    )
                    return failed_result(
                        context,
                        error=error,
                        summary={"detail_level": detail_level, "video_id": _video_business_key(video_identity)},
                    )
                empty_bundle = new_fact_bundle()
                return skipped_result(
                    context,
                    summary={"detail_level": detail_level, "video_id": _video_business_key(video_identity)},
                    result=_result_payload(
                        fact_bundle=empty_bundle,
                        raw_bundle={},
                        payload=payload,
                        video_identity=video_identity,
                    ),
                    warnings=("FastMoss video payload or live session configuration was not provided.",),
                )
            raw_bundle = _normalize_fastmoss_video_bundle(raw_bundle)
            fact_bundle = _build_fastmoss_video_fact_bundle(
                raw_bundle,
                video_identity=video_identity,
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
            summary={"detail_level": detail_level, "video_id": _video_business_key(video_identity)},
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
            summary={"detail_level": detail_level, "video_id": _video_business_key(video_identity)},
        )

    if callable(progress_callback):
        progress_callback("fastmoss_video_mapped", message="fastmoss video facts mapped")

    result = _result_payload(
        fact_bundle=fact_bundle,
        raw_bundle=raw_bundle,
        payload=payload,
        video_identity=video_identity,
    )
    summary = {
        "detail_level": detail_level,
        "video_id": _video_business_key(video_identity),
        "entity_count": sum(len(items) for items in result["entities"].values()),
        "relation_count": len(result["relations"]),
        "observation_count": len(result["observations"]),
        "media_ref_count": len(result["media_refs"]),
    }
    return success_result(context, summary=summary, result=result)


def _normalize_video_identity(payload: dict[str, Any]) -> dict[str, Any]:
    video_identity = coerce_mapping(payload.get("video_identity"))
    source_context = coerce_mapping(payload.get("source_context"))
    source_video = coerce_mapping(source_context.get("video"))
    video_id = first_non_empty(
        video_identity.get("video_id"),
        payload.get("video_id"),
        source_video.get("video_id"),
        source_context.get("video_id"),
    )
    return compact_dict({"video_id": video_id})


def _video_business_key(identity: Mapping[str, Any]) -> str:
    return first_non_empty(identity.get("video_id"))


def _resolve_fastmoss_video_bundle(
    payload: dict[str, Any],
    *,
    video_identity: Mapping[str, Any],
) -> dict[str, Any]:
    for key in ("fastmoss_video_bundle", "video_bundle", "mock_fastmoss_video_bundle"):
        candidate = coerce_mapping(payload.get(key))
        if candidate:
            return candidate

    endpoint_bundle: dict[str, Any] = {}
    for source_key, target_key in (
        ("overview", "overview"),
        ("video_overview", "overview"),
        ("goods", "goods"),
        ("goods_list", "goods"),
    ):
        candidate = coerce_mapping(payload.get(source_key))
        if candidate:
            endpoint_bundle[target_key] = candidate
    if endpoint_bundle:
        return endpoint_bundle

    fastmoss_settings = coerce_mapping(payload.get("fastmoss"))
    video_id = _video_business_key(video_identity)
    live_fetch = coerce_bool(fastmoss_settings.get("live_fetch"), default=bool(video_id and fastmoss_settings))
    if not live_fetch or not video_id:
        return {}

    with _build_fastmoss_session(fastmoss_settings) as session:
        _prepare_fastmoss_session(session, fastmoss_settings)
        return _fetch_live_fastmoss_video_bundle(session, video_id=video_id, payload=payload)


def _normalize_fastmoss_video_bundle(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw_bundle)
    for key in ("overview", "goods"):
        payload = coerce_mapping(raw_bundle.get(key))
        data = coerce_mapping(payload.get("data"))
        if data:
            normalized[key] = data
    return normalized


def _build_fastmoss_video_fact_bundle(
    raw_bundle: dict[str, Any],
    *,
    video_identity: Mapping[str, Any],
) -> dict[str, Any]:
    video_id = _video_business_key(video_identity)
    fact_bundle = new_fact_bundle()
    overview = coerce_mapping(raw_bundle.get("overview"))
    goods = coerce_mapping(raw_bundle.get("goods"))
    if overview:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_video_overview(overview, video_id=video_id))
    if goods:
        fact_bundle = merge_fact_bundles(fact_bundle, map_fastmoss_video_goods(goods, video_id=video_id))
    _append_fastmoss_video_raw_responses(fact_bundle, raw_bundle, video_identity=video_identity)
    return merge_fact_bundles(fact_bundle)


def _fetch_live_fastmoss_video_bundle(
    session: FastMossHTTPSession,
    *,
    video_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = _video_fetch_endpoints(payload)
    bundle: dict[str, Any] = {}
    if "overview" in endpoints:
        bundle["overview"] = session.get_video_overview(video_id)
    if "goods" in endpoints:
        goods_plan = coerce_mapping(fetch_plan.get("goods"))
        bundle["goods"] = session.list_video_goods(
            video_id,
            order=first_non_empty(goods_plan.get("order"), "1,2"),
        )
    bundle["session_snapshot"] = session.cookie_snapshot()
    return bundle


def _video_fetch_endpoints(payload: Mapping[str, Any]) -> set[str]:
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    endpoints = {
        first_non_empty(item)
        for item in fetch_plan.get("endpoints", [])
        if first_non_empty(item)
    } if isinstance(fetch_plan.get("endpoints"), list) else set()
    if endpoints:
        return endpoints
    detail_level = first_non_empty(payload.get("detail_level"), "default").lower()
    endpoints = {"overview"}
    if "goods" in detail_level or "product" in detail_level:
        endpoints.add("goods")
    return endpoints


def _build_fastmoss_session(settings: Mapping[str, Any]) -> FastMossHTTPSession:
    return FastMossHTTPSession(
        phone=first_non_empty(settings.get("phone")),
        password=first_non_empty(settings.get("password")),
        base_url=first_non_empty(settings.get("base_url"), "https://www.fastmoss.com"),
        default_region=first_non_empty(settings.get("region"), "US"),
        timeout=float(settings.get("timeout", 30.0) or 30.0),
        request_delay_range=resolve_api_request_delay_range(settings, provider="fastmoss"),
    )


def _prepare_fastmoss_session(session: FastMossHTTPSession, settings: Mapping[str, Any]) -> None:
    cookies = settings.get("browser_cookies")
    if isinstance(cookies, list):
        session.replace_browser_cookies(cookies)
    if coerce_bool(settings.get("ensure_logged_in"), default=bool(cookies or settings.get("phone"))):
        session.ensure_logged_in()


def _append_fastmoss_video_raw_responses(
    fact_bundle: dict[str, Any],
    raw_bundle: Mapping[str, Any],
    *,
    video_identity: Mapping[str, Any],
) -> None:
    endpoint_by_key = {
        "overview": "video.overview",
        "goods": "video.goods",
    }
    request_params = compact_dict({"video_id": _video_business_key(video_identity)})
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
    video_identity: Mapping[str, Any],
) -> dict[str, Any]:
    media_refs = _contract_media_refs_from_fact_bundle(fact_bundle)
    return {
        "entities": _contract_entities_from_fact_bundle(fact_bundle),
        "relations": _contract_relations_from_fact_bundle(fact_bundle),
        "observations": _build_fastmoss_video_observations(
            raw_bundle,
            payload=payload,
            video_identity=video_identity,
        ),
        "media_refs": media_refs,
        "raw_response_refs": _raw_response_refs_from_video_bundle(fact_bundle, video_identity=video_identity),
        "quality": _video_fetch_quality(fact_bundle, video_identity=video_identity),
        "video_fact_bundle": fact_bundle,
        "fact_bundle": fact_bundle,
    }


def _build_fastmoss_video_observations(
    raw_bundle: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    video_identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    entity_key = _contract_entity_key("video", _strip_key_prefix(_video_business_key(video_identity)))
    fetch_plan = coerce_mapping(payload.get("fetch_plan"))
    window_days = _coerce_positive_int(first_non_empty(fetch_plan.get("d_type"), fetch_plan.get("date_type")), default=0)
    observed_at = first_non_empty(payload.get("observed_at"), _utc_now_iso())
    observations: list[dict[str, Any]] = []
    for metric_name, metric_value in _iter_metric_values(coerce_mapping(raw_bundle.get("overview"))):
        observations.append(
            compact_dict(
                {
                    "entity_key": entity_key,
                    "metric_name": metric_name,
                    "metric_value": metric_value,
                    "window_days": window_days,
                    "observed_at": observed_at,
                    "source": "fastmoss",
                    "source_endpoint": "video.overview",
                }
            )
        )
    return observations


def _raw_response_refs_from_video_bundle(
    fact_bundle: dict[str, Any],
    *,
    video_identity: Mapping[str, Any],
) -> list[str]:
    refs: list[str] = []
    fallback_ref = _video_business_key(video_identity) or "unknown"
    for raw_response in fact_bundle.get("raw_api_responses", []):
        if not isinstance(raw_response, dict):
            continue
        endpoint = first_non_empty(raw_response.get("source_endpoint"))
        params = coerce_mapping(raw_response.get("request_params"))
        video_id = first_non_empty(params.get("video_id"), fallback_ref)
        if endpoint:
            refs.append(f"fastmoss://video/{video_id}/{endpoint}")
    return refs


def _video_fetch_quality(
    fact_bundle: dict[str, Any],
    *,
    video_identity: Mapping[str, Any],
) -> dict[str, Any]:
    videos = fact_bundle.get("videos")
    first_video = videos[0] if isinstance(videos, list) and videos and isinstance(videos[0], dict) else {}
    return {
        "video_resolved": bool(first_non_empty(first_video.get("video_id"), _video_business_key(video_identity))),
        "product_count": len(fact_bundle.get("products", [])) if isinstance(fact_bundle.get("products"), list) else 0,
    }


__all__ = ["CONTRACT", "HANDLER_CODE", "fastmoss_video_fetch_handler"]
