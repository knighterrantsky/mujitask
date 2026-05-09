from __future__ import annotations

import time

from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _set_row_fields(store: RuntimeStore, *, table_name: str, id_column: str, id_value: str, **fields: object) -> None:
    assignments = [f"{column} = :{column}" for column in fields]
    params = dict(fields)
    params["id_value"] = id_value
    with store._engine.begin() as connection:  # noqa: SLF001
        connection.execute(
            store._text(
                f"""
                UPDATE {table_name}
                SET {", ".join(assignments)}
                WHERE {id_column} = :id_value
                """
            ),
            params,
        )


def _submit_request(store: RuntimeStore, *, task_code: str = "tiktok_fastmoss_product_ingest", **overrides: object):
    payload = {"product_url": "https://www.tiktok.com/shop/pdp/123"}
    payload.update(dict(overrides.pop("payload", {}) or {}))
    return store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code=task_code,
        payload=payload,
        requested_by="pytest",
        **overrides,
    )


def test_runtime_schema_exposes_lifecycle_fields_on_unified_tables(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    expected_columns = {
        "task_request": {
            "progress_stage",
            "last_progress_at",
            "max_execution_seconds",
            "error_type",
            "error_code",
            "dead_letter_reason",
        },
        "api_worker_job": {
            "progress_stage",
            "last_progress_at",
            "max_execution_seconds",
            "error_type",
            "error_code",
            "dead_letter_reason",
        },
        "task_execution": {
            "progress_stage",
            "last_progress_at",
            "max_execution_seconds",
            "error_type",
            "error_code",
            "dead_letter_reason",
        },
        "notification_outbox": {
            "progress_stage",
            "last_progress_at",
            "max_execution_seconds",
            "error_type",
            "error_code",
            "dead_letter_reason",
        },
    }

    with store._engine.connect() as connection:  # noqa: SLF001
        for table_name, columns in expected_columns.items():
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = :table_name
                        """
                    ),
                    {"table_name": table_name},
                )
                .scalars()
                .all()
            )
            assert columns <= set(rows)


def test_runtime_progress_helpers_and_stale_scans_cover_unified_tables(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_request(store, max_execution_seconds=300.0)
    claimed_request = store.claim_next_task_request(worker_id="executor-a", lease_seconds=30.0)

    assert claimed_request is not None
    updated_request = store.update_task_request_progress(
        request_id=request.request_id,
        progress_stage="dispatch_children",
        lease_seconds=30.0,
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
    )

    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:fastmoss_product_fetch",
                "max_execution_seconds": 120.0,
                "payload": {"product_id": "123"},
            }
        ],
    )
    claimed_job = store.claim_next_api_worker_job(worker_id="api-worker-a", lease_seconds=30.0)
    assert claimed_job is not None
    updated_job = store.update_api_worker_job_progress(
        job_id=claimed_job["job_id"],
        run_id=claimed_job["run_id"],
        progress_stage="fastmoss_request",
    )

    executions = store.enqueue_task_executions(
        request_id=request.request_id,
        item_code="tiktok_product_browser_fetch",
        workflow_code="tiktok_fastmoss_product_ingest",
        items=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:browser_fetch",
                "max_execution_seconds": 120.0,
                "payload": {"product_id": "123"},
            }
        ],
    )
    execution_id = executions["created_records"][0]["execution_id"]
    claimed_execution = store.claim_browser_execution(
        execution_id=execution_id,
        worker_id="browser-worker-a",
        lease_seconds=30.0,
    )
    assert claimed_execution is not None
    updated_execution = store.update_task_execution_progress(
        execution_id=execution_id,
        run_id=claimed_execution.run_id,
        progress_stage="browser_collect",
    )

    outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target="reply://pytest",
        payload={"request_id": request.request_id},
        dedupe_key=f"task_request.completed:{request.request_id}",
        max_execution_seconds=120.0,
    )
    claimed_outbox = store.claim_next_outbox(worker_id="dispatcher-a", lease_seconds=30.0)
    assert claimed_outbox is not None
    updated_outbox = store.update_outbox_progress(
        outbox_id=outbox.outbox_id,
        progress_stage="dispatching",
        lease_seconds=30.0,
    )

    assert updated_request.progress_stage == "dispatch_children"
    assert updated_job["progress_stage"] == "fastmoss_request"
    assert updated_execution.progress_stage == "browser_collect"
    assert updated_outbox.progress_stage == "dispatching"

    stale_at = time.time() - 180.0
    _set_row_fields(
        store,
        table_name="task_request",
        id_column="request_id",
        id_value=request.request_id,
        last_progress_at=stale_at,
    )
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=claimed_job["job_id"],
        last_progress_at=stale_at,
    )
    _set_row_fields(
        store,
        table_name="task_execution",
        id_column="execution_id",
        id_value=execution_id,
        last_progress_at=stale_at,
    )
    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=outbox.outbox_id,
        last_progress_at=stale_at,
    )

    assert [item.request_id for item in store.scan_stale_task_requests(stale_after_seconds=60.0)] == [
        request.request_id
    ]
    assert [item["job_id"] for item in store.scan_stale_api_worker_jobs(stale_after_seconds=60.0)] == [
        claimed_job["job_id"]
    ]
    assert [item.execution_id for item in store.scan_stale_task_executions(stale_after_seconds=60.0)] == [
        execution_id
    ]
    assert [item.outbox_id for item in store.scan_stale_outbox_items(stale_after_seconds=60.0)] == [outbox.outbox_id]


def test_runtime_timeout_scans_and_outbox_lease_reclaim_helpers(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_request(store, max_execution_seconds=5.0)
    claimed_request = store.claim_next_task_request(worker_id="executor-a", lease_seconds=30.0)

    assert claimed_request is not None
    _set_row_fields(
        store,
        table_name="task_request",
        id_column="request_id",
        id_value=request.request_id,
        started_at=time.time() - 30.0,
        status="running",
    )

    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
    )
    store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:timeout_job",
                "max_execution_seconds": 5.0,
                "payload": {"product_id": "123"},
            }
        ],
    )
    claimed_job = store.claim_next_api_worker_job(worker_id="api-worker-a", lease_seconds=30.0)
    assert claimed_job is not None
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=claimed_job["job_id"],
        started_at=time.time() - 30.0,
    )

    execution_payload = store.enqueue_task_executions(
        request_id=request.request_id,
        item_code="tiktok_product_browser_fetch",
        workflow_code="tiktok_fastmoss_product_ingest",
        items=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:timeout_execution",
                "max_execution_seconds": 5.0,
                "payload": {"product_id": "123"},
            }
        ],
    )
    execution_id = execution_payload["created_records"][0]["execution_id"]
    claimed_execution = store.claim_browser_execution(
        execution_id=execution_id,
        worker_id="browser-worker-a",
        lease_seconds=30.0,
    )
    assert claimed_execution is not None
    _set_row_fields(
        store,
        table_name="task_execution",
        id_column="execution_id",
        id_value=execution_id,
        started_at=time.time() - 30.0,
    )

    timeout_outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target="reply://pytest",
        payload={"request_id": request.request_id, "kind": "timeout"},
        dedupe_key=f"task_request.completed.timeout:{request.request_id}",
        max_execution_seconds=5.0,
    )
    reclaimed_outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target="reply://pytest",
        payload={"request_id": request.request_id, "kind": "reclaim"},
        dedupe_key=f"task_request.completed.reclaim:{request.request_id}",
        max_execution_seconds=5.0,
    )

    first_claim = store.claim_next_outbox(worker_id="dispatcher-a", lease_seconds=30.0)
    second_claim = store.claim_next_outbox(worker_id="dispatcher-b", lease_seconds=30.0)
    assert first_claim is not None and second_claim is not None
    assert {first_claim.outbox_id, second_claim.outbox_id} == {timeout_outbox.outbox_id, reclaimed_outbox.outbox_id}

    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=timeout_outbox.outbox_id,
        last_progress_at=time.time() - 30.0,
    )
    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=reclaimed_outbox.outbox_id,
        lease_until=time.time() - 5.0,
        heartbeat_at=time.time() - 5.0,
    )

    assert [item.request_id for item in store.scan_task_request_execution_timeouts()] == [request.request_id]
    assert [item["job_id"] for item in store.scan_api_worker_job_execution_timeouts()] == [claimed_job["job_id"]]
    assert [item.execution_id for item in store.scan_task_execution_timeouts()] == [execution_id]
    assert [item.outbox_id for item in store.scan_outbox_execution_timeouts()] == [timeout_outbox.outbox_id]
    assert [item.outbox_id for item in store.scan_expired_outbox_leases()] == [reclaimed_outbox.outbox_id]

    reclaimed = store.reclaim_expired_outbox_claims()

    assert len(reclaimed) == 1
    assert reclaimed[0].outbox_id == reclaimed_outbox.outbox_id
    assert reclaimed[0].status == "retry_wait"
    assert reclaimed[0].error_code == "outbox_lease_expired"


def test_reconcile_request_waiting_children_idempotently_promotes_ready_for_summary(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_request(store)
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
    )

    jobs = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:reconcile_job",
                "payload": {"product_id": "123"},
            }
        ],
    )
    job_id = jobs["created_records"][0]["job_id"]
    claimed_job = store.claim_next_api_worker_job(worker_id="api-worker-a", lease_seconds=30.0)
    assert claimed_job is not None and claimed_job["job_id"] == job_id
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=claimed_job["run_id"],
        summary={"handler_status": "success"},
        result={"product_id": "123"},
    )

    executions = store.enqueue_task_executions(
        request_id=request.request_id,
        item_code="tiktok_product_browser_fetch",
        workflow_code="tiktok_fastmoss_product_ingest",
        items=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:reconcile_execution",
                "payload": {"product_id": "123"},
            }
        ],
    )
    execution_id = executions["created_records"][0]["execution_id"]

    first_reconcile = store.reconcile_request_waiting_children(request_id=request.request_id)

    assert first_reconcile["transitioned"] is False
    assert first_reconcile["active_count"] == 1
    assert first_reconcile["child_total_count"] == 2
    assert first_reconcile["child_terminal_count"] == 1
    assert first_reconcile["request"].status == "waiting"

    claimed_execution = store.claim_browser_execution(
        execution_id=execution_id,
        worker_id="browser-worker-a",
        lease_seconds=30.0,
    )
    assert claimed_execution is not None
    store.mark_browser_execution_success(
        execution_id=execution_id,
        run_id=claimed_execution.run_id,
        summary={"handler_status": "success"},
        result={"product_id": "123"},
    )

    second_reconcile = store.reconcile_request_waiting_children(request_id=request.request_id)
    third_reconcile = store.reconcile_request_waiting_children(request_id=request.request_id)

    assert second_reconcile["transitioned"] is False
    assert second_reconcile["active_count"] == 0
    assert second_reconcile["request"].status == "pending"
    assert second_reconcile["request"].current_stage == "ready_for_summary"
    assert second_reconcile["child_total_count"] == 2
    assert second_reconcile["child_terminal_count"] == 2
    assert second_reconcile["child_success_count"] == 2

    assert third_reconcile["transitioned"] is False
    assert third_reconcile["request"].status == "pending"


def test_reconcile_waiting_children_keeps_fallback_required_parent_waiting(runtime_db_url):
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_request(store)
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
        progress_stage="collect_product_data",
    )

    jobs = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "product:123",
                "dedupe_key": f"{request.request_id}:fallback_required_job",
                "payload": {"product_id": "123"},
            }
        ],
    )
    job_id = jobs["created_records"][0]["job_id"]
    claimed_job = store.claim_next_api_worker_job(worker_id="api-worker-a", lease_seconds=30.0)
    assert claimed_job is not None and claimed_job["job_id"] == job_id
    store.mark_api_worker_job_success(
        job_id=job_id,
        run_id=claimed_job["run_id"],
        summary={"handler_status": "fallback_required"},
        result={
            "handler_result": {
                "status": "fallback_required",
                "result": {"fallback_required": True},
            }
        },
        stage="browser_fallback_required",
    )

    with store._engine.connect() as connection:  # noqa: SLF001
        counts = store._aggregate_runtime_request_children(  # noqa: SLF001
            connection,
            request_id=request.request_id,
        )

    assert counts["total_count"] == 1
    assert counts["terminal_count"] == 1
    assert counts["fallback_required_count"] == 1
    assert counts["success_count"] == 0
    assert counts["failed_count"] == 0
    assert counts["skipped_count"] == 0
    assert counts["active_count"] == 0

    reconciled = store.reconcile_request_waiting_children(request_id=request.request_id)

    assert reconciled["transitioned"] is False
    assert reconciled["fallback_required_count"] == 1
    assert reconciled["child_total_count"] == 1
    assert reconciled["child_terminal_count"] == 1
    assert reconciled["child_success_count"] == 0
    assert reconciled["child_failed_count"] == 0
    assert reconciled["child_skipped_count"] == 0
    assert reconciled["request"].status == "waiting"
    assert reconciled["request"].current_stage == "collect_product_data"

    watchdog_candidates = store.scan_waiting_children_reconciliation(now=time.time())

    assert watchdog_candidates == []
