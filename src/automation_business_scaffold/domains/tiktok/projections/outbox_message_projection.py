from __future__ import annotations

import json
from typing import Any

REFRESH_TASK_CODE = "refresh_current_competitor_table"


def build_tiktok_outbox_message_text(
    *,
    request_id: str,
    task_code: str,
    summary: dict[str, Any],
    result: dict[str, Any],
) -> str:
    if task_code == REFRESH_TASK_CODE:
        return json.dumps(
            _build_refresh_competitor_outbox_message(
                request_id=request_id,
                task_code=task_code,
                summary=summary,
                result=result,
            ),
            ensure_ascii=False,
        )

    preview = {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "result_keys": sorted(result.keys()),
    }
    return json.dumps(preview, ensure_ascii=False)


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
    status = "success" if row_status == "success" else "fail"
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


__all__ = ["build_tiktok_outbox_message_text"]
