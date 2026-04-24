from __future__ import annotations

import pytest

from automation_business_scaffold.business.flows import runtime_orchestrator
from automation_business_scaffold.business.flows.runtime_workflow_registry import load_workflow_runtime
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
from automation_business_scaffold.business.tasks.refresh_current_competitor_table import (
    RefreshCurrentCompetitorTableTask,
)

REFRESH_TASK_CODE = "refresh_current_competitor_table"
SOURCE_TABLE_REF = "tbl_competitor_source"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"
SOURCE_RECORD_ID = "row-1"


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _submit_refresh_request(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    task = RefreshCurrentCompetitorTableTask()
    submit_params: dict[str, object] = {
        "control_action": "submit",
        "source_table_ref": SOURCE_TABLE_REF,
        "reply_target": "reply://refresh-executor",
        "source_channel_code": "console",
    }
    submit_params.update(overrides)
    return task.run_runtime_request(_runtime_params(runtime_db_url, **submit_params))


def _status(runtime_db_url: str, request_id: str) -> dict[str, object]:
    return runtime_orchestrator.get_task_request_status(
        REFRESH_TASK_CODE,
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )


def _stage_jobs(
    payload: dict[str, object],
    *,
    stage_code: str,
    job_code: str | None = None,
) -> list[dict[str, object]]:
    jobs = [
        job
        for job in payload.get("api_worker_jobs", [])
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]
    if job_code is not None:
        jobs = [job for job in jobs if str(job.get("job_code") or "") == job_code]
    return jobs


def _stage_executions(
    payload: dict[str, object],
    *,
    stage_code: str,
    item_code: str | None = None,
) -> list[dict[str, object]]:
    executions = [
        execution
        for execution in payload.get("executions", [])
        if str((execution.get("payload") or {}).get("stage_code") or "") == stage_code
    ]
    if item_code is not None:
        executions = [execution for execution in executions if str(execution.get("item_code") or "") == item_code]
    return executions


def _bind_refresh_api_handlers(monkeypatch: pytest.MonkeyPatch, *, request_mode: str) -> None:
    registry = build_api_handler_registry()

    def fake_feishu_table_read(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "feishu_table_read")
        return HandlerResult.success(
            context,
            summary={"rows": 1},
            result={
                "source_rows": [
                    {
                        "source_record_id": SOURCE_RECORD_ID,
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                    }
                ]
            },
        )

    def fake_tiktok_product_request_fetch(context: HandlerContext) -> HandlerResult:
        if request_mode == "fallback":
            _emit_progress(context, "tiktok_request_blocked")
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
                        "product_identity": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                        "fallback_source_job_id": context.job_id,
                    },
                ),
            )

        _emit_progress(context, "tiktok_request_fetch")
        return HandlerResult.success(
            context,
            summary={"transport": "request"},
            result={
                "normalized_product_result": {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "source": "request",
                    "media_assets": [
                        {
                            "source_url": "https://cdn.example.com/p1.jpg",
                            "source_type": "image",
                            "mime_type": "image/jpeg",
                        }
                    ],
                }
            },
        )

    def fake_fastmoss_product_fetch(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "fastmoss_product_fetch")
        return HandlerResult.success(
            context,
            summary={"transport": "fastmoss"},
            result={
                "product_fact_bundle": {
                    "product_id": PRODUCT_ID,
                    "gmv_currency": "USD",
                    "gmv_amount": 100,
                }
            },
        )

    def fake_media_asset_sync(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "media_asset_sync")
        return HandlerResult.success(
            context,
            summary={"synced": 1},
            result={"synced_assets": [{"source_url": "https://cdn.example.com/p1.jpg"}]},
        )

    def fake_fact_bundle_upsert(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "fact_bundle_upsert")
        return HandlerResult.success(
            context,
            summary={"upserted": 1},
            result={"upserted_entities": [PRODUCT_ID]},
        )

    def fake_feishu_table_write(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "feishu_table_write")
        records = list(context.payload.get("records") or [])
        return HandlerResult.success(
            context,
            summary={"written": len(records)},
            result={"written_count": len(records), "target_record_ids": [SOURCE_RECORD_ID]},
        )

    register_api_handler(registry, "feishu_table_read", fake_feishu_table_read)
    register_api_handler(registry, "tiktok_product_request_fetch", fake_tiktok_product_request_fetch)
    register_api_handler(registry, "fastmoss_product_fetch", fake_fastmoss_product_fetch)
    register_api_handler(registry, "media_asset_sync", fake_media_asset_sync)
    register_api_handler(registry, "fact_bundle_upsert", fake_fact_bundle_upsert)
    register_api_handler(registry, "feishu_table_write", fake_feishu_table_write)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def _bind_refresh_browser_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_browser_handler_registry()

    def fake_tiktok_product_browser_fetch(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "browser_fallback_collected")
        return HandlerResult.success(
            context,
            summary={"transport": "browser"},
            result={
                "normalized_product_result": {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "source": "browser",
                    "media_assets": [
                        {
                            "source_url": "https://cdn.example.com/p1-browser.jpg",
                            "source_type": "image",
                            "mime_type": "image/jpeg",
                        }
                    ],
                }
            },
        )

    register_browser_handler(registry, "tiktok_product_browser_fetch", fake_tiktok_product_browser_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_browser_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "BROWSER_HANDLER_REGISTRY", registry, raising=False)


def _emit_progress(context: HandlerContext, stage_code: str) -> None:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(stage_code, message=stage_code)


