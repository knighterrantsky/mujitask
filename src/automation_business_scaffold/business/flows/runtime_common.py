from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


REFRESH_TASK_CODE = "refresh_current_competitor_table"
KEYWORD_TASK_CODE = "search_keyword_competitor_products"
INFLUENCER_POOL_TASK_CODE = "sync_tk_influencer_pool"
PRODUCT_INGEST_TASK_CODE = "tiktok_fastmoss_product_ingest"

FORMAL_TASK_CODES = (
    REFRESH_TASK_CODE,
    KEYWORD_TASK_CODE,
    INFLUENCER_POOL_TASK_CODE,
    PRODUCT_INGEST_TASK_CODE,
)

CONTROL_ACTION_ALIASES = {
    "submit": "submit",
    "queue": "submit",
    "status": "status",
    "result": "result",
    "load": "status",
    "executor_once": "executor_once",
    "api_worker_once": "api_worker_once",
    "browser_once": "browser_once",
    "browser_loop": "browser_loop",
    "outbox_once": "outbox_once",
    "outbox_loop": "outbox_loop",
    "run": "submit",
    "": "submit",
}


@dataclass(frozen=True, slots=True)
class RuntimeExecutionSettings:
    db_url: str
    worker_id: str
    requested_by: str
    lease_seconds: float
    heartbeat_interval_seconds: float
    poll_interval_seconds: float
    retry_delay_seconds: float
    max_idle_cycles: int
    max_iterations: int
    stop_when_idle: bool


def normalize_control_action(raw_value: Any) -> str:
    normalized = str(raw_value or "").strip().lower()
    return CONTROL_ACTION_ALIASES.get(normalized, normalized or "submit")


def build_runtime_settings(params: dict[str, Any]) -> RuntimeExecutionSettings:
    defaults = get_execution_control_defaults()
    hostname = socket.gethostname().strip() or "localhost"
    return RuntimeExecutionSettings(
        db_url=str(params.get("execution_control_db_url") or defaults.db_url).strip(),
        worker_id=str(params.get("execution_worker_id") or defaults.worker_id or f"{hostname}:{int(time.time())}"),
        requested_by=str(params.get("requested_by") or defaults.requested_by or "local-cli").strip() or "local-cli",
        lease_seconds=max(float(params.get("execution_lease_seconds") or defaults.lease_seconds or 120.0), 5.0),
        heartbeat_interval_seconds=max(
            float(params.get("execution_heartbeat_interval_seconds") or defaults.heartbeat_interval_seconds or 10.0),
            0.2,
        ),
        poll_interval_seconds=max(
            float(params.get("execution_control_poll_interval_seconds") or defaults.poll_interval_seconds or 1.0),
            0.05,
        ),
        retry_delay_seconds=max(float(params.get("execution_retry_delay_seconds") or 15.0), 0.1),
        max_idle_cycles=max(int(params.get("execution_control_max_idle_cycles") or 1), 1),
        max_iterations=max(int(params.get("execution_control_max_iterations") or 0), 0),
        stop_when_idle=bool(params.get("execution_control_stop_when_idle", False)),
    )


def create_runtime_store(settings: RuntimeExecutionSettings) -> RuntimeStore:
    return RuntimeStore(db_url=settings.db_url)


def ensure_formal_task_code(task_code: str) -> str:
    normalized = str(task_code or "").strip()
    if normalized not in FORMAL_TASK_CODES:
        supported = ", ".join(FORMAL_TASK_CODES)
        raise ValueError(f"Unsupported task_code '{normalized}'. Supported task_code: {supported}")
    return normalized


def _request_items(request_result: dict[str, Any]) -> list[dict[str, Any]]:
    items = request_result.get("items")
    if isinstance(items, list):
        return [dict(item) for item in items if isinstance(item, dict)]
    return []


def build_request_payload(
    *,
    store: RuntimeStore,
    request_id: str,
    control_action: str,
    message: str,
) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    executions = [record.to_dict() for record in store.list_task_executions(request_id=request_id)]
    api_worker_jobs = store.list_api_worker_jobs_for_request(request_id=request_id)
    api_worker_job_summary = store.summarize_api_worker_jobs_for_request(request_id=request_id)
    outbox = [record.to_dict() for record in store.list_request_outbox(request_id=request_id)]
    result = dict(request.result or {})
    summary = dict(request.summary or {})
    return {
        "control_action": control_action,
        "message": message,
        "request_id": request.request_id,
        "task_code": request.task_code,
        "request_status": request.status,
        "current_stage": request.current_stage,
        "summary": summary or {"total": 0, "counts": {}},
        "result": result,
        "error": request.error_text,
        "child_total_count": request.child_total_count,
        "child_terminal_count": request.child_terminal_count,
        "child_success_count": request.child_success_count,
        "child_failed_count": request.child_failed_count,
        "child_skipped_count": request.child_skipped_count,
        "task_request": request.to_dict(),
        "executions": executions,
        "api_worker_jobs": api_worker_jobs,
        "api_worker_job_summary": api_worker_job_summary,
        "outbox": outbox,
        "item": {
            "request_id": request.request_id,
            "status": request.status,
            "current_stage": request.current_stage,
            "task_code": request.task_code,
        },
        "items": _request_items(result),
    }


def build_idle_payload(
    *,
    control_action: str,
    actor: str,
    message: str,
) -> dict[str, Any]:
    return {
        "control_action": control_action,
        "message": message,
        "request_id": "",
        "request_status": "idle",
        "current_stage": "",
        "summary": {"total": 0, "counts": {}},
        "item": {},
        "items": [],
        f"{actor}_status": "idle",
        "processed_count": 0,
        "success_count": 0,
        "failed_count": 0,
    }


def build_not_ready_payload(
    *,
    control_action: str,
    actor: str,
    message: str,
) -> dict[str, Any]:
    payload = build_idle_payload(control_action=control_action, actor=actor, message=message)
    payload[f"{actor}_status"] = "not_ready"
    return payload


def build_outbox_message_text(*, request_id: str, task_code: str, summary: dict[str, Any], result: dict[str, Any]) -> str:
    preview = {
        "request_id": request_id,
        "task_code": task_code,
        "summary": summary,
        "result_keys": sorted(result.keys()),
    }
    return json.dumps(preview, ensure_ascii=False)

