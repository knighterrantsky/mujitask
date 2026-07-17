from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.control_plane.executor.runner import (
    run_task_request,
)
from automation_business_scaffold.infrastructure.facts.amazon_fact_store import (
    AmazonFactSchemaUnavailableError,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


TASK_CODE = "refresh_amazon_product_row_by_asin"
TABLE_REF = "AMAZON_PRODUCTS"
SOURCE_RECORD_ID = "rec-amazon-1"
ASIN = "B0ABC12345"
ARTIFACT_PREFIX = "pytest-amazon"
ROW_STATUS_CODES = (
    "success",
    "partial_success",
    "unavailable",
    "blocked",
    "failed",
    "skipped",
)
TOP_LEVEL_SUMMARY_FIELDS = {
    "final_status",
    "row_total_count",
    "row_status_counts",
    "aggregate_metrics",
    "row_summary",
    "failed_stage",
    "error_code",
}
AGGREGATE_METRIC_FIELDS = {
    "average_row_duration_ms",
    "max_row_duration_ms",
    "blocked_rate",
    "average_parse_coverage_percentage",
    "media_failure_rate",
    "feishu_failure_rate",
}


def _assert_top_level_summary(
    summary: dict[str, Any],
    *,
    final_status: str,
    row_status: str,
) -> None:
    assert set(summary) == TOP_LEVEL_SUMMARY_FIELDS
    assert summary["final_status"] == final_status
    assert summary["row_total_count"] == 1
    assert summary["row_status_counts"] == {
        status: int(status == row_status) for status in ROW_STATUS_CODES
    }
    assert set(summary["aggregate_metrics"]) == AGGREGATE_METRIC_FIELDS


@pytest.fixture(autouse=True)
def _resolved_browser_target(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_orchestrator,
        "resolve_automation_browser_target_digest",
        lambda *, profile_ref: "digest-only",
    )


def _runtime_params(runtime_db_url: str, **overrides: Any) -> dict[str, Any]:
    params = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": "pytest-amazon-artifacts",
        "execution_control_artifact_object_prefix": ARTIFACT_PREFIX,
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
        "execution_control_db_health_preflight_enabled": False,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _submit(runtime_db_url: str) -> tuple[RuntimeStore, str]:
    submitted = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id=SOURCE_RECORD_ID,
        ),
    )
    request_id = str(submitted["request_id"])
    assert request_id
    return RuntimeStore(db_url=runtime_db_url), request_id


def _executor(runtime_db_url: str) -> dict[str, Any]:
    return run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )


