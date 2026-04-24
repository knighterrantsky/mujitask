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
    build_bound_api_handler_registry,
    build_browser_handler_registry,
    register_api_handler,
    register_browser_handler,
)
from automation_business_scaffold.domains.competitor_intelligence.tasks.search_keyword_competitor_products import (
    SearchKeywordCompetitorProductsTask,
)

TASK_CODE = "search_keyword_competitor_products"
SEARCH_QUERY = "water bottle"
SEED_TABLE_REF = "tbl_keyword_seed"
SEED_RECORD_ID = "seed-row-1"
PRODUCT_ID = "123456789"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
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

    first_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert first_executor["request_id"] == request_id
    assert first_executor["request_status"] == "waiting_children"
    assert first_executor["current_stage"] == "search_product_candidates"
    search_jobs = _stage_jobs(first_executor, stage_code="search_product_candidates", job_code="fastmoss_product_search")
    assert len(search_jobs) == 1
    assert search_jobs[0]["payload"]["search_query"] == SEARCH_QUERY

    search_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert search_worker["request_id"] == request_id
    assert search_worker["api_worker_job"]["job_code"] == "fastmoss_product_search"
    assert search_worker["api_worker_job"]["status"] == "success"
    assert search_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "search_product_candidates",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

    insert_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert insert_wait["request_status"] == "waiting_children"
    assert insert_wait["current_stage"] == "insert_seed_rows"
    seed_jobs = _stage_jobs(insert_wait, stage_code="insert_seed_rows", job_code="feishu_table_write")
    assert len(seed_jobs) == 1
    seed_record = seed_jobs[0]["payload"]["records"][0]
    assert seed_record["product_id"] == PRODUCT_ID
    assert seed_record["search_query"] == SEARCH_QUERY

    seed_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert seed_worker["request_id"] == request_id
    assert seed_worker["api_worker_job"]["job_code"] == "feishu_table_write"
    assert seed_worker["api_worker_job"]["payload"]["stage_code"] == "insert_seed_rows"

    collect_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert collect_wait["request_status"] == "waiting_children"
    assert collect_wait["current_stage"] == "collect_product_data"
    collect_job_codes = {str(job["job_code"]) for job in _stage_jobs(collect_wait, stage_code="collect_product_data")}
    assert collect_job_codes == {"tiktok_product_request_fetch", "fastmoss_product_fetch"}
    collect_jobs = _stage_jobs(collect_wait, stage_code="collect_product_data")
    assert {str(job["payload"]["source_record_id"]) for job in collect_jobs} == {SEED_RECORD_ID}

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

    sync_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert sync_wait["request_status"] == "waiting_children"
    assert sync_wait["current_stage"] == "sync_media"
    media_jobs = _stage_jobs(sync_wait, stage_code="sync_media", job_code="media_asset_sync")
    assert len(media_jobs) == 1
    assert media_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID

    media_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert media_worker["api_worker_job"]["job_code"] == "media_asset_sync"
    assert media_worker["api_worker_job"]["status"] == "success"

    persist_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert persist_wait["request_status"] == "waiting_children"
    assert persist_wait["current_stage"] == "persist_facts"
    fact_jobs = _stage_jobs(persist_wait, stage_code="persist_facts", job_code="fact_bundle_upsert")
    assert len(fact_jobs) == 1
    assert fact_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID

    fact_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert fact_worker["api_worker_job"]["job_code"] == "fact_bundle_upsert"
    assert fact_worker["api_worker_job"]["status"] == "success"

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
    assert projection["source_record_id"] == SEED_RECORD_ID
    assert projection["refresh_status"] == "success"

    writeback_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert writeback_worker["api_worker_job"]["job_code"] == "feishu_table_write"
    assert writeback_worker["api_worker_job"]["payload"]["stage_code"] == "writeback_competitor_rows"
    assert writeback_worker["api_worker_job"]["status"] == "success"

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["candidate_total_count"] == 1
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"

    status_payload = _status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["current_stage"] == "ready_for_summary"
    assert status_payload["summary"]["final_status"] == "success"
    assert status_payload["result"]["row_results"][0]["writeback_status"] == "success"
    assert status_payload["result"]["stage_summary"]["sync_media"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["persist_facts"]["total_count"] == 1


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
        "automation_business_scaffold.business.feishu_common.FeishuBitableClient",
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

    search_wait = runtime_orchestrator.execute_executor_once(worker_params)
    assert search_wait["request_status"] == "waiting_children"
    assert search_wait["current_stage"] == "search_product_candidates"

    search_worker = runtime_orchestrator.execute_api_worker_once(worker_params)
    assert search_worker["api_worker_job"]["job_code"] == "fastmoss_product_search"
    assert search_worker["api_worker_job"]["status"] == "success"

    seed_wait = runtime_orchestrator.execute_executor_once(worker_params)
    assert seed_wait["request_status"] == "waiting_children"
    assert seed_wait["current_stage"] == "insert_seed_rows"
    seed_jobs = _stage_jobs(seed_wait, stage_code="insert_seed_rows", job_code="feishu_table_write")
    assert len(seed_jobs) == 1
    assert seed_jobs[0]["payload"]["mapper_code"] == "competitor_seed_projection_mapper"
    assert seed_jobs[0]["payload"]["write_mode"] == "insert_if_absent"
    assert seed_jobs[0]["payload"]["request_payload"]["table_refs"][SEED_TABLE_REF]["table_id"] == "tbl-token"

    seed_worker = runtime_orchestrator.execute_api_worker_once(worker_params)
    assert seed_worker["request_id"] == request_id
    assert seed_worker["api_worker_job"]["job_code"] == "feishu_table_write"
    assert seed_worker["api_worker_job"]["payload"]["stage_code"] == "insert_seed_rows"
    assert seed_worker["api_worker_job"]["summary"]["written_count"] == 1
    assert seed_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "insert_seed_rows",
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
        "关键词": SEARCH_QUERY,
        "备注": f"通过搜索关键字：{SEARCH_QUERY}",
        "达人查找状态": "待查找",
    }


