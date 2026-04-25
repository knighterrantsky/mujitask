from __future__ import annotations

from automation_business_scaffold.domains.tiktok.flows.search_keyword_competitor_products import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import keyword_search_parameter_mapper
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

TASK_CODE = "search_keyword_competitor_products"
SEED_TABLE_REF = "tbl_keyword_seed"
SEARCH_QUERY = "water bottle"
PRODUCT_URL = "https://www.tiktok.com/shop/pdp/123456789"
PRODUCT_ID = "123456789"


def test_keyword_search_parameter_mapper_builds_fastmoss_search_payload() -> None:
    mapped = keyword_search_parameter_mapper(
        {
            "search_keyword": SEARCH_QUERY,
            "filters": {"country_code": "US"},
            "sales_7d_threshold": "200",
            "max_candidates": "5",
            "fastmoss_search_order": "2,2",
        }
    )

    assert mapped["stage_code"] == "keyword_seed_import"
    assert mapped["search_mode"] == "keyword"
    assert mapped["keyword"] == SEARCH_QUERY
    assert mapped["search_query"] == SEARCH_QUERY
    assert mapped["filters"] == {"country_code": "US"}
    assert mapped["limit"] == 5
    assert mapped["sort"] == {"field": "day7_sold_count", "direction": "desc", "source_order": "2,2"}
    assert mapped["output_conditions"]["business_conditions"]["min_day7_sold_count"] == "200"


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
    _mark_api_job_success(
        store,
        job_id=job_id,
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


def _mark_api_job_success(
    store: RuntimeStore,
    *,
    job_id: str,
    summary: dict,
    result: dict,
) -> None:
    job = store.load_api_worker_job(job_id=job_id)
    stage_code = str((job.get("payload") or {}).get("stage_code") or "")
    store.update_task_request(
        request_id=str(job["request_id"]),
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=str(job["request_id"]),
        job_code=str(job["job_code"]),
    )
    assert claimed is not None and claimed["job_id"] == job_id
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=str(claimed["run_id"]),
        summary=summary,
        result=result,
    )


def _advance_to_refresh_stage(store: RuntimeStore, request, workflow) -> tuple[object, dict]:
    seed_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="keyword_seed_import",
        job_code="keyword_seed_import",
    )
    _mark_api_job_success(
        store,
        job_id=str(seed_job["job_id"]),
        summary={"candidate_count": 1, "written_count": 1},
        result={
            "normalized_candidates": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "search_rank": 1,
                    "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                }
            ],
            "seed_contexts": [
                {
                    "candidate_key": f"product:{PRODUCT_ID}",
                    "business_entity_key": f"product:{PRODUCT_ID}",
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "normalized_product_url": PRODUCT_URL,
                    "search_query": SEARCH_QUERY,
                    "search_rank": 1,
                    "source_context": {"product_id": PRODUCT_ID, "product_url": PRODUCT_URL},
                    "source_record_id": "seed-row-1",
                    "seed_status": "success",
                    "target_record_ids": ["seed-row-1"],
                }
            ],
            "seed_write_results": [{"product_id": PRODUCT_ID, "status": "success"}],
            "written_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "target_record_ids": ["seed-row-1"],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    seed_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_advance["next_stage"] == "dispatch_row_refresh_jobs"

    request = store.load_task_request(request_id=request.request_id)
    dispatch_advance = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_row_refresh_jobs",
    )
    assert dispatch_advance["next_stage"] == "refresh_competitor_rows"

    row_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="refresh_competitor_rows",
        job_code="competitor_row_refresh",
    )
    return request, row_job


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
    _mark_api_job_success(
        store,
        job_id=str(media_job["job_id"]),
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
    _mark_api_job_success(
        store,
        job_id=str(fact_job["job_id"]),
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
    _mark_api_job_success(
        store,
        job_id=str(write_job["job_id"]),
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
    request, row_job = _advance_to_refresh_stage(store, request, workflow)

    assert row_job["payload"]["source_record_id"] == "seed-row-1"
    _mark_api_job_success(
        store,
        job_id=str(row_job["job_id"]),
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
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["action"] == "finalized"
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["candidate_total_count"] == 1
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"


def test_keyword_runtime_zero_candidates_finalizes_success(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    seed_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_waiting["action"] == "waiting"
    seed_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="keyword_seed_import",
        job_code="keyword_seed_import",
    )
    _mark_api_job_success(
        store,
        job_id=str(seed_job["job_id"]),
        summary={"candidate_count": 0, "written_count": 0},
        result={
            "normalized_candidates": [],
            "seed_contexts": [],
            "seed_write_results": [],
            "written_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    seed_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="keyword_seed_import")
    assert seed_advance["next_stage"] == "dispatch_row_refresh_jobs"
    request = store.load_task_request(request_id=request.request_id)
    dispatch_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="dispatch_row_refresh_jobs")
    assert dispatch_advance["next_stage"] == "refresh_competitor_rows"
    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)

    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["summary"]["search_query"] == SEARCH_QUERY
    assert finalized["result"]["candidate_total_count"] == 0


def test_keyword_runtime_browser_fallback_path_finalizes(runtime_db_url: str) -> None:
    store, request, workflow = _submit_keyword_request(runtime_db_url)
    request, row_job = _advance_to_refresh_stage(store, request, workflow)

    _mark_api_job_success(
        store,
        job_id=str(row_job["job_id"]),
        summary={"row_status": "success"},
        result={
            "row_status": "success",
            "step_timeline": [
                {"step": "tiktok_request", "status": "fallback_required"},
                {"step": "browser_fallback", "status": "success"},
                {"step": "media_sync", "status": "success"},
                {"step": "fastmoss_fetch", "status": "success"},
                {"step": "fact_db_upsert", "status": "success"},
                {"step": "feishu_writeback", "status": "success"},
            ],
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    refresh_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="refresh_competitor_rows")
    assert refresh_advance["next_stage"] == "ready_for_summary"
    request = store.update_task_request(
        request_id=request.request_id,
        current_stage="ready_for_summary",
        progress_stage="ready_for_summary",
    )
    finalized = finalize_request(store=store, request=request, workflow=workflow)
    assert finalized["request_status"] == "success"
    assert finalized["summary"]["final_status"] == "success"
    assert finalized["result"]["row_results"][0]["browser_status"] == "success"
    assert finalized["result"]["row_results"][0]["row_status"] == "success"
