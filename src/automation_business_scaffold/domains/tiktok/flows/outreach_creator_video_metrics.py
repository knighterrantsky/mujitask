from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from automation_business_scaffold.capabilities.fact_sources.fastmoss.security import (
    build_fastmoss_session,
    fastmoss_security_fallback_required_result,
    fastmoss_settings_from_payload,
    is_fastmoss_security_verification_error,
    is_fastmoss_session_conflict_error,
    prepare_fastmoss_session,
)
from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.contracts.handler.dispatch import api_handler_callable
from automation_business_scaffold.contracts.handler.shared import (
    build_error,
    coerce_mapping,
    coerce_str,
    failed_result,
    first_non_empty,
    skipped_result,
    success_result,
)
from automation_business_scaffold.infrastructure.fastmoss.http_session import (
    FastMossAuthError,
    FastMossHTTPError,
    FastMossHTTPSession,
)
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

from .outreach_product_videos import canonical_tiktok_video_url


FASTMOSS_VIDEO_OVERVIEW_REQUEST_DELAY_MIN_SECONDS = 3.0
FASTMOSS_VIDEO_OVERVIEW_REQUEST_DELAY_MAX_SECONDS = 6.0
FASTMOSS_VIDEO_OVERVIEW_PAIR_DELAY_MIN_SECONDS = 0.8
FASTMOSS_VIDEO_OVERVIEW_PAIR_DELAY_MAX_SECONDS = 1.5

feishu_table_write_handler = api_handler_callable("feishu_table_write")