def _stage_jobs(
    store: RuntimeStore,
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _mark_api_success(
    store: RuntimeStore,
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
    result: dict[str, Any],
) -> None:
    jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code=stage_code,
        job_code=job_code,
    )
    assert len(jobs) == 1
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code=job_code,
    )
    assert claimed is not None
    assert claimed["job_id"] == jobs[0]["job_id"]
    store.mark_api_worker_job_success(
        job_id=str(claimed["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={"stage_code": stage_code},
        result=result,
    )


def _mark_status_writeback_success(
    store: RuntimeStore,
    *,
    request_id: str,
    stage_code: str,
    row_status: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jobs = [
        job
        for job in _stage_jobs(
            store,
            request_id=request_id,
            stage_code=stage_code,
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == "amazon_stage_status"
        and str((job.get("payload") or {}).get("row_status") or "") == row_status
    ]
    assert len(jobs) == 1
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code="feishu_table_write",
    )
    assert claimed is not None
    assert claimed["job_id"] == jobs[0]["job_id"]
    store.mark_api_worker_job_success(
        job_id=str(claimed["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={"stage_code": stage_code},
        result=result
        or {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )
    return jobs[0]


def _set_api_job_duration(
    runtime_db_url: str,
    *,
    job_id: str,
    duration_ms: float,
) -> None:
    started_at = 1_000_000.0
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE api_worker_job "
                    "SET started_at = :started_at, finished_at = :finished_at "
                    "WHERE job_id = :job_id"
                ),
                {
                    "job_id": job_id,
                    "started_at": started_at,
                    "finished_at": started_at + (duration_ms / 1_000.0),
                },
            )
    finally:
        engine.dispose()


def _read_result(*, lookup_status: str = "matched") -> dict[str, Any]:
    source_rows = []
    if lookup_status == "matched":
        source_rows = [
            {
                "source_record_id": SOURCE_RECORD_ID,
                "source_table_ref": TABLE_REF,
                "business_key": f"amazon:US:{ASIN}",
                "requested_asin": ASIN,
                "canonical_url": f"https://www.amazon.com/dp/{ASIN}",
                "product_identity": {
                    "marketplace_code": "US",
                    "asin": ASIN,
                    "canonical_url": f"https://www.amazon.com/dp/{ASIN}",
                },
            }
        ]
    return {
        "source_rows": source_rows,
        "adapter_summary": {
            "lookup_status": lookup_status,
            "matched_row_count": len(source_rows),
        },
        "source_table_identity": {"base_id": "app-amazon", "table_id": "tbl-amazon"},
    }


def _browser_result(
    *,
    request_id: str = "1" * 32,
    execution_id: str = "2" * 32,
    capture_run_id: str = "3" * 64,
    artifact_object_prefix: str = ARTIFACT_PREFIX,
    requested_asin: str = ASIN,
    resolved_asin: str = ASIN,
    parent_asin: str = "",
    collection_status: str = "success",
) -> dict[str, Any]:
    object_prefix = f"{artifact_object_prefix.strip('/')}/" if artifact_object_prefix else ""
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "pytest-amazon-artifacts",
        "object_key": (
            f"{object_prefix}raw-captures/amazon/us/{ASIN}/2026/07/15/"
            f"{capture_run_id}/{'a' * 64}/normalized.json"
        ),
        "content_digest": "a" * 64,
        "content_type": "application/json",
        "sanitization_status": "normalized",
        "request_id": request_id,
        "execution_id": execution_id,
        "run_id": capture_run_id,
        "collected_at": "2026-07-15T00:00:00Z",
        "created_at": "2026-07-15T00:00:00Z",
    }
    html_ref = {
        "capture_kind": "html",
        "bucket": "pytest-amazon-artifacts",
        "object_key": (
            f"{object_prefix}raw-captures/amazon/us/{ASIN}/2026/07/15/"
            f"{capture_run_id}/{'b' * 64}/page.html.gz"
        ),
        "content_digest": "b" * 64,
        "content_type": "application/gzip",
        "sanitization_status": "sanitized",
        "request_id": request_id,
        "execution_id": execution_id,
        "run_id": capture_run_id,
        "collected_at": "2026-07-15T00:00:00Z",
        "created_at": "2026-07-15T00:00:00Z",
    }
    return {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": resolved_asin,
        "canonical_url": f"https://www.amazon.com/dp/{requested_asin}",
        "collection_status": collection_status,
        "field_coverage": {
            "total": 20,
            "observed": 20,
            "missing": 0,
            "percentage": 100.0,
        },
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [normalized_ref, html_ref],
        "artifact_refs": [normalized_ref, html_ref],
        "media_source_refs": [],
        "browser_target_digest": "digest-only",
        "browser_provider_name": "roxy",
        "stage_durations_ms": {
            "navigation": 12.5,
            "parse": 3.25,
            "artifact": 8.75,
        },
        **({"parent_asin": parent_asin} if parent_asin else {}),
    }


def _persist_result(
    *,
    run_id: str,
    request_id: str = "1" * 32,
    execution_id: str = "2" * 32,
    artifact_object_prefix: str = ARTIFACT_PREFIX,
    row_status: str = "success",
    resolved_asin: str = ASIN,
) -> dict[str, Any]:
    normalized_ref = _browser_result(
        request_id=request_id,
        execution_id=execution_id,
        capture_run_id=run_id,
        artifact_object_prefix=artifact_object_prefix,
        resolved_asin=resolved_asin,
    )["normalized_capture_ref"]
    return {
        "row_status": row_status,
        "source_record_id": SOURCE_RECORD_ID,
        "requested_asin": ASIN,
        "resolved_asin": resolved_asin,
        "run_id": run_id,
        "step_statuses": {
            "media_asset_sync": "skipped",
            "amazon_product_fact_upsert": "success",
            "feishu_table_write": "success",
            "unsafe_extra_step": {"token": "must-not-cross-runtime-boundary"},
        },
        "fact_refs": {
            "product_id": "4" * 32,
            "snapshot_id": "5" * 32,
            "binding_id": "6" * 32,
            "raw_capture_ids": ["7" * 32, {"token": "must-not-cross-runtime-boundary"}],
            "normalized_capture_ref": {
                **normalized_ref,
                "access_token": "must-not-cross-runtime-boundary",
            },
            "cookie": "must-not-cross-runtime-boundary",
        },
        "media_coverage": {
            "expected": 2,
            "materialized": 2,
            "missing": 99,
            "complete": False,
            "access_token": "must-not-cross-runtime-boundary",
        },
        "writeback": {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
            "access_token": "must-not-cross-runtime-boundary",
        },
        "observability": {
            "browser_provider_name": "roxy",
            "stage_durations_ms": {
                "navigation": 12.5,
                "parse": 3.25,
                "artifact": 8.75,
                "fact": 4.5,
                "feishu": 6.5,
            },
            "field_coverage": {
                "total": 20,
                "observed": 20,
                "missing": 0,
                "percentage": 100.0,
            },
            "artifact_count": 2,
            "media_observed_count": 0,
            "media_materialized_count": 0,
            "final_status": row_status,
            "error_code": "",
            "browser_profile_id": "must-not-cross-summary-boundary",
            "browser_workspace_id": "must-not-cross-summary-boundary",
            "browser_provider_token": "must-not-cross-summary-boundary",
        },
    }


def test_runtime_dispatches_exact_amazon_read_browser_persist_summary_chain(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    request = store.load_task_request(request_id=request_id)
    assert request.payload == {"table_ref": TABLE_REF, "source_record_id": SOURCE_RECORD_ID}
    assert request.stage_cursor["runtime_context"] == {
        "browser_target_digest": "digest-only",
        "browser_resource_code": "browser:amazon:digest-only",
        "artifact_bucket": "pytest-amazon-artifacts",
        "artifact_object_prefix": ARTIFACT_PREFIX,
    }
    assert request.current_stage == "read_amazon_product_row"

    read_dispatch = _executor(runtime_db_url)
    assert read_dispatch["request_id"] == request_id
    assert read_dispatch["current_stage"] == "read_amazon_product_row"
    read_jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
    )
    assert len(read_jobs) == 1
    assert read_jobs[0]["payload"] == {
        "request_id": request_id,
        "task_code": TASK_CODE,
        "workflow_code": TASK_CODE,
        "stage_code": "read_amazon_product_row",
        "source_table_ref": TABLE_REF,
        "source_record_id": SOURCE_RECORD_ID,
        "adapter_code": "amazon_product_table_source_adapter",
            "field_names": ["ASIN", "采集标签", "商品链接", "强制刷新", "采集状态"],
    }
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert (
        len(
            _stage_jobs(
                store,
                request_id=request_id,
                stage_code="read_amazon_product_row",
                job_code="feishu_table_read",
            )
        )
        == 1
    )

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    collecting_dispatch = _executor(runtime_db_url)
    assert collecting_dispatch["current_stage"] == "collect_amazon_product_detail"
    collecting_job = _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
    )
    assert collecting_job["payload"]["records"] == [
        {
            "source_record_id": SOURCE_RECORD_ID,
            "requested_asin": ASIN,
            "collection_status": "collecting",
        }
    ]
    assert "collected_at" not in collecting_job["payload"]["records"][0]

    browser_dispatch = _executor(runtime_db_url)
    assert browser_dispatch["current_stage"] == "collect_amazon_product_detail"
    executions = store.list_task_executions(request_id=request_id)
    assert len(executions) == 1
    browser_execution = executions[0]
    assert browser_execution.item_code == "amazon_product_browser_fetch"
    assert browser_execution.resource_code == "browser:amazon:digest-only"
    assert browser_execution.payload["source_record_id"] == SOURCE_RECORD_ID
    assert browser_execution.payload["requested_asin"] == ASIN
    assert browser_execution.payload["stage_code"] == "collect_amazon_product_detail"
    assert browser_execution.payload["run_id"]
    assert not {
        "fallback_required",
        "browser_profile_ref",
        "browser_profile_id",
        "browser_workspace_id",
        "browser_cookies",
    } & set(browser_execution.payload)
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert len(store.list_task_executions(request_id=request_id)) == 1

    claimed_execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request_id,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert claimed_execution is not None
    store.mark_browser_execution_success(
        execution_id=claimed_execution.execution_id,
        run_id=claimed_execution.run_id,
        summary={"collection_status": "success"},
        result=_browser_result(
            request_id=request_id,
            execution_id=claimed_execution.execution_id,
            capture_run_id=str(claimed_execution.payload["run_id"]),
        ),
    )

    persisting_dispatch = _executor(runtime_db_url)
    assert persisting_dispatch["current_stage"] == "persist_amazon_product_detail"
    persisting_job = _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        row_status="persisting",
    )
    assert persisting_job["payload"]["records"] == [
        {
            "source_record_id": SOURCE_RECORD_ID,
            "requested_asin": ASIN,
            "collection_status": "persisting",
        }
    ]
    assert "collected_at" not in persisting_job["payload"]["records"][0]

    persist_dispatch = _executor(runtime_db_url)
    assert persist_dispatch["current_stage"] == "persist_amazon_product_detail"
    persist_jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
    )
    assert len(persist_jobs) == 1
    persist_payload = dict(persist_jobs[0]["payload"])
    assert persist_payload["source_table_identity"] == {
        "base_id": "app-amazon",
        "table_id": "tbl-amazon",
    }
    assert persist_payload["source_record_id"] == SOURCE_RECORD_ID
    assert persist_payload["requested_asin"] == ASIN
    assert persist_payload["run_id"] == browser_execution.payload["run_id"]
    assert persist_payload["browser_provider_name"] == "roxy"
    assert persist_payload["stage_durations_ms"] == {
        "navigation": 12.5,
        "parse": 3.25,
        "artifact": 8.75,
    }
    assert not {"capture", "html", "artifact_refs", "browser_target_digest"} & set(persist_payload)
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert (
        len(
            _stage_jobs(
                store,
                request_id=request_id,
                stage_code="persist_amazon_product_detail",
                job_code="amazon_product_row_persist",
            )
        )
        == 1
    )

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
        result=_persist_result(
            run_id=str(persist_payload["run_id"]),
            request_id=request_id,
            execution_id=claimed_execution.execution_id,
        ),
    )
    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    _assert_top_level_summary(
        finalized["summary"],
        final_status="success",
        row_status="success",
    )
    assert finalized["summary"]["aggregate_metrics"] == {
        "average_row_duration_ms": 35.5,
        "max_row_duration_ms": 35.5,
        "blocked_rate": 0.0,
        "average_parse_coverage_percentage": 100.0,
        "media_failure_rate": 0.0,
        "feishu_failure_rate": 0.0,
    }
    assert finalized["summary"]["failed_stage"] == ""
    assert finalized["summary"]["error_code"] == ""
    assert finalized["result"]["row_results"][0]["source_record_id"] == SOURCE_RECORD_ID
    assert finalized["summary"]["row_summary"] == {
        "source_record_id": SOURCE_RECORD_ID,
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "browser_provider_name": "roxy",
        "stage_durations_ms": {
            "navigation": 12.5,
            "parse": 3.25,
            "artifact": 8.75,
            "fact": 4.5,
            "feishu": 6.5,
        },
        "field_coverage": {
            "total": 20,
            "observed": 20,
            "missing": 0,
            "percentage": 100.0,
        },
        "artifact_count": 2,
        "media_observed_count": 0,
        "media_materialized_count": 0,
        "final_status": "success",
        "error_code": "",
    }
    serialized_summary = repr(finalized["summary"])
    assert "must-not-cross-summary-boundary" not in serialized_summary
    assert "must-not-cross-runtime-boundary" not in repr(finalized["result"])
    assert "must-not-cross-runtime-boundary" not in repr(finalized["outbox"])
    row_result = finalized["result"]["row_results"][0]
    assert set(row_result["fact_refs"]) == {
        "product_id",
        "snapshot_id",
        "binding_id",
        "raw_capture_ids",
        "normalized_capture_ref",
    }
    assert set(row_result["fact_refs"]["normalized_capture_ref"]) == {
        "capture_kind",
        "bucket",
        "object_key",
        "content_digest",
        "content_type",
        "sanitization_status",
        "request_id",
            "execution_id",
            "run_id",
            "collected_at",
            "created_at",
        }
    assert row_result["media_coverage"] == {
        "expected": 2,
        "materialized": 2,
        "missing": 0,
        "complete": True,
    }
    assert row_result["writeback"]["target_record_ids"] == [SOURCE_RECORD_ID]
    assert len(finalized["outbox"]) == 1


