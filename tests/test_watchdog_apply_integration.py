from __future__ import annotations

import time

from automation_business_scaffold.control_plane.watchdog.scanner import (
    EXECUTION_TIMEOUT_RULE,
    LEASE_EXPIRED_RULE,
    OUTBOX_SENDING_TIMEOUT_RULE,
    STALE_PROGRESS_RULE,
    WAITING_CHILDREN_RULE,
    collect_watchdog_candidates,
    decide_watchdog_action,
    execute_watchdog_scan_once,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _set_row_fields(
    store: RuntimeStore,
    *,
    table_name: str,
    id_column: str,
    id_value: str,
    **fields: object,
) -> None:
    assignments = [f"{column} = :{column}" for column in fields]
    params = dict(fields)
    params["id_value"] = id_value
    with store._engine.begin() as connection:  # noqa: SLF001
        connection.execute(
            store._text(  # noqa: SLF001
                f"""
                UPDATE {table_name}
                SET {", ".join(assignments)}
                WHERE {id_column} = :id_value
                """
            ),
            params,
        )


def _submit_request(
    store: RuntimeStore,
    *,
    case_id: str,
    max_execution_seconds: float = 0.0,
):
    return store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="watchdog_apply_integration",
        payload={"case_id": case_id},
        requested_by="pytest",
        max_execution_seconds=max_execution_seconds,
    )


def _submit_waiting_request(store: RuntimeStore, *, case_id: str):
    request = _submit_request(store, case_id=case_id)
    claimed = store.claim_next_task_request(worker_id=f"executor-{case_id}", lease_seconds=600.0)
    assert claimed is not None
    assert claimed.request_id == request.request_id
    return store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="waiting_children",
        progress_stage="waiting_children",
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )


