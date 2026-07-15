from __future__ import annotations

from typing import Any

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
from automation_business_scaffold.control_plane.executor.runner import (
    run_task_request,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


TASK_CODE = "refresh_amazon_product_row_by_asin"
TABLE_REF = "AMAZON_PRODUCTS"
SOURCE_RECORD_ID = "rec-amazon-1"
ASIN = "B0ABC12345"


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
    requested_asin: str = ASIN,
    resolved_asin: str = ASIN,
    parent_asin: str = "",
    collection_status: str = "success",
) -> dict[str, Any]:
    normalized_ref = {
        "capture_kind": "normalized_capture",
        "bucket": "pytest-amazon-artifacts",
        "object_key": f"raw-captures/amazon/us/{ASIN}/capture.json",
        "content_digest": "a" * 64,
        "content_type": "application/json",
    }
    html_ref = {
        "capture_kind": "html",
        "bucket": "pytest-amazon-artifacts",
        "object_key": f"raw-captures/amazon/us/{ASIN}/page.html.gz",
        "content_digest": "b" * 64,
        "content_type": "application/gzip",
        "sanitization_status": "sanitized",
    }
    return {
        "marketplace_code": "US",
        "requested_asin": requested_asin,
        "resolved_asin": resolved_asin,
        "canonical_url": f"https://www.amazon.com/dp/{requested_asin}",
        "collection_status": collection_status,
        "field_coverage": {"total": 20, "observed": 20, "missing": 0},
        "normalized_capture_ref": normalized_ref,
        "raw_capture_refs": [normalized_ref, html_ref],
        "artifact_refs": [normalized_ref, html_ref],
        "media_source_refs": [],
        "browser_target_digest": "digest-only",
        **({"parent_asin": parent_asin} if parent_asin else {}),
    }


def _persist_result(*, run_id: str, row_status: str = "success") -> dict[str, Any]:
    return {
        "row_status": row_status,
        "source_record_id": SOURCE_RECORD_ID,
        "requested_asin": ASIN,
        "resolved_asin": ASIN,
        "run_id": run_id,
        "step_statuses": {
            "media_asset_sync": "skipped",
            "amazon_product_fact_upsert": "success",
            "feishu_table_write": "success",
        },
        "fact_refs": {"marketplace_code": "US", "asin": ASIN},
        "writeback": {"written_count": 1, "record_ids": [SOURCE_RECORD_ID]},
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
        "field_names": ["ASIN", "商品链接", "强制刷新", "采集状态"],
    }
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert len(
        _stage_jobs(
            store,
            request_id=request_id,
            stage_code="read_amazon_product_row",
            job_code="feishu_table_read",
        )
    ) == 1

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_read",
        result=_read_result(),
    )
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
        result=_browser_result(),
    )

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
    assert not {"capture", "html", "artifact_refs", "browser_target_digest"} & set(
        persist_payload
    )
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert len(
        _stage_jobs(
            store,
            request_id=request_id,
            stage_code="persist_amazon_product_detail",
            job_code="amazon_product_row_persist",
        )
    ) == 1

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
        result=_persist_result(run_id=str(persist_payload["run_id"])),
    )
    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "ready_for_summary"
    assert finalized["summary"]["row_status_counts"] == {"success": 1}
    assert finalized["result"]["row_results"][0]["source_record_id"] == SOURCE_RECORD_ID
    assert len(finalized["outbox"]) == 1


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
        result=_browser_result(),
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
            requested_asin=ASIN,
            resolved_asin="B0CHILD001",
            parent_asin=ASIN,
            collection_status="partial_success",
        ),
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
        "writeback_kind": "amazon_terminal_status",
    }
    assert writeback_payload["records"][0]["collected_at"].endswith("Z")
    assert _executor(runtime_db_url)["daemon_status"] == "idle"
    assert len(
        _stage_jobs(
            store,
            request_id=request_id,
            stage_code="read_amazon_product_row",
            job_code="feishu_table_write",
        )
    ) == 1
    assert store.list_task_executions(request_id=request_id) == []
    assert _stage_jobs(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
    ) == []

    _mark_api_success(
        store,
        request_id=request_id,
        stage_code="read_amazon_product_row",
        job_code="feishu_table_write",
        result={
            "written_count": 1,
            "target_record_ids": [SOURCE_RECORD_ID],
        },
    )
    finalized = _executor(runtime_db_url)

    assert finalized["request_status"] == "failed"
    assert finalized["result"]["error_code"] == "invalid_asin"
    assert finalized["summary"]["row_status_counts"] == {"failed": 1}
    assert finalized["result"]["row_results"][0]["writeback"] == {
        "written_count": 1,
        "target_record_ids": [SOURCE_RECORD_ID],
    }
    assert store.list_task_executions(request_id=request_id) == []
    assert _stage_jobs(
        store,
        request_id=request_id,
        stage_code="persist_amazon_product_detail",
        job_code="amazon_product_row_persist",
    ) == []


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
