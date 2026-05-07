from __future__ import annotations
from typing import Iterable

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool.context.models import (
    DISCOVER_CREATORS_STAGE_CODE,
    READ_STAGE_CODE,
    SYNC_INFLUENCER_POOL_STAGE_CODE,
    WRITEBACK_STAGE_CODE,
)
from automation_business_scaffold.contracts.handler.api import (
    build_api_handler_registry,
    register_api_handler,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerError,
    HandlerResult,
)
from automation_business_scaffold.domains.tiktok.tasks.sync_tk_influencer_pool import (
    SyncTKInfluencerPoolTask,
)

TASK_CODE = "sync_tk_influencer_pool"
SOURCE_TABLE_REF = "feishu://competitor-table"
INFLUENCER_POOL_TABLE_REF = "feishu://influencer-pool-table"
COMPETITOR_STATUS_TABLE_REF = "feishu://competitor-status-table"
SOURCE_RECORD_ID = "row-1"
PRODUCT_ID = "product-1"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/10001"


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


def _submit_influencer_request(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    task = SyncTKInfluencerPoolTask()
    submit_params: dict[str, object] = {
        "control_action": "submit",
        "source_table_ref": SOURCE_TABLE_REF,
        "influencer_pool_table_ref": INFLUENCER_POOL_TABLE_REF,
        "competitor_status_table_ref": COMPETITOR_STATUS_TABLE_REF,
        "source_record_ids": [SOURCE_RECORD_ID],
        "source_channel_code": "console",
        "reply_target": "reply://influencer-executor",
    }
    submit_params.update(overrides)
    return task.run_runtime_request(_runtime_params(runtime_db_url, **submit_params))


def _status(runtime_db_url: str, request_id: str) -> dict[str, object]:
    return runtime_orchestrator.get_task_request_status(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="status", request_id=request_id),
    )


def _jobs_for_stage(payload: dict[str, object], stage_code: str, job_code: str = "") -> list[dict[str, object]]:
    jobs = []
    for job in payload.get("api_worker_jobs", []):
        if not isinstance(job, dict):
            continue
        job_payload = dict(job.get("payload") or {})
        if str(job_payload.get("stage_code") or "") != stage_code:
            continue
        if job_code and str(job.get("job_code") or "") != job_code:
            continue
        jobs.append(job)
    return jobs


def _api_job_by_creator_id(payload: dict[str, object], creator_id: str) -> dict[str, object]:
    for job in payload.get("api_worker_jobs", []):
        if not isinstance(job, dict):
            continue
        job_payload = dict(job.get("payload") or {})
        creator_identity = dict(job_payload.get("creator_identity") or {})
        if str(creator_identity.get("creator_id") or "") == creator_id:
            return job
    return {}