def test_keyword_executor_integration_browser_fallback_path(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_keyword_api_handlers(monkeypatch, request_mode="fallback")
    _bind_keyword_browser_handler(monkeypatch)

    submitted = _submit_keyword_request(runtime_db_url, reply_target="reply://keyword-browser")
    request_id = str(submitted["request_id"])

    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    collect_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert collect_wait["request_status"] == "waiting_children"
    assert collect_wait["current_stage"] == "collect_product_data"
    assert {str(job["job_code"]) for job in _stage_jobs(collect_wait, stage_code="collect_product_data")} == {
        "tiktok_product_request_fetch",
        "fastmoss_product_fetch",
    }

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
    assert browser_executions[0]["payload"]["source_record_id"] == SEED_RECORD_ID

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

    sync_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert sync_wait["request_status"] == "waiting_children"
    assert sync_wait["current_stage"] == "sync_media"
    media_jobs = _stage_jobs(sync_wait, stage_code="sync_media", job_code="media_asset_sync")
    assert len(media_jobs) == 1
    assert media_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID

    runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    persist_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert persist_wait["request_status"] == "waiting_children"
    assert persist_wait["current_stage"] == "persist_facts"
    fact_jobs = _stage_jobs(persist_wait, stage_code="persist_facts", job_code="fact_bundle_upsert")
    assert len(fact_jobs) == 1
    assert fact_jobs[0]["payload"]["source_record_id"] == SEED_RECORD_ID

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
    assert writeback_jobs[0]["payload"]["records"][0]["refresh_status"] == "success"

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
    assert status_payload["result"]["stage_summary"]["browser_fallback"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["sync_media"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["persist_facts"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["writeback_competitor_rows"]["total_count"] == 1
    assert len(status_payload["outbox"]) == 1
    assert status_payload["outbox"][0]["event_type"] == "task_request.completed"
