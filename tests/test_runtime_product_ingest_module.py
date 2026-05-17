from __future__ import annotations

from dataclasses import replace

from automation_business_scaffold.control_plane.runtime_config.settings import (
    PRODUCT_INGEST_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.flows.tiktok_fastmoss_product_ingest.orchestrator import (
    advance_stage,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.flows.tiktok_fastmoss_product_ingest.summary import (
    finalize_request,
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

    def update_task_request(
        self, *, request_id: str, **updates: object
    ) -> RuntimeTaskRequestRecord:
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

        return [
            _OutboxRecord(record)
            for record in self.outbox
            if str(record.get("ref_id")) == request_id
        ]

    def enqueue_api_worker_jobs(
        self, *, request_id: str, task_code: str, job_code: str, jobs: list[dict]
    ) -> dict:
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
        return {
            "job_code": job_code,
            "created_count": len(created),
            "job_ids": [item["job_id"] for item in created],
        }

    def enqueue_task_executions(
        self, *, request_id: str, item_code: str, workflow_code: str, items: list[dict]
    ) -> dict:
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
    current_stage: str = "read_selection_rows",
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


def test_read_stage_skips_to_row_dispatch_for_direct_ingest() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(request=request)

    result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="read_selection_rows",
    )

    assert result == {
        "action": "advance",
        "next_stage": "dispatch_selection_row_refresh",
        "details": {"stage_transition": "direct_ingest_skip_selection_read"},
    }
    assert store.api_jobs == []


def test_dispatch_stage_enqueues_single_selection_row_job_for_direct_ingest() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        current_stage="dispatch_selection_row_refresh",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(request=request)

    result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_selection_row_refresh",
    )

    assert result["action"] == "advance"
    assert result["next_stage"] == "collect_selection_rows"
    assert result["details"]["row_count"] == 1
    assert len(store.api_jobs) == 1
    row_job = store.api_jobs[0]
    assert row_job["job_code"] == "selection_row_refresh"
    assert row_job["payload"]["stage_code"] == "collect_selection_rows"
    assert row_job["payload"]["product_identity"]["product_id"] == "1234567890"


def test_dispatch_stage_marks_fact_db_required_without_passing_db_url(monkeypatch) -> None:
    monkeypatch.setenv("TK_FACT_DB_URL", "postgresql+psycopg://runtime-fact")
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        current_stage="dispatch_selection_row_refresh",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(request=request)

    advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_selection_row_refresh",
    )

    row_job = next(job for job in store.api_jobs if job["job_code"] == "selection_row_refresh")
    assert row_job["payload"]["requires_fact_db"] is True
    assert row_job["payload"]["require_database_persistence"] is True
    assert "fact_db_url" not in row_job["payload"]
    assert "execution_control_db_url" not in row_job["payload"]


def test_dispatch_stage_passes_strict_object_storage_summary_without_secrets() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        current_stage="dispatch_selection_row_refresh",
        payload={
            "product_url": "https://www.tiktok.com/shop/pdp/1234567890",
            "artifact_store": {
                "provider": "minio",
                "bucket": "selection-media",
                "object_prefix": "pytest/selection",
            },
        },
    )
    store = FakeRuntimeStore(request=request)

    advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_selection_row_refresh",
    )

    row_job = next(job for job in store.api_jobs if job["job_code"] == "selection_row_refresh")
    payload = row_job["payload"]
    assert payload["requires_object_storage"] is True
    assert payload["require_object_storage"] is True
    assert payload["artifact_store"]["provider"] == "minio"
    assert payload["artifact_store"]["bucket"] == "selection-media"
    assert "minio_endpoint" not in payload
    assert "minio_access_key" not in payload
    assert "minio_secret_key" not in payload


