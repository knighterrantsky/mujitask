from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.fastmoss.mappers.fact_bundle_mapper import (
    map_fastmoss_goods_video,
)
from automation_business_scaffold.capabilities.fact_sources.fastmoss.security import (
    build_fastmoss_session,
    fastmoss_security_fallback_required_result,
    fastmoss_settings_from_payload,
    is_fastmoss_security_verification_error,
    is_fastmoss_session_conflict_error,
    prepare_fastmoss_session,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_creator_key,
    build_error,
    coerce_mapping,
    failed_result,
    first_non_empty,
    merge_fact_bundles,
    success_result,
)
from automation_business_scaffold.domains.tiktok.policies.influencer_monitor_candidate_policy import (
    normalize_min_video_sales_28d,
    select_product_video_creator_candidates,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)


HANDLER_CODE = "product_video_creator_discovery"
PRODUCT_VIDEO_PAGE_SIZE = 5
PRODUCT_VIDEO_ORDER = "1,2"
PRODUCT_VIDEO_D_TYPE = 0
PRODUCT_VIDEO_DATE_TYPE = 28
PRODUCT_VIDEO_IS_PROMOTED = -1

fact_bundle_upsert_handler = api_handler_callable("fact_bundle_upsert")


def product_video_creator_discovery_handler(
    context: HandlerContext,
) -> HandlerResult:
    payload = dict(context.payload)
    product_id = first_non_empty(
        payload.get("product_id"),
        coerce_mapping(payload.get("product_identity")).get("product_id"),
    )
    try:
        threshold = normalize_min_video_sales_28d(
            payload.get("min_video_sales_28d")
        )
    except ValueError as exc:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="invalid_min_video_sales_28d",
                message=str(exc),
                retryable=False,
            ),
            summary={"product_id": product_id, "fetch_status": "failed"},
        )
    if not product_id:
        return failed_result(
            context,
            error=build_error(
                error_type="invalid_input",
                error_code="monitor_product_id_missing",
                message="product_video_creator_discovery requires product_id.",
                retryable=False,
            ),
            summary={"fetch_status": "failed"},
        )

    try:
        rows, pagination = _resolve_product_video_rows(
            payload,
            product_id=product_id,
            threshold=threshold,
        )
    except FastMossAuthError as exc:
        return fastmoss_security_fallback_required_result(
            context,
            exc=exc,
            handler_payload=payload,
            fastmoss_settings=fastmoss_settings_from_payload(payload),
            operation=HANDLER_CODE,
            entity_identity={"product_id": product_id},
            fallback_reason="fastmoss_auth_session_recovery",
            error_type="auth_failure",
            error_code="fastmoss_auth_session_recovery_required",
        )
    except FastMossHTTPError as exc:
        if is_fastmoss_session_conflict_error(
            exc
        ) or is_fastmoss_security_verification_error(exc):
            return fastmoss_security_fallback_required_result(
                context,
                exc=exc,
                handler_payload=payload,
                fastmoss_settings=fastmoss_settings_from_payload(payload),
                operation=HANDLER_CODE,
                entity_identity={"product_id": product_id},
            )
        return failed_result(
            context,
            error=build_error(
                error_type="transport_failure",
                error_code="fastmoss_http_failure",
                message=str(exc),
                retryable=True,
                details=exc.to_dict(),
            ),
            summary={"product_id": product_id, "fetch_status": "failed"},
            result={"product_id": product_id, "fetch_status": "failed"},
        )

    selected = select_product_video_creator_candidates(
        rows,
        product_id=product_id,
        min_video_sales_28d=threshold,
    )
    fact_bundle = _product_video_fact_bundle(
        context=context,
        product_id=product_id,
        rows=rows,
        selected=selected,
    )
    fact_result = fact_bundle_upsert_handler(
        _child_context(
            context,
            handler_code="fact_bundle_upsert",
            step_code="product_video_fact_upsert",
            payload={
                "request_payload": coerce_mapping(payload.get("request_payload")),
                "request_id": context.request_id,
                "task_code": payload.get("task_code"),
                "workflow_code": payload.get("workflow_code"),
                "stage_code": payload.get("stage_code"),
                "source_job_ids": [context.job_id],
                "source_context": {"product_id": product_id},
                "idempotency_context": {
                    "product_id": product_id,
                    "window_days": 28,
                },
                "fact_bundle": fact_bundle,
                "requires_fact_db": True,
                "require_database_persistence": True,
            },
        )
    )
    if fact_result.status == "failed":
        return failed_result(
            context,
            error=fact_result.error
            or build_error(
                error_type="persistence_failure",
                error_code="monitor_product_fact_upsert_failed",
                message="Product-video facts could not be persisted.",
                retryable=True,
            ),
            summary={"product_id": product_id, "fetch_status": "failed"},
            result={
                "product_id": product_id,
                "fetch_status": "failed",
                "fetched_video_count": selected["fetched_video_count"],
            },
        )

    candidates = [
        {
            **candidate,
            "source_record_ids": list(payload.get("source_record_ids") or []),
            "source_product_images": list(
                payload.get("source_product_images") or []
            ),
            "holidays": list(payload.get("holidays") or []),
        }
        for candidate in selected["candidates"]
    ]
    fetch_status = "success" if candidates else "empty"
    fact_summary = {
        "persistence_mode": fact_result.result.get("persistence_mode"),
        "persisted_counts": dict(
            coerce_mapping(fact_result.result.get("persisted_counts"))
        ),
        "observation_ref_count": len(
            list(fact_result.result.get("observation_refs") or [])
        ),
    }
    result = {
        "product_id": product_id,
        "fetch_status": fetch_status,
        "min_video_sales_28d": threshold,
        "fetched_video_count": selected["fetched_video_count"],
        "deduped_video_count": selected["deduped_video_count"],
        "qualified_video_count": selected["qualified_video_count"],
        "qualified_creator_count": len(candidates),
        "invalid_identity_count": selected["invalid_identity_count"],
        "invalid_sales_count": selected["invalid_sales_count"],
        "candidates": candidates,
        "pagination": pagination,
        "fact_summary": fact_summary,
        "query": _query_contract(),
    }
    return success_result(
        context,
        summary={
            "product_id": product_id,
            "fetch_status": fetch_status,
            "fetched_video_count": selected["fetched_video_count"],
            "qualified_video_count": selected["qualified_video_count"],
            "qualified_creator_count": len(candidates),
            "early_stopped": bool(pagination.get("early_stopped")),
        },
        result=result,
        warnings=tuple(fact_result.warnings),
    )


