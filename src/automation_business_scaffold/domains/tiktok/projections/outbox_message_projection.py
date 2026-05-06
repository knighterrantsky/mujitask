from __future__ import annotations

import json
import os
import re
from typing import Any

REFRESH_TASK_CODE = "refresh_current_competitor_table"
KEYWORD_TASK_CODE = "search_keyword_competitor_products"
SELECTION_KEYWORD_TASK_CODE = "search_keyword_selection_products"
INFLUENCER_TASK_CODE = "sync_tk_influencer_pool"
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
    return {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "total_count": int(result.get("row_total_count") or len(rows)),
        "updated_count": success_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "rows": rows,
    }


def _build_refresh_competitor_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    row_status = str(row.get("row_status") or "").strip()
    status = "success" if row_status in {"success", "unavailable"} else "fail"
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
        "row_partial_count": int(summary.get("row_partial_count") or 0),
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