def test_blocked_browser_failure_keeps_fixed_summary_shape(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
    )
    _executor(runtime_db_url)
    execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request_id,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert execution is not None
    store.mark_browser_execution_failed(
        execution_id=execution.execution_id,
        run_id=execution.run_id,
        error_text="captcha blocked",
        summary={"collection_status": "blocked"},
        result=_browser_result(
            request_id=request_id,
            execution_id=execution.execution_id,
            capture_run_id=str(execution.payload["run_id"]),
            collection_status="blocked",
        ),
        error_type="browser_collection_failure",
        error_code="amazon_captcha_blocked",
        dead_letter_reason="",
    )

    terminal_writeback = _executor(runtime_db_url)
    assert terminal_writeback["request_status"] == "waiting"
    terminal_jobs = [
        job
        for job in _stage_jobs(
            store,
            request_id=request_id,
            stage_code="collect_amazon_product_detail",
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == "amazon_terminal_status"
    ]
    assert len(terminal_jobs) == 1
    claimed_writeback = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code="feishu_table_write",
    )
    assert claimed_writeback is not None
    assert claimed_writeback["job_id"] == terminal_jobs[0]["job_id"]
    store.mark_api_worker_job_success(
        job_id=str(claimed_writeback["job_id"]),
        run_id=str(claimed_writeback["run_id"]),
        summary={"stage_code": "collect_amazon_product_detail"},
        result={
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )
    _set_api_job_duration(
        runtime_db_url,
        job_id=str(claimed_writeback["job_id"]),
        duration_ms=5.5,
    )

    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    _assert_top_level_summary(
        finalized["summary"],
        final_status="failed",
        row_status="blocked",
    )
    assert finalized["summary"]["aggregate_metrics"] == {
        "average_row_duration_ms": 30.0,
        "max_row_duration_ms": 30.0,
        "blocked_rate": 1.0,
        "average_parse_coverage_percentage": 100.0,
        "media_failure_rate": 0.0,
        "feishu_failure_rate": 0.0,
    }
    assert finalized["summary"]["failed_stage"] == "collect_amazon_product_detail"
    assert finalized["summary"]["error_code"] == "amazon_captcha_blocked"
    assert finalized["summary"]["row_summary"]["final_status"] == "blocked"
    assert finalized["summary"]["row_summary"]["stage_durations_ms"]["feishu"] == 5.5


@pytest.mark.parametrize(
    "writeback_result",
    [
        {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": ["rec-wrong"],
        },
        {
            "written_count": 1,
            "skipped_count": 1,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
        {
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 1,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
        {
            "written_count": 1,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    ],
)
def test_collecting_status_writeback_requires_exact_source_row_convergence(
    runtime_db_url: str,
    monkeypatch,
    writeback_result: dict[str, Any],
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    assert _executor(runtime_db_url)["current_stage"] == "collect_amazon_product_detail"
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
        result=writeback_result,
    )

    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    assert finalized["result"]["error_code"] == "amazon_collecting_status_writeback_failed"
    assert finalized["result"]["row_results"][0]["writeback"] == {
        "written_count": int(writeback_result.get("written_count") or 0),
        "skipped_count": int(writeback_result.get("skipped_count") or 0),
        "failed_count": int(writeback_result.get("failed_count") or 0),
        "target_record_ids": [
            record_id
            for record_id in writeback_result["target_record_ids"]
            if record_id == SOURCE_RECORD_ID
        ],
    }
    assert store.list_task_executions(request_id=request_id) == []


def test_summary_gate_does_not_finalize_while_persist_job_is_active(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
    )
    _executor(runtime_db_url)
    execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request_id,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert execution is not None
    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id=execution.run_id,
        summary={"collection_status": "success"},
        result=_browser_result(
            request_id=request_id,
            execution_id=execution.execution_id,
            capture_run_id=str(execution.payload["run_id"]),
        ),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        row_status="persisting",
    )
    _executor(runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage="ready_for_summary",
    )

    attempted = _executor(runtime_db_url)

    assert attempted["request_status"] == "waiting"
    assert attempted["current_stage"] == "ready_for_summary"
    assert store.list_request_outbox(request_id=request_id) == []


def test_feishu_persist_failure_writes_terminal_status_and_failure_metric(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
    )
    _executor(runtime_db_url)
    execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request_id,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert execution is not None
    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id=execution.run_id,
        summary={"collection_status": "success"},
        result=_browser_result(
            request_id=request_id,
            execution_id=execution.execution_id,
            capture_run_id=str(execution.payload["run_id"]),
        ),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        row_status="persisting",
    )
    _executor(runtime_db_url)

    persist_jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
    )
    assert len(persist_jobs) == 1
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE api_worker_job SET max_attempts = 1 WHERE job_id = :job_id"),
                {"job_id": persist_jobs[0]["job_id"]},
            )
    finally:
        engine.dispose()
    claimed_persist = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code="amazon_product_row_persist",
    )
    assert claimed_persist is not None
    failed_persist = store.mark_api_worker_job_retry_or_failed(
        job_id=str(claimed_persist["job_id"]),
        run_id=str(claimed_persist["run_id"]),
        error_text="Feishu write failed",
        result={
            "step_statuses": {
                "media_asset_sync": "success",
                "amazon_product_fact_upsert": "success",
                "feishu_table_write": "failed",
                "unsafe_extra_step": {"token": "must-not-cross-runtime-boundary"},
            },
            "observability": {
                "stage_durations_ms": {
                    "navigation": 12.5,
                    "parse": 3.25,
                    "artifact": 8.75,
                    "fact": 4.5,
                    "feishu": 6.5,
                },
                "field_coverage": {
                    "total": 20,
                    "observed": 20,
                    "missing": 0,
                    "percentage": 100.0,
                },
                "artifact_count": 2,
                "media_observed_count": 2,
                "media_materialized_count": 1,
            },
        },
        error_code="feishu_table_write_failed",
    )
    assert failed_persist["result_status"] == "failed"

    writeback_dispatch = _executor(runtime_db_url)

    assert writeback_dispatch["request_status"] == "waiting"
    assert writeback_dispatch["current_stage"] == "persist_amazon_product_detail"
    assert store.list_request_outbox(request_id=request_id) == []
    terminal_jobs = [
        job
        for job in _stage_jobs(
            store,
            request_id=request_id,
            stage_code="persist_amazon_product_detail",
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == "amazon_terminal_status"
    ]
    assert len(terminal_jobs) == 1
    terminal_payload = terminal_jobs[0]["payload"]
    assert terminal_payload["source_record_id"] == SOURCE_RECORD_ID
    assert terminal_payload["row_status"] == "failed"
    assert terminal_payload["error_code"] == "feishu_table_write_failed"

    claimed_writeback = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code="feishu_table_write",
    )
    assert claimed_writeback is not None
    assert claimed_writeback["job_id"] == terminal_jobs[0]["job_id"]
    store.mark_api_worker_job_success(
        job_id=str(claimed_writeback["job_id"]),
        run_id=str(claimed_writeback["run_id"]),
        summary={"stage_code": "persist_amazon_product_detail"},
        result={
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )
    _set_api_job_duration(
        runtime_db_url,
        job_id=str(claimed_writeback["job_id"]),
        duration_ms=7.0,
    )

    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    assert finalized["result"]["error_code"] == "feishu_table_write_failed"
    row_result = finalized["result"]["row_results"][0]
    assert row_result["step_statuses"] == {
        "media_asset_sync": "success",
        "amazon_product_fact_upsert": "success",
        "feishu_table_write": "failed",
    }
    assert "must-not-cross-runtime-boundary" not in repr(row_result)
    assert row_result["writeback"] == {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": [SOURCE_RECORD_ID],
    }
    _assert_top_level_summary(
        finalized["summary"],
        final_status="failed",
        row_status="failed",
    )
    assert finalized["summary"]["aggregate_metrics"] == {
        "average_row_duration_ms": 42.5,
        "max_row_duration_ms": 42.5,
        "blocked_rate": 0.0,
        "average_parse_coverage_percentage": 100.0,
        "media_failure_rate": 0.5,
        "feishu_failure_rate": 1.0,
    }
    assert finalized["summary"]["failed_stage"] == "persist_amazon_product_detail"
    assert finalized["summary"]["error_code"] == "feishu_table_write_failed"
    assert finalized["summary"]["row_summary"]["stage_durations_ms"]["feishu"] == 13.5


def test_declared_parent_redirect_to_child_enters_persist_stage(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
    _executor(runtime_db_url)
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="collect_amazon_product_detail",
        row_status="collecting",
    )
    _executor(runtime_db_url)
    execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request_id,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert execution is not None
    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id=execution.run_id,
        summary={"collection_status": "partial_success"},
        result=_browser_result(
            request_id=request_id,
            execution_id=execution.execution_id,
            capture_run_id=str(execution.payload["run_id"]),
            requested_asin=ASIN,
            resolved_asin="B0CHILD001",
            parent_asin=ASIN,
            collection_status="partial_success",
        ),
    )

    status_dispatched = _executor(runtime_db_url)
    assert status_dispatched["current_stage"] == "persist_amazon_product_detail"
    _mark_status_writeback_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        row_status="persisting",
    )
    dispatched = _executor(runtime_db_url)

    assert dispatched["current_stage"] == "persist_amazon_product_detail"
    persist_jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
    )
    assert len(persist_jobs) == 1
    assert persist_jobs[0]["payload"]["requested_asin"] == ASIN
    assert persist_jobs[0]["payload"]["resolved_asin"] == "B0CHILD001"
    assert persist_jobs[0]["payload"]["collection_status"] == "partial_success"

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
        result=_persist_result(
            run_id=str(persist_jobs[0]["payload"]["run_id"]),
            request_id=request_id,
            execution_id=execution.execution_id,
            row_status="partial_success",
            resolved_asin="B0OTHER001",
        ),
    )
    mismatch = _executor(runtime_db_url)

    assert mismatch["request_status"] == "waiting"
    terminal_jobs = [
        job
        for job in _stage_jobs(
            store,
            request_id=request_id,
            stage_code="persist_amazon_product_detail",
            job_code="feishu_table_write",
        )
        if str((job.get("payload") or {}).get("writeback_kind") or "") == "amazon_terminal_status"
    ]
    assert len(terminal_jobs) == 1
    assert terminal_jobs[0]["payload"]["error_code"] == ("amazon_persist_result_identity_mismatch")
    claimed_writeback = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code="feishu_table_write",
    )
    assert claimed_writeback is not None
    assert claimed_writeback["job_id"] == terminal_jobs[0]["job_id"]
    store.mark_api_worker_job_success(
        job_id=str(claimed_writeback["job_id"]),
        run_id=str(claimed_writeback["run_id"]),
        summary={"stage_code": "persist_amazon_product_detail"},
        result={
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )

    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    assert finalized["result"]["error_code"] == "amazon_persist_result_identity_mismatch"
    assert "B0OTHER001" not in repr(finalized["result"])
    assert "B0OTHER001" not in repr(finalized["outbox"])


def test_invalid_source_asin_writes_status_then_fails_without_browser_or_persist(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    store, request_id = _submit(runtime_db_url)
    _executor(runtime_db_url)
    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(lookup_status="invalid_asin"),
    )

    writeback_dispatch = _executor(runtime_db_url)

    assert writeback_dispatch["request_status"] == "waiting"
    assert writeback_dispatch["current_stage"] == "read_amazon_product_row"
    writeback_jobs = _stage_jobs(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_write",
    )
    assert len(writeback_jobs) == 1
    writeback_payload = dict(writeback_jobs[0]["payload"])
    assert writeback_payload == {
        "request_id": request_id,
        "workflow_code": TASK_CODE,
        "stage_code": "read_amazon_product_row",
        "target_table_ref": TABLE_REF,
        "source_record_id": SOURCE_RECORD_ID,
        "row_status": "failed",
        "error_code": "invalid_asin",
        "feishu_table": {
            "app_token": "app-amazon",
            "table_id": "tbl-amazon",
        },
        "records": [
            {
                "source_record_id": SOURCE_RECORD_ID,
                "requested_asin": "",
                "collection_status": "failed",
                "collected_at": writeback_payload["records"][0]["collected_at"],
                "error_code": "invalid_asin",
                "error_message": "Amazon source row identity validation failed.",
            }
        ],
            "mapper_code": "amazon_product_projection_mapper",
            "write_mode": "update_existing",
            "write_policy": {
                "ignore_missing_fields": True,
                "field_allowlist": [
                    "主图",
                    "侧边栏图片",
                    "送达日期",
                    "包装规格",
                    "促销活动记录",
                ],
            },
            "writeback_kind": "amazon_terminal_status",
        }
    assert writeback_payload["records"][0]["collected_at"].endswith("Z")
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert (
        len(
            _stage_jobs(
                store,
                request_id=request_id,
                stage_code="read_amazon_product_row",
                job_code="feishu_table_write",
            )
        )
        == 1
    )
    assert store.list_task_executions(request_id=request_id) == []
    assert (
        _stage_jobs(
            store,
            request_id=request_id,
            stage_code="persist_amazon_product_detail",
            job_code="amazon_product_row_persist",
        )
        == []
    )

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_write",
        result={
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )
    _set_api_job_duration(
        runtime_db_url,
        job_id=str(writeback_jobs[0]["job_id"]),
        duration_ms=4.0,
    )
    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    assert finalized["result"]["error_code"] == "invalid_asin"
    _assert_top_level_summary(
        finalized["summary"],
        final_status="failed",
        row_status="failed",
    )
    assert finalized["summary"]["aggregate_metrics"] == {
        "average_row_duration_ms": 4.0,
        "max_row_duration_ms": 4.0,
        "blocked_rate": 0.0,
        "average_parse_coverage_percentage": 0.0,
        "media_failure_rate": 0.0,
        "feishu_failure_rate": 0.0,
    }
    assert finalized["summary"]["failed_stage"] == "read_amazon_product_row"
    assert finalized["summary"]["error_code"] == "invalid_asin"
    assert finalized["summary"]["row_summary"]["stage_durations_ms"]["feishu"] == 4.0
    assert finalized["result"]["row_results"][0]["writeback"] == {
        "written_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "target_record_ids": [SOURCE_RECORD_ID],
    }
    assert store.list_task_executions(request_id=request_id) == []
    assert (
        _stage_jobs(
            store,
            request_id=request_id,
            stage_code="persist_amazon_product_detail",
            job_code="amazon_product_row_persist",
        )
        == []
    )


def test_amazon_submit_rejects_missing_profile_and_extra_business_fields(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AMAZON_US_BROWSER_PROFILE_REF", raising=False)
    monkeypatch.delenv("DEFAULT_PROFILE_REF", raising=False)

    no_profile = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id=SOURCE_RECORD_ID,
        ),
    )
    assert no_profile["request_status"] == "rejected"
    assert no_profile["error_code"] == "amazon_browser_profile_missing"

    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    extra_field = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id=SOURCE_RECORD_ID,
            browser_profile_ref="must-not-enter-formal-payload",
        ),
    )
    assert extra_field["request_status"] == "rejected"
    assert "browser_profile_ref" in extra_field["forbidden_runtime_config_fields"]

    def unresolved_target(*, profile_ref: str) -> str:
        del profile_ref
        raise ValueError("missing profile")

    monkeypatch.setattr(
        runtime_orchestrator,
        "resolve_automation_browser_target_digest",
        unresolved_target,
    )
    unresolved_profile = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id="rec-unresolved-profile",
        ),
    )
    assert unresolved_profile["request_status"] == "rejected"
    assert unresolved_profile["error_code"] == "amazon_browser_profile_unavailable"