def test_refresh_executor_integration_request_first_success_path(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_workflow_runtime(REFRESH_TASK_CODE) is not None
    _bind_refresh_api_handlers(monkeypatch, request_mode="success")

    submitted = _submit_refresh_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    first_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert first_executor["request_id"] == request_id
    assert first_executor["request_status"] == "waiting_children"
    assert first_executor["current_stage"] == "read_competitor_rows"
    read_jobs = _stage_jobs(first_executor, stage_code="read_competitor_rows", job_code="feishu_table_read")
    assert len(read_jobs) == 1
    assert read_jobs[0]["payload"]["source_table_ref"] == SOURCE_TABLE_REF

    read_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert read_worker["request_id"] == request_id
    assert read_worker["api_worker_job"]["job_code"] == "feishu_table_read"
    assert read_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "read_competitor_rows",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

    collect_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert collect_wait["request_id"] == request_id
    assert collect_wait["request_status"] == "waiting_children"
    assert collect_wait["current_stage"] == "collect_product_data"
    collect_job_codes = {
        str(job["job_code"])
        for job in _stage_jobs(collect_wait, stage_code="collect_product_data")
    }
    assert collect_job_codes == {"tiktok_product_request_fetch", "fastmoss_product_fetch"}

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    collect_status = _status(runtime_db_url, request_id)
    tiktok_job = _stage_jobs(
        collect_status,
        stage_code="collect_product_data",
        job_code="tiktok_product_request_fetch",
    )[0]
    fastmoss_job = _stage_jobs(
        collect_status,
        stage_code="collect_product_data",
        job_code="fastmoss_product_fetch",
    )[0]
    assert tiktok_job["status"] == "success"
    assert tiktok_job["result"]["normalized_product_result"]["product_id"] == PRODUCT_ID
    assert fastmoss_job["status"] == "success"
    assert fastmoss_job["result"]["product_fact_bundle"]["product_id"] == PRODUCT_ID

    persist_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert persist_wait["request_status"] == "waiting_children"
    assert persist_wait["current_stage"] == "persist_facts"
    persist_job_codes = {
        str(job["job_code"])
        for job in _stage_jobs(persist_wait, stage_code="persist_facts")
    }
    assert persist_job_codes == {"media_asset_sync", "fact_bundle_upsert"}

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    writeback_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert writeback_wait["request_status"] == "waiting_children"
    assert writeback_wait["current_stage"] == "writeback_competitor_rows"
    writeback_jobs = _stage_jobs(
        writeback_wait,
        stage_code="writeback_competitor_rows",
        job_code="feishu_table_write",
    )
    assert len(writeback_jobs) == 1
    projection = writeback_jobs[0]["payload"]["records"][0]
    assert projection["source_record_id"] == SOURCE_RECORD_ID
    assert projection["refresh_status"] == "success"

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_total_count"] == 1
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"

    status_payload = _status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["current_stage"] == "ready_for_summary"
    assert status_payload["result"]["row_results"][0]["writeback_status"] == "success"


def test_refresh_executor_integration_browser_fallback_path(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_refresh_api_handlers(monkeypatch, request_mode="fallback")
    _bind_refresh_browser_handler(monkeypatch)

    submitted = _submit_refresh_request(runtime_db_url, reply_target="reply://refresh-browser")
    request_id = str(submitted["request_id"])

    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    collect_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert collect_wait["current_stage"] == "collect_product_data"
    assert {
        str(job["job_code"]) for job in _stage_jobs(collect_wait, stage_code="collect_product_data")
    } == {"tiktok_product_request_fetch", "fastmoss_product_fetch"}

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    browser_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert browser_wait["request_id"] == request_id
    assert browser_wait["request_status"] == "waiting_children"
    assert browser_wait["current_stage"] == "browser_fallback"
    browser_executions = _stage_executions(
        browser_wait,
        stage_code="browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )
    assert len(browser_executions) == 1
    assert browser_executions[0]["payload"]["source_record_id"] == SOURCE_RECORD_ID

    browser_payload = runtime_orchestrator.execute_browser_once(_runtime_params(runtime_db_url))
    assert browser_payload["request_id"] == request_id
    assert browser_payload["execution"]["status"] == "success"
    assert browser_payload["supervisor"]["worker_type"] == "browser_worker"
    assert browser_payload["supervisor"]["progress_stage"] == "browser_fallback_collected"
    assert browser_payload["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "browser_fallback",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

    persist_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert persist_wait["request_id"] == request_id
    assert persist_wait["request_status"] == "waiting_children"
    assert persist_wait["current_stage"] == "persist_facts"
    assert {
        str(job["job_code"]) for job in _stage_jobs(persist_wait, stage_code="persist_facts")
    } == {"media_asset_sync", "fact_bundle_upsert"}

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    writeback_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert writeback_wait["request_status"] == "waiting_children"
    assert writeback_wait["current_stage"] == "writeback_competitor_rows"
    assert len(
        _stage_jobs(
            writeback_wait,
            stage_code="writeback_competitor_rows",
            job_code="feishu_table_write",
        )
    ) == 1

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["final_status"] == "success"

    status_payload = _status(runtime_db_url, request_id)
    row_result = status_payload["result"]["row_results"][0]
    assert row_result["row_status"] == "success"
    assert row_result["tiktok_status"] == "fallback_required"
    assert row_result["browser_status"] == "success"
    assert row_result["fact_status"] == "success"
    assert row_result["writeback_status"] == "success"
    assert status_payload["result"]["stage_summary"]["persist_facts"]["total_count"] == 2
    assert status_payload["result"]["stage_summary"]["writeback_competitor_rows"]["total_count"] == 1