def test_product_ingest_release_uses_child_summaries_without_full_results() -> None:
    request = _build_request(current_stage="collect_selection_rows")

    class SummaryOnlyStore(FakeRuntimeStore):
        def list_api_worker_job_summaries_for_request(self, *, request_id: str, job_code: str = "") -> list[dict]:
            assert request_id == request.request_id
            assert job_code == ""
            return [
                {
                    "job_id": "job-summary",
                    "request_id": request_id,
                    "job_code": "selection_row_refresh",
                    "status": "success",
                    "result_status": "success",
                    "payload": {"stage_code": "collect_selection_rows"},
                    "summary": {},
                    "result": {},
                }
            ]

        def list_task_execution_summaries_for_request(self, *, request_id: str) -> list[dict]:
            assert request_id == request.request_id
            return []

        def summarize_api_worker_jobs_for_request(self, *, request_id: str) -> dict:
            assert request_id == request.request_id
            return {"total": 1, "counts": {"success": 1}, "active_count": 0}

        def summarize_task_executions_for_request(self, *, request_id: str) -> dict:
            assert request_id == request.request_id
            return {"total": 0, "counts": {}, "active_count": 0}

        def list_api_worker_jobs_for_request(self, *, request_id: str) -> list[dict]:
            raise AssertionError("full api_worker_job result_json should not be loaded for release")

        def list_task_executions(self, *, request_id: str) -> list[RuntimeTaskExecutionRecord]:
            raise AssertionError("full task_execution result_json should not be loaded for release")

    store = SummaryOnlyStore(request=request)

    releases = release_request_after_child_completion(store, request_id=request.request_id)

    assert releases[0]["stage_code"] == "collect_selection_rows"
    assert store.load_task_request(request_id=request.request_id).status == "pending"


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
                stage_code="collect_selection_rows",
                job_code="selection_row_refresh",
                result={
                    "handler_result": {
                        "status": "success",
                        "summary": {
                            "source_record_id": "rec-1",
                            "product_business_key": "1234567890",
                            "row_status": "success",
                        },
                        "result": {"row_status": "success"},
                    },
                },
            ),
        ],
    )

    payload = finalize_request(store=store, request=request, workflow=workflow)
    updated_request = store.load_task_request(request_id=request.request_id)

    assert payload["final_status"] == "success"
    assert updated_request.status == "success"
    assert updated_request.current_stage == "completed"
    assert updated_request.summary["final_status"] == "success"
    assert updated_request.summary["child_success_count"] == 1
    assert updated_request.result["row_count"] == 1
    assert updated_request.result["rows"][0] == {
        "source_record_id": "rec-1",
        "product_id": "1234567890",
        "row_status": "success",
    }
    assert len(store.outbox) == 1
    assert store.outbox[0]["ref_id"] == request.request_id
    assert store.outbox[0]["payload"]["task_code"] == PRODUCT_INGEST_TASK_CODE
    message_text = store.outbox[0]["payload"]["message_text"]
    assert "选品采集完成" in message_text
    assert "状态：success" in message_text
    assert "总数：1 条" in message_text
    assert "成功：1 条" in message_text
    assert "失败：0 条" in message_text
    assert "1. SKU 1234567890" in message_text


def test_finalize_request_uses_nested_row_result_status() -> None:
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
                stage_code="collect_selection_rows",
                job_code="selection_row_refresh",
                result={
                    "handler_result": {
                        "status": "success",
                        "summary": {
                            "source_record_id": "rec-1",
                            "product_business_key": "summary-product",
                            "row_status": "success",
                        },
                        "result": {
                            "source_record_id": "rec-1",
                            "business_entity_key": "1234567890",
                            "row_status": "unavailable",
                        },
                    },
                },
            ),
        ],
    )

    finalize_request(store=store, request=request, workflow=workflow)
    updated_request = store.load_task_request(request_id=request.request_id)

    assert updated_request.result["rows"][0] == {
        "source_record_id": "rec-1",
        "product_id": "1234567890",
        "row_status": "unavailable",
    }


def test_dispatch_stage_applies_selection_limit_to_candidate_rows() -> None:
    workflow = get_workflow_definition(PRODUCT_INGEST_TASK_CODE)
    request = _build_request(
        current_stage="dispatch_selection_row_refresh",
        payload={
            "selection_table_ref": "https://example.feishu.cn/base/app?table=tbl",
            "selection_limit": 1,
        },
    )
    store = FakeRuntimeStore(
        request=request,
        api_jobs=[
            _api_job(
                request_id=request.request_id,
                stage_code="read_selection_rows",
                job_code="feishu_table_read",
                result={
                    "handler_result": {
                        "status": "success",
                        "result": {
                            "source_rows": [
                                {
                                    "source_record_id": "rec-1",
                                    "source_table_ref": "https://example.feishu.cn/base/app?table=tbl",
                                    "product_identity": {"product_id": "1234567890"},
                                },
                                {
                                    "source_record_id": "rec-2",
                                    "source_table_ref": "https://example.feishu.cn/base/app?table=tbl",
                                    "product_identity": {"product_id": "9876543210"},
                                },
                            ],
                        },
                    },
                },
            ),
        ],
    )

    result = advance_stage(
        store=store,
        request=request,
        workflow=workflow,
        stage_code="dispatch_selection_row_refresh",
    )

    assert result["action"] == "advance"
    assert result["next_stage"] == "collect_selection_rows"
    row_jobs = [job for job in store.api_jobs if job["job_code"] == "selection_row_refresh"]
    assert len(row_jobs) == 1
    assert row_jobs[0]["payload"]["source_record_id"] == "rec-1"


def test_release_request_after_child_completion_requeues_pending_executor() -> None:
    request = _build_request(
        current_stage="collect_selection_rows",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/1234567890"},
    )
    store = FakeRuntimeStore(
        request=request,
        api_jobs=[
            _api_job(
                request_id=request.request_id,
                stage_code="collect_selection_rows",
                job_code="selection_row_refresh",
                result={"handler_result": {"status": "success"}},
            ),
        ],
    )

    updates = release_request_after_child_completion(store, request_id=request.request_id)
    updated_request = store.load_task_request(request_id=request.request_id)

    assert updates == [
        {
            "request_id": request.request_id,
            "stage_code": "collect_selection_rows",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    assert updated_request.status == "pending"
    assert updated_request.child_total_count == 1
    assert updated_request.child_terminal_count == 1
    assert updated_request.child_success_count == 1
