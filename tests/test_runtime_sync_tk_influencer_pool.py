from __future__ import annotations

import json

from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerResult
from automation_business_scaffold.domains.tiktok.flows import influencer_sync
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool.context.models import (
    DISCOVER_CREATORS_STAGE_CODE,
    DISPATCH_PRODUCT_STAGE_CODE,
    FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    READ_STAGE_CODE,
    SUMMARY_STAGE_CODE,
    SYNC_INFLUENCER_POOL_STAGE_CODE,
    TASK_CODE,
    WORKFLOW_CODE,
    WRITEBACK_STAGE_CODE,
)
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool.context.runtime_views import (
    _stage_child_summaries,
    _summarize_request_children_from_store,
)
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool.orchestrator import (
    advance_stage,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.flows.sync_tk_influencer_pool.summary import (
    finalize_request,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import build_tiktok_outbox_message_text
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.control_plane.executor.request_aggregation import (
    build_runtime_request_payload,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def test_product_creator_discovery_runtime_result_is_compact(monkeypatch) -> None:
    raw_payload = {"html": "<div>" + ("x" * 200_000) + "</div>"}
    product_fact_bundle = {
        "products": [{"product_id": "product-compact", "product_key": "fastmoss_product:product-compact"}],
        "media_assets": [{"entity_type": "product", "source_url": "https://example.test/image.jpg"}],
        "raw_api_responses": [{"source_endpoint": "goods.overview", "response_payload": raw_payload}],
        "product_sku_metric_snapshots": [{"sku_id": f"sku-{index}", "raw": raw_payload} for index in range(3)],
    }

    def fake_product_fetch(context: HandlerContext) -> HandlerResult:
        return HandlerResult.success(
            context,
            result={
                "product_fact_bundle": product_fact_bundle,
                "related_creators": [
                    {
                        "creator_id": "creator-compact",
                        "creator_identity": {
                            "creator_id": "creator-compact",
                            "uid": "123",
                            "unique_id": "compact.creator",
                            "profile_url": "https://example.test/creator",
                        },
                        "display_name": "Compact Creator",
                        "metrics": {"sold_count": 88, "follower_count": 12000},
                        "source_context": {"candidate_row": {"raw": raw_payload}},
                    }
                ],
                "metrics_snapshot": {"raw": raw_payload},
            },
        )

    monkeypatch.setattr(influencer_sync, "fastmoss_product_fetch_handler", fake_product_fetch)

    result = influencer_sync.run_product_creator_discovery_flow(
        HandlerContext(
            request_id="req-compact",
            job_id="job-compact",
            handler_code="product_creator_discovery",
            worker_type="api_worker",
            runtime_table="api_worker_job",
            payload={
                "source_context": {"source_record_id": "rec-compact", "product_id": "product-compact"},
                "product_identity": {"product_id": "product-compact"},
                "relation_policy": {"creator_sold_count_min": 10, "creator_follower_count_min": 1000},
            },
            workflow_code=WORKFLOW_CODE,
            stage_code=DISCOVER_CREATORS_STAGE_CODE,
            job_code="product_creator_discovery",
        )
    )

    assert result.status == "success"
    assert "product_fetch_result" not in result.result
    assert "raw_api_responses" not in result.result["product_fact_bundle"]
    assert "product_sku_metric_snapshots" not in result.result["product_fact_bundle"]
    assert "candidate_row" not in result.result["normalized_creator_candidates"][0]["source_context"]
    assert len(json.dumps(result.result, ensure_ascii=False)) < 10_000


def test_request_child_summary_uses_db_aggregate_queries() -> None:
    class Store:
        def summarize_api_worker_jobs_for_request(self, *, request_id: str) -> dict[str, object]:
            assert request_id == "req-summary"
            return {
                "total": 2,
                "active_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "counts": {"success": 1, "pending": 1},
            }

        def summarize_task_executions_for_request(self, *, request_id: str) -> dict[str, object]:
            assert request_id == "req-summary"
            return {
                "total": 1,
                "active_count": 0,
                "success_count": 0,
                "failed_count": 1,
                "skipped_count": 0,
                "counts": {"failed": 1},
            }

        def list_api_worker_jobs_for_request(self, **_: object) -> list[dict[str, object]]:
            raise AssertionError("full api_worker_job result_json should not be loaded for count refresh")

        def list_task_executions(self, **_: object) -> list[object]:
            raise AssertionError("full task_execution result_json should not be loaded for count refresh")

    summary = _summarize_request_children_from_store(store=Store(), request_id="req-summary")

    assert summary["total_count"] == 3
    assert summary["terminal_count"] == 2
    assert summary["success_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["counts"] == {"success": 1, "pending": 1, "failed": 1}


def test_stage_child_summaries_do_not_load_full_results() -> None:
    class Store:
        def list_api_worker_job_summaries_for_request(self, *, request_id: str, job_code: str = "") -> list[dict[str, object]]:
            assert request_id == "req-stage"
            assert job_code == ""
            return [
                {
                    "job_id": "job-1",
                    "status": "finished",
                    "payload": {"stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE},
                    "result": {},
                },
                {
                    "job_id": "job-2",
                    "status": "pending",
                    "payload": {"stage_code": WRITEBACK_STAGE_CODE},
                    "result": {},
                },
            ]

        def list_task_execution_summaries_for_request(self, *, request_id: str) -> list[dict[str, object]]:
            assert request_id == "req-stage"
            return [
                {
                    "execution_id": "exec-1",
                    "status": "finished",
                    "payload": {"stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE},
                    "result": {},
                }
            ]

        def list_api_worker_jobs_for_request(self, **_: object) -> list[dict[str, object]]:
            raise AssertionError("full api_worker_job result_json should not be loaded for stage checks")

        def list_task_executions(self, **_: object) -> list[object]:
            raise AssertionError("full task_execution result_json should not be loaded for stage checks")

    records = _stage_child_summaries(
        store=Store(),
        request_id="req-stage",
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )

    assert [record.get("job_id") or record.get("execution_id") for record in records] == ["job-1", "exec-1"]


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


def test_influencer_outbox_uses_product_counts_and_creator_write_counts() -> None:
    message = build_tiktok_outbox_message_text(
        request_id="req-influencer",
        task_code=TASK_CODE,
        summary={
            "final_status": "partial_success",
            "product_group_count": 2,
            "product_group_status_counts": {"success": 1, "failed": 1},
            "child_total_count": 4,
            "child_success_count": 3,
            "product_groups": [
                {
                    "source_record_id": "rec-success",
                    "product_id": "sku-success",
                    "final_status": "success",
                    "influencer_write_updated_count": 2,
                    "influencer_write_created_count": 1,
                },
                {
                    "source_record_id": "rec-failed",
                    "product_id": "sku-failed",
                    "final_status": "failed",
                    "influencer_write_updated_count": 0,
                    "influencer_write_created_count": 0,
                    "warnings": ["creator_sync_failed"],
                },
            ],
        },
        result={},
    )

    assert "商品：2 个" in message
    assert "商品成功：1 个" in message
    assert "商品失败：1 个" in message
    assert "商品组" not in message
    assert "1. SKU sku-success" in message
    assert "   更新达人数量：2" in message
    assert "   创建达人数量：1" in message
    assert "2. SKU sku-failed" in message
    assert "   更新达人数量：0" in message
    assert "   创建达人数量：0" in message


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
            status="waiting",
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
    store.update_task_request(
        request_id=request_id,
        status="waiting",
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
        summary={"handler_status": "success"},
        result=result,
    )


def _mark_stage_job_fastmoss_fallback_required(
    store: RuntimeStore,
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
    result: dict[str, object],
) -> dict[str, object]:
    job = next(
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)
        if (job.get("payload") or {}).get("stage_code") == stage_code
    )
    store.update_task_request(
        request_id=request_id,
        status="waiting",
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
    handler_result = {
        "status": "fallback_required",
        "handler_code": job_code,
        "summary": {"fallback_required": True},
        "result": result,
        "next_action": {"type": "browser_fallback", "payload": result},
    }
    return store.mark_api_worker_job_waiting(
        job_id=str(job["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={"handler_status": "fallback_required", "fallback_required": True},
        result={"handler_result": handler_result, **result},
        stage="browser_fallback_required",
        error_text="FastMoss auth/security recovery required",
        error_type="security_verification",
        error_code="fastmoss_security_verification_required",
    )


def test_api_worker_job_summary_query_does_not_load_large_result(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _create_request(store)
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="influencer_creator_sync",
        jobs=[
            {
                "business_key": "creator-heavy",
                "dedupe_key": f"{request.request_id}:creator-heavy",
                "payload": {
                    "stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE,
                    "creator_identity": {"creator_id": "creator-heavy"},
                    "product_hits": [{"source_record_id": "row-1", "product_id": "product-1", "product_key": "row-1:product-1"}],
                },
            }
        ],
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting",
        current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
        progress_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request.request_id,
        job_code="influencer_creator_sync",
    )
    assert claimed is not None
    store.mark_api_worker_job_success(
        job_id=str(claimed["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={
            "handler_status": "success",
            "internal_steps": {"fact_upsert": "success", "influencer_pool_write": "success"},
            "influencer_pool_write_status": "success",
        },
        result={"fact_result": {"fact_bundle": {"raw": "x" * 1_000_000}}},
    )

    summaries = store.list_api_worker_job_summaries_for_request(
        request_id=request.request_id,
        job_code="influencer_creator_sync",
    )

    assert len(summaries) == 1
    assert summaries[0]["result"] == {}
    assert summaries[0]["summary"]["influencer_pool_write_status"] == "success"


def test_runtime_request_payload_does_not_load_large_child_results(runtime_db_url: str, monkeypatch) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _create_request(store)
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="influencer_creator_sync",
        jobs=[
            {
                "business_key": "creator-heavy",
                "dedupe_key": f"{request.request_id}:creator-heavy",
                "payload": {"stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE},
            }
        ],
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting",
        current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
        progress_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )
    claimed = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request.request_id,
        job_code="influencer_creator_sync",
    )
    assert claimed is not None
    store.mark_api_worker_job_success(
        job_id=str(claimed["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={"handler_status": "success"},
        result={"handler_result": {"status": "success", "result": {"raw": "x" * 1_000_000}}},
    )

    monkeypatch.setattr(
        store,
        "list_api_worker_jobs_for_request",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full api job result_json loaded")),
    )
    monkeypatch.setattr(
        store,
        "list_task_executions",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full task execution result_json loaded")),
    )

    payload = build_runtime_request_payload(
        store=store,
        request_id=request.request_id,
        control_action="api_worker_once",
        message="pytest",
    )

    assert payload["child_total_count"] == 1
    assert payload["child_success_count"] == 1
    assert payload["api_worker_jobs"][0]["result"] == {}
    assert payload["api_worker_job_summary"]["success_count"] == 1


def test_sync_tk_influencer_pool_read_job_carries_max_source_rows(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store, max_source_rows=1)

    read_result = advance_stage(store=store, request=request, workflow=workflow, stage_code=READ_STAGE_CODE)

    assert read_result["action"] == "waiting"
    read_jobs = store.list_api_worker_jobs_for_request(request_id=request.request_id, job_code="feishu_table_read")
    assert len(read_jobs) == 1
    assert read_jobs[0]["payload"]["max_source_rows"] == 1
    assert read_jobs[0]["payload"]["request_payload"]["max_source_rows"] == 1


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
    assert read_job["status"] == "finished"
    assert read_job["result_status"] == "success"

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
        job_code="product_creator_discovery",
        result={
            "normalized_creator_candidates": [
                {
                    "creator_id": "creator-1",
                    "creator_identity": {"creator_id": "creator-1"},
                    "display_name": "Alice",
                    "metrics": {"sold_count": 72, "follower_count": 12000},
                }
            ],
            "product_fact_bundle": {"product_id": "product-1"},
        },
    )
    assert product_job["status"] == "finished"
    assert product_job["result_status"] == "success"

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
    assert discover_release["current_stage"] == SYNC_INFLUENCER_POOL_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=discover_release)

    sync_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        job_code="influencer_creator_sync",
        result={
            "creator_id": "creator-1",
            "status": "success",
            "internal_steps": {"creator_fetch": "success", "fact_upsert": "success", "influencer_pool_write": "success"},
            "influencer_pool_write": {"status": "success", "write_result": {"written_count": 1}},
            "product_hits": [{"source_record_id": "row-1", "product_id": "product-1", "product_key": "row-1:product-1"}],
        },
    )
    assert sync_job["status"] == "finished"
    assert sync_job["result_status"] == "success"

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == SYNC_INFLUENCER_POOL_STAGE_CODE

    request = store.load_task_request(request_id=request.request_id)
    sync_release = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )
    assert sync_release["action"] == "waiting"
    assert sync_release["current_stage"] == WRITEBACK_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=sync_release)

    writeback_job = _mark_stage_job_success(
        store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code="feishu_table_write",
        result={"written_count": 1, "target_record_ids": ["fs-row-status-1"]},
    )
    assert writeback_job["status"] == "finished"
    assert writeback_job["result_status"] == "success"

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
    message_text = finalized["outbox"][0]["payload"]["message_text"]
    assert "TK达人池同步完成" in message_text
    assert "商品：1 个" in message_text
    assert "商品成功：1 个" in message_text
    assert "1. SKU product-1" in message_text


def test_sync_tk_influencer_pool_fastmoss_browser_fallback_requeues_product_discovery(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store, fastmoss={"phone": "18000000000", "password": "secret", "live_fetch": True})
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="product_creator_discovery",
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
            },
            {
                "business_key": "product-2",
                "dedupe_key": f"{request.request_id}:product-2",
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": DISCOVER_CREATORS_STAGE_CODE,
                    "product_identity": {"product_id": "product-2"},
                    "source_context": {"source_record_id": "row-2", "product_id": "product-2"},
                },
            }
        ],
    )
    job_id = enqueue["created_records"][0]["job_id"]
    _mark_stage_job_fastmoss_fallback_required(
        store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="product_creator_discovery",
        result={
            "fallback_required": True,
            "fallback_reason": "fastmoss_api_security_verification",
            "verification_request": {
                "method": "GET",
                "path": "/api/goods/v3/base",
                "params": {"product_id": "product-1"},
                "region": "US",
            },
            "security_context": {"response_code": "MSG_SAFE_0001", "path": "/api/goods/v3/base"},
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    fallback_dispatch = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )
    assert fallback_dispatch["action"] == "waiting"
    assert fallback_dispatch["current_stage"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    _apply_stage_result(store, request_id=request.request_id, stage_result=fallback_dispatch)
    blocked_product_claim = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request.request_id,
        job_code="product_creator_discovery",
    )
    assert blocked_product_claim is None

    executions = store.list_task_executions(request_id=request.request_id)
    assert len(executions) == 1
    assert executions[0].payload["verification_request"]["path"] == "/api/goods/v3/base"
    claimed_browser = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request.request_id,
        item_codes=("fastmoss_security_browser_resolve",),
    )
    assert claimed_browser is not None
    store.mark_browser_execution_success(
        execution_id=claimed_browser.execution_id,
        run_id=claimed_browser.run_id,
        summary={"handler_status": "success"},
        result={
            "handler_result": {
                "status": "success",
                "handler_code": "fastmoss_security_browser_resolve",
                "result": {
                    "verified_path": "/api/goods/v3/base",
                    "cookie_cache": {"enabled": True, "cookie_count": 2},
                },
            },
            "verified_path": "/api/goods/v3/base",
            "cookie_cache": {"enabled": True, "cookie_count": 2},
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    requeue_result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )
    assert requeue_result["action"] == "waiting"
    assert requeue_result["current_stage"] == DISCOVER_CREATORS_STAGE_CODE
    retry_job = store.load_api_worker_job(job_id=job_id)
    assert retry_job["status"] == "pending"
    assert retry_job["payload"]["browser_fallback_resolved"] is True
    assert retry_job["payload"]["fastmoss_security_browser_fallback_attempt"] == 1


def test_sync_tk_influencer_pool_summary_routes_waiting_fallback_before_finalize(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store)
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="product_creator_discovery",
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
    _mark_stage_job_fastmoss_fallback_required(
        store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="product_creator_discovery",
        result={
            "fallback_required": True,
            "fallback_reason": "fastmoss_api_security_verification",
            "verification_request": {
                "method": "GET",
                "path": "/api/goods/v3/base",
                "params": {"product_id": "product-1"},
                "region": "US",
            },
            "security_context": {"response_code": "MSG_SAFE_0001", "path": "/api/goods/v3/base"},
        },
    )
    store.update_task_request(
        request_id=request.request_id,
        status="pending",
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
    )

    summary_result = advance_stage(
        store=store,
        request=store.load_task_request(request_id=request.request_id),
        workflow=workflow,
        stage_code=SUMMARY_STAGE_CODE,
    )

    assert summary_result["action"] == "advance"
    assert summary_result["next_stage"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    assert summary_result["details"]["summary_blocked"] is True


def test_sync_tk_influencer_pool_browser_fallback_failure_terminalizes_waiting_jobs(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store, fastmoss={"phone": "18000000000", "password": "secret", "live_fetch": True})
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="product_creator_discovery",
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
    job_id = enqueue["created_records"][0]["job_id"]
    _mark_stage_job_fastmoss_fallback_required(
        store,
        request_id=request.request_id,
        stage_code=DISCOVER_CREATORS_STAGE_CODE,
        job_code="product_creator_discovery",
        result={
            "fallback_required": True,
            "fallback_reason": "fastmoss_auth_session_recovery",
            "verification_request": {
                "method": "GET",
                "path": "/api/goods/v3/base",
                "params": {"product_id": "product-1"},
                "region": "US",
            },
            "security_context": {"response_code": "MAG_AUTH_3002", "path": "/api/goods/v3/base"},
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    fallback_dispatch = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )
    assert fallback_dispatch["action"] == "waiting"
    _apply_stage_result(store, request_id=request.request_id, stage_result=fallback_dispatch)

    claimed_browser = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request.request_id,
        item_codes=("fastmoss_security_browser_resolve",),
    )
    assert claimed_browser is not None
    store.mark_browser_execution_success(
        execution_id=claimed_browser.execution_id,
        run_id=claimed_browser.run_id,
        summary={"handler_status": "failed", "error_code": "fastmoss_auth_session_recovery_required"},
        result={
            "handler_result": {
                "status": "failed",
                "handler_code": "fastmoss_security_browser_resolve",
                "error": {
                    "error_type": "auth_failure",
                    "error_code": "fastmoss_auth_session_recovery_required",
                    "message": "FastMoss auth recovery is still required after browser fallback.",
                },
                "result": {"verified_path": "/api/goods/v3/base", "resolved": False},
            }
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    failure_result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )

    assert failure_result["action"] == "advance"
    assert failure_result["next_stage"] == DISCOVER_CREATORS_STAGE_CODE
    failed_job = store.load_api_worker_job(job_id=job_id)
    assert failed_job["status"] == "finished"
    assert failed_job["result_status"] == "failed"
    assert failed_job["error_code"] == "fastmoss_security_browser_fallback_failed"
    assert not [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request.request_id)
        if job["status"] == "waiting"
    ]


def test_sync_tk_influencer_pool_fastmoss_browser_fallback_requeues_creator_sync(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    workflow = get_workflow_definition(TASK_CODE)
    request = _create_request(store, fastmoss={"phone": "18000000000", "password": "secret", "live_fetch": True})
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="influencer_creator_sync",
        jobs=[
            {
                "business_key": "creator-1",
                "dedupe_key": f"{request.request_id}:creator-1",
                "payload": {
                    "request_id": request.request_id,
                    "task_code": TASK_CODE,
                    "workflow_code": WORKFLOW_CODE,
                    "stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE,
                    "creator_identity": {"creator_id": "creator-1", "uid": "creator-1"},
                    "product_hits": [{"source_record_id": "row-1", "product_id": "product-1", "product_key": "row-1:product-1"}],
                },
            }
        ],
    )
    job_id = enqueue["created_records"][0]["job_id"]
    _mark_stage_job_fastmoss_fallback_required(
        store,
        request_id=request.request_id,
        stage_code=SYNC_INFLUENCER_POOL_STAGE_CODE,
        job_code="influencer_creator_sync",
        result={
            "fallback_required": True,
            "fallback_reason": "fastmoss_auth_session_recovery",
            "verification_request": {
                "method": "GET",
                "path": "/api/author/v3/detail/baseInfo",
                "params": {"uid": "creator-1"},
                "region": "US",
            },
            "security_context": {"response_code": "MAG_AUTH_3001", "path": "/api/author/v3/detail/baseInfo"},
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    fallback_dispatch = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )
    assert fallback_dispatch["action"] == "waiting"
    _apply_stage_result(store, request_id=request.request_id, stage_result=fallback_dispatch)
    claimed_browser = store.claim_next_browser_execution(
        worker_id="pytest-browser",
        lease_seconds=30.0,
        request_id=request.request_id,
        item_codes=("fastmoss_security_browser_resolve",),
    )
    assert claimed_browser is not None
    store.mark_browser_execution_success(
        execution_id=claimed_browser.execution_id,
        run_id=claimed_browser.run_id,
        summary={"handler_status": "success"},
        result={
            "handler_result": {
                "status": "success",
                "handler_code": "fastmoss_security_browser_resolve",
                "result": {
                    "verified_path": "/api/author/v3/detail/baseInfo",
                    "cookie_cache": {"enabled": True, "cookie_count": 2},
                },
            },
            "verified_path": "/api/author/v3/detail/baseInfo",
            "cookie_cache": {"enabled": True, "cookie_count": 2},
        },
    )

    released = release_request_after_child_completion(store, request_id=request.request_id)
    assert released and released[0]["stage_code"] == FASTMOSS_SECURITY_FALLBACK_STAGE_CODE
    request = store.load_task_request(request_id=request.request_id)
    requeue_result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code=FASTMOSS_SECURITY_FALLBACK_STAGE_CODE,
    )
    assert requeue_result["action"] == "waiting"
    assert requeue_result["current_stage"] == SYNC_INFLUENCER_POOL_STAGE_CODE
    retry_job = store.load_api_worker_job(job_id=job_id)
    assert retry_job["status"] == "pending"
    assert retry_job["payload"]["browser_fallback_resolved"] is True
    assert retry_job["payload"]["fastmoss_security_browser_fallback_attempt"] == 1


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
        job_code="product_creator_discovery",
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
        job_code="product_creator_discovery",
        result={
            "normalized_creator_candidates": [
                {"creator_id": "creator-1", "creator_identity": {"creator_id": "creator-1"}},
                {"creator_id": "creator-2", "creator_identity": {"creator_id": "creator-2"}},
            ]
        },
    )

    creator_enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=TASK_CODE,
        job_code="influencer_creator_sync",
        jobs=[
            {
                "business_key": "creator-1",
                "dedupe_key": f"{request.request_id}:creator-1",
                "max_attempts": 1,
                "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE,
                        "creator_identity": {"creator_id": "creator-1"},
                        "product_hits": [{"source_record_id": "row-1", "product_id": "product-1", "product_key": "row-1:product-1"}],
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
                        "stage_code": SYNC_INFLUENCER_POOL_STAGE_CODE,
                        "creator_identity": {"creator_id": "creator-2"},
                        "product_hits": [{"source_record_id": "row-1", "product_id": "product-1", "product_key": "row-1:product-1"}],
                    },
                },
            ],
    )
    success_job_id = creator_enqueue["created_records"][0]["job_id"]
    failed_job_id = creator_enqueue["created_records"][1]["job_id"]
    store.update_task_request(
        request_id=request.request_id,
        status="waiting",
        current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
        progress_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )
    claimed_success = store.claim_next_api_worker_job(
        worker_id="pytest-api",
        lease_seconds=30.0,
        request_id=request.request_id,
        job_code="influencer_creator_sync",
    )
    assert claimed_success is not None and claimed_success["job_id"] == success_job_id
    store.mark_api_worker_job_success(
        job_id=success_job_id,
        run_id=str(claimed_success["run_id"]),
        summary={"handler_status": "success"},
        result={
            "creator_id": "creator-1",
            "status": "success",
            "internal_steps": {"creator_fetch": "success", "fact_upsert": "success", "influencer_pool_write": "success"},
            "influencer_pool_write": {"status": "success", "write_result": {"written_count": 1}},
        },
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting",
        current_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
        progress_stage=SYNC_INFLUENCER_POOL_STAGE_CODE,
    )
    claimed_failed = store.claim_next_api_worker_job(worker_id="pytest-api", lease_seconds=30.0, request_id=request.request_id, job_code="influencer_creator_sync")
    assert claimed_failed is not None and claimed_failed["job_id"] == failed_job_id
    store.mark_api_worker_job_retry_or_failed(
        job_id=failed_job_id,
        run_id=str(claimed_failed["run_id"]),
        error_text="creator fetch failed",
        error_type="transport",
        error_code="creator_fetch_failed",
        retry_delay_seconds=0.0,
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
