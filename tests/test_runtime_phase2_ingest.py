from __future__ import annotations

import json

import pytest

from automation_business_scaffold.business.flows import runtime_orchestrator
from automation_business_scaffold.business.handlers import (
    HandlerContext,
    HandlerError,
    HandlerNextAction,
    HandlerResult,
    build_api_handler_registry,
    build_browser_handler_registry,
    register_api_handler,
    register_browser_handler,
)
from automation_business_scaffold.business.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

DIRECT_PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123"
DIRECT_PRODUCT_ID = "123"


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _submit_ingest_request(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    task = TikTokFastMossProductIngestTask()
    submit_params: dict[str, object] = {
        "control_action": "submit",
        "product_url": DIRECT_PRODUCT_URL,
        "product_id": DIRECT_PRODUCT_ID,
        "fallback_allowed": True,
        "source_channel_code": "console",
        "reply_target": "reply://phase2",
    }
    submit_params.update(overrides)
    params = _runtime_params(runtime_db_url, **submit_params)
    return task.run_runtime_request(params)


def _load_store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _payload_contains_product_ref(payload: dict[str, object]) -> bool:
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return DIRECT_PRODUCT_URL in payload_text or DIRECT_PRODUCT_ID in payload_text


def _bind_api_handlers_for_success(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_api_handler_registry()

    def fake_tiktok_request_fetch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("tiktok_request_fetch", message="request-first product fetch")
        return HandlerResult.success(
            context,
            summary={"transport": "request"},
            result={
                "normalized_product_result": {
                    "product_id": DIRECT_PRODUCT_ID,
                    "product_url": DIRECT_PRODUCT_URL,
                    "source": "request",
                    "media_assets": [],
                }
            },
        )

    def fake_fastmoss_product_fetch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("fastmoss_product_fetch", message="fastmoss product fetch")
        return HandlerResult.success(
            context,
            summary={"transport": "fastmoss"},
            result={
                "product_fact_bundle": {
                    "product_id": DIRECT_PRODUCT_ID,
                    "gmv_currency": "USD",
                }
            },
        )

    register_api_handler(registry, "tiktok_product_request_fetch", fake_tiktok_request_fetch)
    register_api_handler(registry, "fastmoss_product_fetch", fake_fastmoss_product_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def _bind_api_handlers_for_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_api_handler_registry()

    def fake_tiktok_request_fetch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("tiktok_request_fetch", message="request path blocked")
        error = HandlerError(
            error_type="transport",
            error_code="tiktok_request_blocked",
            message="request path requires browser fallback",
            retryable=False,
            fallback_allowed=True,
            fallback_reason="request_blocked",
        )
        return HandlerResult.fallback_required(
            context,
            error=error,
            summary={"transport": "request"},
            result={
                "fallback_required": True,
                "fallback_reason": "request_blocked",
                "fallback_source_job_id": context.job_id,
            },
            next_action=HandlerNextAction(
                type="browser_fallback",
                payload={
                    "product_identity": {"product_id": DIRECT_PRODUCT_ID, "product_url": DIRECT_PRODUCT_URL},
                    "fallback_source_job_id": context.job_id,
                },
            ),
        )

    def fake_fastmoss_product_fetch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("fastmoss_product_fetch", message="fastmoss product fetch")
        return HandlerResult.success(
            context,
            summary={"transport": "fastmoss"},
            result={"product_fact_bundle": {"product_id": DIRECT_PRODUCT_ID}},
        )

    register_api_handler(registry, "tiktok_product_request_fetch", fake_tiktok_request_fetch)
    register_api_handler(registry, "fastmoss_product_fetch", fake_fastmoss_product_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def _bind_browser_fallback_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_browser_handler_registry()

    def fake_browser_fetch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("browser_fallback_collected", message="browser fallback collected product")
        return HandlerResult.success(
            context,
            summary={"transport": "browser"},
            result={
                "normalized_product_result": {
                    "product_id": DIRECT_PRODUCT_ID,
                    "product_url": DIRECT_PRODUCT_URL,
                    "source": "browser",
                }
            },
        )

    register_browser_handler(registry, "tiktok_product_browser_fetch", fake_browser_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_browser_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "BROWSER_HANDLER_REGISTRY", registry, raising=False)


def test_phase2_submit_then_executor_dispatches_direct_ingest_jobs(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    payload = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["request_status"] == "waiting_children"
    assert payload["current_stage"] == "collect_product_data"
    job_codes = {job["job_code"] for job in payload["api_worker_jobs"]}
    assert {"tiktok_product_request_fetch", "fastmoss_product_fetch"} <= job_codes
    assert "feishu_table_read" not in job_codes

    request_fetch_job = next(
        job for job in payload["api_worker_jobs"] if job["job_code"] == "tiktok_product_request_fetch"
    )
    fastmoss_job = next(job for job in payload["api_worker_jobs"] if job["job_code"] == "fastmoss_product_fetch")
    assert _payload_contains_product_ref(request_fetch_job["payload"])
    assert _payload_contains_product_ref(fastmoss_job["payload"])


def test_phase2_api_worker_once_dispatches_registry_and_persists_results(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    _bind_api_handlers_for_success(monkeypatch)

    first_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    second_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    assert first_job["request_id"] == request_id
    assert second_job["request_id"] == request_id
    assert first_job["supervisor"]["worker_type"] == "api_worker"
    assert second_job["supervisor"]["worker_type"] == "api_worker"

    status_payload = runtime_orchestrator.get_task_request_status(
        "tiktok_fastmoss_product_ingest",
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )
    jobs_by_code = {job["job_code"]: job for job in status_payload["api_worker_jobs"]}

    assert jobs_by_code["tiktok_product_request_fetch"]["status"] == "success"
    assert jobs_by_code["tiktok_product_request_fetch"]["result"]["normalized_product_result"]["product_id"] == (
        DIRECT_PRODUCT_ID
    )
    assert jobs_by_code["tiktok_product_request_fetch"]["summary"]["progress_stage"] == "tiktok_request_fetch"
    assert jobs_by_code["tiktok_product_request_fetch"]["result"]["supervisor"]["progress_stage"] == (
        "tiktok_request_fetch"
    )
    assert jobs_by_code["fastmoss_product_fetch"]["status"] == "success"
    assert jobs_by_code["fastmoss_product_fetch"]["result"]["product_fact_bundle"]["product_id"] == DIRECT_PRODUCT_ID


def test_phase2_browser_fallback_path_from_request_handler(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    _bind_api_handlers_for_browser_fallback(monkeypatch)
    _bind_browser_fallback_handler(monkeypatch)

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    reconcile = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    browser_items = [item for item in reconcile["executions"] if item["item_code"] == "tiktok_product_browser_fetch"]
    assert reconcile["request_id"] == request_id
    assert browser_items, "executor should enqueue a browser fallback execution for request-path fallback"

    browser_payload = runtime_orchestrator.execute_browser_once(_runtime_params(runtime_db_url))

    assert browser_payload["request_id"] == request_id
    assert browser_payload["supervisor"]["worker_type"] == "browser_worker"
    assert browser_payload["supervisor"]["progress_stage"] == "browser_fallback_collected"
    refreshed = runtime_orchestrator.get_task_request_status(
        "tiktok_fastmoss_product_ingest",
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )
    browser_execution = next(
        item for item in refreshed["executions"] if item["item_code"] == "tiktok_product_browser_fetch"
    )
    assert browser_execution["status"] == "success"
    assert browser_execution["result"]["normalized_product_result"]["source"] == "browser"
    assert browser_execution["result"]["supervisor"]["progress_stage"] == "browser_fallback_collected"


def test_phase2_final_executor_once_summarizes_and_creates_notification_outbox(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    store = _load_store(runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="waiting_children",
        current_stage="persist_facts",
    )
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fact_bundle_upsert",
        jobs=[
            {
                "business_key": DIRECT_PRODUCT_ID,
                "dedupe_key": f"{request_id}:fact_bundle_upsert:{DIRECT_PRODUCT_ID}",
                "payload": {
                    "request_id": request_id,
                    "task_code": "tiktok_fastmoss_product_ingest",
                    "workflow_code": "tiktok_fastmoss_product_ingest",
                    "stage_code": "persist_facts",
                    "product_identity": {"product_id": DIRECT_PRODUCT_ID},
                },
            }
        ],
    )
    job_id = enqueue["created_records"][0]["job_id"]
    claimed = store.claim_next_api_worker_job(worker_id="pytest-api", lease_seconds=30.0)
    assert claimed is not None and claimed["job_id"] == job_id
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=claimed["run_id"],
        summary={"total": 1, "counts": {"success": 1}},
        result={
            "final_status": "success",
            "normalized_product_result": {"product_id": DIRECT_PRODUCT_ID},
            "product_fact_bundle": {"product_id": DIRECT_PRODUCT_ID},
        },
    )
    store.update_task_request(
        request_id=request_id,
        status="ready_for_summary",
        current_stage="ready_for_summary",
    )

    payload = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["request_status"] == "success"
    assert payload["current_stage"] == "completed"
    assert payload["outbox"], "final executor step should create notification_outbox"
    assert payload["outbox"][0]["event_type"] == "task_request.completed"
    assert payload["outbox"][0]["ref_id"] == request_id


@pytest.mark.parametrize("channel_code", ["noop", "console"])
def test_phase2_outbox_once_marks_noop_or_console_sent(runtime_db_url: str, channel_code: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url, source_channel_code=channel_code)
    request_id = str(submitted["request_id"])
    store = _load_store(runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="success",
        current_stage="completed",
        summary={"final_status": "success"},
        result={"normalized_product_result": {"product_id": DIRECT_PRODUCT_ID}},
    )
    runtime_orchestrator.ensure_request_outbox(store=store, request_id=request_id)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["channel_code"] == channel_code
    assert payload["item"]["status"] == "sent"
    assert payload["processed_count"] == 1
