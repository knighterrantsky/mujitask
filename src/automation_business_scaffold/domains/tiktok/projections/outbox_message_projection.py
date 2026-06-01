from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import json
import os
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REFRESH_TASK_CODE = "refresh_current_competitor_table"
KEYWORD_TASK_CODE = "search_keyword_competitor_products"
SELECTION_KEYWORD_TASK_CODE = "search_keyword_selection_products"
INFLUENCER_TASK_CODE = "sync_tk_influencer_pool"
OUTREACH_TASK_CODE = "tiktok_influencer_outreach_sync"
PRODUCT_INGEST_TASK_CODE = "tiktok_fastmoss_product_ingest"
DEFAULT_MESSAGE_FORMAT = "plain_text_detail"
SUPPORTED_MESSAGE_FORMATS = {"json", "plain_text_summary", "plain_text_detail", "template"}


def build_tiktok_outbox_message_text(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
    message_format: str | None = None,
    message_template: str | None = None,
) -> str:
    selected_format = _resolve_message_format(message_format=message_format, message_template=message_template)
    if task_code == REFRESH_TASK_CODE:
        payload = _build_refresh_competitor_outbox_message(
            request_id=request_id,
            task_code=task_code,
            summary=summary,
            result=result,
        )
        return _render_message_payload(payload, message_format=selected_format, message_template=message_template)
    if task_code in {KEYWORD_TASK_CODE, SELECTION_KEYWORD_TASK_CODE}:
        payload = _build_keyword_outbox_message(
            request_id=request_id,
            task_code=task_code,
            summary=summary,
            result=result,
        )
        return _render_message_payload(payload, message_format=selected_format, message_template=message_template)
    if task_code == PRODUCT_INGEST_TASK_CODE:
        payload = _build_selection_ingest_outbox_message(
            request_id=request_id,
            task_code=task_code,
            summary=summary,
            result=result,
        )
        return _render_message_payload(payload, message_format=selected_format, message_template=message_template)
    if task_code == INFLUENCER_TASK_CODE:
        payload = _build_influencer_outbox_message(
            request_id=request_id,
            task_code=task_code,
            summary=summary,
            result=result,
        )
        return _render_message_payload(payload, message_format=selected_format, message_template=message_template)
    if task_code == OUTREACH_TASK_CODE:
        payload = _build_outreach_outbox_message(
            request_id=request_id,
            task_code=task_code,
            summary=summary,
            result=result,
        )
        return _render_message_payload(payload, message_format=selected_format, message_template=message_template)

    preview = {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "result_keys": sorted(result.keys()),
    }
    return _render_message_payload(preview, message_format=selected_format, message_template=message_template)


def _build_refresh_competitor_outbox_message(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    row_results = [dict(item) for item in result.get("row_results", []) if isinstance(item, dict)]
    rows = [_build_refresh_competitor_outbox_row(item) for item in row_results]
    success_count = sum(1 for item in rows if item.get("status") == "success")
    failed_count = sum(1 for item in rows if item.get("status") == "fail")
    partial_count = sum(1 for item in rows if item.get("status") == "partial_success")
    cancelled_count = sum(1 for item in rows if item.get("status") == "cancelled")
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "total_count": int(result.get("row_total_count") or len(rows)),
        "updated_count": success_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "partial_count": partial_count,
        "cancelled_count": cancelled_count,
        "rows": rows,
    }


def _build_refresh_competitor_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    row_status = str(row.get("row_status") or "").strip()
    status = _outbox_row_status(row_status)
    payload: dict[str, Any] = {
        "sku": str(row.get("product_id") or "").strip(),
        "product_id": str(row.get("product_id") or "").strip(),
        "source_record_id": str(row.get("source_record_id") or "").strip(),
        "status": status,
    }
    if row_status and row_status not in {"success", "failed"}:
        payload["row_status"] = row_status
    if status == "fail":
        payload["failure_reason"] = _refresh_competitor_failure_reason(row)
    return payload


