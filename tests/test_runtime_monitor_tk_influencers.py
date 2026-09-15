from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers import orchestrator
from automation_business_scaffold.infrastructure.runtime.runtime_records import RuntimeTaskExecutionRecord

from automation_business_scaffold.control_plane.executor.workflow_registry import (
    WORKFLOW_RUNTIME_MODULES,
    get_workflow_definition,
    load_workflow_runtime,
)
from automation_business_scaffold.control_plane.runtime_config.settings import (
    FORMAL_TASK_CODES,
    INFLUENCER_MONITOR_TASK_CODE,
)
from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.orchestrator import (
    DISCOVERY_STAGE_CODE,
    READ_STAGE_CODE,
    SUMMARY_STAGE_CODE,
    SYNC_STAGE_CODE,
    TASK_CODE,
    advance_stage,
    release_request_after_child_completion,
)
from automation_business_scaffold.domains.tiktok.tasks import monitor_tk_influencers as task_module


class RecoveryStore:
    def __init__(self):
        self.jobs = [self.job("A")]
        self.executions = []
        self.request = SimpleNamespace(
            request_id="req-recovery", payload={}, task_code=TASK_CODE,
            status="waiting", current_stage=DISCOVERY_STAGE_CODE,
        )

    @staticmethod
    def job(job_id, status="waiting"):
        return {
            "job_id": job_id, "job_code": "product_video_creator_discovery",
            "status": status, "payload": {"stage_code": DISCOVERY_STAGE_CODE},
            "result": {"fallback_required": True},
        }

    def list_api_worker_jobs_for_request(self, *, request_id, job_code=None):
        return [job for job in self.jobs if not job_code or job["job_code"] == job_code]

    def list_task_executions(self, **kwargs):
        return self.executions

    def load_task_request(self, **kwargs):
        return self.request

    def update_task_request(self, **kwargs):
        self.request.__dict__.update(kwargs)

    def enqueue_task_executions(self, *, items, request_id, item_code, workflow_code):
        for item in items:
            self.executions.append(RuntimeTaskExecutionRecord(
                **deepcopy(item), execution_id=str(len(self.executions) + 1),
                request_id=request_id, item_code=item_code, workflow_code=workflow_code,
                status="pending", queue_seq=len(self.executions),
            ))
        return {"created_count": len(items)}

    def requeue_waiting_api_worker_job(self, *, job_id, payload, **kwargs):
        next(job for job in self.jobs if job["job_id"] == job_id).update(
            status="pending", payload=payload, result={},
        )

    def mark_waiting_api_worker_job_failed(self, *, job_id, **kwargs):
        next(job for job in self.jobs if job["job_id"] == job_id).update(
            status="failed", result=kwargs["result"],
        )

    def advance(self):
        return advance_stage(
            store=self, request=self.request, workflow=orchestrator.WORKFLOW,
            stage_code=orchestrator.FALLBACK_STAGE_CODE,
        )


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_recovery_consumes_original_sources_when_waiting_set_grows(outcome):
    store = RecoveryStore()
    store.advance()
    store.executions[0] = replace(store.executions[0], status="finished", result_status=outcome)
    store.jobs.append(store.job("B"))
    store.advance()
    assert len(store.executions) == 1
    assert store.jobs[0]["status"] == ("pending" if outcome == "success" else "failed")
    assert store.jobs[1]["status"] == "waiting"


def test_recovery_waits_for_active_execution_and_retries_source_before_new_recovery():
    store = RecoveryStore()
    store.advance()
    store.jobs.append(store.job("B"))
    store.advance()
    assert len(store.executions) == 1
    store.executions[0] = replace(store.executions[0], status="finished", result_status="success")
    store.advance()
    store.advance()
    assert len(store.executions) == 1
    assert store.jobs[0]["payload"]["fastmoss_security_browser_fallback_attempt"] == 1
    assert release_request_after_child_completion(store, request_id=store.request.request_id) == []
    store.request.current_stage = orchestrator.FALLBACK_STAGE_CODE
    assert release_request_after_child_completion(store, request_id=store.request.request_id)
    assert store.request.current_stage == DISCOVERY_STAGE_CODE
    store.jobs[0]["status"] = "finished"
    store.advance()
    assert len(store.executions) == 2
    assert store.executions[1].payload["source_job_ids"] == ["B"]


def test_recovery_dispatches_one_source_and_does_not_repeat_after_replayed_fallback():
    store = RecoveryStore()
    store.jobs.append(store.job("B"))
    store.advance()
    assert store.executions[0].payload["source_job_ids"] == ["A"]
    store.executions[0] = replace(store.executions[0], status="finished", result_status="success")
    store.advance()
    store.jobs[0].update(status="waiting", result={"fallback_required": True})
    store.advance()
    assert store.jobs[0]["status"] == "failed"
    assert len(store.executions) == 1


