from __future__ import annotations

from automation_business_scaffold.control_plane.executor.workflow_registry import load_workflow_runtime
from automation_business_scaffold.domains.tiktok.flows.refresh_current_competitor_table import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

TASK_CODE = "refresh_competitor_row_by_url"
SOURCE_TABLE_REF = "tbl_competitor_source"
PRODUCT_ID = "123456789"
PRODUCT_URL = f"https://www.tiktok.com/shop/pdp/{PRODUCT_ID}"


def _store(runtime_db_url: str) -> RuntimeStore:
    return RuntimeStore(db_url=runtime_db_url)


def _submit_request(runtime_db_url: str) -> tuple[RuntimeStore, object, object]:
    store = _store(runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=TASK_CODE,
        payload={
            "source_table_ref": SOURCE_TABLE_REF,
            "product_url": PRODUCT_URL,
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


def _mark_stage_job_success(
    store: RuntimeStore,
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
    summary: dict,
    result: dict,
) -> dict:
    job = _latest_stage_job(store, request_id=request_id, stage_code=stage_code, job_code=job_code)
    store.update_task_request(
        request_id=request_id,
        status="waiting_children",
        current_stage=stage_code,
        progress_stage=stage_code,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request_id,
        job_code=job_code,
    )
    assert claimed is not None and claimed["job_id"] == job["job_id"]
    return store.mark_api_worker_job_success(
        job_id=str(job["job_id"]),
        run_id=str(claimed["run_id"]),
        summary=summary,
        result=result,
    )


def test_runtime_module_is_loadable_for_refresh_competitor_row_by_url(runtime_db_url: str) -> None:
    runtime = load_workflow_runtime(TASK_CODE)
    assert runtime is not None
    assert runtime.advance_stage is advance_stage
    assert runtime.finalize_request is finalize_request
    assert runtime.release_request_after_child_completion is release_request_after_child_completion

    store, request, workflow = _submit_request(runtime_db_url)

    read_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_waiting["action"] == "waiting"
    read_job = _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
    )
    assert read_job["payload"]["product_url"] == PRODUCT_URL
    assert read_job["payload"]["filter_spec"] == {}

    _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
        summary={"rows": 1},
        result={
            "source_rows": [
                {
                    "source_record_id": "row-1",
                    "product_id": PRODUCT_ID,
                    "product_url": PRODUCT_URL,
                    "product_identity": {
                        "product_id": PRODUCT_ID,
                        "product_url": PRODUCT_URL,
                        "normalized_product_url": PRODUCT_URL,
                    },
                    "source_context": {
                        "source_record_id": "row-1",
                        "source_table_ref": SOURCE_TABLE_REF,
                        "source_fields": {"产品链接": PRODUCT_URL},
                    },
                    "business_key": f"product:{PRODUCT_ID}",
                    "normalized_product_url": PRODUCT_URL,
                }
            ],
            "adapter_summary": {"lookup_status": "matched", "matched_row_count": 1},
        },
    )
    request = store.load_task_request(request_id=request.request_id)
    read_advance = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_advance["action"] == "advance"
    assert read_advance["next_stage"] == "dispatch_product_collection"


def test_runtime_finalize_fails_when_product_url_is_not_found(runtime_db_url: str) -> None:
    store, request, workflow = _submit_request(runtime_db_url)

    read_waiting = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_waiting["action"] == "waiting"
    _latest_stage_job(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
    )
    _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code="read_competitor_rows",
        job_code="feishu_table_read",
        summary={"rows": 0},
        result={
            "source_rows": [],
            "adapter_summary": {"lookup_status": "not_found", "matched_row_count": 0},
        },
    )

    request = store.load_task_request(request_id=request.request_id)
    read_finalize = advance_stage(store=store, request=request, workflow=workflow, stage_code="read_competitor_rows")
    assert read_finalize["action"] == "finalize"
    assert read_finalize["final_status"] == "failed"
    assert read_finalize["summary"]["counts"] == {"competitor_row_not_found": 1}
