from __future__ import annotations

from typing import Any

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_runner
import automation_business_scaffold.domains.amazon.flows.amazon_product_row_refresh.orchestrator as row_refresh_flow
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.control_plane.executor import worker_dispatch
from automation_business_scaffold.control_plane.executor.runner import run_task_request
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorOutcome,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import (
    get_workflow_definition as get_registered_workflow_definition,
    load_workflow_runtime,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    AMAZON_PRODUCT_BATCH_TASK_CODE,
    FORMAL_TASK_CODES,
)
from automation_business_scaffold.domains.amazon.tasks.refresh_current_amazon_product_table import (
    RefreshCurrentAmazonProductTableTask,
)
from automation_business_scaffold.domains.amazon.workflows import get_workflow_definition
from automation_business_scaffold.domains.amazon.workflows.refresh_current_amazon_product_table import (
    REFRESH_CURRENT_AMAZON_PRODUCT_TABLE_DEFINITION,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


TASK_CODE = "refresh_current_amazon_product_table"
ROW_JOB_CODE = "amazon_product_row_refresh"
TABLE_REF = "AMAZON_PRODUCTS"
EXPECTED_STAGES = (
    "read_amazon_product_rows",
    "dispatch_amazon_product_rows",
    "collect_amazon_product_rows",
    "collect_amazon_product_browsers",
    "ready_for_summary",
)


@pytest.fixture(autouse=True)
def _resolved_browser_target(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_runner,
        "resolve_automation_browser_target_digest",
        lambda *, profile_ref: "batch-digest",
    )


def _runtime_params(runtime_db_url: str, **overrides: Any) -> dict[str, Any]:
    params = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": "pytest-amazon-artifacts",
        "execution_control_artifact_object_prefix": "pytest-amazon-batch",
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
        "execution_control_db_health_preflight_enabled": False,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def test_amazon_batch_task_and_workflow_contract_are_registered() -> None:
    task = RefreshCurrentAmazonProductTableTask()
    workflow = task.build_workflow({})
    definition = REFRESH_CURRENT_AMAZON_PRODUCT_TABLE_DEFINITION

    assert task.name == TASK_CODE
    assert workflow.workflow_id == TASK_CODE
    assert definition.task_code == TASK_CODE
    assert definition.stage_codes == EXPECTED_STAGES
    assert definition.payload_contract.field_names() == ("table_ref",)
    assert definition.stages[0].job_bindings[0].adapter_code == (
        "amazon_product_batch_source_adapter"
    )
    assert definition.stages[1].execution_mode == "executor_action"
    assert definition.stages[2].job_bindings[0].job_code == ROW_JOB_CODE
    assert definition.stages[3].job_bindings[0].job_code == "amazon_product_browser_fetch"
    assert AMAZON_PRODUCT_BATCH_TASK_CODE == TASK_CODE
    assert TASK_CODE in FORMAL_TASK_CODES
    assert get_workflow_definition(TASK_CODE) is definition
    assert get_registered_workflow_definition(TASK_CODE) is definition
    assert load_workflow_runtime(TASK_CODE) is not None


def _row_handler_context(*, browser_execution: dict[str, Any] | None = None) -> HandlerContext:
    payload: dict[str, Any] = {
        "request_id": "a" * 32,
        "workflow_code": TASK_CODE,
        "stage_code": "collect_amazon_product_rows",
        "table_ref": TABLE_REF,
        "source_record_id": "rec-handler",
        "requested_asin": "B0ABC12345",
        "canonical_url": "https://www.amazon.com/dp/B0ABC12345",
        "source_table_identity": {
            "base_id": "app-amazon",
            "table_id": "tbl-amazon",
        },
        "runtime_context": {
            "browser_target_digest": "batch-digest",
            "browser_resource_code": "browser:amazon:batch-digest",
            "artifact_bucket": "pytest-amazon-artifacts",
            "artifact_object_prefix": "pytest-amazon-batch",
        },
    }
    if browser_execution is not None:
        payload["browser_execution"] = browser_execution
    return HandlerContext(
        request_id="a" * 32,
        job_id="row-job",
        handler_code=ROW_JOB_CODE,
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload=payload,
        workflow_code=TASK_CODE,
        stage_code="collect_amazon_product_rows",
        job_code=ROW_JOB_CODE,
        business_key="rec-handler:B0ABC12345",
        dedupe_key="row-dedupe",
    )


def _successful_status_write(context: HandlerContext) -> HandlerResult:
    return HandlerResult.success(
        context,
        result={
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": ["rec-handler"],
        },
    )


def _missing_optional_status_fields(context: HandlerContext) -> HandlerResult:
    return HandlerResult.skipped(
        context,
        result={
            "written_count": 0,
            "skipped_count": 1,
            "failed_count": 0,
            "target_record_ids": [],
            "records": [
                {
                    "record_id": "rec-handler",
                    "status": "skipped",
                    "message": "empty_fields",
                }
            ],
        },
    )


def test_amazon_row_refresh_uses_primary_browser_required(monkeypatch) -> None:
    monkeypatch.setattr(
        row_refresh_flow,
        "feishu_table_write_handler",
        _successful_status_write,
    )

    result = row_refresh_flow.run_amazon_product_row_refresh_flow(
        _row_handler_context()
    )

    assert result.status == "browser_required"
    assert result.error is None
    assert result.result["row_status"] == "waiting_browser"
    assert result.result["browser_required"] is True
    assert result.next_action.type == "browser_required"
    assert result.result["browser_request"] == result.next_action.payload
    assert result.result["browser_request"]["handler_code"] == (
        "amazon_product_browser_fetch"
    )
    assert len(result.result["browser_request"]["payload"]["run_id"]) == 64


def test_amazon_row_refresh_does_not_block_when_status_columns_are_absent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        row_refresh_flow,
        "feishu_table_write_handler",
        _missing_optional_status_fields,
    )

    result = row_refresh_flow.run_amazon_product_row_refresh_flow(
        _row_handler_context()
    )

    assert result.status == "browser_required"
    assert result.result["row_status"] == "waiting_browser"