def test_legacy_batch_result_is_consumed_serially_after_partial_requeue():
    store = RecoveryStore()
    store.advance()
    payload = {**store.executions[0].payload, "source_job_ids": ["A", "B"]}
    store.executions[0] = replace(store.executions[0], payload=payload, status="finished", result_status="success")
    store.jobs.append(store.job("B"))
    store.advance()
    assert [job["status"] for job in store.jobs] == ["pending", "waiting"]
    store.advance()
    assert store.jobs[1]["status"] == "waiting"
    store.jobs[0]["status"] = "finished"
    store.advance()
    assert store.jobs[1]["status"] == "pending"
    assert len(store.executions) == 1


@pytest.mark.parametrize("outcome", ["success", "failed"])
def test_monitor_claim_blocks_following_jobs_until_recovery_source_terminal(runtime_db_url, outcome):
    from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold", task_code=TASK_CODE, payload={},
        requested_by="pytest",
    )
    store.update_task_request(
        request_id=request.request_id, status="waiting", current_stage=DISCOVERY_STAGE_CODE,
    )
    store.enqueue_api_worker_jobs(
        request_id=request.request_id, task_code=TASK_CODE,
        job_code="product_video_creator_discovery",
        jobs=[{
            "business_key": key, "dedupe_key": key,
            "payload": {"stage_code": DISCOVERY_STAGE_CODE},
        } for key in ("A", "B")],
    )
    claim_args = dict(worker_id="test-worker", lease_seconds=30, request_id=request.request_id)
    source = store.claim_next_api_worker_job(**claim_args)
    assert source["business_key"] == "A"
    store.mark_api_worker_job_waiting(
        job_id=source["job_id"], run_id=source["run_id"], summary={},
        result={"handler_result": {"status": "fallback_required", "result": {}}},
        stage="browser_fallback_required",
    )
    # The parent has NOT changed stage yet; the next serial worker iteration must stop.
    assert store.claim_next_api_worker_job(**claim_args) is None
    advance_stage(store=store, request=request, workflow=orchestrator.WORKFLOW,
                  stage_code=orchestrator.FALLBACK_STAGE_CODE)
    executions = store.list_task_executions(request_id=request.request_id)
    assert len(executions) == 1
    assert executions[0].payload["source_job_ids"] == [source["job_id"]]
    assert store.claim_next_api_worker_job(**claim_args) is None
    with store._engine.begin() as connection:
        connection.execute(store._text(
            "UPDATE task_execution SET status='finished', result_status=:outcome "
            "WHERE execution_id=:execution_id"
        ), {"outcome": outcome, "execution_id": executions[0].execution_id})
    advance_stage(store=store, request=request, workflow=orchestrator.WORKFLOW,
                  stage_code=orchestrator.FALLBACK_STAGE_CODE)
    if outcome == "success":
        # Even an ordinary delayed retry of A must block B.
        with store._engine.begin() as connection:
            connection.execute(store._text(
                "UPDATE api_worker_job SET available_at=:available_at WHERE job_id=:job_id"
            ), {"available_at": 9999999999, "job_id": source["job_id"]})
        assert store.claim_next_api_worker_job(**claim_args) is None
        with store._engine.begin() as connection:
            connection.execute(store._text(
                "UPDATE api_worker_job SET available_at=0 WHERE job_id=:job_id"
            ), {"job_id": source["job_id"]})
        retry = store.claim_next_api_worker_job(**claim_args)
        assert retry["job_id"] == source["job_id"]
        assert store.claim_next_api_worker_job(**claim_args) is None
        store.mark_api_worker_job_success(
            job_id=retry["job_id"], run_id=retry["run_id"], summary={},
            result={"status": "success"}, stage="completed",
        )
    following = store.claim_next_api_worker_job(**claim_args)
    assert following["business_key"] == "B"


def test_monitor_workflow_is_a_registered_independent_formal_task() -> None:
    assert INFLUENCER_MONITOR_TASK_CODE == "monitor_tk_influencers"
    assert TASK_CODE == INFLUENCER_MONITOR_TASK_CODE
    assert TASK_CODE in FORMAL_TASK_CODES
    assert TASK_CODE in WORKFLOW_RUNTIME_MODULES
    workflow = get_workflow_definition(TASK_CODE)
    assert workflow.contract_revision == "2"
    assert workflow.entry_stage_code == READ_STAGE_CODE
    assert [stage.stage_code for stage in workflow.stages] == [
        "read_competitor_products",
        "discover_product_video_creators",
        "fastmoss_security_browser_fallback",
        "sync_monitored_influencers",
        "ready_for_summary",
    ]
    assert load_workflow_runtime(TASK_CODE) is not None


