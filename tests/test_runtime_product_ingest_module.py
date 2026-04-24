from __future__ import annotations

from dataclasses import replace

from automation_business_scaffold.control_plane.runtime_config.settings import PRODUCT_INGEST_TASK_CODE
from automation_business_scaffold.domains.tiktok.flows.tiktok_fastmoss_product_ingest import (
    advance_stage,
    finalize_request,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition
from automation_business_scaffold.infrastructure.runtime.runtime_records import (
    RuntimeTaskExecutionRecord,
    RuntimeTaskRequestRecord,
)


class FakeRuntimeStore:
    def __init__(
        self,
        *,
        request: RuntimeTaskRequestRecord,
        api_jobs: list[dict] | None = None,
        executions: list[RuntimeTaskExecutionRecord] | None = None,
    ) -> None:
        self.requests = {request.request_id: request}
        self.api_jobs = list(api_jobs or [])
        self.executions = list(executions or [])
        self.outbox: list[dict] = []
        self._job_seq = len(self.api_jobs)
        self._execution_seq = len(self.executions)

    def load_task_request(self, *, request_id: str) -> RuntimeTaskRequestRecord:
        return self.requests[request_id]

    def update_task_request(self, *, request_id: str, **updates: object) -> RuntimeTaskRequestRecord:
        current = self.requests[request_id]
        self.requests[request_id] = replace(current, **updates)
        return self.requests[request_id]

    def list_api_worker_jobs_for_request(self, *, request_id: str) -> list[dict]:
        return [job for job in self.api_jobs if str(job.get("request_id")) == request_id]

    def summarize_api_worker_jobs_for_request(self, *, request_id: str) -> dict:
        jobs = self.list_api_worker_jobs_for_request(request_id=request_id)
        counts: dict[str, int] = {}
        for job in jobs:
            status = str(job.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {"total": len(jobs), "counts": counts}

    def list_task_executions(self, *, request_id: str) -> list[RuntimeTaskExecutionRecord]:
        return [execution for execution in self.executions if execution.request_id == request_id]

    def list_request_outbox(self, *, request_id: str) -> list[object]:
        class _OutboxRecord:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def to_dict(self) -> dict:
                return dict(self._payload)

        return [_OutboxRecord(record) for record in self.outbox if str(record.get("ref_id")) == request_id]

    def enqueue_api_worker_jobs(self, *, request_id: str, task_code: str, job_code: str, jobs: list[dict]) -> dict:
        created: list[dict] = []
        for job in jobs:
            self._job_seq += 1
            record = {
                "job_id": f"job-{self._job_seq}",
                "request_id": request_id,
                "task_code": task_code,
                "job_code": job_code,
                "status": "pending",
                "business_key": str(job.get("business_key") or ""),
                "dedupe_key": str(job.get("dedupe_key") or ""),
                "attempt_count": 0,
                "max_attempts": int(job.get("max_attempts") or 1),
                "payload": dict(job.get("payload") or {}),
                "result": {},
            }
            self.api_jobs.append(record)
            created.append(record)
        return {"job_code": job_code, "created_count": len(created), "job_ids": [item["job_id"] for item in created]}

    def enqueue_task_executions(self, *, request_id: str, item_code: str, workflow_code: str, items: list[dict]) -> dict:
        created: list[RuntimeTaskExecutionRecord] = []
        for item in items:
            self._execution_seq += 1
            record = RuntimeTaskExecutionRecord(
                execution_id=f"exec-{self._execution_seq}",
                request_id=request_id,
                item_code=item_code,
                workflow_code=workflow_code,
                business_key=str(item.get("business_key") or ""),
                dedupe_key=str(item.get("dedupe_key") or ""),
                resource_code=str(item.get("resource_code") or ""),
                status="pending",
                queue_seq=self._execution_seq,
                payload=dict(item.get("payload") or {}),
                max_attempts=int(item.get("max_attempts") or 1),
            )
            self.executions.append(record)
            created.append(record)
        return {
            "item_code": item_code,
            "created_count": len(created),
            "execution_ids": [item.execution_id for item in created],
        }

    def create_notification_outbox(
        self,
        *,
        channel_code: str,
        event_type: str,
        ref_id: str,
        reply_target: str,
        payload: dict,
        dedupe_key: str,
    ) -> dict:
        record = {
            "channel_code": channel_code,
            "event_type": event_type,
            "ref_id": ref_id,
            "reply_target": reply_target,
            "payload": dict(payload),
            "dedupe_key": dedupe_key,
        }
        self.outbox.append(record)
        return record


def _build_request(
    *,
    request_id: str = "req-1",
    current_stage: str = "collect_product_data",
    payload: dict | None = None,
) -> RuntimeTaskRequestRecord:
    return RuntimeTaskRequestRecord(
        request_id=request_id,
        project_code="automation-business-scaffold",
        task_code=PRODUCT_INGEST_TASK_CODE,
        status="pending",
        current_stage=current_stage,
        payload=dict(payload or {}),
        source_channel_code="feishu",
        reply_target="record://reply",
    )


def _api_job(
    *,
    request_id: str,
    stage_code: str,
    job_code: str,
    status: str = "success",
    result: dict | None = None,
    business_key: str = "1234567890",
) -> dict:
    return {
        "job_id": f"{job_code}-{stage_code}",
        "request_id": request_id,
        "task_code": PRODUCT_INGEST_TASK_CODE,
        "job_code": job_code,
        "status": status,
        "business_key": business_key,
        "dedupe_key": f"{request_id}:{stage_code}:{job_code}",
        "attempt_count": 1,
        "max_attempts": 1,
        "payload": {"stage_code": stage_code},
        "result": dict(result or {}),
    }


def test_advance_stage_dispatches_collect_jobs_for_direct_ingest() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(request=request)

    result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="collect_product_data",
    )

    assert result["action"] == "waiting"
    assert result["current_stage"] == "collect_product_data"
    dispatch_payload = result["details"]["dispatch_payload"]
    assert set(dispatch_payload) == {"tiktok_product_request_fetch", "fastmoss_product_fetch"}
    assert len(store.api_jobs) == 2
    assert {job["job_code"] for job in store.api_jobs} == {
        "tiktok_product_request_fetch",
        "fastmoss_product_fetch",
    }
    assert result["details"]["product_identity"]["product_id"] == "1234567890"


def test_advance_stage_moves_to_browser_fallback_when_request_fetch_requires_it() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(
        request=request,
        api_jobs=[
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="tiktok_product_request_fetch",
                result={
                    "handler_result": {
                        "status": "fallback_required",
                        "next_action": {"payload": {"source_url": "https://www.tiktok.com/shop/pdp/1234567890"}},
                    }
                },
            ),
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="fastmoss_product_fetch",
                result={"handler_result": {"status": "success"}, "product_id": "1234567890"},
            ),
        ],
    )

    result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="collect_product_data",
    )

    assert result["action"] == "advance"
    assert result["next_stage"] == "browser_fallback"
    assert result["details"]["tiktok_product_request_fetch"]["job_code"] == "tiktok_product_request_fetch"


