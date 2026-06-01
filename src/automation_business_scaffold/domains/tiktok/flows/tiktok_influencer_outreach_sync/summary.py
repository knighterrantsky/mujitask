from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_mapping
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    extract_effective_result_payload,
    extract_handler_result_status,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

TASK_CODE = "tiktok_influencer_outreach_sync"
WORKFLOW_CODE = get_workflow_definition(TASK_CODE).workflow_code
READ_STAGE_CODE = "read_outreach_rows"
CHECK_STAGE_CODE = "index_product_videos"
REFRESH_STAGE_CODE = "refresh_creator_video_metrics_and_writeback"
SUMMARY_STAGE_CODE = "ready_for_summary"
OUTBOX_DETAIL_ROW_LIMIT = 20
OUTBOX_ROW_DETAIL_FIELDS = {"视频链接", "视频数量", "播放量", "视频发布时间"}


def finalize_request(
    *, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None
) -> dict[str, Any]:
    del workflow
    summary = force_result or _build_summary(store=store, request=request)
    final_status = str(summary.get("final_status") or "success")
    finished_at = time.time()
    outbox_detail = (
        {}
        if force_result is not None
        else _build_outbox_detail(store=store, request=request)
    )
    result = {
        "summary": summary,
        "title": "达人建联检查完成",
        "execution_window": {
            "started_at": float(getattr(request, "started_at", 0.0) or 0.0),
            "finished_at": finished_at,
        },
        "outbox_detail": outbox_detail,
    }
    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
        summary=summary,
        result=result,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        error_text="",
        error_type="",
        error_code="",
        dead_letter_reason="",
        finished_at=finished_at,
    )
    outbox = store.create_notification_outbox(
        channel_code=str(getattr(request, "source_channel_code", "") or "noop"),
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(getattr(request, "reply_target", "") or ""),
        payload={
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": WORKFLOW_CODE,
            "summary_payload": summary,
            "result": result,
            "message_text": build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
                message_format=str(
                    (getattr(request, "payload", {}) or {}).get("outbox_message_format") or ""
                ),
                message_template=str(
                    (getattr(request, "payload", {}) or {}).get("outbox_message_template") or ""
                ),
            ),
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    return {
        "action": "finalized",
        "request_id": request.request_id,
        "request_status": updated.result_status or updated.status,
        "status": updated.status,
        "result_status": updated.result_status,
        "current_stage": updated.current_stage,
        "summary": updated.summary,
        "result": updated.result,
        "task_request": updated.to_dict(),
        "outbox": [outbox.to_dict()],
    }


def _build_summary(*, store: Any, request: Any) -> dict[str, Any]:
    read_result = {}
    for job in reversed(
        _stage_jobs(
            store=store,
            request_id=request.request_id,
            stage_code=READ_STAGE_CODE,
            job_code="feishu_table_read",
        )
    ):
        result = extract_effective_result_payload(job)
        if isinstance(result, dict):
            read_result = result
            break
    check_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=CHECK_STAGE_CODE,
        job_code="product_video_outreach_check",
    )
    refresh_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=REFRESH_STAGE_CODE,
        job_code="outreach_creator_video_metric_refresh",
    )
    indexed_video_count = 0
    new_video_count = 0
    updated_video_count = 0
    product_success = 0
    product_failed = 0
    for job in check_jobs:
        result = extract_effective_result_payload(job)
        if isinstance(result, dict) and result.get("fetch_status") == "success":
            product_success += 1
            indexed_video_count += int(result.get("indexed_video_count") or 0)
            new_video_count += int(result.get("new_video_count") or 0)
            updated_video_count += int(result.get("updated_video_count") or 0)
        elif extract_handler_result_status(job) in {"failed", "fallback_required"} or str(
            job.get("result_status") or job.get("status") or ""
        ) in {"failed", "waiting"}:
            product_failed += 1
    refresh_success = 0
    refresh_skipped = 0
    refresh_failed = 0
    feishu_written = 0
    feishu_failed = 0
    video_count_total = 0
    play_count_total = 0
    no_video_checked = 0
    index_missing_skipped = 0
    overview_failed = 0
    video_count_changed = 0
    play_count_changed = 0
    highest_video_changed = 0
    for job in refresh_jobs:
        result = extract_effective_result_payload(job)
        status = extract_handler_result_status(job)
        if isinstance(result, dict) and result.get("refresh_status") == "success":
            refresh_success += 1
            video_count_total += int(result.get("video_count") or 0)
            play_count_total += int(result.get("total_play_count") or 0)
            written_fields = set(result.get("written_fields") or [])
            if int(result.get("video_count") or 0) == 0 and "检查时间" in written_fields:
                no_video_checked += 1
            if "视频数量" in written_fields:
                video_count_changed += 1
            if "播放量" in written_fields:
                play_count_changed += 1
            if "视频链接" in written_fields:
                highest_video_changed += 1
            if result.get("feishu_written"):
                feishu_written += 1
        elif status == "skipped" or (
            isinstance(result, dict) and result.get("refresh_status") == "skipped"
        ):
            refresh_skipped += 1
            if (
                isinstance(result, dict)
                and result.get("skip_reason") == "existing_link_missing_from_index"
            ):
                index_missing_skipped += 1
        elif status in {"failed", "fallback_required"} or str(
            job.get("result_status") or job.get("status") or ""
        ) in {"failed", "waiting"}:
            refresh_failed += 1
            if isinstance(result, dict) and result.get("error_stage") == "video_overview":
                overview_failed += 1
            if isinstance(result, dict) and result.get("feishu_write"):
                feishu_failed += 1
    final_status = (
        "failed"
        if product_success == 0 and refresh_success == 0 and (product_failed or refresh_failed)
        else "partial_success"
        if product_failed or refresh_failed
        else "success"
    )
    adapter_summary = read_result.get("adapter_summary") if isinstance(read_result, dict) else {}
    return {
        "final_status": final_status,
        "title": "达人建联检查完成",
        "total_rows_read": int((adapter_summary or {}).get("input_row_count") or 0),
        "candidate_row_count": int((adapter_summary or {}).get("source_row_count") or 0),
        "skipped_rows": int((adapter_summary or {}).get("skipped_count") or 0),
        "skip_reasons": dict((adapter_summary or {}).get("skip_reasons") or {}),
        "product_count": len(check_jobs),
        "product_fetch_success_count": product_success,
        "product_fetch_failed_count": product_failed,
        "indexed_video_count": indexed_video_count,
        "new_video_count": new_video_count,
        "updated_video_count": updated_video_count,
        "creator_refresh_success_count": refresh_success,
        "creator_refresh_skipped_count": refresh_skipped,
        "creator_refresh_failed_count": refresh_failed,
        "no_video_checked_count": no_video_checked,
        "index_missing_skipped_count": index_missing_skipped,
        "overview_failed_count": overview_failed,
        "feishu_write_success_count": feishu_written,
        "feishu_write_failed_count": feishu_failed,
        "video_count_change_count": video_count_changed,
        "play_count_change_count": play_count_changed,
        "highest_video_change_count": highest_video_changed,
        "aggregated_video_count": video_count_total,
        "aggregated_play_count": play_count_total,
    }