def test_amazon_submit_rejects_arbitrary_table_reference(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")

    rejected = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref="https://muji.feishu.cn/base/other?table=tblOther",
            source_record_id=SOURCE_RECORD_ID,
        ),
    )

    assert rejected["request_status"] == "rejected"
    assert rejected["error_code"] == "unsupported_amazon_table_ref"
    assert rejected["required_table_ref"] == TABLE_REF


def test_amazon_submit_rejects_fact_schema_revision_mismatch_before_task_creation(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE fact_alembic_version SET version_num = 'outdated_revision'")
            )
    finally:
        engine.dispose()

    rejected = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id="rec-schema-mismatch",
        ),
    )

    assert rejected["request_status"] == "rejected"
    assert rejected["error_code"] == "amazon_fact_schema_not_ready"
    assert rejected["required_fact_schema_revision"] == "20260714_0007"
    engine = create_engine(runtime_db_url, future=True)
    try:
        with engine.connect() as connection:
            task_count = connection.execute(
                text("SELECT COUNT(*) FROM task_request WHERE task_code = :task_code"),
                {"task_code": TASK_CODE},
            ).scalar_one()
    finally:
        engine.dispose()
    assert task_count == 0


def test_amazon_submit_marks_fact_schema_connectivity_failure_retryable(
    runtime_db_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMAZON_US_BROWSER_PROFILE_REF", "amazon-us-test")

    class UnavailableFactStore:
        def __init__(self, *, db_url: str) -> None:
            assert db_url == runtime_db_url

        def require_schema_revision(self) -> str:
            raise AmazonFactSchemaUnavailableError("temporary database failure")

        def close(self) -> None:
            return None

    monkeypatch.setattr(runtime_orchestrator, "AmazonFactStore", UnavailableFactStore)

    rejected = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            source_record_id="rec-schema-unavailable",
        ),
    )

    assert rejected["request_status"] == "rejected"
    assert rejected["error_code"] == "amazon_fact_schema_check_failed"
    assert rejected["retryable"] is True
    assert rejected["required_fact_schema_revision"] == "20260714_0007"