def _seed_watchdog_runtime_records(store: RuntimeStore) -> dict[str, str]:
    stale_request = _submit_request(store, case_id="stale-request")
    claimed_stale_request = store.claim_next_task_request(
        worker_id="executor-stale",
        lease_seconds=600.0,
    )
    assert claimed_stale_request is not None
    store.update_task_request(
        request_id=stale_request.request_id,
        current_stage="dispatch_children",
        progress_stage="dispatch_children",
        stage_cursor={"dispatch_children": {"offset": 1}},
    )

    lease_request = _submit_waiting_request(store, case_id="lease-job")
    lease_jobs = store.enqueue_api_worker_jobs(
        request_id=lease_request.request_id,
        task_code="watchdog_apply_integration",
        job_code="lease_expired_job",
        jobs=[
            {
                "business_key": "lease-job",
                "dedupe_key": f"{lease_request.request_id}:lease-job",
                "max_attempts": 3,
                "payload": {"kind": "lease"},
            }
        ],
    )
    lease_job_id = lease_jobs["created_records"][0]["job_id"]
    claimed_lease_job = store.claim_next_api_worker_job(
        worker_id="api-worker-lease",
        lease_seconds=600.0,
    )
    assert claimed_lease_job is not None
    assert claimed_lease_job["job_id"] == lease_job_id

    timeout_request = _submit_waiting_request(store, case_id="timeout-execution")
    timeout_executions = store.enqueue_task_executions(
        request_id=timeout_request.request_id,
        item_code="timeout_browser_execution",
        workflow_code="watchdog_apply_integration",
        items=[
            {
                "business_key": "timeout-execution",
                "dedupe_key": f"{timeout_request.request_id}:timeout-execution",
                "max_attempts": 1,
                "max_execution_seconds": 1.0,
                "payload": {"kind": "timeout"},
            }
        ],
    )
    timeout_execution_id = timeout_executions["created_records"][0]["execution_id"]
    claimed_timeout_execution = store.claim_browser_execution(
        execution_id=timeout_execution_id,
        worker_id="browser-worker-timeout",
        lease_seconds=600.0,
    )
    assert claimed_timeout_execution is not None

    retry_execution_request = _submit_waiting_request(store, case_id="retry-execution")
    retry_executions = store.enqueue_task_executions(
        request_id=retry_execution_request.request_id,
        item_code="retry_browser_execution",
        workflow_code="watchdog_apply_integration",
        items=[
            {
                "business_key": "retry-execution",
                "dedupe_key": f"{retry_execution_request.request_id}:retry-execution",
                "max_attempts": 3,
                "max_execution_seconds": 1.0,
                "payload": {"kind": "retry"},
            }
        ],
    )
    retry_execution_id = retry_executions["created_records"][0]["execution_id"]
    claimed_retry_execution = store.claim_browser_execution(
        execution_id=retry_execution_id,
        worker_id="browser-worker-retry",
        lease_seconds=600.0,
    )
    assert claimed_retry_execution is not None

    api_fail_request = _submit_waiting_request(store, case_id="api-fail")
    api_fail_jobs = store.enqueue_api_worker_jobs(
        request_id=api_fail_request.request_id,
        task_code="watchdog_apply_integration",
        job_code="api_fail_job",
        jobs=[
            {
                "business_key": "api-fail",
                "dedupe_key": f"{api_fail_request.request_id}:api-fail",
                "max_attempts": 1,
                "max_execution_seconds": 1.0,
                "payload": {"kind": "api-fail"},
            }
        ],
    )
    api_fail_job_id = api_fail_jobs["created_records"][0]["job_id"]
    claimed_api_fail_job = store.claim_next_api_worker_job(
        worker_id="api-worker-fail",
        lease_seconds=600.0,
    )
    assert claimed_api_fail_job is not None
    assert claimed_api_fail_job["job_id"] == api_fail_job_id

    repair_request = _submit_waiting_request(store, case_id="repair-parent")
    repair_jobs = store.enqueue_api_worker_jobs(
        request_id=repair_request.request_id,
        task_code="watchdog_apply_integration",
        job_code="terminal_child_job",
        jobs=[
            {
                "business_key": "repair-child",
                "dedupe_key": f"{repair_request.request_id}:repair-child",
                "payload": {"kind": "repair"},
            }
        ],
    )
    repair_job_id = repair_jobs["created_records"][0]["job_id"]
    claimed_repair_job = store.claim_next_api_worker_job(
        worker_id="api-worker-repair",
        lease_seconds=600.0,
    )
    assert claimed_repair_job is not None
    assert claimed_repair_job["job_id"] == repair_job_id
    store.mark_api_worker_job_success(
        job_id=repair_job_id,
        run_id=claimed_repair_job["run_id"],
        summary={"handler_status": "success"},
        result={"case_id": "repair-parent"},
    )

    priority_request = _submit_waiting_request(store, case_id="priority-dedupe")
    priority_jobs = store.enqueue_api_worker_jobs(
        request_id=priority_request.request_id,
        task_code="watchdog_apply_integration",
        job_code="timeout_beats_lease_job",
        jobs=[
            {
                "business_key": "priority-job",
                "dedupe_key": f"{priority_request.request_id}:priority-job",
                "max_attempts": 3,
                "max_execution_seconds": 1.0,
                "payload": {"kind": "priority"},
            }
        ],
    )
    priority_job_id = priority_jobs["created_records"][0]["job_id"]
    claimed_priority_job = store.claim_next_api_worker_job(
        worker_id="api-worker-priority",
        lease_seconds=600.0,
    )
    assert claimed_priority_job is not None
    assert claimed_priority_job["job_id"] == priority_job_id

    retry_outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="watchdog.apply",
        ref_id=stale_request.request_id,
        reply_target="reply://pytest",
        payload={"case_id": "outbox"},
        dedupe_key=f"watchdog.apply:{stale_request.request_id}",
    )
    claimed_outbox = store.claim_next_outbox(worker_id="dispatcher-watchdog", lease_seconds=600.0)
    assert claimed_outbox is not None
    assert claimed_outbox.outbox_id == retry_outbox.outbox_id

    fail_outbox = store.create_notification_outbox(
        channel_code="noop",
        event_type="watchdog.apply.exhausted",
        ref_id=stale_request.request_id,
        reply_target="reply://pytest",
        payload={"case_id": "outbox-fail"},
        dedupe_key=f"watchdog.apply.exhausted:{stale_request.request_id}",
    )
    claimed_fail_outbox = store.claim_next_outbox(
        worker_id="dispatcher-watchdog-fail",
        lease_seconds=600.0,
    )
    assert claimed_fail_outbox is not None
    assert claimed_fail_outbox.outbox_id == fail_outbox.outbox_id

    now = time.time()
    _set_row_fields(
        store,
        table_name="task_request",
        id_column="request_id",
        id_value=stale_request.request_id,
        lease_until=now + 600.0,
        heartbeat_at=now,
        last_progress_at=now - 900.0,
        started_at=now,
    )
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=lease_job_id,
        lease_until=now - 10.0,
        heartbeat_at=now,
        last_progress_at=now,
        started_at=now,
    )
    _set_row_fields(
        store,
        table_name="task_execution",
        id_column="execution_id",
        id_value=timeout_execution_id,
        started_at=now - 30.0,
        heartbeat_at=now,
        last_progress_at=now,
    )
    _set_row_fields(
        store,
        table_name="task_execution",
        id_column="execution_id",
        id_value=retry_execution_id,
        started_at=now - 30.0,
        heartbeat_at=now,
        last_progress_at=now,
    )
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=api_fail_job_id,
        lease_until=now + 600.0,
        heartbeat_at=now,
        last_progress_at=now,
        started_at=now - 30.0,
    )
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=priority_job_id,
        lease_until=now - 10.0,
        heartbeat_at=now,
        last_progress_at=now,
        started_at=now - 30.0,
    )
    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=retry_outbox.outbox_id,
        lease_until=now - 10.0,
        heartbeat_at=now,
        last_progress_at=now,
    )
    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=fail_outbox.outbox_id,
        retry_count=0,
        max_retry_count=1,
        lease_until=now - 10.0,
        heartbeat_at=now,
        last_progress_at=now,
    )

    return {
        "stale_request_id": stale_request.request_id,
        "lease_job_id": lease_job_id,
        "timeout_execution_id": timeout_execution_id,
        "retry_execution_id": retry_execution_id,
        "api_fail_job_id": api_fail_job_id,
        "repair_request_id": repair_request.request_id,
        "priority_job_id": priority_job_id,
        "retry_outbox_id": retry_outbox.outbox_id,
        "fail_outbox_id": fail_outbox.outbox_id,
    }