def _resolve_product_video_rows(
    payload: Mapping[str, Any],
    *,
    product_id: str,
    threshold: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mock_pages = payload.get("mock_fastmoss_product_video_pages")
    if isinstance(mock_pages, list):
        return _collect_pages(
            mock_pages,
            threshold=threshold,
            page_size=PRODUCT_VIDEO_PAGE_SIZE,
        )
    mock_rows = payload.get("mock_fastmoss_product_videos")
    if isinstance(mock_rows, list):
        return _collect_pages(
            [{"data": {"list": mock_rows, "total": len(mock_rows)}}],
            threshold=threshold,
            page_size=PRODUCT_VIDEO_PAGE_SIZE,
        )

    settings = fastmoss_settings_from_payload(payload)
    if not bool(settings.get("live_fetch") or settings.get("_has_live_config")):
        raise FastMossAuthError(
            "FastMoss live fetch config is missing for product-video creator discovery."
        )
    max_pages = _positive_int(
        first_non_empty(
            payload.get("fastmoss_video_max_pages"),
            settings.get("video_max_pages"),
        )
    )
    pages: list[dict[str, Any]] = []
    with build_fastmoss_session(
        settings,
        session_factory=FastMossHTTPSession,
    ) as session:
        prepare_fastmoss_session(session, settings=settings)
        page = 1
        while True:
            response = session.list_product_videos(
                product_id,
                page=page,
                pagesize=PRODUCT_VIDEO_PAGE_SIZE,
                order=PRODUCT_VIDEO_ORDER,
                d_type=PRODUCT_VIDEO_D_TYPE,
                date_type=PRODUCT_VIDEO_DATE_TYPE,
                is_promoted=PRODUCT_VIDEO_IS_PROMOTED,
            )
            pages.append(response)
            page_rows = _page_rows(response)
            _, incremental_pagination = _collect_pages(
                pages,
                threshold=threshold,
                page_size=PRODUCT_VIDEO_PAGE_SIZE,
            )
            if incremental_pagination["early_stopped"]:
                break
            if not page_rows or len(page_rows) < PRODUCT_VIDEO_PAGE_SIZE:
                break
            total = _page_total(response)
            if total > 0 and page * PRODUCT_VIDEO_PAGE_SIZE >= total:
                break
            page += 1
            if max_pages > 0 and page > max_pages:
                break
    return _collect_pages(
        pages,
        threshold=threshold,
        page_size=PRODUCT_VIDEO_PAGE_SIZE,
    )


def _collect_pages(
    pages: list[Any],
    *,
    threshold: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_sales: float | None = None
    monotonic = True
    early_stopped = False
    stop_reason = "source_exhausted"
    pages_fetched = 0

    for page_payload in pages:
        page_rows = _page_rows(page_payload)
        pages_fetched += 1
        boundary_seen = False
        for row in page_rows:
            sales = _sales_number(row.get("sold_count"))
            if sales is not None:
                if previous_sales is not None and sales > previous_sales:
                    monotonic = False
                previous_sales = sales
                if sales <= threshold:
                    boundary_seen = True
            rows.append(dict(row))
        if monotonic and boundary_seen:
            early_stopped = True
            stop_reason = "sorted_sales_threshold_reached"
            break
        if not page_rows or len(page_rows) < page_size:
            break
        total = _page_total(page_payload)
        if total > 0 and len(rows) >= total:
            break
    if not monotonic:
        stop_reason = "source_exhausted_non_monotonic"
    return rows, {
        "page_size": page_size,
        "pages_fetched": pages_fetched,
        "early_stopped": early_stopped,
        "monotonic_sales_prefix": monotonic,
        "stop_reason": stop_reason,
    }


def _product_video_fact_bundle(
    *,
    context: HandlerContext,
    product_id: str,
    rows: list[dict[str, Any]],
    selected: Mapping[str, Any],
) -> dict[str, Any]:
    mapped = map_fastmoss_goods_video(
        {"data": {"product_id": product_id, "list": rows}},
        product_id=product_id,
    )
    mapped["media_assets"] = []
    video_performance = []
    for row in selected.get("valid_sales_rows") or []:
        video_id = first_non_empty(row.get("video_id"))
        creator_id = first_non_empty(row.get("creator_id"))
        if not video_id or not creator_id:
            continue
        video_performance.append(
            {
                "performance_id": _performance_id(
                    context.request_id,
                    "video_product",
                    product_id,
                    video_id,
                ),
                "video_key": f"video:{video_id}",
                "video_id": video_id,
                "product_id": product_id,
                "creator_key": build_creator_key(
                    creator_id=creator_id,
                    uid=creator_id,
                ),
                "source_platform": "fastmoss",
                "window_days": 28,
                "sold_count": row.get("video_product_sales_28d"),
                "payload": {
                    "metric_name": "sold_count",
                    "source_endpoint": "goods.v3.video",
                },
            }
        )
    creator_performance = []
    for candidate in selected.get("candidates") or []:
        creator_id = first_non_empty(candidate.get("creator_id"))
        if not creator_id:
            continue
        creator_performance.append(
            {
                "performance_id": _performance_id(
                    context.request_id,
                    "creator_product",
                    product_id,
                    creator_id,
                ),
                "creator_key": build_creator_key(
                    creator_id=creator_id,
                    uid=creator_id,
                ),
                "product_id": product_id,
                "source_platform": "fastmoss",
                "window_days": 28,
                "sold_count": candidate.get("video_product_sales_28d"),
                "payload": {
                    "metric_name": "sold_count",
                    "aggregation": "max_qualified_video",
                    "winning_video_id": candidate.get("winning_video_id"),
                },
            }
        )
    return merge_fact_bundles(
        mapped,
        {
            "video_product_window_performance": video_performance,
            "creator_product_window_performance": creator_performance,
        },
    )


def _query_contract() -> dict[str, Any]:
    return {
        "pagesize": PRODUCT_VIDEO_PAGE_SIZE,
        "order": PRODUCT_VIDEO_ORDER,
        "d_type": PRODUCT_VIDEO_D_TYPE,
        "date_type": PRODUCT_VIDEO_DATE_TYPE,
        "is_promoted": PRODUCT_VIDEO_IS_PROMOTED,
    }


def _page_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if isinstance(data, Mapping):
        rows = data.get("list")
        if isinstance(rows, list):
            return [dict(item) for item in rows if isinstance(item, Mapping)]
    rows = payload.get("list")
    if isinstance(rows, list):
        return [dict(item) for item in rows if isinstance(item, Mapping)]
    return []


def _page_total(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    data = payload.get("data")
    value = data.get("total") if isinstance(data, Mapping) else payload.get("total")
    return _positive_int(value)


def _positive_int(value: Any) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(normalized, 0)


def _sales_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _performance_id(*parts: Any) -> str:
    normalized = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _child_context(
    context: HandlerContext,
    *,
    handler_code: str,
    payload: dict[str, Any],
    step_code: str,
) -> HandlerContext:
    return HandlerContext(
        request_id=context.request_id,
        job_id=context.job_id,
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code=context.workflow_code,
        stage_code=context.stage_code,
        job_code=handler_code,
        item_code=step_code,
        business_key=context.business_key,
        dedupe_key=f"{context.dedupe_key}:{step_code}",
        resource_code=context.resource_code,
        worker_id=context.worker_id,
        attempt_count=context.attempt_count,
        max_attempts=context.max_attempts,
        metadata=dict(context.metadata),
    )


__all__ = [
    "HANDLER_CODE",
    "PRODUCT_VIDEO_DATE_TYPE",
    "PRODUCT_VIDEO_D_TYPE",
    "PRODUCT_VIDEO_IS_PROMOTED",
    "PRODUCT_VIDEO_ORDER",
    "PRODUCT_VIDEO_PAGE_SIZE",
    "product_video_creator_discovery_handler",
]