def test_amazon_row_refresh_resumes_and_delegates_existing_persist_handler(
    monkeypatch,
) -> None:
    captured_persist_payload: dict[str, Any] = {}

    def fake_persist(context: HandlerContext) -> HandlerResult:
        captured_persist_payload.update(context.payload)
        return HandlerResult.success(
            context,
            result={
                "row_status": "success",
                "source_record_id": "rec-handler",
                "requested_asin": "B0ABC12345",
                "resolved_asin": "B0ABC12345",
                "run_id": context.payload["run_id"],
                "step_statuses": {
                    "media_asset_sync": "success",
                    "amazon_product_fact_upsert": "success",
                    "feishu_table_write": "success",
                },
                "writeback": {
                    "written_count": 1,
                    "skipped_count": 0,
                    "failed_count": 0,
                    "target_record_ids": ["rec-handler"],
                },
            },
        )

    monkeypatch.setattr(
        row_refresh_flow,
        "feishu_table_write_handler",
        _successful_status_write,
    )
    monkeypatch.setattr(row_refresh_flow, "_validate_browser_result", lambda **_: None)
    monkeypatch.setattr(
        row_refresh_flow,
        "amazon_product_row_persist_handler",
        fake_persist,
    )
    context = _row_handler_context(
        browser_execution={
            "execution_id": "b" * 32,
            "status": "success",
            "result": {
                "resolved_asin": "B0ABC12345",
                "collection_status": "success",
            },
        }
    )

    result = row_refresh_flow.run_amazon_product_row_refresh_flow(context)

    assert result.status == "success"
    assert result.result["row_status"] == "success"
    assert captured_persist_payload["source_record_id"] == "rec-handler"
    assert captured_persist_payload["requested_asin"] == "B0ABC12345"
    assert captured_persist_payload["table_ref"] == TABLE_REF


def test_runtime_marks_browser_required_row_job_waiting() -> None:
    context = _row_handler_context()
    worker_result = HandlerResult.browser_required(
        context,
        summary={"row_status": "waiting_browser"},
        result={"row_status": "waiting_browser", "browser_required": True},
    )
    outcome = ExecutionSupervisorOutcome(
        context=context,
        worker_result=worker_result,
        supervisor_status="handler_completed",
        started_at=1.0,
        finished_at=2.0,
        heartbeat_count=0,
    )

    class Store:
        def __init__(self) -> None:
            self.waiting: dict[str, Any] = {}

        def mark_api_worker_job_waiting(self, **kwargs: Any) -> dict[str, Any]:
            self.waiting = dict(kwargs)
            return {"status": "waiting", "result_status": "browser_required"}

    store = Store()
    marked, success_count, failed_count = worker_dispatch.persist_api_worker_outcome(
        store=store,  # type: ignore[arg-type]
        job_id=context.job_id,
        run_id="claim-run",
        outcome=outcome,
        retry_delay_seconds=5,
    )

    assert marked["status"] == "waiting"
    assert (success_count, failed_count) == (0, 0)
    assert store.waiting["stage"] == "browser_required"
    assert store.waiting["error_code"] == ""


