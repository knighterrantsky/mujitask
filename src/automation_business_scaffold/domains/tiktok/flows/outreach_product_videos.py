from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import csv
import json
import time
from pathlib import Path
from typing import Any

from automation_business_scaffold.config import get_execution_control_defaults
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
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

PRODUCT_VIDEO_PAGE_RETRY_DELAYS_SECONDS = (10.0, 20.0, 30.0)
RETRYABLE_PRODUCT_VIDEO_RESPONSE_CODES = {"500"}


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
        error_result = {
            "product_id": product_id,
            "fetch_status": "failed",
            "error": exc.to_dict(),
        }
        payload_state = _partial_video_page_state(exc)
        if payload_state:
            error_result.update(payload_state)
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
            result=error_result,
        )

    match_result = match_outreach_rows_to_videos(
        product_id=product_id,
        rows=rows,
        videos=videos,
        query_window=query_window,
        trigger_date=trigger_date,
    )
    try:
        index_result = index_product_videos(payload=payload, product_id=product_id, videos=videos)
    except Exception as exc:  # noqa: BLE001 - worker retry boundary keeps persistence failures recoverable.
        return failed_result(
            context,
            error=build_error(
                error_type="persistence_failure",
                error_code="product_video_index_failed",
                message=str(exc),
                retryable=True,
                details={"product_id": coerce_str(product_id)},
            ),
            summary={"product_id": coerce_str(product_id), "fetch_status": "failed"},
            result={"product_id": coerce_str(product_id), "fetch_status": "failed", "error": {"message": str(exc)}},
        )
    audit = persist_product_video_audit(context=context, product_id=product_id, videos=videos)
    result = {
        "product_id": coerce_str(product_id),
        "fetch_status": "success",
        "query_window": dict(query_window),
        **index_result,
        "summary": {
            "product_id": coerce_str(product_id),
            "fetch_status": "success",
            "fetched_video_count": len(videos),
            **{key: value for key, value in match_result["summary"].items() if key not in {"product_id", "fetch_status", "fetched_video_count"}},
            **index_result,
        },
    }
    result["video_audit"] = audit
    result["summary"].update(
        {
            "video_audit_ref": audit.get("json_path", ""),
            "unique_creator_count": audit.get("unique_creator_count", 0),
        }
    )
    return success_result(context, summary=result["summary"], result=result)


def index_product_videos(*, payload: Mapping[str, Any], product_id: str, videos: list[Mapping[str, Any]]) -> dict[str, Any]:
    fact_store = _create_fact_store(payload)
    if fact_store is None:
        if _requires_fact_db(payload):
            raise RuntimeError("Product video outreach indexing requires Fact DB persistence, but no fact_db_url was configured.")
        return {
            "persistence_mode": "skipped",
            "indexed_video_count": 0,
            "new_video_count": 0,
            "updated_video_count": 0,
            "failed_video_count": 0,
        }

    indexed = 0
    new_count = 0
    updated_count = 0
    failed_count = 0
    for video in videos:
        normalized = normalize_product_video_rows([video])[0]
        video_id = coerce_str(normalized.get("video_id"))
        creator_unique_id = coerce_str(normalized.get("creator_unique_id"))
        normalized_product_id = first_non_empty(normalized.get("product_id"), product_id)
        if not video_id or not creator_unique_id or not normalized_product_id:
            failed_count += 1
            continue
        creator_key = fact_store.build_creator_key(unique_id=creator_unique_id)
        video_url = first_non_empty(normalized.get("video_url"), canonical_tiktok_video_url(creator_unique_id, video_id))
        published_date = coerce_str(normalized.get("published_date"))
        fact_store.upsert_creator(
            unique_id=creator_unique_id,
            profile_url=f"https://www.tiktok.com/@{creator_unique_id}",
            source_platform="fastmoss",
        )
        video_row = fact_store.upsert_video(
            video_id=video_id,
            creator_key=creator_key,
            creator_unique_id=creator_unique_id,
            product_id=normalized_product_id,
            video_url=video_url,
            source_platform="fastmoss",
            facts={
                "published_date": published_date,
                "create_date": published_date,
                "source_endpoint": "goods.v3.video",
            },
            include_mutation_status=True,
        )
        if not video_row:
            failed_count += 1
            continue
        fact_store.upsert_creator_video_relation(
            creator_key=creator_key,
            video_key=video_row["video_key"],
            source_platform="fastmoss",
            metadata={"source_endpoint": "goods.v3.video"},
        )
        fact_store.upsert_video_product_relation(
            video_key=video_row["video_key"],
            product_id=normalized_product_id,
            source_platform="fastmoss",
            metadata={"source_endpoint": "goods.v3.video"},
        )
        indexed += 1
        if video_row.get("_mutation_status") == "created":
            new_count += 1
        else:
            updated_count += 1
    return {
        "persistence_mode": "database",
        "indexed_video_count": indexed,
        "new_video_count": new_count,
        "updated_video_count": updated_count,
        "failed_video_count": failed_count,
    }


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
    page_size = 5
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
            payload = _list_product_videos_with_page_retry(
                session,
                product_id=product_id,
                page=page,
                pagesize=page_size,
                **kwargs,
            )
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


