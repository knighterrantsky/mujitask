from __future__ import annotations

import json

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
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
from automation_business_scaffold.domains.tiktok.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

DIRECT_PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123"
DIRECT_PRODUCT_ID = "123"


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


def _submit_ingest_request(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    task = TikTokFastMossProductIngestTask()
    submit_params: dict[str, object] = {
        "control_action": "submit",
        "product_url": DIRECT_PRODUCT_URL,
        "product_id": DIRECT_PRODUCT_ID,
        "fallback_allowed": True,
        "source_channel_code": "console",
        "reply_target": "reply://product_fact",
    }
    submit_params.update(overrides)
    params = _runtime_params(runtime_db_url, **submit_params)
    return task.run_runtime_request(params)


def _load_store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _payload_contains_product_ref(payload: dict[str, object]) -> bool:
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return DIRECT_PRODUCT_URL in payload_text or DIRECT_PRODUCT_ID in payload_text


def _stage_jobs(
    payload: dict[str, object],
    *,
    stage_code: str,
    job_code: str = "",
) -> list[dict[str, object]]:
    jobs = payload.get("api_worker_jobs", [])
    assert isinstance(jobs, list)
    return [
        job
        for job in jobs
        if isinstance(job, dict)
        and str((job.get("payload") or {}).get("stage_code") or "") == stage_code
        and (not job_code or str(job.get("job_code") or "") == job_code)
    ]


def _stage_executions(
    payload: dict[str, object],
    *,
    stage_code: str,
    item_code: str = "",
) -> list[dict[str, object]]:
    executions = payload.get("executions", [])
    assert isinstance(executions, list)
    return [
        execution
        for execution in executions
        if isinstance(execution, dict)
        and str((execution.get("payload") or {}).get("stage_code") or "") == stage_code
        and (not item_code or str(execution.get("item_code") or "") == item_code)
    ]


def _bind_api_handlers_for_success(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_api_handler_registry()

    def fake_selection_row_refresh(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("selection_row_refresh", message="selection row refreshed")
        return HandlerResult.success(
            context,
            summary={
                "source_record_id": str(context.payload.get("source_record_id") or ""),
                "product_business_key": DIRECT_PRODUCT_ID,
                "row_status": "success",
                "browser_fallback_used": False,
            },
            result={
                "source_record_id": str(context.payload.get("source_record_id") or ""),
                "business_entity_key": DIRECT_PRODUCT_ID,
                "row_status": "success",
                "runtime_evidence": {"browser_fallback_used": False},
            },
        )

    register_api_handler(registry, "selection_row_refresh", fake_selection_row_refresh)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def _bind_api_handlers_for_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_api_handler_registry()

    def fake_selection_row_refresh(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("selection_row_refresh", message="selection row refresh")
        if context.payload["stage_code"] == "collect_selection_rows":
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
                "normalized_product_url": DIRECT_PRODUCT_URL,
                "source_record_id": str(context.payload.get("source_record_id") or ""),
                "fallback_source_job_id": context.job_id,
            }
            return HandlerResult.fallback_required(
                context,
                error=error,
                summary={
                    "source_record_id": str(context.payload.get("source_record_id") or ""),
                    "product_business_key": DIRECT_PRODUCT_ID,
                    "row_status": "fallback_required",
                    "fallback_required": True,
                    "fallback_handler": "tiktok_product_browser_fetch",
                    "browser_fallback_used": True,
                },
                result={
                    "source_record_id": str(context.payload.get("source_record_id") or ""),
                    "business_entity_key": DIRECT_PRODUCT_ID,
                    "row_status": "fallback_required",
                    "fallback_required": True,
                    "fallback_handler": "tiktok_product_browser_fetch",
                    "fallback_reason": "request_blocked",
                    "browser_fallback_payload": browser_payload,
                    "runtime_evidence": {"browser_fallback_used": True},
                },
                next_action=HandlerNextAction(type="browser_fallback", payload=browser_payload),
            )
        assert context.payload["stage_code"] == "resume_selection_rows_after_browser_fallback"
        assert context.payload["normalized_product_result"]["source"] == "browser"
        if callable(progress_callback):
            progress_callback("selection_row_refresh", message="browser fallback collected product")
        return HandlerResult.success(
            context,
            summary={
                "source_record_id": str(context.payload.get("source_record_id") or ""),
                "product_business_key": DIRECT_PRODUCT_ID,
                "row_status": "success",
                "browser_fallback_used": True,
            },
            result={
                "source_record_id": str(context.payload.get("source_record_id") or ""),
                "business_entity_key": DIRECT_PRODUCT_ID,
                "row_status": "success",
                "runtime_evidence": {"browser_fallback_used": True},
            },
        )

    register_api_handler(registry, "selection_row_refresh", fake_selection_row_refresh)
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


def test_product_fact_submit_rejects_missing_strict_persistence_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BUSINESS_EXECUTION_CONTROL_DB_URL",
        "EXECUTION_CONTROL_DB_URL",
        "TK_FACT_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL",
        "EXECUTION_CONTROL_FACT_DB_URL",
        "FACT_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
        "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER",
        "BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT",
        "EXECUTION_CONTROL_MINIO_ENDPOINT",
        "BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY",
        "EXECUTION_CONTROL_MINIO_ACCESS_KEY",
        "BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY",
        "EXECUTION_CONTROL_MINIO_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    task = TikTokFastMossProductIngestTask()
    payload = task.run_runtime_request(
        {
            "control_action": "submit",
            "product_url": DIRECT_PRODUCT_URL,
            "product_id": DIRECT_PRODUCT_ID,
        }
    )

    assert payload["request_status"] == "rejected"
    assert payload["error_code"] == "strict_persistence_config_missing"
    assert "Fact DB URL" in payload["missing_required_config"]
    assert "object storage provider" in payload["missing_required_config"]


def test_product_fact_submit_rejects_runtime_config_payload_without_test_override(runtime_db_url: str) -> None:
    task = TikTokFastMossProductIngestTask()

    payload = task.run_runtime_request(
        {
            "control_action": "submit",
            "execution_control_db_url": runtime_db_url,
            "fact_db_url": runtime_db_url,
            "execution_control_artifact_store_provider": "minio",
            "execution_control_artifact_bucket": "pytest-runtime-artifacts",
            "execution_control_minio_endpoint": "127.0.0.1:9000",
            "execution_control_minio_access_key": "minioadmin",
            "execution_control_minio_secret_key": "miniosecret",
            "product_url": DIRECT_PRODUCT_URL,
            "product_id": DIRECT_PRODUCT_ID,
        }
    )

    assert payload["request_status"] == "rejected"
    assert payload["error_code"] == "strict_persistence_config_missing"
    assert "execution_control_db_url" in payload["forbidden_runtime_config_fields"]
    assert "fact_db_url" in payload["forbidden_runtime_config_fields"]


def test_product_fact_submit_persists_strict_persistence_summary_into_request_payload(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    store = _load_store(runtime_db_url)

    stored_payload = store.load_task_request(request_id=request_id).payload

    assert stored_payload["requires_fact_db"] is True
    assert stored_payload["requires_object_storage"] is True
    assert stored_payload["require_database_persistence"] is True
    assert stored_payload["require_object_storage"] is True
    assert stored_payload["runtime_config_source"] == "test_submit_override"
    assert stored_payload["persistence"]["fact_db_configured"] is True
    assert stored_payload["persistence"]["runtime_db_configured"] is True
    assert stored_payload["artifact_store"]["provider"] == "minio"
    assert stored_payload["artifact_store"]["bucket"] == "pytest-runtime-artifacts"
    assert "fact_db_url" not in stored_payload
    assert "execution_control_db_url" not in stored_payload
    assert "minio_secret_key" not in stored_payload


def test_product_fact_submit_then_executor_dispatches_direct_ingest_jobs(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    payload = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["request_status"] == "waiting_children"
    assert payload["current_stage"] == "collect_selection_rows"
    job_codes = {job["job_code"] for job in payload["api_worker_jobs"]}
    assert job_codes == {"selection_row_refresh"}
    assert "feishu_table_read" not in job_codes

    row_job = next(job for job in payload["api_worker_jobs"] if job["job_code"] == "selection_row_refresh")
    assert _payload_contains_product_ref(row_job["payload"])
    assert row_job["payload"]["stage_code"] == "collect_selection_rows"


def test_product_fact_api_worker_once_dispatches_registry_and_persists_results(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    _bind_api_handlers_for_success(monkeypatch)

    first_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))

    assert first_job["request_id"] == request_id
    assert first_job["supervisor"]["worker_type"] == "api_worker"

    status_payload = runtime_orchestrator.get_task_request_status(
        "tiktok_fastmoss_product_ingest",
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )
    jobs_by_code = {job["job_code"]: job for job in status_payload["api_worker_jobs"]}

    assert jobs_by_code["selection_row_refresh"]["status"] == "success"
    assert jobs_by_code["selection_row_refresh"]["result"]["handler_result"]["result"]["row_status"] == "success"
    assert jobs_by_code["selection_row_refresh"]["summary"]["progress_stage"] == "selection_row_refresh"
    assert jobs_by_code["selection_row_refresh"]["result"]["supervisor"]["progress_stage"] == "selection_row_refresh"


def test_product_fact_browser_fallback_path_from_request_handler(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    _bind_api_handlers_for_browser_fallback(monkeypatch)
    _bind_browser_fallback_handler(monkeypatch)

    first_row = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert first_row["api_worker_job"]["status"] == "success"
    assert first_row["api_worker_job"]["result"]["handler_result"]["status"] == "fallback_required"

    fallback_wait = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert fallback_wait["request_id"] == request_id
    assert fallback_wait["request_status"] == "waiting_children"
    assert fallback_wait["current_stage"] == "selection_row_browser_fallback"
    fallback_executions = _stage_executions(
        fallback_wait,
        stage_code="selection_row_browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )
    assert len(fallback_executions) == 1

    browser_worker = runtime_orchestrator.execute_browser_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )
    assert browser_worker["execution"]["item_code"] == "tiktok_product_browser_fetch"
    assert browser_worker["execution_status"] == "success"
    assert browser_worker["parent_updates"] == [
        {
            "request_id": request_id,
            "stage_code": "resume_selection_rows_after_browser_fallback",
            "released": True,
            "next_executor_status": "pending",
        }
    ]

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
    assert finalized["current_stage"] == "completed"
    refreshed = runtime_orchestrator.get_task_request_status(
        "tiktok_fastmoss_product_ingest",
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )
    row_job = next(
        item
        for item in refreshed["api_worker_jobs"]
        if item["job_code"] == "selection_row_refresh"
        and item["payload"]["stage_code"] == "resume_selection_rows_after_browser_fallback"
    )
    assert row_job["status"] == "success"
    assert row_job["result"]["handler_result"]["summary"]["browser_fallback_used"] is True
    assert refreshed["result"]["rows"][0]["row_status"] == "success"


def test_product_fact_resume_stage_backfills_missing_resume_jobs(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    store = _load_store(runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="waiting_children",
        current_stage="resume_selection_rows_after_browser_fallback",
    )
    product_ids = ("123", "456")
    collect_enqueue = store.enqueue_api_worker_jobs(
        request_id=request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="selection_row_refresh",
        jobs=[
            {
                "business_key": product_id,
                "dedupe_key": f"{request_id}:collect_selection_rows:row-{product_id}",
                "payload": {
                    "request_id": request_id,
                    "task_code": "tiktok_fastmoss_product_ingest",
                    "workflow_code": "tiktok_fastmoss_product_ingest",
                    "stage_code": "collect_selection_rows",
                    "source_record_id": f"row-{product_id}",
                    "product_identity": {
                        "product_id": product_id,
                        "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                    },
                },
            }
            for product_id in product_ids
        ],
    )
    for created in collect_enqueue["created_records"]:
        claimed = store.claim_next_api_worker_job(worker_id="pytest-api", lease_seconds=30.0)
        assert claimed is not None
        product_id = str(claimed["business_key"])
        store.mark_api_worker_job_success(
            job_id=claimed["job_id"],
            run_id=claimed["run_id"],
            summary={"row_status": "fallback_required", "fallback_handler": "tiktok_product_browser_fetch"},
            result={
                "handler_result": {
                    "status": "fallback_required",
                    "summary": {
                        "row_status": "fallback_required",
                        "fallback_handler": "tiktok_product_browser_fetch",
                    },
                    "result": {
                        "source_record_id": f"row-{product_id}",
                        "business_entity_key": product_id,
                        "row_status": "fallback_required",
                        "fallback_required": True,
                        "fallback_handler": "tiktok_product_browser_fetch",
                        "browser_fallback_payload": {
                            "source_record_id": f"row-{product_id}",
                            "business_entity_key": product_id,
                            "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                            "product_identity": {
                                "product_id": product_id,
                                "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                            },
                            "fallback_source_job_id": claimed["job_id"],
                        },
                    },
                    "next_action": {"type": "browser_fallback", "payload": {}},
                }
            },
        )
    store.enqueue_task_executions(
        request_id=request_id,
        item_code="tiktok_product_browser_fetch",
        workflow_code="tiktok_fastmoss_product_ingest",
        items=[
            {
                "business_key": f"https://www.tiktok.com/shop/pdp/{product_id}",
                "dedupe_key": f"{request_id}:selection_row_browser_fallback:{product_id}",
                "payload": {
                    "stage_code": "selection_row_browser_fallback",
                    "source_record_id": f"row-{product_id}",
                    "business_entity_key": product_id,
                    "fallback_handler": "tiktok_product_browser_fetch",
                    "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                    "product_identity": {
                        "product_id": product_id,
                        "normalized_product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                    },
                },
            }
            for product_id in product_ids
        ],
    )
    for product_id in product_ids:
        claimed_execution = store.claim_next_browser_execution(
            worker_id="pytest-browser",
            lease_seconds=30.0,
            item_codes=("tiktok_product_browser_fetch",),
        )
        assert claimed_execution is not None
        store.mark_browser_execution_success(
            execution_id=claimed_execution.execution_id,
            run_id=claimed_execution.run_id,
            summary={"transport": "browser"},
            result={
                "handler_result": {
                    "status": "success",
                    "summary": {"transport": "browser"},
                    "result": {
                        "normalized_product_result": {
                            "product_id": product_id,
                            "product_url": f"https://www.tiktok.com/shop/pdp/{product_id}",
                            "source": "browser",
                        }
                    },
                }
            },
        )
    from automation_business_scaffold.domains.tiktok.flows.tiktok_fastmoss_product_ingest.context.runtime_views import (
        _selection_row_browser_resume_candidates,
    )
    from automation_business_scaffold.domains.tiktok.flows.tiktok_fastmoss_product_ingest.context.stage_inputs import (
        _selection_row_resume_job,
    )
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

    request = store.load_task_request(request_id=request_id)
    workflow = get_workflow_definition("tiktok_fastmoss_product_ingest")
    row_job_def = workflow.require_job("selection_row_refresh")
    candidates = _selection_row_browser_resume_candidates(  # noqa: SLF001
        store=store,
        request_id=request_id,
    )
    assert len(candidates) == 2
    existing_resume = store.enqueue_api_worker_jobs(
        request_id=request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="selection_row_refresh",
        jobs=[
            _selection_row_resume_job(  # noqa: SLF001
                request=request,
                workflow=workflow,
                stage_code="resume_selection_rows_after_browser_fallback",
                row_job_def=row_job_def,
                candidate=candidates[0],
            )
        ],
    )
    assert existing_resume["created_count"] == 1
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage="resume_selection_rows_after_browser_fallback",
    )

    payload = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    assert payload["current_stage"] == "resume_selection_rows_after_browser_fallback"
    assert payload.get("created_count") == 1, payload
    status_payload = runtime_orchestrator.get_task_request_status(
        "tiktok_fastmoss_product_ingest",
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )
    resume_jobs = _stage_jobs(
        status_payload,
        stage_code="resume_selection_rows_after_browser_fallback",
        job_code="selection_row_refresh",
    )
    assert len(resume_jobs) == 2
    assert {job["payload"]["source_record_id"] for job in resume_jobs} == {"row-123", "row-456"}


def test_product_fact_final_executor_once_summarizes_and_creates_notification_outbox(runtime_db_url: str) -> None:
    submitted = _submit_ingest_request(runtime_db_url)
    request_id = str(submitted["request_id"])
    store = _load_store(runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="waiting_children",
        current_stage="collect_selection_rows",
    )
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="selection_row_refresh",
        jobs=[
            {
                "business_key": DIRECT_PRODUCT_ID,
                "dedupe_key": f"{request_id}:collect_selection_rows:{DIRECT_PRODUCT_ID}",
                "payload": {
                    "request_id": request_id,
                    "task_code": "tiktok_fastmoss_product_ingest",
                    "workflow_code": "tiktok_fastmoss_product_ingest",
                    "stage_code": "collect_selection_rows",
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
            "handler_result": {
                "status": "success",
                "summary": {
                    "source_record_id": "",
                    "product_business_key": DIRECT_PRODUCT_ID,
                    "row_status": "success",
                },
                "result": {"row_status": "success"},
            },
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
def test_product_fact_outbox_once_marks_noop_or_console_sent(runtime_db_url: str, channel_code: str) -> None:
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