def test_monitor_task_submits_fastmoss_credential_env_references(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"status": "pending"}

    monkeypatch.setattr(task_module, "run_monitor_tk_influencers_request", fake_run)
    monkeypatch.setattr(task_module, "_task_business_date", lambda: "2026-06-27")

    result = task_module.MonitorTKInfluencersTask().run_runtime_request(
        {"control_action": "submit"}
    )

    assert result == {"status": "pending"}
    assert captured["fastmoss_phone_env"] == "FASTMOSS_PHONE"
    assert captured["fastmoss_password_env"] == "FASTMOSS_PASSWORD"
    assert "fastmoss_phone" not in captured
    assert "fastmoss_password" not in captured
    assert captured["min_video_sales_28d"] == 50
    assert captured["related_product_sales_reset_days"] == 28
    assert captured["task_business_date"] == "2026-06-27"


def test_monitor_task_snapshots_custom_reset_days_and_rejects_invalid_value(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        task_module,
        "run_monitor_tk_influencers_request",
        lambda payload: captured.update(payload) or {"status": "pending"},
    )
    monkeypatch.setattr(task_module, "_task_business_date", lambda: "2026-06-27")

    task_module.MonitorTKInfluencersTask().run_runtime_request(
        {
            "control_action": "submit",
            "related_product_sales_reset_days": 7,
            "task_business_date": "2099-01-01",
        }
    )

    assert captured["related_product_sales_reset_days"] == 7
    assert captured["task_business_date"] == "2026-06-27"

    try:
        task_module.MonitorTKInfluencersTask().run_runtime_request(
            {
                "control_action": "submit",
                "related_product_sales_reset_days": 0,
            }
        )
    except ValueError as exc:
        assert "positive integer" in str(exc)
    else:
        raise AssertionError("zero reset days must be rejected")


def test_summary_stage_is_not_released_back_to_pending() -> None:
    class Request:
        request_id = "req-monitor-summary"
        task_code = TASK_CODE
        status = "pending"
        current_stage = SUMMARY_STAGE_CODE

    class Store:
        def load_task_request(self, *, request_id: str) -> Request:
            assert request_id == Request.request_id
            return Request()

        def update_task_request(self, **_: object) -> None:
            raise AssertionError("summary stage must be finalized, not released")

    assert release_request_after_child_completion(
        Store(), request_id=Request.request_id
    ) == []


def test_read_stage_dispatches_all_sku_source_adapter() -> None:
    class Request:
        request_id = "req-monitor-read"
        payload = {
            "source_table_ref": "feishu://mujitask/tk_competitor",
            "target_table_ref": "feishu://mujitask/tk_influencer_monitoring",
            "min_video_sales_28d": 50,
        }

    class Store:
        def __init__(self) -> None:
            self.jobs: list[dict[str, object]] = []

        def list_api_worker_jobs_for_request(
            self, **_: object
        ) -> list[dict[str, object]]:
            return list(self.jobs)

        def enqueue_api_worker_jobs(
            self,
            *,
            request_id: str,
            task_code: str,
            job_code: str,
            jobs: list[dict[str, object]],
        ) -> dict[str, int]:
            assert request_id == Request.request_id
            assert task_code == TASK_CODE
            for job in jobs:
                self.jobs.append({**job, "job_code": job_code})
            return {"created_count": len(jobs)}

    store = Store()

    result = advance_stage(
        store=store,
        request=Request(),
        workflow=get_workflow_definition(TASK_CODE),
        stage_code=READ_STAGE_CODE,
    )

    assert result["action"] == "waiting"
    jobs = store.list_api_worker_jobs_for_request(request_id=Request.request_id)
    assert len(jobs) == 1
    assert jobs[0]["job_code"] == "feishu_table_read"
    assert jobs[0]["payload"]["adapter_code"] == "influencer_monitor_source_adapter"
    assert set(jobs[0]["payload"]["field_names"]) >= {
        "SKU-ID",
        "图片",
        "节日",
        "商品状态",
        "达人查找状态",
    }


