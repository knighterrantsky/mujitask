from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import csv
import json
from pathlib import Path
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.fastmoss.security import (
    build_fastmoss_session,
    fastmoss_security_fallback_required_result,
    fastmoss_settings_from_payload,
    is_fastmoss_security_verification_error,
    is_fastmoss_session_conflict_error,
    prepare_fastmoss_session,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_mapping_list,
    coerce_str,
    failed_result,
    first_non_empty,
    success_result,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
    _extract_data,
    _extract_list,
)


def product_video_outreach_check_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    product_id = first_non_empty(payload.get("product_id"), coerce_mapping(payload.get("product_identity")).get("product_id"))
    rows = coerce_mapping_list(payload.get("rows"))
    query_window = coerce_mapping(payload.get("query_window")) or {"mode": "d_type", "d_type": 90}
    trigger_date = first_non_empty(payload.get("trigger_date"), date.today().isoformat())

    try:
        videos = _resolve_product_videos(payload, product_id=product_id, query_window=query_window)
    except FastMossAuthError as exc:
        fastmoss_settings = fastmoss_settings_from_payload(payload)
        result = fastmoss_security_fallback_required_result(
            context,
            exc=exc,
            handler_payload=payload,
            fastmoss_settings=fastmoss_settings,
            operation="product_video_outreach_check",
            entity_identity={"product_id": product_id},
            fallback_reason="fastmoss_auth_session_recovery",
            error_type="auth_failure",
            error_code="fastmoss_auth_session_recovery_required",
        )
        return _attach_partial_video_page_state(result, exc)
    except FastMossHTTPError as exc:
        if is_fastmoss_session_conflict_error(exc):
            return fastmoss_security_fallback_required_result(
                context,
                exc=exc,
                handler_payload=payload,
                fastmoss_settings=fastmoss_settings_from_payload(payload),
                operation="product_video_outreach_check",
                entity_identity={"product_id": product_id},
                fallback_reason="fastmoss_auth_session_recovery",
                error_type="auth_failure",
                error_code="fastmoss_auth_session_recovery_required",
            )
        if is_fastmoss_security_verification_error(exc):
            result = fastmoss_security_fallback_required_result(
                context,
                exc=exc,
                handler_payload=payload,
                fastmoss_settings=fastmoss_settings_from_payload(payload),
                operation="product_video_outreach_check",
                entity_identity={"product_id": product_id},
            )
            return _attach_partial_video_page_state(result, exc)
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
            result={"product_id": product_id, "fetch_status": "failed", "error": exc.to_dict()},
        )

    result = match_outreach_rows_to_videos(
        product_id=product_id,
        rows=rows,
        videos=videos,
        query_window=query_window,
        trigger_date=trigger_date,
    )
    audit = persist_product_video_audit(context=context, product_id=product_id, videos=videos)
    result["video_audit"] = audit
    result["summary"].update(
        {
            "video_audit_ref": audit.get("json_path", ""),
            "unique_creator_count": audit.get("unique_creator_count", 0),
        }
    )
    return success_result(context, summary=result["summary"], result=result)