def test_amazon_batch_submit_accepts_only_table_ref(runtime_db_url: str) -> None:
    submitted = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
        ),
    )

    assert submitted["request_status"] == "pending"
    assert submitted["task_request"]["payload"] == {"table_ref": TABLE_REF}

    rejected = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
            collection_tag="A",
        ),
    )
    assert rejected["request_status"] == "rejected"
    assert rejected["error_code"] == "invalid_amazon_task_payload"
    assert rejected["unexpected_business_fields"] == ["collection_tag"]


def test_amazon_batch_dispatches_same_request_row_jobs_and_summarizes(
    runtime_db_url: str,
) -> None:
    submitted = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
        ),
    )
    parent_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)

    run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    read_job = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=parent_id,
        job_code="feishu_table_read",
    )
    assert read_job is not None
    source_rows = [
        {
            "source_record_id": "rec-t-1",
            "business_key": "amazon:US:B0ABC12345",
            "requested_asin": "B0ABC12345",
            "canonical_url": "https://www.amazon.com/dp/B0ABC12345",
        },
        {
            "source_record_id": "rec-t-2",
            "business_key": "amazon:US:B0ABC12346",
            "requested_asin": "B0ABC12346",
            "canonical_url": "https://www.amazon.com/dp/B0ABC12346",
        },
        {
            "source_record_id": "rec-t-1",
            "business_key": "amazon:US:B0ABC12345",
            "requested_asin": "B0ABC12345",
            "canonical_url": "https://www.amazon.com/dp/B0ABC12345",
        },
    ]
    store.mark_api_worker_job_success(
        job_id=str(read_job["job_id"]),
        run_id=str(read_job["run_id"]),
        summary={"source_row_count": 3},
        result={
            "source_rows": source_rows,
            "source_table_identity": {"base_id": "app-amazon", "table_id": "tbl-amazon"},
            "adapter_summary": {
                "adapter_code": "amazon_product_batch_source_adapter",
                "input_row_count": 4,
                "tagged_row_count": 3,
                "source_row_count": 3,
                "selection_field": "采集标签",
                "selection_value": "T",
                "invalid_asin_count": 0,
                "identity_mismatch_count": 0,
                "unsupported_marketplace_count": 0,
                "missing_record_id_count": 0,
            },
        },
    )

    dispatched = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    assert dispatched["current_stage"] == "collect_amazon_product_rows"
    parent = store.load_task_request(request_id=parent_id)
    stage_result = parent.stage_cursor["stage_results"]["dispatch_amazon_product_rows"]
    assert "child_requests" not in stage_result
    row_jobs = store.list_api_worker_jobs_for_request(
        request_id=parent_id,
        job_code=ROW_JOB_CODE,
    )
    assert [job["payload"]["source_record_id"] for job in row_jobs] == [
        "rec-t-1",
        "rec-t-2",
    ]
    assert {job["request_id"] for job in row_jobs} == {parent_id}
    assert all(
        job["payload"]["runtime_context"]["browser_resource_code"]
        == "browser:amazon:batch-digest"
        for job in row_jobs
    )

    first = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        job_code=ROW_JOB_CODE,
    )
    assert first is not None
    assert first["request_id"] == parent_id
    store.mark_api_worker_job_success(
        job_id=str(first["job_id"]),
        run_id=str(first["run_id"]),
        summary={"row_status": "success"},
        result={"source_record_id": first["payload"]["source_record_id"], "row_status": "success"},
    )
    second = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        job_code=ROW_JOB_CODE,
    )
    assert second is not None
    store.mark_api_worker_job_retry_or_failed(
        job_id=str(second["job_id"]),
        run_id=str(second["run_id"]),
        error_text="test failure",
        summary={"row_status": "failed"},
        result={"source_record_id": second["payload"]["source_record_id"], "row_status": "failed"},
        error_type="test_failure",
        error_code="test_failure",
        force_terminal=True,
    )

    finalized = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    assert finalized["request_status"] == "partial_success"
    assert finalized["summary"]["row_total_count"] == 2
    assert finalized["summary"]["row_status_counts"] == {
        "success": 1,
        "partial_success": 0,
        "unavailable": 0,
        "blocked": 0,
        "failed": 1,
        "skipped": 0,
    }
    assert finalized["summary"]["adapter_summary"]["selection_value"] == "T"
    assert len(finalized["outbox"]) == 1