def outreach_creator_video_metric_refresh_handler(context: HandlerContext) -> HandlerResult:
    payload = dict(context.payload)
    product_id = coerce_str(payload.get("product_id"))
    creator_unique_id = coerce_str(payload.get("creator_unique_id"))
    source_record_id = coerce_str(payload.get("source_record_id"))
    trigger_date = first_non_empty(payload.get("trigger_date"), date.today().isoformat())
    source_fields = coerce_mapping(payload.get("source_fields"))

    try:
        fact_store = _create_fact_store(payload)
        videos = fact_store.list_videos_by_product_and_creator(
            product_id=product_id,
            creator_unique_id=creator_unique_id,
        )
    except Exception as exc:  # noqa: BLE001 - worker retry boundary.
        return failed_result(
            context,
            error=build_error(
                error_type="persistence_failure",
                error_code="outreach_video_index_read_failed",
                message=str(exc),
                retryable=True,
                details={"product_id": product_id, "creator_unique_id": creator_unique_id},
            ),
            summary=_base_summary(product_id, creator_unique_id, source_record_id, "failed"),
            result={"error_stage": "fact_index_read"},
        )

    if not videos:
        return _handle_no_videos(context, payload=payload, trigger_date=trigger_date)

    try:
        overview_rows = _fetch_video_overviews(payload, videos=videos)
    except FastMossAuthError as exc:
        return fastmoss_security_fallback_required_result(
            context,
            exc=exc,
            handler_payload=payload,
            fastmoss_settings=fastmoss_settings_from_payload(payload),
            operation="outreach_creator_video_metric_refresh",
            entity_identity={"product_id": product_id, "creator_unique_id": creator_unique_id},
            fallback_reason="fastmoss_auth_session_recovery",
            error_type="auth_failure",
            error_code="fastmoss_auth_session_recovery_required",
        )
    except FastMossHTTPError as exc:
        if is_fastmoss_session_conflict_error(exc):
            return fastmoss_security_fallback_required_result(
                context,
                exc=exc,
                handler_payload=payload,
                fastmoss_settings=fastmoss_settings_from_payload(payload),
                operation="outreach_creator_video_metric_refresh",
                entity_identity={"product_id": product_id, "creator_unique_id": creator_unique_id},
                fallback_reason="fastmoss_auth_session_recovery",
                error_type="auth_failure",
                error_code="fastmoss_auth_session_recovery_required",
            )
        if is_fastmoss_security_verification_error(exc):
            return fastmoss_security_fallback_required_result(
                context,
                exc=exc,
                handler_payload=payload,
                fastmoss_settings=fastmoss_settings_from_payload(payload),
                operation="outreach_creator_video_metric_refresh",
                entity_identity={"product_id": product_id, "creator_unique_id": creator_unique_id},
            )
        return failed_result(
            context,
            error=build_error(
                error_type="transport_failure",
                error_code="fastmoss_video_overview_failed",
                message=str(exc),
                retryable=True,
                details=exc.to_dict(),
            ),
            summary=_base_summary(product_id, creator_unique_id, source_record_id, "failed"),
            result={"error_stage": "video_overview", "error": exc.to_dict()},
        )

    try:
        snapshots = _record_metric_snapshots(fact_store, videos=videos, overview_rows=overview_rows)
    except Exception as exc:  # noqa: BLE001 - worker retry boundary.
        return failed_result(
            context,
            error=build_error(
                error_type="persistence_failure",
                error_code="outreach_video_metric_snapshot_failed",
                message=str(exc),
                retryable=True,
                details={"product_id": product_id, "creator_unique_id": creator_unique_id},
            ),
            summary=_base_summary(product_id, creator_unique_id, source_record_id, "failed"),
            result={"error_stage": "video_metric_snapshot"},
        )

    aggregate = _aggregate_metrics(videos=videos, snapshots=snapshots, creator_unique_id=creator_unique_id)
    if not _text_value(aggregate.get("highest_play_video_url")) and _existing_video_url(payload, source_fields):
        result = {
            **_base_summary(product_id, creator_unique_id, source_record_id, "skipped"),
            "video_count": aggregate["video_count"],
            "overview_success_count": len(overview_rows),
            "overview_failed_count": 0,
            "total_play_count": aggregate["total_play_count"],
            "highest_play_video_url": "",
            "highest_play_count": aggregate["highest_play_count"],
            "earliest_published_date": aggregate["earliest_published_date"],
            "skip_reason": "existing_link_missing_from_index",
            "feishu_written": False,
            "written_fields": [],
        }
        return skipped_result(context, summary=result, result=result)
    write_fields = _build_write_fields(
        payload,
        source_fields=source_fields,
        aggregate=aggregate,
        trigger_date=trigger_date,
    )
    write_result = _write_feishu_row(context, payload=payload, source_record_id=source_record_id, fields=write_fields)
    if write_result.status == "failed" or (
        write_result.status == "partial_success" and int(write_result.summary.get("failed_count") or 0) > 0
    ):
        return failed_result(
            context,
            error=write_result.error
            or build_error(
                error_type="upstream_error",
                error_code="feishu_write_failed",
                message="Feishu outreach row writeback failed.",
                retryable=True,
                details=write_result.result,
            ),
            summary=_base_summary(product_id, creator_unique_id, source_record_id, "failed"),
            result={"feishu_write": write_result.result},
        )

    result = {
        "product_id": product_id,
        "creator_unique_id": creator_unique_id,
        "source_record_id": source_record_id,
        "refresh_status": "success",
        "video_count": aggregate["video_count"],
        "overview_success_count": len(overview_rows),
        "overview_failed_count": 0,
        "total_play_count": aggregate["total_play_count"],
        "highest_play_video_url": aggregate["highest_play_video_url"],
        "highest_play_count": aggregate["highest_play_count"],
        "earliest_published_date": aggregate["earliest_published_date"],
        "feishu_written": bool(write_fields and write_result.status != "skipped"),
        "written_fields": list(write_fields.keys()) if write_result.status != "skipped" else [],
    }
    return success_result(context, summary=result, result=result)


def _handle_no_videos(context: HandlerContext, *, payload: Mapping[str, Any], trigger_date: str) -> HandlerResult:
    product_id = coerce_str(payload.get("product_id"))
    creator_unique_id = coerce_str(payload.get("creator_unique_id"))
    source_record_id = coerce_str(payload.get("source_record_id"))
    source_fields = coerce_mapping(payload.get("source_fields"))
    if _existing_video_url(payload, source_fields):
        result = {
            **_base_summary(product_id, creator_unique_id, source_record_id, "skipped"),
            "video_count": 0,
            "skip_reason": "existing_link_missing_from_index",
            "feishu_written": False,
            "written_fields": [],
        }
        return skipped_result(context, summary=result, result=result)
    fields = _diff_fields(
        {"检查时间": trigger_date},
        {
            "检查时间": first_non_empty(payload.get("last_checked_at"), source_fields.get("检查时间")),
        },
    )
    write_result = _write_feishu_row(context, payload=payload, source_record_id=source_record_id, fields=fields)
    if write_result.status == "failed":
        return failed_result(
            context,
            error=write_result.error
            or build_error(
                error_type="upstream_error",
                error_code="feishu_write_failed",
                message="Feishu outreach check time writeback failed.",
                retryable=True,
                details=write_result.result,
            ),
            summary=_base_summary(product_id, creator_unique_id, source_record_id, "failed"),
        )
    result = {
        **_base_summary(product_id, creator_unique_id, source_record_id, "success"),
        "video_count": 0,
        "overview_success_count": 0,
        "overview_failed_count": 0,
        "total_play_count": 0,
        "highest_play_video_url": "",
        "highest_play_count": 0,
        "earliest_published_date": "",
        "feishu_written": bool(fields and write_result.status != "skipped"),
        "written_fields": list(fields.keys()) if write_result.status != "skipped" else [],
    }
    return success_result(context, summary=result, result=result)


