from __future__ import annotations

import importlib

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.contracts.handler.api import (
    build_api_handler_registry,
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
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    extract_effective_result_payload,
)
from automation_business_scaffold.domains.tiktok.tasks.refresh_current_competitor_table import (
    RefreshCurrentCompetitorTableTask,
)
from automation_business_scaffold.infrastructure.artifacts.artifact_store import StoredArtifact
from automation_business_scaffold.infrastructure.facts.tk_fact_store import TKFactStore

REFRESH_TASK_CODE = "refresh_current_competitor_table"
SOURCE_TABLE_REF = "tbl_competitor_source"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"
SOURCE_RECORD_ID = "row-1"
row_handler_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.jobs.competitor_row_refresh"
)
row_flow_module = importlib.import_module(
    "automation_business_scaffold.domains.tiktok.flows.competitor_row_refresh.pipeline.finalization"
)


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


def _configure_project_runtime_env(monkeypatch: pytest.MonkeyPatch, runtime_db_url: str) -> None:
    monkeypatch.setenv("TK_FACT_DB_URL", runtime_db_url)
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER", "minio")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET", "pytest-runtime-artifacts")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT", "127.0.0.1:9000")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY", "miniosecret")


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
        normalized = context.payload.get("normalized_product_result")
        if isinstance(normalized, dict) and normalized:
            _emit_progress(context, "tiktok_request_fetch_from_browser_result")
            return HandlerResult.success(
                context,
                summary={"transport": "browser_after_fallback"},
                result={"normalized_product_result": normalized},
            )
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
                },
                next_action=HandlerNextAction(
                    type="browser_fallback",
                    payload={
                        "product_identity": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
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

    monkeypatch.setattr(row_flow_module, "tiktok_product_request_fetch_handler", fake_tiktok_product_request_fetch)
    monkeypatch.setattr(row_flow_module, "fastmoss_product_fetch_handler", fake_fastmoss_product_fetch)
    monkeypatch.setattr(row_flow_module, "media_asset_sync_handler", fake_media_asset_sync)
    monkeypatch.setattr(row_flow_module, "fact_bundle_upsert_handler", fake_fact_bundle_upsert)
    monkeypatch.setattr(row_flow_module, "feishu_table_write_handler", fake_feishu_table_write)

    register_api_handler(registry, "feishu_table_read", fake_feishu_table_read)
    register_api_handler(registry, "competitor_row_refresh", row_handler_module.competitor_row_refresh_handler)
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
        assert context.runtime_table == "task_execution"
        assert context.worker_type == "browser_worker"
        assert context.payload["stage_code"] == "browser_fallback"
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
    assert first_executor["request_status"] == "waiting"
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
    assert collect_wait["request_status"] == "waiting"
    assert collect_wait["current_stage"] == "collect_product_data"
    collect_job_codes = {
        str(job["job_code"])
        for job in _stage_jobs(collect_wait, stage_code="collect_product_data")
    }
    assert collect_job_codes == {"competitor_row_refresh"}

    row_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert row_worker["api_worker_job"]["job_code"] == "competitor_row_refresh"
    assert row_worker["api_worker_job"]["status"] == "finished"
    assert row_worker["api_worker_job"]["result_status"] == "success"
    assert row_worker["api_worker_job"]["result"]["row_status"] == "success"
    collect_status = _status(runtime_db_url, request_id)
    row_job = _stage_jobs(
        collect_status,
        stage_code="collect_product_data",
        job_code="competitor_row_refresh",
    )[0]
    assert row_job["status"] == "finished"
    assert row_job["result_status"] == "success"
    assert row_job["result"]["normalized_product_result"]["product_id"] == PRODUCT_ID
    assert row_job["result"]["product_fact_bundle"]["product_id"] == PRODUCT_ID
    assert row_job["result"]["writeback_result"]["written_count"] == 1

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
    } == {"competitor_row_refresh"}

    row_worker = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert row_worker["request_id"] == request_id
    assert row_worker["api_worker_job"]["job_code"] == "competitor_row_refresh"
    assert row_worker["api_worker_job"]["status"] == "waiting"
    assert row_worker["api_worker_job"]["result_status"] == ""
    assert row_worker["api_worker_job"]["result"]["handler_result"]["status"] == "fallback_required"
    assert row_worker["api_worker_job"]["result"]["runtime_evidence"]["browser_fallback_used"] is True

    fallback_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert fallback_wait["request_id"] == request_id
    assert fallback_wait["request_status"] == "waiting"
    assert fallback_wait["current_stage"] == "browser_fallback"
    fallback_executions = [
        execution
        for execution in fallback_wait.get("executions", [])
        if str((execution.get("payload") or {}).get("stage_code") or "") == "browser_fallback"
    ]
    assert len(fallback_executions) == 1
    assert fallback_executions[0]["item_code"] == "tiktok_product_browser_fetch"

    browser_worker = runtime_orchestrator.execute_browser_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert browser_worker["execution"]["item_code"] == "tiktok_product_browser_fetch"
    assert browser_worker["execution_status"] == "success"
    assert browser_worker["execution"]["payload"]["source_record_id"] == SOURCE_RECORD_ID
    assert browser_worker["parent_updates"] == [
            {
                "request_id": request_id,
                "stage_code": "browser_fallback",
                "released": True,
                "next_executor_status": "pending",
            }
        ]

    after_browser_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert after_browser_wait["request_id"] == request_id
    assert after_browser_wait["request_status"] == "waiting"
    assert after_browser_wait["current_stage"] == "collect_product_data"
    after_browser_jobs = [
        job
        for job in _stage_jobs(
            after_browser_wait,
            stage_code="collect_product_data",
            job_code="competitor_row_refresh",
        )
        if job["payload"].get("browser_fallback_resolved")
    ]
    assert len(after_browser_jobs) == 1
    assert after_browser_jobs[0]["payload"]["normalized_product_result"]["source"] == "browser"
    assert after_browser_jobs[0]["payload"]["force_fallback"] is False

    after_browser_row = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert after_browser_row["api_worker_job"]["payload"]["normalized_product_result"]["source"] == "browser"
    assert after_browser_row["api_worker_job"]["result"]["row_status"] == "success"

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["final_status"] == "success"

    status_payload = _status(runtime_db_url, request_id)
    row_result = status_payload["result"]["row_results"][0]
    assert row_result["row_status"] == "success"
    assert row_result["tiktok_status"] == "success"
    assert row_result["browser_status"] == "success"
    assert row_result["media_status"] == "success"
    assert row_result["fact_status"] == "success"
    assert row_result["writeback_status"] == "success"
    assert status_payload["result"]["stage_summary"]["collect_product_data"]["total_count"] == 1
    assert status_payload["result"]["stage_summary"]["browser_fallback"]["total_count"] == 1


def test_refresh_executor_real_business_e2e_with_bound_handlers(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _configure_project_runtime_env(monkeypatch, runtime_db_url)

    class FakeFeishuClient:
        rows: list[dict[str, object]] = []
        updated: list[dict[str, object]] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def list_records(self, app_token, table_id, **kwargs):
            assert app_token == "app-token"
            assert table_id == "tbl-token"
            assert kwargs["view_id"] == "vew-token"
            return {"data": {"items": list(self.rows), "has_more": False}}

        def update_record(self, app_token, table_id, record_id, fields):
            self.updated.append({"record_id": record_id, "fields": dict(fields)})
            for row in self.rows:
                if row["record_id"] == record_id:
                    row_fields = row.setdefault("fields", {})
                    assert isinstance(row_fields, dict)
                    row_fields.update(fields)
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

    FakeFeishuClient.rows = [
        {
            "record_id": SOURCE_RECORD_ID,
            "fields": {
                "产品链接": {"text": PRODUCT_URL, "link": PRODUCT_URL},
                "SKU-ID": PRODUCT_ID,
                "商品状态": "",
                "图片": "",
                "标题": "",
                "卖家": "",
                "价格": "",
                "Fastmoss价格": "",
                "昨日销量": "",
                "近7天销量": "",
                "近90天销量": "",
                "记录日期": "",
            },
        }
    ]
    FakeFeishuClient.updated = []
    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.input_sources.feishu.table_common.FeishuBitableClient",
        FakeFeishuClient,
    )
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", None, raising=False)

    class FakeArtifactStore:
        provider_code = "minio"

        def upload_file(self, *, bucket, object_key, local_path, content_type, metadata=None):
            return StoredArtifact(
                bucket=bucket,
                object_key=object_key,
                etag="pytest-etag",
                size=1,
                content_type=content_type,
                uri=f"minio://{bucket}/{object_key}",
                metadata={"remote_uri": f"minio://{bucket}/{object_key}", **dict(metadata or {})},
            )

        def build_uri(self, *, bucket, object_key):
            return f"minio://{bucket}/{object_key}"

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.media.asset_sync_handler.create_store_from_settings",
        lambda settings: FakeArtifactStore(),
    )

    class FakeImageResponse:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"fake-image-bytes"

    monkeypatch.setattr(
        "automation_business_scaffold.capabilities.media.asset_sync_handler.urlopen",
        lambda request, timeout: FakeImageResponse(),
    )

    submitted = _submit_refresh_request(
        runtime_db_url,
        table_refs={
            SOURCE_TABLE_REF: {
                "app_token": "app-token",
                "table_id": "tbl-token",
                "view_id": "vew-token",
                "access_token": "access-token",
            }
        },
        field_names=[
            "产品链接",
            "SKU-ID",
            "商品状态",
            "图片",
            "标题",
            "卖家",
            "价格",
            "Fastmoss价格",
            "昨日销量",
            "近7天销量",
            "近90天销量",
            "记录日期",
        ],
        refresh_filter={
            "candidate_policy": "missing_auto_maintained_fields",
            "auto_fields": ["标题", "卖家", "Fastmoss价格", "昨日销量", "近7天销量", "近90天销量", "记录日期"],
            "skip_product_status": ["已下架/区域不可售"],
        },
        raw_request_result={
            "product": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
                "title": "Graduation Candy Boxes",
                "shop_name": "Party Supply Co",
                "main_image_url": "https://cdn.example.com/tiktok-main.jpg",
                "price_text": "$14.50",
            },
            "sku_list": [{"sku_id": "sku-blue", "sku_name": "Blue", "price": "$14.50", "stock": 7}],
        },
        fastmoss_bundle={
            "base": {
                "data": {
                    "product": {
                        "product_id": PRODUCT_ID,
                        "title": "Graduation Candy Boxes",
                        "real_price": "$14.50",
                        "img": "https://cdn.example.com/fastmoss-main.jpg",
                    },
                    "shop": {"seller_id": "seller-1", "name": "Party Supply Co", "region": "US"},
                }
            },
            "overview": {
                "data": {
                    "product_id": PRODUCT_ID,
                    "d_type": 28,
                    "overview": {
                        "real_price": "$14.50",
                        "yday_sold_count": 38,
                        "day7_sold_count": 412,
                        "day90_sold_count": 2310,
                    },
                    "chart_list": [{"dt": "2026-04-23", "inc_sold_count": 38, "inc_sale_amount": 551.0}],
                }
            },
            "skus": {
                "data": {
                    "product_id": PRODUCT_ID,
                    "d_type": 28,
                    "sku_list": [{"sku_id": "sku-blue", "sku_name": "Blue", "sold_count": 31, "stock": 7}],
                }
            },
        },
        fact_db_url=runtime_db_url,
        artifact_root=str(tmp_path),
        sync_referenced_files=True,
        require_materialized_assets=False,
        execution_child_runner_mode="inline",
    )
    request_id = str(submitted["request_id"])

    first_executor = runtime_orchestrator.execute_executor_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert first_executor["current_stage"] == "read_competitor_rows"
    read_worker = runtime_orchestrator.execute_api_worker_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert read_worker["api_worker_job"]["job_code"] == "feishu_table_read"
    assert read_worker["api_worker_job"]["status"] == "finished"
    assert read_worker["api_worker_job"]["result_status"] == "success"
    read_result = extract_effective_result_payload(read_worker["api_worker_job"])
    assert read_result["source_rows"][0]["source_record_id"] == SOURCE_RECORD_ID

    collect_wait = runtime_orchestrator.execute_executor_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert {str(job["job_code"]) for job in _stage_jobs(collect_wait, stage_code="collect_product_data")} == {
        "competitor_row_refresh",
    }
    row_worker = runtime_orchestrator.execute_api_worker_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert row_worker["api_worker_job"]["job_code"] == "competitor_row_refresh"
    assert row_worker["api_worker_job"]["status"] == "finished"
    assert row_worker["api_worker_job"]["result_status"] == "success"
    assert row_worker["api_worker_job"]["result"]["fact_upsert"]["persistence_mode"] == "database"
    assert TKFactStore(db_url=runtime_db_url).get_product(product_id=PRODUCT_ID)["title"] == "Graduation Candy Boxes"
    projection_fields = row_worker["api_worker_job"]["result"]["writeback_projection"]["fields"]
    assert projection_fields["Fastmoss价格"] == "14.5"
    assert projection_fields["近7天销量"] == "412"
    assert FakeFeishuClient.updated
    updated_fields = FakeFeishuClient.updated[0]["fields"]
    assert updated_fields["标题"] == "Graduation Candy Boxes"
    assert updated_fields["卖家"] == "Party Supply Co"
    assert updated_fields["Fastmoss价格"] == "14.5"
    assert updated_fields["昨日销量"] == "38"
    assert updated_fields["近7天销量"] == "412"
    assert updated_fields["近90天销量"] == "2310"
    assert "商品状态" not in updated_fields
    assert "产品链接" not in updated_fields
    assert "记录日期" in updated_fields

    finalized = runtime_orchestrator.execute_executor_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