def test_watchdog_collects_real_runtime_candidates_and_dedupes_priority(runtime_db_url) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    ids = _seed_watchdog_runtime_records(store)

    candidates, missing_helpers = collect_watchdog_candidates(store, limit_per_rule=50)

    assert missing_helpers == ()
    assert {candidate.rule_code for candidate in candidates} == {
        LEASE_EXPIRED_RULE,
        STALE_PROGRESS_RULE,
        EXECUTION_TIMEOUT_RULE,
        WAITING_CHILDREN_RULE,
        OUTBOX_SENDING_TIMEOUT_RULE,
    }
    assert len(candidates) == 9
    priority_candidate = next(
        candidate for candidate in candidates if candidate.target_id == ids["priority_job_id"]
    )
    assert priority_candidate.rule_code == EXECUTION_TIMEOUT_RULE
    outbox_candidate = next(
        candidate for candidate in candidates if candidate.target_id == ids["retry_outbox_id"]
    )
    assert outbox_candidate.max_retries == 10


def test_watchdog_apply_once_persists_retry_fail_repair_and_is_idempotent(runtime_db_url) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    ids = _seed_watchdog_runtime_records(store)

    payload = execute_watchdog_scan_once({"apply_actions": True, "limit_per_rule": 50}, store=store)

    assert payload["status"] == "ok"
    assert payload["action_count"] == 9
    assert payload["applied_count"] == 9
    assert payload["counts_by_action"] == {"fail": 3, "retry": 5, "repair": 1}
    stale_request = store.load_task_request(request_id=ids["stale_request_id"])
    assert stale_request.status == "pending"
    assert stale_request.current_stage == ""
    assert stale_request.stage_cursor == {}

    lease_job = store.load_api_worker_job(job_id=ids["lease_job_id"])
    assert lease_job["status"] == "retry_wait"
    assert lease_job["worker_id"] == ""
    assert lease_job["lease_until"] == 0.0
    assert lease_job["error_type"] == "lease_expired"

    timed_out_execution = store.load_task_execution(execution_id=ids["timeout_execution_id"])
    assert timed_out_execution.status == "failed"
    assert timed_out_execution.error_type == "timeout"
    assert timed_out_execution.dead_letter_reason == "watchdog_failed"

    retry_execution = store.load_task_execution(execution_id=ids["retry_execution_id"])
    assert retry_execution.status == "retry_wait"
    assert retry_execution.worker_id == ""
    assert retry_execution.error_type == "timeout"
    assert retry_execution.dead_letter_reason == ""

    failed_api_job = store.load_api_worker_job(job_id=ids["api_fail_job_id"])
    assert failed_api_job["status"] == "failed"
    assert failed_api_job["worker_id"] == ""
    assert failed_api_job["lease_until"] == 0.0
    assert failed_api_job["error_type"] == "timeout"
    assert failed_api_job["dead_letter_reason"] == "watchdog_failed"

    repaired_request = store.load_task_request(request_id=ids["repair_request_id"])
    assert repaired_request.status == "ready_for_summary"
    assert repaired_request.child_total_count == 1
    assert repaired_request.child_terminal_count == 1

    priority_job = store.load_api_worker_job(job_id=ids["priority_job_id"])
    assert priority_job["status"] == "retry_wait"
    assert priority_job["error_type"] == "timeout"

    retry_outbox = store.load_outbox(outbox_id=ids["retry_outbox_id"])
    assert retry_outbox.status == "retry_wait"
    assert retry_outbox.retry_count == 1
    assert retry_outbox.worker_id == ""
    assert retry_outbox.lease_until == 0.0
    assert retry_outbox.error_type == "outbox_sending_timeout"

    fail_outbox = store.load_outbox(outbox_id=ids["fail_outbox_id"])
    assert fail_outbox.status == "failed"
    assert fail_outbox.retry_count == 1
    assert fail_outbox.worker_id == ""
    assert fail_outbox.lease_until == 0.0
    assert fail_outbox.error_type == "outbox_sending_timeout"
    assert fail_outbox.dead_letter_reason == "watchdog_failed"

    second_payload = execute_watchdog_scan_once({"apply_actions": True, "limit_per_rule": 50}, store=store)

    assert second_payload["status"] == "idle"
    assert second_payload["action_count"] == 0
    assert second_payload["applied_count"] == 0


