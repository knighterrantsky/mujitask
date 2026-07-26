from __future__ import annotations

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


def test_monitor_workflow_is_a_registered_independent_formal_task() -> None:
    assert INFLUENCER_MONITOR_TASK_CODE == "monitor_tk_influencers"
    assert TASK_CODE == INFLUENCER_MONITOR_TASK_CODE
    assert TASK_CODE in FORMAL_TASK_CODES
    assert TASK_CODE in WORKFLOW_RUNTIME_MODULES
    workflow = get_workflow_definition(TASK_CODE)
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

    result = task_module.MonitorTKInfluencersTask().run_runtime_request(
        {"control_action": "submit"}
    )

    assert result == {"status": "pending"}
    assert captured["fastmoss_phone_env"] == "FASTMOSS_PHONE"
    assert captured["fastmoss_password_env"] == "FASTMOSS_PASSWORD"
    assert "fastmoss_phone" not in captured
    assert "fastmoss_password" not in captured


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
        }

    def discovery_job(product_id: str, sales: int, image: str) -> dict:
        candidate = {
            "creator_id": "creator-a",
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
    assert payload["creator_identity"]["creator_id"] == "creator-a"
    assert payload["creator_run_max_sales_28d"] == 120
    assert [hit["product_id"] for hit in payload["product_hits"]] == [
        "sku-a",
        "sku-b",
    ]
    assert payload["source_product_images"] == [
        {"file_token": "img-a"},
        {"file_token": "img-b"},
    ]
    assert payload["holidays"] == ["holiday-sku-a", "holiday-sku-b"]
