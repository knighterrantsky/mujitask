from __future__ import annotations

import importlib

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.contracts.handler.api import (
    build_api_handler_registry,
    build_bound_api_handler_registry,
    register_api_handler,
)
from automation_business_scaffold.contracts.handler.browser import (
    build_browser_handler_registry,
    register_browser_handler,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerNextAction,
    HandlerResult,
)
from automation_business_scaffold.domains.tiktok.tasks.search_keyword_competitor_products import (
    SearchKeywordCompetitorProductsTask,
)
from automation_business_scaffold.domains.tiktok.tasks.search_keyword_selection_products import (
    SearchKeywordSelectionProductsTask,
)

TASK_CODE = "search_keyword_competitor_products"
SELECTION_TASK_CODE = "search_keyword_selection_products"
SEARCH_QUERY = "water bottle"
SEED_TABLE_REF = "tbl_keyword_seed"
SELECTION_TABLE_REF = "tbl_selection_keyword_seed"
SEED_RECORD_ID = "seed-row-1"
PRODUCT_ID = "123456789"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": "pytest-runtime-artifacts",
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _submit_keyword_request(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    task = SearchKeywordCompetitorProductsTask()
    submit_params: dict[str, object] = {
        "control_action": "submit",
        "search_query": SEARCH_QUERY,
        "filters": {"country_code": "US"},
        "output_conditions": {"require_product_url": True},
        "max_candidates": 5,
        "seed_table_ref": SEED_TABLE_REF,
        "reply_target": "reply://keyword-executor",
        "source_channel_code": "console",
    }
    submit_params.update(overrides)
    return task.run_runtime_request(_runtime_params(runtime_db_url, **submit_params))


def _status(runtime_db_url: str, request_id: str) -> dict[str, object]:
    return runtime_orchestrator.get_task_request_status(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )


def _selection_status(runtime_db_url: str, request_id: str) -> dict[str, object]:
    return runtime_orchestrator.get_task_request_status(
        SELECTION_TASK_CODE,
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


def _emit_progress(context: HandlerContext, stage_code: str) -> None:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback(stage_code, message=stage_code)


def _bind_keyword_api_handlers(monkeypatch: pytest.MonkeyPatch, *, request_mode: str) -> None:
    registry = build_api_handler_registry()

    def fake_fastmoss_product_search(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "fastmoss_product_search")
        return HandlerResult.success(
            context,
            summary={"candidates": 1},
            result={
                "candidates": [
                    {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "rank": 1,
                        "title": "Water bottle",
                    }
                ],
                "condition_context": {"normalized": True},
            },
        )

    def fake_feishu_table_write(context: HandlerContext) -> HandlerResult:
        stage_code = str(context.payload.get("stage_code") or context.stage_code or "")
        _emit_progress(context, stage_code or "feishu_table_write")
        records = list(context.payload.get("records") or [])
        target_record_ids = [SEED_RECORD_ID] if stage_code == "insert_seed_rows" else [SEED_RECORD_ID for _ in records]
        return HandlerResult.success(
            context,
            summary={"written": len(records), "stage_code": stage_code},
            result={
                "written_count": len(records),
                "target_record_ids": target_record_ids,
                "records": records,
                "stage_code": stage_code,
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

    def fake_keyword_seed_import(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "keyword_seed_import")
        return HandlerResult.success(
            context,
            summary={"candidate_count": 1, "written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={
                "search_parameters": {
                    "search_query": SEARCH_QUERY,
                    "filters": {"country_code": "US"},
                    "output_conditions": {"require_product_url": True},
                    "condition_context": {"business_conditions": {}},
                    "sort": {"field": "day7_sold_count", "direction": "desc", "source_order": "2,2"},
                    "pagination": {"page": 1, "page_size": 10, "max_pages": 50, "stop_when_no_new_product": True},
                },
                "normalized_candidates": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "search_query": SEARCH_QUERY,
                        "search_rank": 1,
                        "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                    }
                ],
                "seed_contexts": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "search_query": SEARCH_QUERY,
                        "search_rank": 1,
                        "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                        "source_record_id": SEED_RECORD_ID,
                        "seed_status": "success",
                        "feishu_row": {
                            "record_id": SEED_RECORD_ID,
                            "status": "success",
                            "op": "append",
                            "fields": {
                                "SKU-ID": PRODUCT_ID,
                                "产品链接": {"text": PRODUCT_URL, "link": PRODUCT_URL},
                                "备注": f"通过搜索关键字：{SEARCH_QUERY}",
                            },
                        },
                        "target_record_ids": [SEED_RECORD_ID],
                    }
                ],
                "seed_write_results": [
                    {
                        "product_id": PRODUCT_ID,
                        "source_record_id": SEED_RECORD_ID,
                        "status": "success",
                        "feishu_row": {
                            "record_id": SEED_RECORD_ID,
                            "status": "success",
                            "op": "append",
                            "fields": {
                                "SKU-ID": PRODUCT_ID,
                                "产品链接": {"text": PRODUCT_URL, "link": PRODUCT_URL},
                                "备注": f"通过搜索关键字：{SEARCH_QUERY}",
                            },
                        },
                    }
                ],
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": [SEED_RECORD_ID],
            },
        )

    def fake_competitor_row_refresh(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "competitor_row_refresh")
        browser_status = "success" if request_mode == "fallback" else "skipped"
        tiktok_status = "fallback_required" if request_mode == "fallback" else "success"
        return HandlerResult.success(
            context,
            summary={"row_status": "success"},
            result={
                "row_status": "success",
                "step_timeline": [
                    {"step": "tiktok_request", "status": tiktok_status},
                    {"step": "browser_fallback", "status": browser_status},
                    {"step": "media_sync", "status": "success"},
                    {"step": "fastmoss_fetch", "status": "success"},
                    {"step": "fact_db_upsert", "status": "success"},
                    {"step": "feishu_writeback", "status": "success"},
                ],
            },
        )

    register_api_handler(registry, "keyword_seed_import", fake_keyword_seed_import)
    register_api_handler(registry, "competitor_row_refresh", fake_competitor_row_refresh)
    register_api_handler(registry, "fastmoss_product_search", fake_fastmoss_product_search)
    register_api_handler(registry, "feishu_table_write", fake_feishu_table_write)
    register_api_handler(registry, "tiktok_product_request_fetch", fake_tiktok_product_request_fetch)
    register_api_handler(registry, "fastmoss_product_fetch", fake_fastmoss_product_fetch)
    register_api_handler(registry, "media_asset_sync", fake_media_asset_sync)
    register_api_handler(registry, "fact_bundle_upsert", fake_fact_bundle_upsert)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def _bind_keyword_browser_handler(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_keyword_executor_integration_happy_path(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_workflow_runtime(TASK_CODE) is not None
    _bind_keyword_api_handlers(monkeypatch, request_mode="success")

    submitted = _submit_keyword_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    seed_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert seed_wait["request_id"] == request_id
    assert seed_wait["request_status"] == "waiting_children"
    assert seed_wait["current_stage"] == "keyword_seed_import"
    seed_import_jobs = _stage_jobs(seed_wait, stage_code="keyword_seed_import", job_code="keyword_seed_import")
    assert len(seed_import_jobs) == 1
    assert seed_import_jobs[0]["payload"]["search_request"]["search_query"] == SEARCH_QUERY
    assert seed_import_jobs[0]["payload"]["seed_write"]["mapper_code"] == "competitor_seed_projection_mapper"

    seed_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert seed_worker["request_id"] == request_id
    assert seed_worker["api_worker_job"]["job_code"] == "keyword_seed_import"
    assert seed_worker["api_worker_job"]["status"] == "success"
    assert seed_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "keyword_seed_import",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

    refresh_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert refresh_wait["request_status"] == "waiting_children"
    assert refresh_wait["current_stage"] == "refresh_competitor_rows"
    row_jobs = _stage_jobs(refresh_wait, stage_code="refresh_competitor_rows", job_code="competitor_row_refresh")
    assert len(row_jobs) == 1
    assert row_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID
    assert row_jobs[0]["payload"]["product_identity"]["product_id"] == PRODUCT_ID

    row_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert row_worker["request_id"] == request_id
    assert row_worker["api_worker_job"]["job_code"] == "competitor_row_refresh"
    assert row_worker["api_worker_job"]["status"] == "success"

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["summary"]["search_filter_info"]["filters"] == {"country_code": "US"}
    assert finalized["result"]["candidate_total_count"] == 1
    assert finalized["result"]["search_parameters"]["search_query"] == SEARCH_QUERY
    assert finalized["result"]["seed_write_results"][0]["feishu_row"]["record_id"] == SEED_RECORD_ID
    assert finalized["result"]["seed_write_results"][0]["feishu_row"]["fields"]["SKU-ID"] == PRODUCT_ID
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["result"]["row_results"][0]["feishu_row"]["record_id"] == SEED_RECORD_ID
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"

    status_payload = _status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["current_stage"] == "ready_for_summary"
    assert status_payload["summary"]["final_status"] == "success"
    assert status_payload["result"]["row_results"][0]["writeback_status"] == "success"
    assert status_payload["result"]["row_results"][0]["feishu_row"]["fields"]["备注"] == f"通过搜索关键字：{SEARCH_QUERY}"
    assert status_payload["result"]["stage_summary"]["keyword_seed_import"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["refresh_competitor_rows"]["total_count"] == 1


def test_selection_keyword_executor_dispatches_selection_row_refresh(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_workflow_runtime(SELECTION_TASK_CODE) is not None
    registry = build_api_handler_registry()

    def fake_keyword_seed_import(context: HandlerContext) -> HandlerResult:
        search_request = dict(context.payload.get("search_request") or {})
        assert context.payload["seed_write"]["mapper_code"] == "selection_seed_projection_mapper"
        assert search_request["keyword_workflow_mode"] == "selection"
        assert search_request["output_conditions"]["business_conditions"] == {
            "min_day7_sold_count": "500",
            "min_price_range_max_amount": "10.99",
        }
        return HandlerResult.success(
            context,
            summary={"candidate_count": 1, "written_count": 1, "skipped_count": 0, "failed_count": 0},
            result={
                "search_parameters": search_request,
                "normalized_candidates": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "search_query": SEARCH_QUERY,
                        "search_rank": 1,
                        "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                    }
                ],
                "seed_contexts": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "search_query": SEARCH_QUERY,
                        "search_rank": 1,
                        "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                        "source_record_id": SEED_RECORD_ID,
                        "seed_status": "success",
                        "feishu_row": {
                            "record_id": SEED_RECORD_ID,
                            "status": "success",
                            "op": "append",
                            "fields": {
                                "商品ID": PRODUCT_ID,
                                "商品链接": {"text": PRODUCT_URL, "link": PRODUCT_URL},
                                "关键词": SEARCH_QUERY,
                                "备注": f"通过搜索关键字：{SEARCH_QUERY}",
                            },
                        },
                        "target_record_ids": [SEED_RECORD_ID],
                    }
                ],
                "seed_write_results": [
                    {
                        "product_id": PRODUCT_ID,
                        "source_record_id": SEED_RECORD_ID,
                        "status": "success",
                    }
                ],
                "written_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
                "target_record_ids": [SEED_RECORD_ID],
            },
        )

    def fake_selection_row_refresh(context: HandlerContext) -> HandlerResult:
        assert context.payload["source_record_id"] == SEED_RECORD_ID
        assert context.payload["source_table_ref"] == SELECTION_TABLE_REF
        assert context.payload["target_table_ref"] == SELECTION_TABLE_REF
        assert context.payload["product_identity"]["product_id"] == PRODUCT_ID
        return HandlerResult.success(
            context,
            summary={"row_status": "success"},
            result={
                "row_status": "success",
                "step_timeline": [
                    {"step": "tiktok_request", "status": "success"},
                    {"step": "fastmoss_fetch", "status": "success"},
                    {"step": "fact_db_upsert", "status": "success"},
                    {"step": "feishu_writeback", "status": "success"},
                ],
            },
        )

    register_api_handler(registry, "keyword_seed_import", fake_keyword_seed_import)
    register_api_handler(registry, "selection_row_refresh", fake_selection_row_refresh)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)

    task = SearchKeywordSelectionProductsTask()
    submitted = task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            search_query=SEARCH_QUERY,
            selection_table_ref=SELECTION_TABLE_REF,
            reply_target="reply://selection-keyword-executor",
            source_channel_code="console",
        )
    )
    request_id = str(submitted["request_id"])

    seed_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    seed_jobs = _stage_jobs(seed_wait, stage_code="keyword_seed_import", job_code="keyword_seed_import")
    assert len(seed_jobs) == 1
    assert seed_jobs[0]["payload"]["seed_write"]["target_table_ref"] == SELECTION_TABLE_REF
    assert seed_jobs[0]["payload"]["seed_write"]["mapper_code"] == "selection_seed_projection_mapper"

    seed_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert seed_worker["api_worker_job"]["job_code"] == "keyword_seed_import"
    assert seed_worker["api_worker_job"]["status"] == "success"

    refresh_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert refresh_wait["current_stage"] == "refresh_selection_rows"
    row_jobs = _stage_jobs(refresh_wait, stage_code="refresh_selection_rows", job_code="selection_row_refresh")
    assert len(row_jobs) == 1
    assert row_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID
    assert row_jobs[0]["payload"]["target_table_ref"] == SELECTION_TABLE_REF

    row_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert row_worker["api_worker_job"]["job_code"] == "selection_row_refresh"
    assert row_worker["api_worker_job"]["status"] == "success"

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["result"]["stage_summary"]["refresh_selection_rows"]["total_count"] == 1
    message_text = finalized["outbox"][0]["payload"]["message_text"]
    assert "关键词选品入库完成" in message_text
    assert f"关键词：{SEARCH_QUERY}" in message_text
    assert "候选：1 条" in message_text
    assert "详情成功：1 条" in message_text
    assert f"1. SKU {PRODUCT_ID}" in message_text

    status_payload = _selection_status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["result"]["row_results"][0]["feishu_row"]["fields"]["备注"] == f"通过搜索关键字：{SEARCH_QUERY}"