def _resolve_product_videos(payload: Mapping[str, Any], *, product_id: str, query_window: Mapping[str, Any]) -> list[dict[str, Any]]:
    mock_rows = coerce_mapping_list(payload.get("mock_fastmoss_product_videos"))
    if mock_rows:
        return normalize_product_video_rows(mock_rows)
    fastmoss_settings = fastmoss_settings_from_payload(payload)
    live_fetch = bool(fastmoss_settings.get("live_fetch") or fastmoss_settings.get("_has_live_config"))
    if not product_id:
        return []
    if not live_fetch:
        raise FastMossAuthError("FastMoss live fetch config is missing for product video outreach check.")
    page_size = _positive_int(first_non_empty(payload.get("fastmoss_video_page_size"), fastmoss_settings.get("video_page_size")), default=5)
    max_pages = _optional_positive_int(first_non_empty(payload.get("fastmoss_video_max_pages"), fastmoss_settings.get("video_max_pages")))
    fastmoss_settings.setdefault("fastmoss_api_request_delay_min_seconds", 1.0)
    fastmoss_settings.setdefault("fastmoss_api_request_delay_max_seconds", 3.0)
    with build_fastmoss_session(fastmoss_settings, session_factory=FastMossHTTPSession) as session:
        prepare_fastmoss_session(session, settings=fastmoss_settings)
        kwargs = _query_window_kwargs(query_window)
        start_page = _positive_int(payload.get("fastmoss_video_start_page"), default=1)
        carried_rows = coerce_mapping_list(payload.get("fastmoss_video_carried_rows"))
        return normalize_product_video_rows(
            [*carried_rows, *_fetch_product_video_pages(session, product_id=product_id, page_size=page_size, max_pages=max_pages, start_page=start_page, **kwargs)]
        )


