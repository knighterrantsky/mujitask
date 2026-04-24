from __future__ import annotations

from automation_business_scaffold.domains.tiktok.flows.refresh_current_competitor_table import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
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


def test_refresh_runtime_module_is_loadable_and_happy_path_finalizes(runtime_db_url: str) -> None:
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
    tiktok_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="tiktok_product_request_fetch",
    )
    fastmoss_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="fastmoss_product_fetch",
    )
    assert tiktok_job["payload"]["source_record_id"] == "row-1"
    assert fastmoss_job["payload"]["source_record_id"] == "row-1"

    store.mark_api_worker_job_success(
        job_id=tiktok_job["job_id"],
        run_id="pytest:tiktok",
        summary={"transport": "request"},
        result={
            "normalized_product_result": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
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
    store.mark_api_worker_job_success(
        job_id=fastmoss_job["job_id"],
        run_id="pytest:fastmoss",
        summary={"transport": "fastmoss"},
        result={
            "product_fact_bundle": {
                "product_id": PRODUCT_ID,
                "gmv_currency": "USD",
                "gmv_amount": 100,
            }
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    collect_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="collect_product_data")
    assert collect_advance["action"] == "advance"
    assert collect_advance["next_stage"] == "persist_facts"

    request = store.load_task_request(request_id=request.request_id)
    persist_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_waiting["action"] == "waiting"
    media_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="persist_facts",
        job_code="media_asset_sync",
    )
    fact_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="persist_facts",
        job_code="fact_bundle_upsert",
    )
    assert media_job["payload"]["source_record_id"] == "row-1"
    assert fact_job["payload"]["fact_bundle"]["product_identity"]["product_id"] == PRODUCT_ID

    store.mark_api_worker_job_success(
        job_id=media_job["job_id"],
        run_id="pytest:media",
        summary={"synced": 1},
        result={"synced_assets": [{"source_url": "https://cdn.example.com/p1.jpg"}]},
    )
    store.mark_api_worker_job_success(
        job_id=fact_job["job_id"],
        run_id="pytest:fact",
        summary={"upserted": 1},
        result={"upserted_entities": [PRODUCT_ID]},
    )
    request = store.load_task_request(request_id=request.request_id)
    persist_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_advance["action"] == "advance"
    assert persist_advance["next_stage"] == "writeback_competitor_rows"

    request = store.load_task_request(request_id=request.request_id)
    write_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="writeback_competitor_rows")
    assert write_waiting["action"] == "waiting"
    write_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="writeback_competitor_rows",
        job_code="feishu_table_write",
    )
    record_payload = write_job["payload"]["records"][0]
    assert record_payload["source_record_id"] == "row-1"
    assert record_payload["refresh_status"] == "success"

    store.mark_api_worker_job_success(
        job_id=write_job["job_id"],
        run_id="pytest:writeback",
        summary={"written": 1},
        result={"written_count": 1, "target_record_ids": ["row-1"]},
    )
    request = store.load_task_request(request_id=request.request_id)
    write_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="writeback_competitor_rows",
    )
    assert write_advance["action"] == "advance"
    assert write_advance["next_stage"] == "ready_for_summary"

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
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"
    assert finalized["outbox"][0]["payload"]["summary_payload"]["final_status"] == "success"


def test_refresh_runtime_browser_fallback_stage_enqueues_execution_and_advances(runtime_db_url: str) -> None:
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
        summary={"rows": 1},
        result={
            "source_rows": [
                {
                    "source_record_id": "row-bf",
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                }
            ]
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    request = store.load_task_request(request_id=request.request_id)
    advance_stage(store=store, request=request, workflow=workflow, stage_code="dispatch_product_collection")

    tiktok_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="tiktok_product_request_fetch",
    )
    fastmoss_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="collect_product_data",
        job_code="fastmoss_product_fetch",
    )
    store.mark_api_worker_job_success(
        job_id=tiktok_job["job_id"],
        run_id="pytest:tiktok-fallback",
        summary={"transport": "request"},
        result={
            "handler_result": {
                "status": "fallback_required",
                "handler_code": "tiktok_product_request_fetch",
                "request_id": request.request_id,
                "job_id": tiktok_job["job_id"],
            },
            "fallback_required": True,
            "fallback_reason": "request_blocked",
            "fallback_source_job_id": tiktok_job["job_id"],
        },
    )
    store.mark_api_worker_job_success(
        job_id=fastmoss_job["job_id"],
        run_id="pytest:fastmoss",
        summary={"transport": "fastmoss"},
        result={"product_fact_bundle": {"product_id": PRODUCT_ID}},
    )

    request = store.load_task_request(request_id=request.request_id)
    collect_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="collect_product_data")
    assert collect_advance["action"] == "advance"
    assert collect_advance["next_stage"] == "browser_fallback"

    request = store.load_task_request(request_id=request.request_id)
    browser_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="browser_fallback")
    assert browser_waiting["action"] == "waiting"
    executions = [
        execution
        for execution in store.list_task_executions(request_id=request.request_id)
        if execution.item_code == "tiktok_product_browser_fetch"
    ]
    assert len(executions) == 1
    execution = executions[0]
    assert execution.payload["source_record_id"] == "row-bf"

    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id="pytest:browser",
        summary={"transport": "browser"},
        result={
            "normalized_product_result": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
            }
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    browser_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="browser_fallback")
    assert browser_advance["action"] == "advance"
    assert browser_advance["next_stage"] == "persist_facts"


def test_refresh_runtime_release_request_after_child_completion_requeues_worker_stage(runtime_db_url: str) -> None:
    store, request, workflow = _submit_refresh_request(runtime_db_url)
    request = store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="persist_facts",
        progress_stage="persist_facts",
    )
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code="fact_bundle_upsert",
        jobs=[
            {
                "business_key": PRODUCT_ID,
                "dedupe_key": f"{request.request_id}:persist:{PRODUCT_ID}",
                "payload": {
                    "stage_code": "persist_facts",
                    "source_record_id": "row-release",
                    "fact_bundle": {"product_id": PRODUCT_ID},
                },
            }
        ],
    )
    fact_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="persist_facts",
        job_code="fact_bundle_upsert",
    )
    store.mark_api_worker_job_success(
        job_id=fact_job["job_id"],
        run_id="pytest:release",
        summary={"upserted": 1},
        result={"upserted_entities": [PRODUCT_ID]},
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released == [
        {
            "request_id": request.request_id,
            "stage_code": "persist_facts",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    updated = store.load_task_request(request_id=request.request_id)
    assert updated.status == "pending"
    assert updated.current_stage == "persist_facts"
