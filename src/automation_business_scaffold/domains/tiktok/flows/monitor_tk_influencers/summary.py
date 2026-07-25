from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_mapping
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    extract_effective_result_payload,
    extract_handler_result_status,
)


TASK_CODE = "monitor_tk_influencers"
WORKFLOW_CODE = TASK_CODE
READ_STAGE_CODE = "read_competitor_products"
DISCOVERY_STAGE_CODE = "discover_product_video_creators"
SYNC_STAGE_CODE = "sync_monitored_influencers"
SUMMARY_STAGE_CODE = "ready_for_summary"


def finalize_request(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del workflow
    summary = force_result or _build_summary(store=store, request=request)
    final_status = str(summary.get("final_status") or "success")
    finished_at = time.time()
    result = {
        "summary": summary,
        "title": "TK达人监控完成",
        "execution_window": {
            "started_at": float(getattr(request, "started_at", 0.0) or 0.0),
            "finished_at": finished_at,
        },
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
            "message_text": _message_text(summary),
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
    read_result: dict[str, Any] = {}
    read_jobs = _stage_jobs(
        store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if read_jobs:
        read_result = extract_effective_result_payload(read_jobs[-1])
    adapter_summary = coerce_mapping(read_result.get("adapter_summary"))
    product_jobs = _stage_jobs(
        store,
        request_id=request.request_id,
        stage_code=DISCOVERY_STAGE_CODE,
        job_code="product_video_creator_discovery",
    )
    creator_jobs = _stage_jobs(
        store,
        request_id=request.request_id,
        stage_code=SYNC_STAGE_CODE,
        job_code="influencer_monitor_sync",
    )

    product_success = 0
    product_empty = 0
    product_failed = 0
    fetched_video_count = 0
    qualified_video_count = 0
    early_stopped = 0
    creator_ids: set[str] = set()
    for job in product_jobs:
        result = extract_effective_result_payload(job)
        status = str(result.get("fetch_status") or "")
        if status == "success":
            product_success += 1
        elif status == "empty":
            product_empty += 1
        elif extract_handler_result_status(job) == "failed" or status == "failed":
            product_failed += 1
        fetched_video_count += int(result.get("fetched_video_count") or 0)
        qualified_video_count += int(result.get("qualified_video_count") or 0)
        early_stopped += int(bool(coerce_mapping(result.get("pagination")).get("early_stopped")))
        creator_ids.update(
            str(item.get("creator_id") or "")
            for item in result.get("candidates") or []
            if isinstance(item, Mapping) and item.get("creator_id")
        )

    creator_created = 0
    creator_updated = 0
    creator_unchanged = 0
    creator_failed = 0
    for job in creator_jobs:
        result = extract_effective_result_payload(job)
        status = extract_handler_result_status(job)
        if status == "failed":
            creator_failed += 1
            continue
        write_summary = coerce_mapping(result.get("write_summary"))
        creator_created += int(write_summary.get("created_count") or 0)
        creator_updated += int(write_summary.get("updated_count") or 0)
        creator_unchanged += int(write_summary.get("unchanged_count") or 0)

    success_units = product_success + product_empty + creator_created + creator_updated + creator_unchanged
    failure_units = product_failed + creator_failed
    final_status = (
        "failed"
        if failure_units and not success_units
        else "partial_success"
        if failure_units
        else "success"
    )
    return {
        "final_status": final_status,
        "result_status": final_status,
        "title": "TK达人监控完成",
        "effective_min_video_sales_28d": int(
            coerce_mapping(getattr(request, "payload", {})).get(
                "min_video_sales_28d", 50
            )
            or 50
        ),
        "source_row_count": int(adapter_summary.get("input_row_count") or 0),
        "valid_product_count": int(
            adapter_summary.get("valid_product_row_count") or 0
        ),
        "deduped_product_count": int(
            adapter_summary.get("deduped_product_count") or 0
        ),
        "product_discovery_success_count": product_success,
        "product_discovery_empty_count": product_empty,
        "product_discovery_failed_count": product_failed,
        "fetched_video_count": fetched_video_count,
        "qualified_video_count": qualified_video_count,
        "qualified_creator_count": len(creator_ids),
        "creator_created_count": creator_created,
        "creator_updated_count": creator_updated,
        "creator_unchanged_count": creator_unchanged,
        "creator_failed_count": creator_failed,
        "early_stopped_product_count": early_stopped,
        "warnings": [],
        "failed_items": [],
    }


def _stage_jobs(
    store: Any,
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
) -> list[dict[str, Any]]:
    try:
        jobs = store.list_api_worker_jobs_for_request(
            request_id=request_id,
            job_code=job_code,
        )
    except TypeError:
        jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    return [
        dict(job)
        for job in jobs
        if str(coerce_mapping(job.get("payload")).get("stage_code") or "")
        == stage_code
    ]


def _message_text(summary: Mapping[str, Any]) -> str:
    return (
        "TK达人监控完成\n"
        f"结果：{summary.get('final_status', 'success')}\n"
        f"商品：{summary.get('deduped_product_count', 0)}\n"
        f"达标达人：{summary.get('qualified_creator_count', 0)}\n"
        f"创建：{summary.get('creator_created_count', 0)}，"
        f"更新：{summary.get('creator_updated_count', 0)}，"
        f"未变化：{summary.get('creator_unchanged_count', 0)}"
    )


__all__ = ["finalize_request"]
