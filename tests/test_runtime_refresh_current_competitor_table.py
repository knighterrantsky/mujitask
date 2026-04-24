from __future__ import annotations

from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.domains.tiktok.flows.refresh_current_competitor_table import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

REFRESH_TASK_CODE = "refresh_current_competitor_table"
SOURCE_TABLE_REF = "tbl_competitor_source"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"


def _store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _submit_refresh_request(runtime_db_url: str) -> tuple[RuntimeStore, object, object]:
    store = _store(runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=REFRESH_TASK_CODE,
        payload={
            "source_table_ref": SOURCE_TABLE_REF,
            "reply_target": "reply://pytest",
        },
        requested_by="pytest",
        source_channel_code="console",
        reply_target="reply://pytest",
    )
    workflow = get_workflow_definition(REFRESH_TASK_CODE)
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage=workflow.entry_stage_code,
        progress_stage=workflow.entry_stage_code,
    )
    return store, request, workflow


def _latest_stage_job(store: RuntimeStore, *, request_id: str, stage_code: str, job_code: str) -> dict:
    jobs = [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
        and str(job.get("job_code") or "") == job_code
    ]
    assert jobs, f"expected stage job {stage_code}/{job_code}"
    return jobs[-1]


def test_refresh_runtime_module_is_loadable_and_row_pipeline_finalizes(runtime_db_url: str) -> None:
    runtime = load_workflow_runtime(REFRESH_TASK_CODE)
    assert runtime is not None
    assert runtime.advance_stage is advance_stage
    assert runtime.finalize_request is finalize_request
    assert runtime.release_request_after_child_completion is release_request_after_child_completion

    store, request, workflow = _submit_refresh_request(runtime_db_url)

    read_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_waiting["action"] == "waiting"
    read_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
    )
    assert read_job["payload"]["source_table_ref"] == SOURCE_TABLE_REF
    assert read_job["payload"]["field_names"][-1] == "商品状态"
    assert read_job["payload"]["filter_spec"]["candidate_policy"] == "missing_auto_maintained_fields"

    store.mark_api_worker_job_success(
        job_id=read_job["job_id"],
        run_id="pytest:read",
        summary={"rows": 1},
        result={
            "source_rows": [
                {
                    "source_record_id": "row-1",
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                }
            ]
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    read_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_advance["action"] == "advance"
    assert read_advance["next_stage"] == "dispatch_product_collection"

    request = store.load_task_request(request_id=request.request_id)
    dispatch = advance_stage(store=store, request=request, workflow=workflow, stage_code="dispatch_product_collection")
    assert dispatch["action"] == "advance"
    assert dispatch["next_stage"] == "collect_product_data"
    row_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="competitor_row_refresh",
    )
    assert row_job["payload"]["source_record_id"] == "row-1"
    assert row_job["payload"]["request_payload"]["source_table_ref"] == SOURCE_TABLE_REF

    store.mark_api_worker_job_success(
        job_id=row_job["job_id"],
        run_id="pytest:row-refresh",
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "success"},
                {"step": "browser_fallback", "status": "skipped"},
                {"step": "media_sync", "status": "success"},
                {"step": "fastmoss_fetch", "status": "success"},
                {"step": "fact_db_upsert", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
            "runtime_evidence": {"browser_fallback_used": False},
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    collect_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="collect_product_data")
    assert collect_advance["action"] == "advance"
    assert collect_advance["next_stage"] == "ready_for_summary"

    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["action"] == "finalized"
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_total_count"] == 1
    row_result = finalized["result"]["row_results"][0]
    assert row_result["row_status"] == "success"
    assert row_result["tiktok_status"] == "success"
    assert row_result["writeback_status"] == "success"


def test_refresh_runtime_read_stage_deletes_rows_with_all_fields_empty(runtime_db_url: str) -> None:
    store, request, workflow = _submit_refresh_request(runtime_db_url)

    read_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_waiting["action"] == "waiting"
    read_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
    )
    store.mark_api_worker_job_success(
        job_id=read_job["job_id"],
        run_id="pytest:read",
        summary={"rows": 2},
        result={
            "raw_rows_all": [
                {
                    "record_id": "rec-empty",
                    "fields": {"产品链接": "", "SKU-ID": "", "备注": ""},
                },
                {
                    "record_id": "rec-manual",
                    "fields": {"产品链接": "", "SKU-ID": "", "备注": "keep"},
                },
            ],
            "raw_rows": [
                {"record_id": "rec-empty", "fields": {"产品链接": "", "SKU-ID": ""}},
                {"record_id": "rec-manual", "fields": {"产品链接": "", "SKU-ID": ""}},
            ],
            "source_rows": [],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    cleanup_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert cleanup_waiting["action"] == "waiting"
    cleanup_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_write",
    )
    assert cleanup_job["payload"]["cleanup_kind"] == "delete_empty_rows"
    assert cleanup_job["payload"]["records"] == [
        {
            "op": "delete",
            "record_id": "rec-empty",
            "business_entity_key": "empty-row:rec-empty",
            "source_context": {"cleanup_reason": "empty_row"},
        }
    ]

    store.mark_api_worker_job_success(
        job_id=cleanup_job["job_id"],
        run_id="pytest:cleanup",
        summary={"deleted": 1},
        result={"deleted_count": 1, "target_record_ids": ["rec-empty"]},
    )
    request = store.load_task_request(request_id=request.request_id)
    read_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_advance["action"] == "advance"
    assert read_advance["next_stage"] == "dispatch_product_collection"


def test_refresh_runtime_release_request_after_child_completion_requeues_collect_stage(runtime_db_url: str) -> None:
    store, request, workflow = _submit_refresh_request(runtime_db_url)
    request = store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
        progress_stage="collect_product_data",
    )
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code="competitor_row_refresh",
        jobs=[
            {
                "business_key": PRODUCT_ID,
                "dedupe_key": f"{request.request_id}:collect:{PRODUCT_ID}",
                "payload": {
                    "stage_code": "collect_product_data",
                    "source_record_id": "row-release",
                    "product_identity": {"product_id": PRODUCT_ID},
                },
            }
        ],
    )
    row_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="competitor_row_refresh",
    )
    store.mark_api_worker_job_success(
        job_id=row_job["job_id"],
        run_id="pytest:release",
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released == [
        {
            "request_id": request.request_id,
            "stage_code": "collect_product_data",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    updated = store.load_task_request(request_id=request.request_id)
    assert updated.status == "pending"
    assert updated.current_stage == "collect_product_data"
