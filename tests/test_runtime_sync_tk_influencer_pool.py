from __future__ import annotations

from automation_business_scaffold.business.flows.runtime_sync_tk_influencer_pool import (
    COLLECT_CREATOR_STAGE_CODE,
    DISCOVER_CREATORS_STAGE_CODE,
    DISPATCH_PRODUCT_STAGE_CODE,
    FINALIZE_PRODUCT_STAGE_CODE,
    READ_STAGE_CODE,
    SUMMARY_STAGE_CODE,
    TASK_CODE,
    WORKFLOW_CODE,
    WRITEBACK_STAGE_CODE,
    WRITE_POOL_STAGE_CODE,
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.competitor_intelligence.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _create_request(store: RuntimeStore, **payload: object):
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload={
            "source_table_ref": "feishu://competitor-table",
            "influencer_pool_table_ref": "feishu://influencer-pool-table",
            "competitor_status_table_ref": "feishu://competitor-status-table",
            "reply_target": "reply://pytest",
            **payload,
        },
        requested_by="pytest",
    )
    store.update_task_request(
        request_id=request.request_id,
        current_stage=READ_STAGE_CODE,
        progress_stage=READ_STAGE_CODE,
    )
    return store.load_task_request(request_id=request.request_id)


def _apply_stage_result(store: RuntimeStore, *, request_id: str, stage_result: dict[str, object]) -> None:
    action = str(stage_result["action"])
    if action == "advance":
        next_stage = str(stage_result["next_stage"])
        store.update_task_request(
            request_id=request_id,
            status="pending",
            current_stage=next_stage,
            progress_stage=next_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            error_text="",
        )
    elif action == "waiting":
        current_stage = str(stage_result.get("current_stage") or "")
        store.update_task_request(
            request_id=request_id,
            status="waiting_children",
            current_stage=current_stage,
            progress_stage=current_stage,
            worker_id="",
            lease_until=0.0,
            heartbeat_at=0.0,
            error_text="",
        )


def _mark_stage_job_success(store: RuntimeStore, *, request_id: str, stage_code: str, job_code: str, result: dict[str, object]) -> dict[str, object]:
    job = next(
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
        if (job.get("payload") or {}).get("stage_code") == stage_code
    )
    return store.mark_api_worker_job_success(
        job_id=str(job["job_id"]),
        run_id=f"pytest-{job['job_id']}",
        summary={"handler_status": "success"},
        result=result,
    )


def test_sync_tk_influencer_pool_runtime_module_walks_all_stages(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store)

    read_result = advance_stage(store=store, request=request, workflow=workflow, stage_code=READ_STAGE_CODE)
    assert read_result["action"] == "waiting"
    assert read_result["current_stage"] == READ_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=read_result)

    read_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
        result={
            "source_rows": [
                {
                    "source_record_id": "row-1",
                    "product_id": "product-1",
                    "product_identity": {"product_id": "product-1", "product_url": "https://example.com/p/1"},
                }
            ]
        },
    )
    assert read_job["status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == READ_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    read_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code=READ_STAGE_CODE)
    assert read_advance == {"action": "advance", "next_stage": DISPATCH_PRODUCT_STAGE_CODE, "details": {"stage_transition": "competitor_candidates_ready"}}
    _apply_stage_result(store, request_id=request.request_id, stage_result=read_advance)

    request = store.load_task_request(request_id=request.request_id)
    dispatch_products = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=DISPATCH_PRODUCT_STAGE_CODE,
    )
    assert dispatch_products["action"] == "waiting"
    assert dispatch_products["current_stage"] == DISCOVER_CREATORS_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=dispatch_products)

    product_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
        result={
            "related_creators": [
                {"creator_id": "creator-1", "creator_identity": {"creator_id": "creator-1"}, "display_name": "Alice"}
            ],
            "product_fact_bundle": {"product_id": "product-1"},
        },
    )
    assert product_job["status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == DISCOVER_CREATORS_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    discover_release = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
    )
    assert discover_release["action"] == "waiting"
    assert discover_release["current_stage"] == COLLECT_CREATOR_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=discover_release)

    creator_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
        job_code="fastmoss_creator_fetch",
        result={"creator_fact_bundle": {"creator_id": "creator-1", "display_name": "Alice"}},
    )
    assert creator_job["status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == COLLECT_CREATOR_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    collect_release = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=COLLECT_CREATOR_STAGE_CODE,
    )
    assert collect_release["action"] == "waiting"
    assert collect_release["current_stage"] == WRITE_POOL_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=collect_release)

    write_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
        result={"written_count": 1, "target_record_ids": ["fs-row-1"]},
    )
    assert write_job["status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == WRITE_POOL_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    write_release = advance_stage(store=store, request=request, workflow=workflow, stage_code=WRITE_POOL_STAGE_CODE)
    assert write_release == {"action": "advance", "next_stage": FINALIZE_PRODUCT_STAGE_CODE}
    _apply_stage_result(store, request_id=request.request_id, stage_result=write_release)

    request = store.load_task_request(request_id=request.request_id)
    finalize_products = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FINALIZE_PRODUCT_STAGE_CODE,
    )
    assert finalize_products["action"] == "waiting"
    assert finalize_products["current_stage"] == WRITEBACK_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=finalize_products)

    writeback_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code="feishu_table_write",
        result={"written_count": 1, "target_record_ids": ["fs-row-status-1"]},
    )
    assert writeback_job["status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == WRITEBACK_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    writeback_release = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=WRITEBACK_STAGE_CODE,
    )
    assert writeback_release == {"action": "advance", "next_stage": SUMMARY_STAGE_CODE}
    _apply_stage_result(store, request_id=request.request_id, stage_result=writeback_release)

    request = store.load_task_request(request_id=request.request_id)
    finalized = finalize_request(store=store, request=request, workflow=workflow)

    assert finalized["request_status"] == "success"
    assert finalized["current_stage"] == "completed"
    assert finalized["final_status"] == "success"
    assert finalized["summary"]["product_group_count"] == 1
    assert finalized["summary"]["product_group_status_counts"] == {"success": 1}
    assert finalized["outbox"]
    assert finalized["outbox"][0]["event_type"] == "task_request.completed"