def _build_outbox_detail(*, store: Any, request: Any) -> dict[str, Any]:
    check_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=CHECK_STAGE_CODE,
        job_code="product_video_outreach_check",
    )
    refresh_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=REFRESH_STAGE_CODE,
        job_code="outreach_creator_video_metric_refresh",
    )

    product_groups: dict[str, dict[str, Any]] = {}
    for job in check_jobs:
        result = _job_result(job)
        summary = _job_summary(job)
        payload = _job_payload(job)
        product_id = _first_non_empty(
            result.get("product_id"),
            summary.get("product_id"),
            payload.get("product_id"),
            str(job.get("business_key") or "").removeprefix("product:"),
        )
        if not product_id:
            continue
        group = product_groups.setdefault(product_id, _empty_outbox_product_group(product_id))
        group["product_fetch_status"] = _first_non_empty(
            result.get("fetch_status"),
            summary.get("fetch_status"),
            job.get("result_status"),
            job.get("status"),
        )
        group["fetched_video_count"] = _int_value(
            _first_non_empty(summary.get("fetched_video_count"), result.get("fetched_video_count"))
        )
        group["matched_row_count"] = _int_value(
            _first_non_empty(summary.get("matched_row_count"), result.get("matched_row_count"))
        )
        group["indexed_video_count"] = _int_value(
            _first_non_empty(summary.get("indexed_video_count"), result.get("indexed_video_count"))
        )
        group["new_video_count"] = _int_value(
            _first_non_empty(summary.get("new_video_count"), result.get("new_video_count"))
        )
        group["updated_video_count"] = _int_value(
            _first_non_empty(summary.get("updated_video_count"), result.get("updated_video_count"))
        )

    for job in refresh_jobs:
        result = _job_result(job)
        status = _first_non_empty(result.get("refresh_status"), extract_handler_result_status(job))
        product_id = _first_non_empty(result.get("product_id"), _job_payload(job).get("product_id"))
        if not product_id:
            continue
        group = product_groups.setdefault(product_id, _empty_outbox_product_group(product_id))
        written_fields = _text_list(result.get("written_fields"))
        video_count = _int_value(result.get("video_count"))
        if status == "success" and video_count == 0 and "检查时间" in written_fields:
            group["no_video_checked_count"] += 1
            continue
        detail_fields = [field for field in written_fields if field in OUTBOX_ROW_DETAIL_FIELDS]
        if not detail_fields:
            continue
        group["changed_row_count"] += 1
        if len(group["changed_rows"]) >= OUTBOX_DETAIL_ROW_LIMIT:
            group["truncated_changed_row_count"] += 1
            continue
        group["changed_rows"].append(
            {
                "source_record_id": _first_non_empty(result.get("source_record_id")),
                "creator_unique_id": _first_non_empty(result.get("creator_unique_id")),
                "updated_fields": written_fields,
                "display_updated_fields": detail_fields,
                "video_count": video_count,
                "total_play_count": _int_value(result.get("total_play_count")),
                "highest_play_video_url": _first_non_empty(result.get("highest_play_video_url")),
                "highest_play_count": _int_value(result.get("highest_play_count")),
                "earliest_published_date": _first_non_empty(result.get("earliest_published_date")),
            }
        )

    groups = [product_groups[key] for key in sorted(product_groups)]
    return {
        "detail_row_limit": OUTBOX_DETAIL_ROW_LIMIT,
        "changed_row_count": sum(int(group.get("changed_row_count") or 0) for group in groups),
        "truncated_changed_row_count": sum(
            int(group.get("truncated_changed_row_count") or 0) for group in groups
        ),
        "product_groups": groups,
    }


def _empty_outbox_product_group(product_id: str) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "product_fetch_status": "",
        "fetched_video_count": 0,
        "matched_row_count": 0,
        "indexed_video_count": 0,
        "new_video_count": 0,
        "updated_video_count": 0,
        "changed_row_count": 0,
        "truncated_changed_row_count": 0,
        "no_video_checked_count": 0,
        "changed_rows": [],
    }


def _stage_jobs(
    *, store: Any, request_id: str, stage_code: str, job_code: str | None = None
) -> list[dict[str, Any]]:
    list_jobs = getattr(store, "list_api_worker_jobs_for_request")
    try:
        jobs = (
            list_jobs(request_id=request_id, job_code=job_code)
            if job_code
            else list_jobs(request_id=request_id)
        )
    except TypeError:
        jobs = list_jobs(request_id=request_id)
    return [
        dict(job)
        for job in jobs
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _job_result(job: Mapping[str, Any]) -> dict[str, Any]:
    result = extract_effective_result_payload(job)
    return dict(result) if isinstance(result, Mapping) else {}


def _job_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    return coerce_mapping(job.get("summary"))


def _job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    return coerce_mapping(job.get("payload"))


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = ["finalize_request"]