def _bind_influencer_api_handlers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    creator_failure_ids: Iterable[str] = (),
) -> None:
    registry = build_api_handler_registry()
    failure_ids = {str(value) for value in creator_failure_ids}

    def fake_feishu_table_read(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("read_candidates", message="feishu candidate rows loaded")
        return HandlerResult.success(
            context,
            summary={"source": "feishu", "row_count": 1},
            result={
                "source_rows": [
                    {
                        "source_record_id": SOURCE_RECORD_ID,
                        "product_id": PRODUCT_ID,
                        "product_identity": {
                            "product_id": PRODUCT_ID,
                            "product_url": PRODUCT_URL,
                        },
                    }
                ]
            },
        )

    def fake_product_creator_discovery(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("discover_creators", message="fastmoss related creators loaded")
        creator_candidates = [
            {
                "creator_id": "creator-success",
                "creator_identity": {"creator_id": "creator-success"},
                "display_name": "Alice",
            }
        ]
        if failure_ids:
            creator_candidates.append(
                {
                    "creator_id": "creator-fail",
                    "creator_identity": {"creator_id": "creator-fail"},
                    "display_name": "Bob",
                }
            )
        return HandlerResult.success(
            context,
            summary={"source": "fastmoss_product", "creator_candidate_count": len(creator_candidates)},
            result={
                "product_fact_bundle": {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                },
                "normalized_creator_candidates": creator_candidates,
                "related_creators": creator_candidates,
                "product_hit_context": {
                    "source_record_id": SOURCE_RECORD_ID,
                    "product_id": PRODUCT_ID,
                    "matched_creator_count": len(creator_candidates),
                },
            },
        )

    def fake_influencer_creator_sync(context: HandlerContext) -> HandlerResult:
        creator_identity = dict(context.payload.get("creator_identity") or {})
        creator_id = str(creator_identity.get("creator_id") or "")
        progress_callback = context.metadata.get("progress_callback")
        if callable(progress_callback):
            progress_callback("sync_influencer_pool", message=f"creator sync {creator_id}")
        if creator_id in failure_ids:
            return HandlerResult.fallback_required(
                context,
                error=HandlerError(
                    error_type="upstream",
                    error_code="creator_profile_blocked",
                    message=f"creator detail unavailable for {creator_id}",
                    retryable=False,
                    fallback_allowed=True,
                    fallback_reason="creator_detail_missing",
                ),
                summary={"creator_id": creator_id, "source": "fastmoss_creator"},
                result={
                    "creator_id": creator_id,
                    "status": "failed",
                    "product_hits": list(context.payload.get("product_hits") or []),
                },
            )
        return HandlerResult.success(
            context,
            summary={"creator_id": creator_id, "source": "fastmoss_creator"},
            result={
                "creator_id": creator_id,
                "status": "success",
                "internal_steps": {"creator_fetch": "success", "fact_upsert": "success", "influencer_pool_write": "success"},
                "creator_fact_bundle": {"creator_id": creator_id, "display_name": "Alice" if creator_id == "creator-success" else creator_id},
                "influencer_pool_write": {"status": "success", "write_result": {"written_count": 1}},
                "product_hits": list(context.payload.get("product_hits") or []),
            },
        )

    def fake_feishu_table_write(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata.get("progress_callback")
        stage_code = str(context.payload.get("stage_code") or context.stage_code or "")
        if callable(progress_callback):
            progress_callback(stage_code or "write_records", message=f"feishu write for {stage_code}")
        records = list(context.payload.get("records") or [])
        return HandlerResult.success(
            context,
            summary={"stage_code": stage_code, "record_count": len(records)},
            result={
                "stage_code": stage_code,
                "written_count": len(records),
                "target_record_ids": [f"{stage_code}-row-{index + 1}" for index, _ in enumerate(records)],
                "records": records,
            },
        )

    register_api_handler(registry, "feishu_table_read", fake_feishu_table_read)
    register_api_handler(registry, "product_creator_discovery", fake_product_creator_discovery)
    register_api_handler(registry, "influencer_creator_sync", fake_influencer_creator_sync)
    register_api_handler(registry, "feishu_table_write", fake_feishu_table_write)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def test_sync_tk_influencer_pool_executor_happy_path_through_runtime_registry(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_influencer_api_handlers(monkeypatch)
    submitted = _submit_influencer_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    first_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert first_executor["request_id"] == request_id
    assert first_executor["request_status"] == "waiting_children"
    assert first_executor["current_stage"] == READ_STAGE_CODE
    assert {job["job_code"] for job in first_executor["api_worker_jobs"]} == {"feishu_table_read"}

    read_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert read_job["request_id"] == request_id
    assert read_job["api_worker_job"]["job_code"] == "feishu_table_read"
    assert read_job["api_worker_job"]["status"] == "success"

    discover_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert discover_executor["request_id"] == request_id
    assert discover_executor["request_status"] == "waiting_children"
    assert discover_executor["current_stage"] == DISCOVER_CREATORS_STAGE_CODE
    product_jobs = _jobs_for_stage(discover_executor, DISCOVER_CREATORS_STAGE_CODE, "product_creator_discovery")
    assert len(product_jobs) == 1
    assert product_jobs[0]["payload"]["product_identity"]["product_id"] == PRODUCT_ID

    product_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert product_job["request_id"] == request_id
    assert product_job["api_worker_job"]["job_code"] == "product_creator_discovery"
    assert product_job["api_worker_job"]["status"] == "success"

    creator_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert creator_executor["request_id"] == request_id
    assert creator_executor["request_status"] == "waiting_children"
    assert creator_executor["current_stage"] == SYNC_INFLUENCER_POOL_STAGE_CODE
    creator_jobs = _jobs_for_stage(creator_executor, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")
    assert len(creator_jobs) == 1
    assert creator_jobs[0]["payload"]["creator_identity"]["creator_id"] == "creator-success"

    creator_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert creator_job["request_id"] == request_id
    assert creator_job["api_worker_job"]["job_code"] == "influencer_creator_sync"
    assert creator_job["api_worker_job"]["status"] == "success"

    writeback_executor = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert writeback_executor["request_id"] == request_id
    assert writeback_executor["request_status"] == "waiting_children"
    assert writeback_executor["current_stage"] == WRITEBACK_STAGE_CODE
    writeback_jobs = _jobs_for_stage(writeback_executor, WRITEBACK_STAGE_CODE, "feishu_table_write")
    assert len(writeback_jobs) == 1
    assert writeback_jobs[0]["payload"]["records"][0]["influencer_sync_status"] == "success"

    writeback_job = runtime_orchestrator.execute_api_worker_once(_runtime_params(runtime_db_url))
    assert writeback_job["request_id"] == request_id
    assert writeback_job["api_worker_job"]["job_code"] == "feishu_table_write"
    assert writeback_job["api_worker_job"]["status"] == "success"
    assert writeback_job["api_worker_job"]["result"]["stage_code"] == WRITEBACK_STAGE_CODE

    finalized = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))
    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "completed"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["summary"]["product_group_status_counts"] == {"success": 1}

    status_payload = _status(runtime_db_url, request_id)
    assert status_payload["request_status"] == "success"
    assert status_payload["current_stage"] == "completed"
    assert status_payload["summary"]["final_status"] == "success"
    assert status_payload["summary"]["product_group_count"] == 1
    assert status_payload["summary"]["product_groups"][0]["creator_detail_success_count"] == 1
    assert _jobs_for_stage(status_payload, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")[0]["status"] == "success"
    assert len(status_payload["outbox"]) == 1
    assert status_payload["outbox"][0]["event_type"] == "task_request.completed"


def test_sync_tk_influencer_pool_executor_aggregates_partial_success_after_child_failure(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_influencer_api_handlers(monkeypatch, creator_failure_ids={"creator-fail"})
    runtime_params = _runtime_params(runtime_db_url)
    submitted = _submit_influencer_request(runtime_db_url)
    request_id = str(submitted["request_id"])

    runtime_orchestrator.execute_executor_once(runtime_params)
    runtime_orchestrator.execute_api_worker_once(runtime_params)
    runtime_orchestrator.execute_executor_once(runtime_params)
    runtime_orchestrator.execute_api_worker_once(runtime_params)

    creator_dispatch = runtime_orchestrator.execute_executor_once(runtime_params)
    creator_jobs = _jobs_for_stage(creator_dispatch, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")
    assert len(creator_jobs) == 2

    first_creator_job = runtime_orchestrator.execute_api_worker_once(runtime_params)
    second_creator_job = runtime_orchestrator.execute_api_worker_once(runtime_params)
    creator_statuses = {
        first_creator_job["api_worker_job"]["payload"]["creator_identity"]["creator_id"]: first_creator_job["worker_result"]["status"],
        second_creator_job["api_worker_job"]["payload"]["creator_identity"]["creator_id"]: second_creator_job["worker_result"]["status"],
    }
    assert creator_statuses["creator-success"] == "success"
    assert creator_statuses["creator-fail"] == "fallback_required"

    writeback_executor = runtime_orchestrator.execute_executor_once(runtime_params)
    assert writeback_executor["current_stage"] == WRITEBACK_STAGE_CODE
    writeback_jobs = _jobs_for_stage(writeback_executor, WRITEBACK_STAGE_CODE, "feishu_table_write")
    assert len(writeback_jobs) == 1
    assert writeback_jobs[0]["payload"]["records"][0]["influencer_sync_status"] == "partial_success"

    runtime_orchestrator.execute_api_worker_once(runtime_params)
    finalized = runtime_orchestrator.execute_executor_once(runtime_params)

    assert finalized["request_id"] == request_id
    assert finalized["request_status"] == "partial_success"
    assert finalized["current_stage"] == "completed"
    assert finalized["summary"]["final_status"] == "partial_success"
    assert finalized["summary"]["product_group_status_counts"] == {"partial_success": 1}

    status_payload = _status(runtime_db_url, request_id)
    creator_detail_jobs = _jobs_for_stage(status_payload, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")
    assert {job["status"] for job in creator_detail_jobs} == {"success"}
    assert status_payload["request_status"] == "partial_success"
    assert status_payload["summary"]["warnings"] == ["partial_creator_projection"]
    assert status_payload["summary"]["product_groups"][0]["creator_detail_success_count"] == 1
    assert status_payload["summary"]["product_groups"][0]["creator_detail_failed_count"] == 1
    assert status_payload["summary"]["product_groups"][0]["influencer_write_success_count"] == 1
    assert _api_job_by_creator_id(status_payload, "creator-fail")["result"]["handler_result"]["status"] == (
        "fallback_required"
    )
    assert _jobs_for_stage(status_payload, SYNC_INFLUENCER_POOL_STAGE_CODE, "influencer_creator_sync")[0]["status"] == "success"
    assert len(status_payload["outbox"]) == 1
    assert status_payload["outbox"][0]["event_type"] == "task_request.completed"