def _list_product_videos_with_page_retry(
    session: FastMossHTTPSession,
    *,
    product_id: str,
    page: int,
    pagesize: int,
    **kwargs: Any,
) -> dict[str, Any]:
    delays = PRODUCT_VIDEO_PAGE_RETRY_DELAYS_SECONDS
    for attempt in range(len(delays) + 1):
        try:
            return session.list_product_videos(product_id, page=page, pagesize=pagesize, **kwargs)
        except FastMossHTTPError as exc:
            if attempt >= len(delays) or not _is_retryable_product_video_page_error(exc):
                raise
            time.sleep(delays[attempt])
    raise FastMossHTTPError("FastMoss product video page retry exhausted")


def _is_retryable_product_video_page_error(exc: FastMossHTTPError) -> bool:
    if coerce_str(exc.path) != "/api/goods/v3/video":
        return False
    if coerce_str(exc.response_code) in RETRYABLE_PRODUCT_VIDEO_RESPONSE_CODES:
        return True
    payload = exc.payload if isinstance(exc.payload, Mapping) else {}
    return coerce_str(payload.get("code")) in RETRYABLE_PRODUCT_VIDEO_RESPONSE_CODES


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
        creator_unique_id = first_non_empty(row.get("creator_unique_id"), row.get("unique_id"), author.get("unique_id"))
        video_id = first_non_empty(row.get("video_id"), row.get("id"))
        normalized.append(
            {
                "product_id": first_non_empty(row.get("product_id"), row.get("goods_id")),
                "creator_unique_id": creator_unique_id,
                "video_id": video_id,
                "published_date": first_non_empty(row.get("published_date"), row.get("create_date")),
                "video_url": first_non_empty(row.get("video_url"), canonical_tiktok_video_url(creator_unique_id, video_id)),
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
    result.result.update(_partial_video_page_state(exc))
    return result


def _partial_video_page_state(exc: FastMossHTTPError) -> dict[str, Any]:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    partial_rows = payload.get("partial_rows")
    failed_page = payload.get("failed_page")
    if partial_rows not in (None, [], {}) or failed_page:
        return {"partial_video_rows": partial_rows or [], "failed_page": failed_page}
    return {}


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


def _create_fact_store(payload: Mapping[str, Any]) -> TKFactStore | None:
    request_payload = coerce_mapping(payload.get("request_payload"))
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
        request_payload.get("db_url"),
    )
    if not fact_db_url and _requires_fact_db(payload):
        fact_db_url = get_execution_control_defaults().fact_db_url
    return TKFactStore(db_url=fact_db_url) if fact_db_url else None


def _requires_fact_db(payload: Mapping[str, Any]) -> bool:
    request_payload = coerce_mapping(payload.get("request_payload"))
    for source in (
        payload,
        request_payload,
        coerce_mapping(payload.get("persistence")),
        coerce_mapping(request_payload.get("persistence")),
    ):
        for key in ("requires_fact_db", "require_database_persistence", "strict_persistence"):
            value = source.get(key)
            if isinstance(value, bool):
                return value
            if coerce_str(value).lower() in {"1", "true", "yes", "on"}:
                return True
    return False


__all__ = [
    "canonical_tiktok_video_url",
    "index_product_videos",
    "match_outreach_rows_to_videos",
    "normalize_product_video_rows",
    "persist_product_video_audit",
    "product_video_outreach_check_handler",
]