def _fetch_video_overviews(payload: Mapping[str, Any], *, videos: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    mock_rows = _mock_overviews(payload)
    if "mock_fastmoss_video_overviews" in payload:
        return [_overview_for_video(mock_rows, video) for video in videos]
    fastmoss_settings = fastmoss_settings_from_payload(payload)
    live_fetch = bool(fastmoss_settings.get("live_fetch") or fastmoss_settings.get("_has_live_config"))
    if not live_fetch:
        raise FastMossAuthError("FastMoss live fetch config is missing for outreach video metric refresh.")
    if not fastmoss_settings.get("fastmoss_api_request_delay_min_seconds"):
        fastmoss_settings["fastmoss_api_request_delay_min_seconds"] = (
            FASTMOSS_VIDEO_OVERVIEW_REQUEST_DELAY_MIN_SECONDS
        )
    if not fastmoss_settings.get("fastmoss_api_request_delay_max_seconds"):
        fastmoss_settings["fastmoss_api_request_delay_max_seconds"] = (
            FASTMOSS_VIDEO_OVERVIEW_REQUEST_DELAY_MAX_SECONDS
        )
    with build_fastmoss_session(fastmoss_settings, session_factory=FastMossHTTPSession) as session:
        prepare_fastmoss_session(session, settings=fastmoss_settings)
        video_delay_range = session.request_delay_range
        return [
            _fetch_frontend_video_overview_pair(
                session,
                coerce_str(video.get("video_id")),
                video_delay_range=video_delay_range,
            )
            for video in videos
        ]


def _fetch_frontend_video_overview_pair(
    session: FastMossHTTPSession,
    video_id: str,
    *,
    video_delay_range: tuple[float, float],
) -> dict[str, Any]:
    session.request_delay_range = video_delay_range
    overview = dict(session.get_video_overview(video_id))
    session.request_delay_range = (
        FASTMOSS_VIDEO_OVERVIEW_PAIR_DELAY_MIN_SECONDS,
        FASTMOSS_VIDEO_OVERVIEW_PAIR_DELAY_MAX_SECONDS,
    )
    overview_data = dict(session.get_video_overview_data(video_id))
    return {
        **overview,
        **overview_data,
        "_frontend_overview": overview,
        "_frontend_overview_data": overview_data,
    }


def _record_metric_snapshots(
    fact_store: TKFactStore,
    *,
    videos: list[Mapping[str, Any]],
    overview_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for video, overview in zip(videos, overview_rows, strict=True):
        counts = _overview_counts(overview)
        snapshots.append(
            fact_store.record_video_metric_snapshot(
                video_key=coerce_str(video.get("video_key")),
                video_id=coerce_str(video.get("video_id")),
                creator_key=coerce_str(video.get("creator_key")),
                source_platform="fastmoss",
                source_endpoint="video.overview",
                play_count=counts["play_count"],
                digg_count=counts["digg_count"],
                comment_count=counts["comment_count"],
                share_count=counts["share_count"],
                payload=dict(overview),
            )
        )
    return snapshots


def _aggregate_metrics(
    *,
    videos: list[Mapping[str, Any]],
    snapshots: list[Mapping[str, Any]],
    creator_unique_id: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for video, snapshot in zip(videos, snapshots, strict=True):
        published_date = _published_date(video)
        video_id = coerce_str(video.get("video_id"))
        rows.append(
            {
                "video_id": video_id,
                "video_url": first_non_empty(video.get("video_url"), canonical_tiktok_video_url(creator_unique_id, video_id)),
                "published_date": published_date,
                "play_count": _int(snapshot.get("play_count")),
            }
        )
    highest = sorted(rows, key=lambda row: (-row["play_count"], row["published_date"], row["video_id"]))[0]
    published_dates = [row["published_date"] for row in rows if row["published_date"]]
    return {
        "video_count": len(rows),
        "total_play_count": sum(row["play_count"] for row in rows),
        "highest_play_video_url": highest["video_url"],
        "highest_play_count": highest["play_count"],
        "earliest_published_date": min(published_dates) if published_dates else "",
    }


def _build_write_fields(
    payload: Mapping[str, Any],
    *,
    source_fields: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    trigger_date: str,
) -> dict[str, Any]:
    existing_video_url = _existing_video_url(payload, source_fields)
    aggregate_video_url = _link_value(aggregate.get("highest_play_video_url"))
    if not _text_value(aggregate_video_url):
        if existing_video_url:
            return {}
        return _diff_fields(
            {"检查时间": trigger_date},
            {
                "检查时间": first_non_empty(payload.get("last_checked_at"), _text_value(source_fields.get("检查时间"))),
            },
        )

    existing_published = first_non_empty(payload.get("existing_video_published_date"), _text_value(source_fields.get("视频发布时间")))
    desired: dict[str, Any] = {
        "视频链接": aggregate_video_url,
        "播放量": _format_feishu_play_count(aggregate.get("total_play_count")),
        "视频数量": int(aggregate.get("video_count") or 0),
    }
    if not existing_published and aggregate.get("earliest_published_date"):
        desired["视频发布时间"] = aggregate["earliest_published_date"]
    existing = {
        "视频链接": existing_video_url,
        "视频发布时间": existing_published,
        "播放量": _existing_play_count_display(payload, source_fields),
        "视频数量": _int(first_non_empty(payload.get("existing_video_count"), source_fields.get("视频数量"))),
    }
    fields = _diff_fields(desired, existing)
    if fields:
        fields["更新时间"] = trigger_date
    return fields


def _write_feishu_row(
    context: HandlerContext,
    *,
    payload: Mapping[str, Any],
    source_record_id: str,
    fields: Mapping[str, Any],
) -> HandlerResult:
    if not fields:
        return skipped_result(
            _child_context(context, "feishu_table_write", {}),
            summary={"written_count": 0, "skipped_count": 0, "failed_count": 0},
            result={"written_count": 0, "records": []},
        )
    if not _writeback_enabled(payload):
        return skipped_result(
            _child_context(context, "feishu_table_write", {}),
            summary={"written_count": 0, "skipped_count": 1, "failed_count": 0},
            result={"written_count": 0, "records": [], "writeback_suppressed": True},
        )
    target_table_ref = first_non_empty(
        payload.get("target_table_ref"),
        coerce_mapping(payload.get("writeback_context")).get("target_table_ref"),
        coerce_mapping(payload.get("request_payload")).get("target_table_ref"),
        payload.get("source_table_ref"),
        coerce_mapping(payload.get("request_payload")).get("source_table_ref"),
    )
    write_payload = {
        **dict(payload.get("request_payload") or {}),
        "target_table_ref": target_table_ref,
        "write_mode": "update",
        "records": [
            {
                "op": "update",
                "record_id": source_record_id,
                "business_entity_key": first_non_empty(payload.get("business_key"), f"outreach:{source_record_id}"),
                "fields": dict(fields),
                "source_context": {
                    "source_record_id": source_record_id,
                    "product_id": coerce_str(payload.get("product_id")),
                    "creator_unique_id": coerce_str(payload.get("creator_unique_id")),
                    "workflow_code": coerce_str(payload.get("workflow_code")),
                    "stage_code": coerce_str(payload.get("stage_code")),
                },
            }
        ],
    }
    return feishu_table_write_handler(_child_context(context, "feishu_table_write", write_payload))


def _child_context(context: HandlerContext, handler_code: str, payload: Mapping[str, Any]) -> HandlerContext:
    return HandlerContext(
        request_id=context.request_id,
        job_id=f"{context.job_id}:{handler_code}",
        handler_code=handler_code,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=dict(payload),
        workflow_code=context.workflow_code,
        stage_code=context.stage_code,
        job_code=handler_code,
        worker_id=context.worker_id,
        attempt_count=context.attempt_count,
        max_attempts=context.max_attempts,
        metadata=context.metadata,
    )


def _create_fact_store(payload: Mapping[str, Any]) -> TKFactStore:
    request_payload = coerce_mapping(payload.get("request_payload"))
    fact_db_url = first_non_empty(
        payload.get("fact_db_url"),
        request_payload.get("fact_db_url"),
        request_payload.get("execution_control_fact_db_url"),
        coerce_mapping(payload.get("persistence")).get("fact_db_url"),
        coerce_mapping(request_payload.get("persistence")).get("fact_db_url"),
        payload.get("db_url"),
        request_payload.get("db_url"),
        get_execution_control_defaults().fact_db_url,
    )
    if not fact_db_url:
        raise RuntimeError("outreach_creator_video_metric_refresh requires Fact DB persistence.")
    return TKFactStore(db_url=fact_db_url)


def _mock_overviews(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("mock_fastmoss_video_overviews")
    if isinstance(raw, Mapping):
        return {coerce_str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}
    if isinstance(raw, list):
        return {
            first_non_empty(item.get("video_id"), item.get("id"), item.get("aweme_id")): dict(item)
            for item in raw
            if isinstance(item, Mapping)
        }
    return {}


def _overview_for_video(mock_rows: Mapping[str, Mapping[str, Any]], video: Mapping[str, Any]) -> dict[str, Any]:
    video_id = coerce_str(video.get("video_id"))
    overview = dict(mock_rows.get(video_id) or {})
    if not overview:
        raise FastMossHTTPError(f"Missing mock overview for video {video_id}", payload={"video_id": video_id})
    overview.setdefault("video_id", video_id)
    return overview


def _overview_counts(overview: Mapping[str, Any]) -> dict[str, int]:
    stats = coerce_mapping(overview.get("stats"))
    return {
        "play_count": _int(first_non_empty(overview.get("play_count"), stats.get("play_count"))),
        "digg_count": _int(first_non_empty(overview.get("digg_count"), stats.get("digg_count"))),
        "comment_count": _int(first_non_empty(overview.get("comment_count"), stats.get("comment_count"))),
        "share_count": _int(first_non_empty(overview.get("share_count"), stats.get("share_count"))),
    }


def _published_date(video: Mapping[str, Any]) -> str:
    facts = coerce_mapping(video.get("facts"))
    return first_non_empty(video.get("published_date"), video.get("create_date"), facts.get("published_date"), facts.get("create_date"))


def _diff_fields(desired: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in desired.items():
        if key == "视频链接":
            if _text_value(value) and _text_value(value) != _text_value(existing.get(key)):
                fields[key] = value
            continue
        existing_value = existing.get(key)
        if str(value) != str(existing_value if existing_value is not None else ""):
            fields[key] = value
    return fields


def _writeback_enabled(payload: Mapping[str, Any]) -> bool:
    request_payload = coerce_mapping(payload.get("request_payload"))
    for source in (payload, request_payload):
        value = source.get("writeback_enabled")
        if value in (None, ""):
            value = source.get("allow_feishu_writeback")
        if isinstance(value, bool):
            return value
        text = coerce_str(value).lower()
        if text in {"0", "false", "no", "off"}:
            return False
        if text in {"1", "true", "yes", "on"}:
            return True
    return True


def _existing_video_url(payload: Mapping[str, Any], source_fields: Mapping[str, Any]) -> str:
    return first_non_empty(payload.get("existing_video_url"), _text_value(source_fields.get("视频链接")))


def _link_value(value: Any) -> dict[str, str] | str:
    url = coerce_str(value)
    return {"link": url, "text": url} if url else ""


def _format_feishu_play_count(value: Any) -> str:
    play_count = max(0, _int(value))
    if play_count < 10000:
        return "<1W"
    return f"{play_count // 10000}W"


def _existing_play_count_display(payload: Mapping[str, Any], source_fields: Mapping[str, Any]) -> str:
    if "播放量" in source_fields:
        return _text_value(source_fields.get("播放量"))
    existing_play_count = payload.get("existing_play_count")
    if existing_play_count not in (None, ""):
        return _format_feishu_play_count(existing_play_count)
    return ""


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return first_non_empty(value.get("link"), value.get("text"), value.get("value"), value.get("name"))
    if isinstance(value, list):
        return first_non_empty(*(_text_value(item) for item in value))
    return coerce_str(value)


def _int(value: Any) -> int:
    text = _text_value(value).replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _base_summary(product_id: str, creator_unique_id: str, source_record_id: str, status: str) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "creator_unique_id": creator_unique_id,
        "source_record_id": source_record_id,
        "refresh_status": status,
    }


__all__ = ["outreach_creator_video_metric_refresh_handler"]
