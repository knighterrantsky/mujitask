from __future__ import annotations

import time

from automation_business_scaffold.flows.phase1_runtime_store import Phase1RuntimeStore


def test_claim_next_task_request_requeues_expired_cleanup_request(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "phase1-store.sqlite3")
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="refresh_current_competitor_table",
        payload={"table_url": "https://example.com/table"},
        requested_by="pytest",
    )
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
    assert reclaimed.worker_id == "worker-b"
    assert reclaimed.stage_cursor == {}


def test_claim_next_task_request_requeues_ready_for_summary_without_reset(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "phase1-ready.sqlite3")
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


def test_claim_next_outbox_requeues_expired_sending_record(tmp_path):
    store = Phase1RuntimeStore(db_path=tmp_path / "phase1-outbox.sqlite3")
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
