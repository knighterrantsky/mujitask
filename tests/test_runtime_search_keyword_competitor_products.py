from __future__ import annotations

from automation_business_scaffold.business.flows.runtime_search_keyword_competitor_products import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.business.flows.runtime_workflow_registry import load_workflow_runtime
from automation_business_scaffold.business.workflow_defs import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

TASK_CODE = "search_keyword_competitor_products"
SEED_TABLE_REF = "tbl_keyword_seed"
SEARCH_QUERY = "water bottle"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"


def _store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _submit_keyword_request(runtime_db_url: str) -> tuple[RuntimeStore, object, object]:
    store = _store(runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload={
            "search_query": SEARCH_QUERY,
            "filters": {"country_code": "US"},
            "output_conditions": {"require_product_url": True},
            "max_candidates": 5,
            "seed_table_ref": SEED_TABLE_REF,
            "reply_target": "reply://pytest",
        },
        requested_by="pytest",
        source_channel_code="console",
        reply_target="reply://pytest",
    )
    workflow = get_workflow_definition(TASK_CODE)
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


def _latest_stage_execution(store: RuntimeStore, *, request_id: str, stage_code: str, item_code: str):
    executions = [
        execution
        for execution in store.list_task_executions(request_id=request_id)
        if str((execution.payload or {}).get("stage_code") or "") == stage_code
        and str(execution.item_code or "") == item_code
    ]
    assert executions, f"expected stage execution {stage_code}/{item_code}"
    return executions[-1]


def _mark_search_success(store: RuntimeStore, *, job_id: str, candidate_suffix: str = "1") -> None:
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=f"pytest:search:{candidate_suffix}",
        summary={"candidates": 1},
        result={
            "candidates": [
                {
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "rank": 1,
                    "title": f"Water bottle {candidate_suffix}",
                }
            ],
            "condition_context": {"normalized": True},
        },
    )


def _advance_to_collect_stage(store: RuntimeStore, request, workflow) -> tuple[object, dict, dict]:
    search_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="search_product_candidates")
    assert search_waiting["action"] == "waiting"
    search_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="search_product_candidates",
        job_code="fastmoss_product_search",
    )
    _mark_search_success(store, job_id=search_job["job_id"])

    request = store.load_task_request(request_id=request.request_id)
    search_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="search_product_candidates")
    assert search_advance["next_stage"] == "process_product_candidates"

    request = store.load_task_request(request_id=request.request_id)
    process_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="process_product_candidates")
    assert process_advance["next_stage"] == "insert_seed_rows"

    request = store.load_task_request(request_id=request.request_id)
    insert_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="insert_seed_rows")
    assert insert_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="insert_seed_rows",
        job_code="feishu_table_write",
    )
    store.mark_api_worker_job_success(
        job_id=seed_job["job_id"],
        run_id="pytest:seed-write",
        summary={"written": 1},
        result={"written_count": 1, "target_record_ids": ["seed-row-1"]},
    )

    request = store.load_task_request(request_id=request.request_id)
    insert_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="insert_seed_rows")
    assert insert_advance["next_stage"] == "dispatch_product_collection"

    request = store.load_task_request(request_id=request.request_id)
    dispatch_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_product_collection",
    )
    assert dispatch_advance["next_stage"] == "collect_product_data"

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
    return request, tiktok_job, fastmoss_job


def _advance_from_sync_to_finalization(store: RuntimeStore, request, workflow) -> dict:
    request = store.load_task_request(request_id=request.request_id)
    sync_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="sync_media")
    assert sync_waiting["action"] == "waiting"
    media_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="sync_media",
        job_code="media_asset_sync",
    )
    store.mark_api_worker_job_success(
        job_id=media_job["job_id"],
        run_id="pytest:media",
        summary={"synced": 1},
        result={"synced_assets": [{"source_url": "https://cdn.example.com/p1.jpg"}]},
    )

    request = store.load_task_request(request_id=request.request_id)
    sync_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="sync_media")
    assert sync_advance["next_stage"] == "persist_facts"

    request = store.load_task_request(request_id=request.request_id)
    persist_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_waiting["action"] == "waiting"
    fact_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="persist_facts",
        job_code="fact_bundle_upsert",
    )
    store.mark_api_worker_job_success(
        job_id=fact_job["job_id"],
        run_id="pytest:fact",
        summary={"upserted": 1},
        result={"upserted_entities": [PRODUCT_ID]},
    )

    request = store.load_task_request(request_id=request.request_id)
    persist_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="persist_facts")
    assert persist_advance["next_stage"] == "writeback_competitor_rows"

    request = store.load_task_request(request_id=request.request_id)
    write_waiting = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="writeback_competitor_rows",
    )
    assert write_waiting["action"] == "waiting"
    write_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="writeback_competitor_rows",
        job_code="feishu_table_write",
    )
    store.mark_api_worker_job_success(
        job_id=write_job["job_id"],
        run_id="pytest:writeback",
        summary={"written": 1},
        result={"written_count": 1, "target_record_ids": ["seed-row-1"]},
    )

    request = store.load_task_request(request_id=request.request_id)
    write_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="writeback_competitor_rows",
    )
    assert write_advance["next_stage"] == "ready_for_summary"

    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    return finalize_request(store=store, request=request, workflow=workflow)


def test_keyword_runtime_module_is_loadable_and_happy_path_finalizes(runtime_db_url: str) -> None:
    runtime = load_workflow_runtime(TASK_CODE)
    assert runtime is not None
    assert runtime.advance_stage is advance_stage
    assert runtime.finalize_request is finalize_request
    assert runtime.release_request_after_child_completion is release_request_after_child_completion

    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, tiktok_job, fastmoss_job = _advance_to_collect_stage(store, request, workflow)

    assert tiktok_job["payload"]["source_record_id"] == "seed-row-1"
    assert fastmoss_job["payload"]["source_record_id"] == "seed-row-1"
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
    assert collect_advance["next_stage"] == "sync_media"

    finalized = _advance_from_sync_to_finalization(store, request, workflow)
    assert finalized["action"] == "finalized"
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["candidate_total_count"] == 1
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"


def test_keyword_runtime_browser_fallback_path_finalizes(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, tiktok_job, fastmoss_job = _advance_to_collect_stage(store, request, workflow)

    store.mark_api_worker_job_success(
        job_id=tiktok_job["job_id"],
        run_id="pytest:tiktok-fallback",
        summary={"transport": "request"},
        result={
            "fallback_required": True,
            "fallback_source_job_id": "fallback-job-1",
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
                "gmv_amount": 200,
            }
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    collect_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="collect_product_data")
    assert collect_advance["next_stage"] == "browser_fallback"

    request = store.load_task_request(request_id=request.request_id)
    browser_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="browser_fallback")
    assert browser_waiting["action"] == "waiting"
    execution = _latest_stage_execution(
        store,
        request_id=request.request_id,
        stage_code="browser_fallback",
        item_code="tiktok_product_browser_fetch",
    )
    store.mark_browser_execution_success(
        execution_id=execution.execution_id,
        run_id="pytest:browser",
        summary={"transport": "browser"},
        result={
            "normalized_product_result": {
                "product_id": PRODUCT_ID,
                "product_url": PRODUCT_URL,
                "media_assets": [
                    {
                        "source_url": "https://cdn.example.com/p-browser.jpg",
                        "source_type": "image",
                        "mime_type": "image/jpeg",
                    }
                ],
            }
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    browser_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="browser_fallback")
    assert browser_advance["next_stage"] == "sync_media"

    finalized = _advance_from_sync_to_finalization(store, request, workflow)
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_results"][0]["browser_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