def test_finalize_request_updates_request_and_creates_outbox() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        current_stage="ready_for_summary",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(
        request=request,
        api_jobs=[
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="tiktok_product_request_fetch",
                result={
                    "handler_result": {"status": "success"},
                    "product_id": "1234567890",
                    "normalized_product_result": {"logical_fields": {"product_id": "1234567890"}},
                },
            ),
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="fastmoss_product_fetch",
                result={"handler_result": {"status": "success"}, "product_id": "1234567890", "sales": 12},
            ),
            _api_job(
                request_id=request.request_id,
                stage_code="sync_media",
                job_code="media_asset_sync",
                result={"handler_result": {"status": "success"}, "stored_assets": 1},
            ),
            _api_job(
                request_id=request.request_id,
                stage_code="persist_facts",
                job_code="fact_bundle_upsert",
                result={"handler_result": {"status": "success"}, "upserted_products": 1},
            ),
        ],
    )

    payload = finalize_request(store=store, request=request, workflow=workflow)
    updated_request = store.load_task_request(request_id=request.request_id)

    assert payload["final_status"] == "success"
    assert updated_request.status == "success"
    assert updated_request.current_stage == "completed"
    assert updated_request.summary["child_success_count"] == 4
    assert updated_request.result["product_id"] == "1234567890"
    assert updated_request.result["fact_bundle_upsert"]["upserted_products"] == 1
    assert len(store.outbox) == 1
    assert store.outbox[0]["ref_id"] == request.request_id
    assert store.outbox[0]["payload"]["task_code"] == PRODUCT_INGEST_TASK_CODE


def test_release_request_after_child_completion_requeues_pending_executor() -> None:
    request = _build_request(
        current_stage="collect_product_data",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(
        request=request,
        api_jobs=[
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="tiktok_product_request_fetch",
                result={"handler_result": {"status": "success"}},
            ),
            _api_job(
                request_id=request.request_id,
                stage_code="collect_product_data",
                job_code="fastmoss_product_fetch",
                result={"handler_result": {"status": "success"}},
            ),
        ],
    )

    updates = release_request_after_child_completion(store, request_id=request.request_id)
    updated_request = store.load_task_request(request_id=request.request_id)

    assert updates == [
        {
            "request_id": request.request_id,
            "stage_code": "collect_product_data",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    assert updated_request.status == "pending"
    assert updated_request.child_total_count == 2
    assert updated_request.child_terminal_count == 2
    assert updated_request.child_success_count == 2