def test_invalid_amazon_row_result_force_terminal_on_first_attempt(
    runtime_db_url: str,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload={"table_ref": TABLE_REF, "source_record_id": SOURCE_RECORD_ID},
        requested_by="pytest",
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting",
        current_stage="persist_amazon_product_detail",
    )
    enqueued = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="amazon_product_row_persist",
        jobs=[
            {
                "business_key": f"amazon:US:{ASIN}",
                "dedupe_key": f"{request.request_id}:amazon-persist:{ASIN}",
                "payload": {"requested_asin": ASIN},
                "max_attempts": 3,
            }
        ],
    )
    job_id = str(enqueued["created_records"][0]["job_id"])
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request.request_id,
        job_code="amazon_product_row_persist",
    )
    assert claimed is not None

    marked = store.mark_api_worker_job_retry_or_failed(
        job_id=job_id,
        run_id=str(claimed["run_id"]),
        error_text="Amazon row persistence returned an invalid compact result.",
        summary={"error_code": "invalid_handler_result"},
        result={"handler_result": {"status": "failed"}},
        retry_delay_seconds=30.0,
        error_type="runtime_result_validation_failure",
        error_code="invalid_handler_result",
        dead_letter_reason="invalid_handler_result",
        force_terminal=True,
    )

    assert marked["status"] == "finished"
    assert marked["result_status"] == "failed"
    assert marked["attempt_count"] == 1
    assert marked["max_attempts"] == 3
    assert marked["dead_letter_reason"] == "invalid_handler_result"
    assert (
        store.claim_next_api_worker_job(
            worker_id="pytest-api-retry",
            lease_seconds=30.0,
            request_id=request.request_id,
            job_code="amazon_product_row_persist",
        )
        is None
    )