def test_amazon_batch_resumes_the_same_row_job_after_primary_browser(
    runtime_db_url: str,
) -> None:
    submitted = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="submit", table_ref=TABLE_REF),
    )
    parent_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)

    run_task_request(TASK_CODE, _runtime_params(runtime_db_url, control_action="executor_once"))
    read_job = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=parent_id,
        job_code="feishu_table_read",
    )
    assert read_job is not None
    store.mark_api_worker_job_success(
        job_id=str(read_job["job_id"]),
        run_id=str(read_job["run_id"]),
        summary={"source_row_count": 1},
        result={
            "source_rows": [
                {
                    "source_record_id": "rec-browser",
                    "business_key": "amazon:US:B0ABC12345",
                    "requested_asin": "B0ABC12345",
                    "canonical_url": "https://www.amazon.com/dp/B0ABC12345",
                }
            ],
            "source_table_identity": {"base_id": "app-amazon", "table_id": "tbl-amazon"},
            "adapter_summary": {
                "adapter_code": "amazon_product_batch_source_adapter",
                "input_row_count": 1,
                "tagged_row_count": 1,
                "source_row_count": 1,
                "selection_field": "采集标签",
                "selection_value": "T",
            },
        },
    )
    run_task_request(TASK_CODE, _runtime_params(runtime_db_url, control_action="executor_once"))

    row_job = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        job_code=ROW_JOB_CODE,
    )
    assert row_job is not None
    browser_payload = {
        "workflow_code": TASK_CODE,
        "stage_code": "collect_amazon_product_browsers",
        "source_record_id": "rec-browser",
        "requested_asin": "B0ABC12345",
        "run_id": "a" * 64,
        "artifact_bucket": "pytest-amazon-artifacts",
        "artifact_object_prefix": "pytest-amazon-batch",
    }
    store.mark_api_worker_job_waiting(
        job_id=str(row_job["job_id"]),
        run_id=str(row_job["run_id"]),
        summary={"row_status": "waiting_browser"},
        result={
            "source_record_id": "rec-browser",
            "requested_asin": "B0ABC12345",
            "browser_required": True,
            "browser_request": {
                "handler_code": "amazon_product_browser_fetch",
                "resource_code": "browser:amazon:batch-digest",
                "payload": browser_payload,
            },
        },
        stage="browser_required",
    )

    waiting = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    assert waiting["current_stage"] == "collect_amazon_product_browsers"
    execution = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        item_codes=("amazon_product_browser_fetch",),
    )
    assert execution is not None
    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id=execution.run_id,
        summary={"collection_status": "success"},
        result={
            "requested_asin": "B0ABC12345",
            "resolved_asin": "B0ABC12345",
            "collection_status": "success",
        },
    )

    resumed = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    assert resumed["current_stage"] == "collect_amazon_product_rows"
    reclaimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        job_code=ROW_JOB_CODE,
    )
    assert reclaimed is not None
    assert reclaimed["job_id"] == row_job["job_id"]
    assert reclaimed["payload"]["browser_execution"]["execution_id"] == execution.execution_id


def test_amazon_batch_with_no_t_tagged_rows_finishes_without_row_jobs(
    runtime_db_url: str,
) -> None:
    submitted = run_task_request(
        TASK_CODE,
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            table_ref=TABLE_REF,
        ),
    )
    parent_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)

    run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )
    read_job = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=parent_id,
        job_code="feishu_table_read",
    )
    assert read_job is not None
    store.mark_api_worker_job_success(
        job_id=str(read_job["job_id"]),
        run_id=str(read_job["run_id"]),
        summary={"source_row_count": 0},
        result={
            "source_rows": [],
            "adapter_summary": {
                "adapter_code": "amazon_product_batch_source_adapter",
                "input_row_count": 3,
                "tagged_row_count": 0,
                "source_row_count": 0,
                "selection_field": "采集标签",
                "selection_value": "T",
            },
        },
    )

    finalized = run_task_request(
        TASK_CODE,
        _runtime_params(runtime_db_url, control_action="executor_once"),
    )

    assert finalized["request_status"] == "success"
    assert finalized["summary"]["row_total_count"] == 0
    assert finalized["result"]["row_results"] == []
    assert store.list_api_worker_jobs_for_request(
        request_id=parent_id,
        job_code=ROW_JOB_CODE,
    ) == []
    assert len(finalized["outbox"]) == 1