def test_discovery_stage_fans_out_each_deduplicated_product() -> None:
    class Request:
        request_id = "req-monitor-discovery"
        payload = {
            "source_table_ref": "feishu://mujitask/tk_competitor",
            "target_table_ref": "feishu://mujitask/tk_influencer_monitoring",
            "min_video_sales_28d": 77,
            "fastmoss": {"live_fetch": True},
        }

    source_result = {
        "source_rows": [
            {
                "product_id": "sku-a",
                "source_record_ids": ["rec-a"],
                "source_product_images": [{"file_token": "img-a"}],
                "holidays": ["万圣节"],
            },
            {
                "product_id": "sku-b",
                "source_record_ids": ["rec-b"],
                "source_product_images": [{"file_token": "img-b"}],
                "holidays": ["圣诞节"],
            },
        ]
    }

    class Store:
        def __init__(self) -> None:
            self.jobs = [
                {
                    "job_code": "feishu_table_read",
                    "status": "finished",
                    "payload": {"stage_code": READ_STAGE_CODE},
                    "result": {
                        "handler_result": {
                            "status": "success",
                            "result": source_result,
                        },
                        **source_result,
                    },
                }
            ]

        def list_api_worker_jobs_for_request(
            self, *, request_id: str, job_code: str | None = None
        ) -> list[dict]:
            assert request_id == Request.request_id
            return [
                job
                for job in self.jobs
                if not job_code or job["job_code"] == job_code
            ]

        def enqueue_api_worker_jobs(self, **payload) -> dict[str, int]:
            for job in payload["jobs"]:
                self.jobs.append(
                    {
                        **job,
                        "job_code": payload["job_code"],
                        "status": "pending",
                    }
                )
            return {"created_count": len(payload["jobs"])}

    store = Store()
    result = advance_stage(
        store=store,
        request=Request(),
        workflow=get_workflow_definition(TASK_CODE),
        stage_code=DISCOVERY_STAGE_CODE,
    )

    assert result["action"] == "waiting"
    discovery_jobs = [
        job
        for job in store.jobs
        if job["job_code"] == "product_video_creator_discovery"
    ]
    assert [job["payload"]["product_id"] for job in discovery_jobs] == [
        "sku-a",
        "sku-b",
    ]
    assert all(
        job["payload"]["min_video_sales_28d"] == 77 for job in discovery_jobs
    )
    assert discovery_jobs[0]["payload"]["source_product_images"] == [
        {"file_token": "img-a"}
    ]


def test_sync_stage_deduplicates_creator_and_uses_cross_product_max() -> None:
    class Request:
        request_id = "req-monitor-sync"
        payload = {
            "target_table_ref": "feishu://mujitask/tk_influencer_monitoring",
            "related_product_sales_reset_days": 7,
            "task_business_date": "2026-06-27",
        }

    def discovery_job(product_id: str, sales: int, image: str) -> dict:
        candidate = {
            "creator_id": "alice",
            "uid": "creator-a",
            "unique_id": "alice",
            "product_id": product_id,
            "video_product_sales_28d": sales,
            "winning_video_id": f"video-{product_id}",
        }
        result = {"fetch_status": "success", "candidates": [candidate]}
        return {
            "job_code": "product_video_creator_discovery",
            "status": "finished",
            "payload": {
                "stage_code": DISCOVERY_STAGE_CODE,
                "product_id": product_id,
                "source_product_images": [{"file_token": image}],
                "holidays": [f"holiday-{product_id}"],
            },
            "result": {
                "handler_result": {"status": "success", "result": result},
                **result,
            },
        }

    class Store:
        def __init__(self) -> None:
            self.jobs = [
                discovery_job("sku-a", 90, "img-a"),
                discovery_job("sku-b", 120, "img-b"),
            ]

        def list_api_worker_jobs_for_request(
            self, *, request_id: str, job_code: str | None = None
        ) -> list[dict]:
            assert request_id == Request.request_id
            return [
                job
                for job in self.jobs
                if not job_code or job["job_code"] == job_code
            ]

        def enqueue_api_worker_jobs(self, **payload) -> dict[str, int]:
            for job in payload["jobs"]:
                self.jobs.append(
                    {
                        **job,
                        "job_code": payload["job_code"],
                        "status": "pending",
                    }
                )
            return {"created_count": len(payload["jobs"])}

    store = Store()
    result = advance_stage(
        store=store,
        request=Request(),
        workflow=get_workflow_definition(TASK_CODE),
        stage_code=SYNC_STAGE_CODE,
    )

    assert result["action"] == "waiting"
    sync_jobs = [
        job for job in store.jobs if job["job_code"] == "influencer_monitor_sync"
    ]
    assert len(sync_jobs) == 1
    payload = sync_jobs[0]["payload"]
    assert payload["creator_identity"] == {
        "creator_id": "alice",
        "uid": "creator-a",
        "unique_id": "alice",
    }
    assert payload["creator_run_max_sales_28d"] == 120
    assert payload["related_product_sales_reset_days"] == 7
    assert payload["task_business_date"] == "2026-06-27"
    assert [hit["product_id"] for hit in payload["product_hits"]] == [
        "sku-a",
        "sku-b",
    ]
    assert payload["source_product_images"] == [
        {"file_token": "img-a"},
        {"file_token": "img-b"},
    ]
    assert payload["holidays"] == ["holiday-sku-a", "holiday-sku-b"]
