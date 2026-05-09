from __future__ import annotations

import time

import pytest

from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def test_claim_next_task_request_requeues_expired_cleanup_request(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="refresh_current_competitor_table",
        payload={"table_url": "https://example.com/table"},
        requested_by="pytest",
    )
    assert request.progress_stage == "submitted"
    assert request.max_execution_seconds == 0.0
    claimed = store.claim_next_task_request(worker_id="worker-a", lease_seconds=30.0)
    assert claimed is not None
    store.update_task_request(
        request_id=request.request_id,
        current_stage="cleanup",
        stage_cursor={"cleanup": {"done": False}},
        worker_id="worker-a",
        lease_until=time.time() - 1.0,
        heartbeat_at=time.time() - 1.0,
    )

    reclaimed = store.claim_next_task_request(worker_id="worker-b", lease_seconds=30.0)

    assert reclaimed is not None
    assert reclaimed.request_id == request.request_id
    assert reclaimed.status == "running"
    assert reclaimed.current_stage == ""
    assert reclaimed.progress_stage == "claimed"
    assert reclaimed.worker_id == "worker-b"
    assert reclaimed.stage_cursor == {}


def test_claim_next_task_request_requeues_ready_for_summary_without_reset(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="refresh_current_competitor_table",
        payload={"table_url": "https://example.com/table"},
        requested_by="pytest",
    )
    _ = store.claim_next_task_request(worker_id="worker-a", lease_seconds=30.0)
    store.update_task_request(
        request_id=request.request_id,
        status="running",
        current_stage="ready_for_summary",
        worker_id="worker-a",
        lease_until=time.time() - 1.0,
        heartbeat_at=time.time() - 1.0,
    )

    reclaimed = store.claim_next_task_request(worker_id="worker-b", lease_seconds=30.0)

    assert reclaimed is not None
    assert reclaimed.request_id == request.request_id
    assert reclaimed.status == "running"
    assert reclaimed.current_stage == "ready_for_summary"
    assert reclaimed.worker_id == "worker-b"


def test_claim_next_outbox_requeues_expired_sending_record(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="task_request.completed",
        ref_id="req-1",
        reply_target="",
        payload={"message_text": "hello"},
        dedupe_key="task_request.completed:req-1",
    )
    claimed = store.claim_next_outbox(worker_id="dispatcher-a", lease_seconds=30.0)
    assert claimed is not None
    with store._engine.begin() as connection:  # noqa: SLF001
        connection.execute(
            store._text(
                """
                UPDATE notification_outbox
                SET lease_until = :lease_until,
                    heartbeat_at = :heartbeat_at
                WHERE outbox_id = :outbox_id
                """
            ),
            {
                "outbox_id": outbox.outbox_id,
                "lease_until": time.time() - 1.0,
                "heartbeat_at": time.time() - 1.0,
            },
        )

    reclaimed = store.claim_next_outbox(worker_id="dispatcher-b", lease_seconds=30.0)

    assert reclaimed is not None
    assert reclaimed.outbox_id == outbox.outbox_id
    assert reclaimed.status == "sending"
    assert reclaimed.worker_id == "dispatcher-b"
    assert reclaimed.retry_count == 1


def test_api_worker_job_queue_round_trip(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="tiktok_fastmoss_product_ingest",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/123"},
        requested_by="pytest",
    )
    _ = store.claim_next_task_request(worker_id="executor-a", lease_seconds=30.0)
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="waiting_api_worker",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    enqueue = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="tiktok_fastmoss_product_ingest",
        jobs=[
            {
                "business_key": "123",
                "dedupe_key": f"tiktok_fastmoss_product_ingest:{request.request_id}",
                "payload": {"product_url": "https://www.tiktok.com/shop/pdp/123"},
            }
        ],
    )

    claimed = store.claim_next_api_worker_job(worker_id="api-worker-a", lease_seconds=30.0)

    assert enqueue["created_count"] == 1
    assert claimed is not None
    assert claimed["request_id"] == request.request_id
    assert claimed["job_code"] == "tiktok_fastmoss_product_ingest"
    assert claimed["status"] == "running"
    assert claimed["progress_stage"] == "job_claimed"

    store.mark_api_worker_job_success(
        job_id=claimed["job_id"],
        run_id=claimed["run_id"],
        summary={"total": 1, "counts": {"success": 1}},
        result={"product_id": "123"},
    )
    summary = store.summarize_api_worker_jobs_for_request(request_id=request.request_id)

    assert summary["total"] == 1
    assert summary["success_count"] == 1
    assert summary["active_count"] == 0
    assert store.load_api_worker_job(job_id=claimed["job_id"])["progress_stage"] == "completed"


def test_fastmoss_cookie_cache_round_trip(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    saved = store.save_fastmoss_cookie_cache(
        cache_key="fm-test",
        namespace="",
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[
            {
                "name": "fd_tk",
                "value": "secret-cookie-value",
                "domain": ".fastmoss.com",
                "path": "/",
                "secure": True,
            }
        ],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="digest-1",
        expires_at=time.time() + 3600,
        last_login_at=time.time(),
    )

    loaded = store.load_fastmoss_cookie_cache(cache_key="fm-test")

    assert saved["cache_key"] == "fm-test"
    assert loaded is not None
    assert loaded["cookie_count"] == 1
    assert loaded["has_fd_tk"] is True
    assert loaded["fd_tk_digest"] == "digest-1"
    assert loaded["cookies"][0]["value"] == "secret-cookie-value"


def test_fastmoss_cookie_cache_auth_failed_marker(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    store.save_fastmoss_cookie_cache(
        cache_key="fm-test-auth",
        account_key="18000000000",
        base_url="https://www.fastmoss.com",
        region="US",
        cookies=[{"name": "fd_tk", "value": "old", "domain": ".fastmoss.com", "path": "/"}],
        cookie_count=1,
        has_fd_tk=True,
        fd_tk_digest="old-digest",
        expires_at=time.time() + 3600,
    )

    marked = store.mark_fastmoss_cookie_cache_auth_failed(cache_key="fm-test-auth")

    assert marked is not None
    assert marked["last_auth_failed_at"] > 0


def test_runtime_claim_fails_fast_when_schema_missing(unbootstrapped_runtime_db_url):
    store = RuntimeStore(db_url=unbootstrapped_runtime_db_url)

    with pytest.raises(RuntimeError, match="explicit runtime schema bootstrap"):
        store.claim_next_task_request(worker_id="worker-a", lease_seconds=30.0)


def test_runtime_bootstrap_path_is_explicit(unbootstrapped_runtime_db_url):
    store = RuntimeStore(db_url=unbootstrapped_runtime_db_url)

    store.bootstrap_schema()

    assert store.claim_next_task_request(worker_id="worker-a", lease_seconds=30.0) is None