def test_watchdog_apply_skips_stale_runtime_candidate(runtime_db_url) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    ids = _seed_watchdog_runtime_records(store)
    candidates, _ = collect_watchdog_candidates(store, limit_per_rule=50)
    candidate = next(candidate for candidate in candidates if candidate.target_id == ids["lease_job_id"])
    action = decide_watchdog_action(candidate)

    job = store.load_api_worker_job(job_id=ids["lease_job_id"])
    store.mark_api_worker_job_success(
        job_id=ids["lease_job_id"],
        run_id=str(job["run_id"]),
        summary={"handler_status": "success"},
        result={"case_id": "stale-candidate"},
    )

    result = store.apply_watchdog_action(action=action.to_dict())

    assert result["applied"] is False
    assert result["status"] == "success"
    assert store.load_api_worker_job(job_id=ids["lease_job_id"])["status"] == "success"


def test_watchdog_apply_skips_refreshed_same_status_runtime_candidate(runtime_db_url) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    ids = _seed_watchdog_runtime_records(store)
    candidates, _ = collect_watchdog_candidates(store, limit_per_rule=50)
    actions = {
        candidate.target_id: decide_watchdog_action(candidate).to_dict()
        for candidate in candidates
    }
    now = time.time()

    _set_row_fields(
        store,
        table_name="task_request",
        id_column="request_id",
        id_value=ids["stale_request_id"],
        last_progress_at=now,
        heartbeat_at=now,
        lease_until=now + 600.0,
    )
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=ids["lease_job_id"],
        lease_until=now + 600.0,
        heartbeat_at=now,
    )
    _set_row_fields(
        store,
        table_name="notification_outbox",
        id_column="outbox_id",
        id_value=ids["retry_outbox_id"],
        lease_until=now + 600.0,
        heartbeat_at=now,
    )

    request_result = store.apply_watchdog_action(action=actions[ids["stale_request_id"]])
    job_result = store.apply_watchdog_action(action=actions[ids["lease_job_id"]])
    outbox_result = store.apply_watchdog_action(action=actions[ids["retry_outbox_id"]])

    assert request_result["applied"] is False
    assert job_result["applied"] is False
    assert outbox_result["applied"] is False
    assert store.load_task_request(request_id=ids["stale_request_id"]).status == "running"
    assert store.load_api_worker_job(job_id=ids["lease_job_id"])["status"] == "running"
    assert store.load_outbox(outbox_id=ids["retry_outbox_id"]).status == "sending"