def _outbox_row_status(row_status: str) -> str:
    if row_status in {"success", "unavailable"}:
        return "success"
    if row_status == "partial_success":
        return "partial_success"
    if row_status == "cancelled":
        return "cancelled"
    if row_status in {"skipped", "skip_existing"}:
        return "skipped"
    return "fail"


def _refresh_competitor_failure_reason(row: dict[str, Any]) -> str:
    for key in ("failure_reason", "error_text", "error_message", "error_code"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    failed_steps = [
        step
        for step in ("tiktok", "browser", "media", "fastmoss", "fact", "writeback")
        if str(row.get(f"{step}_status") or "").strip() == "failed"
    ]
    if failed_steps:
        return f"failed_steps={','.join(failed_steps)}"
    row_status = str(row.get("row_status") or "").strip()
    if row_status:
        return f"row_status={row_status}"
    return "unknown"


def _build_keyword_outbox_message(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    row_results = [dict(item) for item in result.get("row_results", []) if isinstance(item, dict)]
    rows = [
        _build_refresh_competitor_outbox_row(item)
        for item in row_results
        if _keyword_row_should_appear_in_detail(item)
    ]
    seed_write_results = [dict(item) for item in result.get("seed_write_results", []) if isinstance(item, dict)]
    seed_status_counts = _count_values(seed_write_results, "status")
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "search_query": str(result.get("search_query") or summary.get("search_query") or "").strip(),
        "search_filter_info": result.get("search_filter_info") or summary.get("search_filter_info") or {},
        "candidate_total_count": int(result.get("candidate_total_count") or summary.get("candidate_total_count") or 0),
        "seed_total_count": int(result.get("seed_total_count") or 0),
        "seed_success_count": _sum_counts(seed_status_counts, ("success", "inserted", "created", "updated")),
        "seed_skipped_count": _sum_counts(seed_status_counts, ("skip_existing", "skipped", "duplicate")),
        "seed_failed_count": _sum_counts(seed_status_counts, ("fail", "failed", "error")),
        "row_success_count": sum(1 for item in rows if item.get("status") == "success"),
        "row_failed_count": sum(1 for item in rows if item.get("status") == "fail"),
        "row_partial_count": sum(1 for item in rows if item.get("status") == "partial_success"),
        "row_cancelled_count": sum(1 for item in rows if item.get("status") == "cancelled"),
        "rows": rows,
    }


def _build_selection_ingest_outbox_message(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    raw_rows = result.get("rows") if isinstance(result.get("rows"), list) else result.get("row_results")
    row_results = [dict(item) for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else []
    rows = [_build_refresh_competitor_outbox_row(item) for item in row_results]
    success_count = sum(1 for item in rows if item.get("status") == "success")
    failed_count = sum(1 for item in rows if item.get("status") == "fail")
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "final_status": str(summary.get("final_status") or result.get("final_status") or "").strip(),
        "total_count": int(result.get("row_count") or len(rows)),
        "updated_count": success_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "child_total_count": int(summary.get("child_total_count") or summary.get("total") or 0),
        "child_success_count": int(summary.get("child_success_count") or 0),
        "child_failed_count": int(summary.get("child_failed_count") or 0),
        "child_skipped_count": int(summary.get("child_skipped_count") or 0),
        "rows": rows,
    }


def _keyword_row_should_appear_in_detail(row: dict[str, Any]) -> bool:
    failure_reason = _refresh_competitor_failure_reason(row)
    row_status = str(row.get("row_status") or row.get("status") or "").strip()
    seed_status = str(row.get("seed_status") or "").strip()
    if failure_reason in {"existing_record", "skip_existing", "duplicate"}:
        return False
    if row_status in {"skipped", "skip_existing"} or seed_status in {"skipped", "skip_existing"}:
        return False
    return True


def _build_influencer_outbox_message(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    product_groups = [
        dict(item)
        for item in (
            summary.get("product_groups")
            or result.get("product_groups")
            or []
        )
        if isinstance(item, dict)
    ]
    group_status_counts = dict(
        summary.get("product_group_status_counts")
        or _count_values(product_groups, "final_status")
    )
    product_success_count = _sum_counts(group_status_counts, ("success", "partial_success"))
    product_failed_count = _sum_counts(group_status_counts, ("failed", "cancelled"))
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "final_status": str(result.get("final_status") or summary.get("final_status") or "").strip(),
        "product_count": int(summary.get("product_group_count") or len(product_groups)),
        "product_success_count": product_success_count,
        "product_failed_count": product_failed_count,
        "child_total_count": int(summary.get("child_total_count") or 0),
        "child_success_count": int(summary.get("child_success_count") or 0),
        "child_failed_count": int(summary.get("child_failed_count") or 0),
        "product_groups": product_groups,
    }


def _build_outreach_outbox_message(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    detail = result.get("outbox_detail") if isinstance(result.get("outbox_detail"), dict) else {}
    product_groups = [
        dict(item)
        for item in detail.get("product_groups", [])
        if isinstance(item, dict)
    ]
    execution_window = (
        result.get("execution_window")
        if isinstance(result.get("execution_window"), dict)
        else {}
    )
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "final_status": str(summary.get("final_status") or result.get("final_status") or "").strip(),
        "execution_window": execution_window,
        "changed_row_count": int(detail.get("changed_row_count") or 0),
        "truncated_changed_row_count": int(detail.get("truncated_changed_row_count") or 0),
        "product_groups": product_groups,
    }


def _resolve_message_format(*, message_format: str | None, message_template: str | None) -> str:
    if message_template:
        return "template"
    configured = str(message_format or os.environ.get("MUJITASK_OUTBOX_MESSAGE_FORMAT", "") or "").strip()
    if not configured:
        return DEFAULT_MESSAGE_FORMAT
    normalized = configured.lower()
    return normalized if normalized in SUPPORTED_MESSAGE_FORMATS else DEFAULT_MESSAGE_FORMAT


def _render_message_payload(
    payload: dict[str, Any],
    *,
    message_format: str,
    message_template: str | None,
) -> str:
    if message_format == "json":
        return json.dumps(payload, ensure_ascii=False)
    if message_format == "template":
        return _render_template(str(message_template or ""), payload)
    if message_format == "plain_text_summary":
        return _render_plain_text(payload, include_rows=False)
    return _render_plain_text(payload, include_rows=True)


def _render_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    task_code = str(payload.get("task_code") or "").strip()
    if task_code == REFRESH_TASK_CODE:
        return _render_refresh_plain_text(payload, include_rows=include_rows)
    if task_code in {KEYWORD_TASK_CODE, SELECTION_KEYWORD_TASK_CODE}:
        return _render_keyword_plain_text(payload, include_rows=include_rows)
    if task_code == PRODUCT_INGEST_TASK_CODE:
        return _render_selection_ingest_plain_text(payload, include_rows=include_rows)
    if task_code == INFLUENCER_TASK_CODE:
        return _render_influencer_plain_text(payload, include_rows=include_rows)
    if task_code == OUTREACH_TASK_CODE:
        return _render_outreach_plain_text(payload, include_rows=include_rows)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "任务完成",
        "",
        f"任务：{task_code or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
    ]
    final_status = str(summary.get("final_status") or "").strip()
    if final_status:
        lines.append(f"状态：{final_status}")
    return "\n".join(lines).strip()


def _render_keyword_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rows = [dict(item) for item in payload.get("rows", []) if isinstance(item, dict)]
    title = "关键词搜索选品写入完成" if payload.get("task_code") == SELECTION_KEYWORD_TASK_CODE else "关键词搜索竞品写入完成"
    lines = [
        title,
        "",
        f"任务：{payload.get('task_code') or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
        f"状态：{summary.get('final_status') or '-'}",
        f"关键词：{payload.get('search_query') or '-'}",
        f"候选：{payload.get('candidate_total_count', 0)} 条",
        f"种子写入成功：{payload.get('seed_success_count', 0)} 条",
        f"种子跳过：{payload.get('seed_skipped_count', 0)} 条",
        f"种子失败：{payload.get('seed_failed_count', 0)} 条",
        f"详情成功：{payload.get('row_success_count', 0)} 条",
        f"详情部分成功：{payload.get('row_partial_count', 0)} 条",
        f"详情取消：{payload.get('row_cancelled_count', 0)} 条",
        f"详情失败：{payload.get('row_failed_count', 0)} 条",
    ]
    filter_text = _format_filter_info(payload.get("search_filter_info"))
    if filter_text:
        lines.append(f"过滤：{filter_text}")
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    if warnings:
        lines.append(f"警告：{len(warnings)} 条")
    if not include_rows or not rows:
        return "\n".join(lines).strip()
    lines.extend(["", "明细："])
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. SKU {row.get('sku') or row.get('product_id') or '-'}")
        lines.append(f"   record: {row.get('source_record_id') or '-'}")
        lines.append(f"   status: {row.get('status') or '-'}")
        failure_reason = str(row.get("failure_reason") or "").strip()
        if failure_reason:
            lines.append(f"   failure_reason: {failure_reason}")
    return "\n".join(lines).strip()


def _render_selection_ingest_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rows = [dict(item) for item in payload.get("rows", []) if isinstance(item, dict)]
    lines = [
        "选品采集完成",
        "",
        f"任务：{payload.get('task_code') or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
        f"状态：{payload.get('final_status') or summary.get('final_status') or '-'}",
        f"总数：{payload.get('total_count', 0)} 条",
        f"更新：{payload.get('updated_count', 0)} 条",
        f"成功：{payload.get('success_count', 0)} 条",
        f"失败：{payload.get('failed_count', 0)} 条",
        f"子任务：{payload.get('child_success_count', 0)}/{payload.get('child_total_count', 0)} 成功",
    ]
    skipped_count = int(payload.get("child_skipped_count") or 0)
    if skipped_count:
        lines.append(f"子任务跳过：{skipped_count} 条")
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    if warnings:
        lines.append(f"警告：{len(warnings)} 条")
    if not include_rows or not rows:
        return "\n".join(lines).strip()
    lines.extend(["", "明细："])
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. SKU {row.get('sku') or row.get('product_id') or '-'}")
        lines.append(f"   record: {row.get('source_record_id') or '-'}")
        lines.append(f"   status: {row.get('status') or '-'}")
        row_status = str(row.get("row_status") or "").strip()
        if row_status:
            lines.append(f"   row_status: {row_status}")
        failure_reason = str(row.get("failure_reason") or "").strip()
        if failure_reason:
            lines.append(f"   failure_reason: {failure_reason}")
    return "\n".join(lines).strip()


def _render_influencer_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    groups = [dict(item) for item in payload.get("product_groups", []) if isinstance(item, dict)]
    lines = [
        "TK达人池同步完成",
        "",
        f"任务：{payload.get('task_code') or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
        f"状态：{payload.get('final_status') or summary.get('final_status') or '-'}",
        f"商品：{payload.get('product_count', 0)} 个",
        f"商品成功：{payload.get('product_success_count', 0)} 个",
        f"商品失败：{payload.get('product_failed_count', 0)} 个",
        f"子任务：{payload.get('child_success_count', 0)}/{payload.get('child_total_count', 0)} 成功",
    ]
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    if warnings:
        lines.append(f"警告：{len(warnings)} 条")
    if not include_rows or not groups:
        return "\n".join(lines).strip()
    lines.extend(["", "明细："])
    for index, group in enumerate(groups, start=1):
        lines.append(f"{index}. SKU {group.get('product_id') or '-'}")
        lines.append(f"   record: {group.get('source_record_id') or '-'}")
        lines.append(f"   status: {group.get('final_status') or '-'}")
        lines.append(f"   更新达人数量：{group.get('influencer_write_updated_count', 0)}")
        lines.append(f"   创建达人数量：{group.get('influencer_write_created_count', 0)}")
        group_warnings = group.get("warnings") if isinstance(group.get("warnings"), list) else []
        if group_warnings:
            lines.append(f"   warnings: {','.join(str(item) for item in group_warnings)}")
    return "\n".join(lines).strip()


def _render_outreach_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    groups = [dict(item) for item in payload.get("product_groups", []) if isinstance(item, dict)]
    lines = [
        "TK达人建联表检查完成",
        "",
        f"任务：{payload.get('task_code') or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
        f"状态：{payload.get('final_status') or summary.get('final_status') or '-'}",
    ]
    execution_time = _format_execution_window(payload.get("execution_window"))
    if execution_time:
        lines.append(f"执行时间：{execution_time}")
    lines.extend(
        [
            "",
            "整体汇总：",
            f"- 读取飞书行数：{_format_int(summary.get('total_rows_read'))}",
            f"- SKU 数量：{_format_int(summary.get('product_count'))}",
            "- 商品视频分页："
            f"{_format_int(summary.get('product_fetch_success_count'))}/"
            f"{_format_int(summary.get('product_count'))} 成功",
            f"- 索引视频数：{_format_int(summary.get('indexed_video_count'))}",
            "- 达人视频指标刷新："
            f"成功 {_format_int(summary.get('creator_refresh_success_count'))}，"
            f"跳过 {_format_int(summary.get('creator_refresh_skipped_count'))}，"
            f"失败 {_format_int(summary.get('creator_refresh_failed_count'))}",
            "- 飞书写回："
            f"成功 {_format_int(summary.get('feishu_write_success_count'))}，"
            f"失败 {_format_int(summary.get('feishu_write_failed_count'))}",
            f"- 仅写入检查时间：{_format_int(summary.get('no_video_checked_count'))}",
            f"- 视频链接更新：{_format_int(summary.get('highest_video_change_count'))}",
            f"- 播放量更新：{_format_int(summary.get('play_count_change_count'))}",
            f"- 视频数量更新：{_format_int(summary.get('video_count_change_count'))}",
            f"- 聚合视频总数：{_format_int(summary.get('aggregated_video_count'))}",
            f"- 聚合播放量：{_format_int(summary.get('aggregated_play_count'))}",
        ]
    )
    if include_rows and groups:
        for group in groups:
            lines.extend(["", f"SKU：{group.get('product_id') or '-'}"])
            lines.append(
                "商品视频分页结果："
                f"{_format_int(group.get('fetched_video_count'))} 条视频，"
                f"命中达人 {_format_int(group.get('matched_row_count'))} 行"
            )
            changed_rows = [
                dict(item)
                for item in group.get("changed_rows", [])
                if isinstance(item, dict)
            ]
            if changed_rows:
                lines.extend(["", "本次字段发生变化的达人："])
                for index, row in enumerate(changed_rows, start=1):
                    lines.append(f"{index}. 达人ID：{row.get('creator_unique_id') or '-'}")
                    lines.append(f"   更新字段：{_format_field_list(row.get('updated_fields'))}")
                    lines.append(f"   视频数量：{_format_int(row.get('video_count'))}")
                    lines.append(f"   聚合播放量：{_format_int(row.get('total_play_count'))}")
                    lines.append(
                        f"   最高播放视频播放量：{_format_int(row.get('highest_play_count'))}"
                    )
                    lines.append("   最高播放视频链接：")
                    lines.append(f"   {row.get('highest_play_video_url') or '-'}")
            elif int(group.get("changed_row_count") or 0) > 0:
                lines.extend(["", "本次字段发生变化的达人："])
                lines.append("明细过长，已省略。")
            truncated_count = int(group.get("truncated_changed_row_count") or 0)
            if truncated_count:
                lines.append(f"其余 {truncated_count} 条已写入，详见运行明细。")
            no_video_count = int(group.get("no_video_checked_count") or 0)
            if no_video_count:
                lines.extend(["", "无视频结果："])
                lines.append(
                    f"- {no_video_count} 行未发现可写视频链接，仅写入检查时间"
                )
    _append_outreach_exceptions(lines, summary)
    return "\n".join(lines).strip()


def _append_outreach_exceptions(lines: list[str], summary: dict[str, Any]) -> None:
    lines.extend(
        [
            "",
            "异常：",
            f"- 失败达人刷新：{_format_int(summary.get('creator_refresh_failed_count'))}",
            f"- overview 失败：{_format_int(summary.get('overview_failed_count'))}",
            f"- 飞书写入失败：{_format_int(summary.get('feishu_write_failed_count'))}",
        ]
    )


def _format_filter_info(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    filters = value.get("filters") if isinstance(value.get("filters"), dict) else {}
    output_conditions = value.get("output_conditions") if isinstance(value.get("output_conditions"), dict) else {}
    for key, item in {**filters, **output_conditions}.items():
        if item not in (None, ""):
            parts.append(f"{key}={item}")
    return ", ".join(parts)


def _render_refresh_plain_text(payload: dict[str, Any], *, include_rows: bool) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    rows = [dict(item) for item in payload.get("rows", []) if isinstance(item, dict)]
    lines = [
        "竞品采集完成",
        "",
        f"任务：{payload.get('task_code') or '-'}",
        f"请求：{payload.get('request_id') or '-'}",
        f"状态：{summary.get('final_status') or '-'}",
        f"总数：{payload.get('total_count', 0)} 条",
        f"更新：{payload.get('updated_count', 0)} 条",
        f"成功：{payload.get('success_count', 0)} 条",
        f"部分成功：{payload.get('partial_count', 0)} 条",
        f"取消：{payload.get('cancelled_count', 0)} 条",
        f"失败：{payload.get('failed_count', 0)} 条",
    ]
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    if warnings:
        lines.append(f"警告：{len(warnings)} 条")
    if not include_rows or not rows:
        return "\n".join(lines).strip()
    lines.extend(["", "明细："])
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. SKU {row.get('sku') or row.get('product_id') or '-'}")
        lines.append(f"   record: {row.get('source_record_id') or '-'}")
        lines.append(f"   status: {row.get('status') or '-'}")
        row_status = str(row.get("row_status") or "").strip()
        if row_status:
            lines.append(f"   row_status: {row_status}")
        failure_reason = str(row.get("failure_reason") or "").strip()
        if failure_reason:
            lines.append(f"   failure_reason: {failure_reason}")
    return "\n".join(lines).strip()


def _render_template(template: str, payload: dict[str, Any]) -> str:
    if not template:
        return _render_plain_text(payload, include_rows=True)
    values = _flatten_template_values(payload)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return str(values.get(key, ""))

    return re.sub(r"\{([^{}]+)\}", replace, template)


def _flatten_template_values(payload: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                walk(next_prefix, child)
            return
        if isinstance(value, list):
            values[prefix] = len(value)
            return
        values[prefix] = value

    walk("", payload)
    return values


def _format_execution_window(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    started = _format_timestamp(value.get("started_at"))
    finished = _format_timestamp(value.get("finished_at"))
    if started and finished:
        return f"{started} ~ {finished}"
    return started or finished


def _format_timestamp(value: Any) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "").strip()
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, tz=_display_timezone()).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _display_timezone() -> tzinfo:
    zone_name = os.environ.get("MUJITASK_DISPLAY_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8))


def _format_int(value: Any) -> str:
    try:
        return f"{int(float(str(value or '0').replace(',', ''))):,}"
    except (TypeError, ValueError):
        return "0"


def _format_field_list(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item or "").strip()) or "-"
    if isinstance(value, tuple):
        return "、".join(str(item).strip() for item in value if str(item or "").strip()) or "-"
    return str(value or "-").strip() or "-"


def _count_values(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _sum_counts(counts: dict[str, int], keys: tuple[str, ...]) -> int:
    return sum(int(counts.get(key, 0) or 0) for key in keys)


__all__ = ["build_tiktok_outbox_message_text"]