def _fetch_product_video_pages(
    session: FastMossHTTPSession,
    *,
    product_id: str,
    page_size: int,
    max_pages: int | None,
    start_page: int,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    page = max(1, int(start_page or 1))
    seen_rows = 0
    rows: list[dict[str, Any]] = []
    while True:
        try:
            payload = session.list_product_videos(product_id, page=page, pagesize=page_size, **kwargs)
        except FastMossHTTPError as exc:
            carried = list(rows)
            if isinstance(exc.payload, dict):
                exc.payload.setdefault("partial_rows", carried)
                exc.payload.setdefault("failed_page", page)
            else:
                exc.payload = {"partial_rows": carried, "failed_page": page}
            raise
        data = _extract_data(payload)
        page_rows = _extract_list(payload)
        if not page_rows:
            break
        rows.extend(page_rows)
        seen_rows += len(page_rows)
        total = data.get("total")
        if isinstance(total, int) and total > 0 and seen_rows >= total:
            break
        if len(page_rows) < page_size:
            break
        page += 1
        if max_pages is not None and page > max_pages:
            break
    return rows


def match_outreach_rows_to_videos(
    *,
    product_id: str,
    rows: list[Mapping[str, Any]],
    videos: list[Mapping[str, Any]],
    query_window: Mapping[str, Any],
    trigger_date: str,
) -> dict[str, Any]:
    normalized_product_id = coerce_str(product_id)
    videos_by_creator: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        item = dict(video)
        if coerce_str(item.get("product_id")) != normalized_product_id:
            continue
        creator_unique_id = coerce_str(item.get("creator_unique_id"))
        video_id = coerce_str(item.get("video_id"))
        if not creator_unique_id or not video_id:
            continue
        videos_by_creator.setdefault(creator_unique_id, []).append(item)

    matched_rows: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []
    for row in rows:
        source_record_id = coerce_str(row.get("source_record_id"))
        creator_unique_id = coerce_str(row.get("creator_unique_id"))
        candidates = sorted(videos_by_creator.get(creator_unique_id, []), key=lambda item: coerce_str(item.get("published_date")))
        if candidates:
            selected = candidates[0]
            video_url = canonical_tiktok_video_url(creator_unique_id, selected.get("video_id"))
            matched_rows.append(
                {
                    "source_record_id": source_record_id,
                    "product_id": normalized_product_id,
                    "creator_unique_id": creator_unique_id,
                    "video_id": coerce_str(selected.get("video_id")),
                    "video_url": video_url,
                    "published_date": coerce_str(selected.get("published_date")),
                    "checked_at": trigger_date,
                    "match_status": "matched",
                    "writeback_context": coerce_mapping(row.get("writeback_context")),
                }
            )
        else:
            unmatched_rows.append(
                {
                    "source_record_id": source_record_id,
                    "product_id": normalized_product_id,
                    "creator_unique_id": creator_unique_id,
                    "checked_at": trigger_date,
                    "match_status": "unmatched",
                    "writeback_context": coerce_mapping(row.get("writeback_context")),
                }
            )
    summary = {
        "product_id": normalized_product_id,
        "fetch_status": "success",
        "fetched_video_count": len(videos),
        "matched_row_count": len(matched_rows),
        "unmatched_row_count": len(unmatched_rows),
    }
    return {
        "product_id": normalized_product_id,
        "fetch_status": "success",
        "query_window": dict(query_window),
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "summary": summary,
    }


def normalize_product_video_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        author = coerce_mapping(row.get("author"))
        creator_unique_id = first_non_empty(row.get("unique_id"), author.get("unique_id"))
        video_id = first_non_empty(row.get("video_id"), row.get("id"))
        normalized.append(
            {
                "product_id": first_non_empty(row.get("product_id"), row.get("goods_id")),
                "creator_unique_id": creator_unique_id,
                "video_id": video_id,
                "published_date": first_non_empty(row.get("create_date"), row.get("published_date")),
                "video_url": canonical_tiktok_video_url(creator_unique_id, video_id),
            }
        )
    return normalized


def persist_product_video_audit(*, context: HandlerContext, product_id: str, videos: list[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [_audit_video_row(video) for video in videos]
    creator_ids = sorted({row["creator_unique_id"] for row in normalized if row["creator_unique_id"]})
    artifact_dir = Path("runtime/reports/outreach_video_audit") / _safe_path_part(context.request_id or "unknown-request")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"product_videos_{_safe_path_part(product_id or 'unknown-product')}_{_safe_path_part(context.job_id or 'job')}"
    json_path = artifact_dir / f"{base_name}.json"
    csv_path = artifact_dir / f"{base_name}.csv"
    json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = ["product_id", "creator_unique_id", "video_id", "published_date", "video_url"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(normalized)
    return {
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "video_count": len(normalized),
        "unique_creator_count": len(creator_ids),
        "creator_ids": creator_ids,
    }


def _audit_video_row(video: Mapping[str, Any]) -> dict[str, Any]:
    creator_unique_id = coerce_str(video.get("creator_unique_id"))
    video_id = coerce_str(video.get("video_id"))
    return {
        "product_id": coerce_str(video.get("product_id")),
        "creator_unique_id": creator_unique_id,
        "video_id": video_id,
        "published_date": coerce_str(video.get("published_date")),
        "video_url": first_non_empty(video.get("video_url"), canonical_tiktok_video_url(creator_unique_id, video_id)),
    }


def canonical_tiktok_video_url(unique_id: Any, video_id: Any) -> str:
    creator = coerce_str(unique_id).lstrip("@")
    video = coerce_str(video_id)
    return f"https://www.tiktok.com/@{creator}/video/{video}" if creator and video else ""


def _attach_partial_video_page_state(result: HandlerResult, exc: FastMossHTTPError) -> HandlerResult:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    partial_rows = payload.get("partial_rows")
    failed_page = payload.get("failed_page")
    if partial_rows not in (None, [], {}) or failed_page:
        result.result["partial_video_rows"] = partial_rows or []
        result.result["failed_page"] = failed_page
    return result


def _safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe.strip("-") or "unknown"


def _query_window_kwargs(query_window: Mapping[str, Any]) -> dict[str, Any]:
    if coerce_str(query_window.get("mode")) == "date_range":
        return {
            "start_date": coerce_str(query_window.get("start_date")),
            "end_date": coerce_str(query_window.get("end_date")),
        }
    return {"d_type": first_non_empty(query_window.get("d_type"), 0)}


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(float(coerce_str(value)))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(coerce_str(value)))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = [
    "canonical_tiktok_video_url",
    "match_outreach_rows_to_videos",
    "normalize_product_video_rows",
    "persist_product_video_audit",
    "product_video_outreach_check_handler",
]