def test_sync_tk_influencer_pool_finalize_request_reports_partial_success(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store)

    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="feishu_table_read",
        jobs=[
            {
                "business_key": "feishu://competitor-table",
                "dedupe_key": f"{request.request_id}:read",
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": READ_STAGE_CODE,
                    "source_table_ref": "feishu://competitor-table",
                },
            }
        ],
    )
    _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
        result={
            "source_rows": [
                {
                    "source_record_id": "row-1",
                    "product_id": "product-1",
                    "product_identity": {"product_id": "product-1"},
                }
            ]
        },
    )

    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "product-1",
                "dedupe_key": f"{request.request_id}:product-1",
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": DISCOVER_CREATORS_STAGE_CODE,
                    "product_identity": {"product_id": "product-1"},
                    "source_context": {"source_record_id": "row-1", "product_id": "product-1"},
                },
            }
        ],
    )
    _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="fastmoss_product_fetch",
        result={
            "related_creators": [
                {"creator_id": "creator-1", "creator_identity": {"creator_id": "creator-1"}},
                {"creator_id": "creator-2", "creator_identity": {"creator_id": "creator-2"}},
            ]
        },
    )

    creator_enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="fastmoss_creator_fetch",
        jobs=[
            {
                "business_key": "creator-1",
                "dedupe_key": f"{request.request_id}:creator-1",
                "max_attempts": 1,
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": COLLECT_CREATOR_STAGE_CODE,
                    "creator_identity": {"creator_id": "creator-1"},
                    "source_context": {"source_record_id": "row-1", "product_id": "product-1"},
                },
            },
            {
                "business_key": "creator-2",
                "dedupe_key": f"{request.request_id}:creator-2",
                "max_attempts": 1,
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": COLLECT_CREATOR_STAGE_CODE,
                    "creator_identity": {"creator_id": "creator-2"},
                    "source_context": {"source_record_id": "row-1", "product_id": "product-1"},
                },
            },
        ],
    )
    success_job_id = creator_enqueue["created_records"][0]["job_id"]
    failed_job_id = creator_enqueue["created_records"][1]["job_id"]
    store.mark_api_worker_job_success(
        job_id=success_job_id,
        run_id=f"pytest-{success_job_id}",
        summary={"handler_status": "success"},
        result={"creator_fact_bundle": {"creator_id": "creator-1"}},
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage=COLLECT_CREATOR_STAGE_CODE,
        progress_stage=COLLECT_CREATOR_STAGE_CODE,
    )
    claimed_failed = store.claim_next_api_worker_job(worker_id="pytest-api", lease_seconds=30.0, request_id=request.request_id, job_code="fastmoss_creator_fetch")
    assert claimed_failed is not None and claimed_failed["job_id"] == failed_job_id
    store.mark_api_worker_job_retry_or_failed(
        job_id=failed_job_id,
        run_id=f"pytest-{failed_job_id}",
        error_text="creator fetch failed",
        error_type="transport",
        error_code="creator_fetch_failed",
        retry_delay_seconds=0.0,
    )

    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="feishu_table_write",
        jobs=[
            {
                "business_key": "creator-1",
                "dedupe_key": f"{request.request_id}:write:creator-1",
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": WRITE_POOL_STAGE_CODE,
                        "target_table_ref": "feishu://influencer-pool-table",
                        "source_context": {"source_record_id": "row-1", "product_id": "product-1"},
                        "records": [{"creator_id": "creator-1"}],
                    },
                }
            ],
        )
    _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
        result={"written_count": 1, "target_record_ids": ["fs-row-1"]},
    )

    store.update_task_request(
        request_id=request.request_id,
        status="pending",
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
    )
    request = store.load_task_request(request_id=request.request_id)

    finalized = finalize_request(store=store, request=request, workflow=workflow)

    assert finalized["request_status"] == "partial_success"
    assert finalized["final_status"] == "partial_success"
    assert finalized["summary"]["product_group_status_counts"] == {"partial_success": 1}
    assert "partial_creator_projection" in finalized["summary"]["warnings"]