def test_selection_keyword_executor_dispatches_row_browser_fallback_task_execution(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_api_handler_registry()
    browser_registry = build_browser_handler_registry()

    def fake_keyword_seed_import(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            summary={"candidate_count": 1, "written_count": 1},
            result={
                "search_parameters": dict(context.payload.get("search_request") or {}),
                "normalized_candidates": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "source_context": {"product_id": PRODUCT_ID},
                    }
                ],
                "seed_contexts": [
                    {
                        "candidate_key": f"product:{PRODUCT_ID}",
                        "business_entity_key": f"product:{PRODUCT_ID}",
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                            "normalized_product_url": PRODUCT_URL,
                        },
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                        "source_record_id": SEED_RECORD_ID,
                        "seed_status": "success",
                        "feishu_row": {"record_id": SEED_RECORD_ID, "fields": {"商品ID": PRODUCT_ID}},
                        "target_record_ids": [SEED_RECORD_ID],
                    }
                ],
                "seed_write_results": [{"source_record_id": SEED_RECORD_ID, "status": "success"}],
            },
        )

    def fake_selection_row_refresh(context: HandlerContext) -> HandlerResult:
        if context.payload["stage_code"] == "refresh_selection_rows":
            error = HandlerError(
                error_type="browser_fallback_required",
                error_code="tiktok_product_browser_fetch_required",
                message="browser fallback required",
                retryable=False,
                fallback_allowed=True,
                fallback_reason="request_blocked",
            )
            browser_payload = {
                "product_identity": dict(context.payload["product_identity"]),
                "normalized_product_url": context.payload["normalized_product_url"],
                "source_record_id": context.payload["source_record_id"],
                "fallback_source_job_id": context.job_id,
            }
            return HandlerResult.fallback_required(
                context,
                error=error,
                summary={
                    "row_status": "fallback_required",
                    "fallback_required": True,
                    "fallback_handler": "tiktok_product_browser_fetch",
                },
                result={
                    "source_record_id": context.payload["source_record_id"],
                    "business_entity_key": context.payload["business_key"],
                    "row_status": "fallback_required",
                    "fallback_required": True,
                    "fallback_handler": "tiktok_product_browser_fetch",
                    "fallback_reason": "request_blocked",
                    "browser_fallback_payload": browser_payload,
                    "step_timeline": [
                        {"step": "tiktok_request", "status": "fallback_required"},
                        {"step": "browser_fallback", "status": "fallback_required"},
                    ],
                    "runtime_evidence": {"browser_fallback_used": True},
                },
                next_action=HandlerNextAction(type="browser_fallback", payload=browser_payload),
            )
        assert context.payload["stage_code"] == "resume_selection_rows_after_browser_fallback"
        assert context.payload["normalized_product_result"]["source"] == "browser"
        return HandlerResult.success(
            context,
            summary={"row_status": "success"},
            result={
                "row_status": "success",
                "step_timeline": [
                    {"step": "tiktok_request", "status": "success"},
                    {"step": "browser_fallback", "status": "success"},
                    {"step": "media_sync", "status": "success"},
                    {"step": "fastmoss_fetch", "status": "success"},
                    {"step": "fact_db_upsert", "status": "success"},
                    {"step": "feishu_writeback", "status": "success"},
                ],
            },
        )

    def fake_tiktok_product_browser_fetch(context: HandlerContext) -> HandlerResult:
        assert context.runtime_table == "task_execution"
        assert context.worker_type == "browser_worker"
        assert context.payload["stage_code"] == "selection_row_browser_fallback"
        return HandlerResult.success(
            context,
            summary={"transport": "browser"},
            result={
                "normalized_product_result": {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "source": "browser",
                }
            },
        )

    register_api_handler(registry, "keyword_seed_import", fake_keyword_seed_import)
    register_api_handler(registry, "selection_row_refresh", fake_selection_row_refresh)
    register_browser_handler(browser_registry, "tiktok_product_browser_fetch", fake_tiktok_product_browser_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "build_browser_handler_registry", lambda: browser_registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "BROWSER_HANDLER_REGISTRY", browser_registry, raising=False)

    task = SearchKeywordSelectionProductsTask()
    submitted = task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            search_query=SEARCH_QUERY,
            selection_table_ref=SELECTION_TABLE_REF,
            reply_target="reply://selection-browser-fallback",
            source_channel_code="console",
        )
    )
    request_id = str(submitted["request_id"])

    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    first_row = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert first_row["api_worker_job"]["status"] == "success"
    assert first_row["api_worker_job"]["result"]["handler_result"]["status"] == "fallback_required"

    fallback_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert fallback_wait["current_stage"] == "selection_row_browser_fallback"
    status_after_fallback_wait = _selection_status(runtime_db_url, request_id)
    assert status_after_fallback_wait["current_stage"] == "selection_row_browser_fallback"
    fallback_executions = _stage_executions(
        fallback_wait,
        stage_code="selection_row_browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )
    assert len(fallback_executions) == 1

    browser_worker = runtime_orchestrator.execute_browser_once(_runtime_params(runtime_db_url))
    assert browser_worker["execution"]["item_code"] == "tiktok_product_browser_fetch"
    assert browser_worker["execution_status"] == "success"
    assert browser_worker["execution"]["payload"]["source_record_id"] == SEED_RECORD_ID
    assert browser_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "resume_selection_rows_after_browser_fallback",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    status_after_browser = _selection_status(runtime_db_url, request_id)
    assert status_after_browser["current_stage"] == "resume_selection_rows_after_browser_fallback"
    stored_execution = _stage_executions(
        status_after_browser,
        stage_code="selection_row_browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )[0]
    assert stored_execution["result"]["handler_result"]["result"]["normalized_product_result"]["source"] == "browser"
    selection_runtime = importlib.import_module(
        "automation_business_scaffold.domains.tiktok.flows.search_keyword_selection_products"
    )
    settings = runtime_orchestrator.build_runtime_settings(_runtime_params(runtime_db_url))
    store = runtime_orchestrator.create_runtime_store(settings)
    assert selection_runtime._selection_row_browser_fallback_candidates(  # noqa: SLF001
        store=store,
        request_id=request_id,
    )
    assert selection_runtime._selection_row_browser_resume_candidates(  # noqa: SLF001
        store=store,
        request_id=request_id,
    )
    resume_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert resume_wait["current_stage"] == "resume_selection_rows_after_browser_fallback"
    resume_jobs = _stage_jobs(
        resume_wait,
        stage_code="resume_selection_rows_after_browser_fallback",
        job_code="selection_row_refresh",
    )
    assert len(resume_jobs) == 1

    resumed_row = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert resumed_row["api_worker_job"]["payload"]["normalized_product_result"]["source"] == "browser"
    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["result"]["row_results"][0]["browser_status"] == "success"


def test_keyword_executor_passes_zero_candidate_limit_to_seed_import(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_workflow_runtime(TASK_CODE) is not None
    _bind_keyword_api_handlers(monkeypatch, request_mode="success")

    submitted = _submit_keyword_request(runtime_db_url, max_candidates=0)
    request_id = str(submitted["request_id"])

    seed_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert seed_wait["request_id"] == request_id
    seed_jobs = _stage_jobs(seed_wait, stage_code="keyword_seed_import", job_code="keyword_seed_import")
    assert len(seed_jobs) == 1
    assert seed_jobs[0]["payload"]["search_request"]["limit"] == 0
    assert seed_jobs[0]["payload"]["search_request"]["output_conditions"]["max_candidates"] == 0


def test_keyword_search_seed_e2e_writes_competitor_seed_row(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        created: list[dict[str, object]] = []
        rows: list[dict[str, object]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_all_records(self, app_token, table_id, page_size=100, view_id=None):
            return list(self.rows)

        def create_record(self, app_token, table_id, fields):
            record_id = f"rec-seed-{len(self.rows) + 1}"
            self.created.append({"record_id": record_id, "fields": dict(fields)})
            self.rows.append({"record_id": record_id, "fields": dict(fields)})
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

        def update_record(self, app_token, table_id, record_id, fields):
            raise AssertionError("keyword seed e2e should create new rows, not update existing rows")

    FakeClient.created = []
    FakeClient.rows = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeClient,
    )

    registry = build_bound_api_handler_registry()

    def fake_fastmoss_product_search(context: HandlerContext) -> HandlerResult:
        _emit_progress(context, "fastmoss_product_search")
        return HandlerResult.success(
            context,
            summary={"candidates": 1},
            result={
                "candidates": [
                    {
                        "product_id": PRODUCT_ID,
                        "product_url": "https://www.fastmoss.com/zh/e-commerce/detail/123456789",
                        "rank": 1,
                        "title": "Water bottle",
                    }
                ],
                "condition_context": {"normalized": True},
            },
        )

    register_api_handler(registry, "fastmoss_product_search", fake_fastmoss_product_search, replace=True)
    keyword_seed_import_flow = importlib.import_module(
        "automation_business_scaffold.domains.tiktok.flows.keyword_seed_import"
    )
    monkeypatch.setattr(keyword_seed_import_flow, "fastmoss_product_search_handler", fake_fastmoss_product_search)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)

    submitted = _submit_keyword_request(
        runtime_db_url,
        seed_table_ref=SEED_TABLE_REF,
        table_refs={
            SEED_TABLE_REF: {
                "app_token": "app-token",
                "table_id": "tbl-token",
                "view_id": "vew-token",
                "access_token": "access-token",
            }
        },
    )
    request_id = str(submitted["request_id"])
    worker_params = _runtime_params(runtime_db_url, execution_child_runner_mode="inline")

    seed_wait = runtime_orchestrator.execute_executor_once(worker_params)
    assert seed_wait["request_status"] == "waiting_children"
    assert seed_wait["current_stage"] == "keyword_seed_import"
    seed_jobs = _stage_jobs(seed_wait, stage_code="keyword_seed_import", job_code="keyword_seed_import")
    assert len(seed_jobs) == 1
    assert seed_jobs[0]["payload"]["seed_write"]["mapper_code"] == "competitor_seed_projection_mapper"
    assert seed_jobs[0]["payload"]["seed_write"]["write_mode"] == "insert_if_absent"

    seed_worker = runtime_orchestrator.execute_api_worker_once(worker_params)
    assert seed_worker["request_id"] == request_id
    assert seed_worker["api_worker_job"]["job_code"] == "keyword_seed_import"
    assert seed_worker["api_worker_job"]["payload"]["stage_code"] == "keyword_seed_import"
    assert seed_worker["api_worker_job"]["summary"]["written_count"] == 1
    assert seed_worker["api_worker_job"]["result"]["seed_write_results"][0]["feishu_row"]["record_id"] == "rec-seed-1"
    assert seed_worker["api_worker_job"]["result"]["seed_write_results"][0]["feishu_row"]["fields"] == {
        "SKU-ID": PRODUCT_ID,
        "产品链接": {
            "text": PRODUCT_URL,
            "link": PRODUCT_URL,
        },
        "备注": f"通过搜索关键字：{SEARCH_QUERY}",
    }
    assert seed_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "keyword_seed_import",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    assert FakeClient.created[0]["fields"] == {
        "SKU-ID": PRODUCT_ID,
        "产品链接": {
            "text": PRODUCT_URL,
            "link": PRODUCT_URL,
        },
        "备注": f"通过搜索关键字：{SEARCH_QUERY}",
    }


def test_keyword_executor_integration_browser_fallback_path(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_keyword_api_handlers(monkeypatch, request_mode="fallback")

    submitted = _submit_keyword_request(runtime_db_url, reply_target="reply://keyword-browser")
    request_id = str(submitted["request_id"])

    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    refresh_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert refresh_wait["request_status"] == "waiting_children"
    assert refresh_wait["current_stage"] == "refresh_competitor_rows"
    row_jobs = _stage_jobs(refresh_wait, stage_code="refresh_competitor_rows", job_code="competitor_row_refresh")
    assert len(row_jobs) == 1
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
    assert status_payload["result"]["stage_summary"]["keyword_seed_import"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["refresh_competitor_rows"]["total_count"] == 1
    assert len(status_payload["outbox"]) == 1
    assert status_payload["outbox"][0]["event_type"] == "task_request.completed"
