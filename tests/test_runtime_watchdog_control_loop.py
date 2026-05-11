from __future__ import annotations

import time
from typing import Any

import pytest

from automation_business_scaffold.control_plane.watchdog import scanner as watchdog_scanner
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


def _submit_waiting_request(store: RuntimeStore, case_id: str = "watchdog-control"):
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="runtime_watchdog_control_loop",
        payload={"case_id": case_id},
        requested_by="pytest",
    )
    return store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="test_jobs",
        progress_stage="test_jobs",
    )


def _enqueue_api_job(
    store: RuntimeStore,
    *,
    request_id: str,
    business_key: str,
    max_execution_seconds: float = 0.0,
    max_idle_seconds: float = 0.0,
    heartbeat_timeout_seconds: float = 0.0,
) -> dict[str, Any]:
    payload = store.enqueue_api_worker_jobs(
        request_id=request_id,
        task_code="runtime_watchdog_control_loop",
        job_code="test_sleep_job",
        jobs=[
            {
                "business_key": business_key,
                "dedupe_key": f"{request_id}:{business_key}",
                "max_attempts": 1,
                "max_execution_seconds": max_execution_seconds,
                "max_idle_seconds": max_idle_seconds,
                "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                "payload": {
                    "case_id": business_key,
                    "max_idle_seconds": max_idle_seconds,
                    "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                },
            }
        ],
    )
    return dict(payload["created_records"][0])


def _claim_api_job(store: RuntimeStore, *, worker_id: str, worker_pid: int) -> dict[str, Any]:
    claimed = store.claim_next_api_worker_job(
        worker_id=worker_id,
        worker_pid=worker_pid,
        lease_seconds=600.0,
    )
    assert claimed is not None
    return claimed


def test_normal_job_records_run_ownership_and_finishes(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_waiting_request(store)
    queued = _enqueue_api_job(store, request_id=request.request_id, business_key="normal-a")

    claimed = _claim_api_job(store, worker_id="api-worker-normal", worker_pid=12345)

    assert claimed["job_id"] == queued["job_id"]
    assert claimed["status"] == "running"
    assert claimed["run_id"]
    assert claimed["worker_pid"] == 12345
    assert claimed["started_at"] > 0
    assert claimed["heartbeat_at"] > 0
    assert claimed["last_progress_at"] > 0

    succeeded = store.mark_api_worker_job_success(
        job_id=str(claimed["job_id"]),
        run_id=str(claimed["run_id"]),
        summary={"handler_status": "success"},
        result={"case_id": "normal-a"},
    )

    assert succeeded["status"] == "finished"
    assert succeeded["result_status"] == "success"
    assert succeeded["finished_at"] > 0
    assert succeeded["run_id"] == claimed["run_id"]


def test_watchdog_marks_total_timeout_failed_then_kills_worker(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_waiting_request(store)
    _enqueue_api_job(
        store,
        request_id=request.request_id,
        business_key="timeout-b",
        max_execution_seconds=1.0,
    )
    claimed = _claim_api_job(store, worker_id="api-worker-timeout", worker_pid=22222)
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=str(claimed["job_id"]),
        started_at=time.time() - 10.0,
        heartbeat_at=time.time(),
        last_progress_at=time.time(),
    )
    killed: list[int] = []
    monkeypatch.setattr(
        watchdog_scanner,
        "kill_worker_process",
        lambda worker_pid, **_: killed.append(int(worker_pid)) or {"killed": True, "worker_pid": int(worker_pid)},
    )

    payload = watchdog_scanner.execute_watchdog_scan_once({"apply_actions": True}, store=store)

    failed = store.load_api_worker_job(job_id=str(claimed["job_id"]))
    assert payload["applied_count"] == 1
    assert failed["status"] == "finished"
    assert failed["result_status"] == "failed"
    assert failed["error_type"] == "timeout"
    assert failed["error_code"] == "job_total_timeout"
    assert killed == [22222]


def test_watchdog_detects_no_progress_while_heartbeat_is_alive(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_waiting_request(store)
    _enqueue_api_job(
        store,
        request_id=request.request_id,
        business_key="no-progress-c",
        max_idle_seconds=1.0,
    )
    claimed = _claim_api_job(store, worker_id="api-worker-no-progress", worker_pid=33333)
    heartbeat_before = time.time()
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=str(claimed["job_id"]),
        heartbeat_at=heartbeat_before,
        last_progress_at=heartbeat_before - 5.0,
    )
    killed: list[int] = []
    monkeypatch.setattr(
        watchdog_scanner,
        "kill_worker_process",
        lambda worker_pid, **_: killed.append(int(worker_pid)) or {"killed": True, "worker_pid": int(worker_pid)},
    )

    watchdog_scanner.execute_watchdog_scan_once({"apply_actions": True}, store=store)

    failed = store.load_api_worker_job(job_id=str(claimed["job_id"]))
    assert failed["status"] == "finished"
    assert failed["result_status"] == "failed"
    assert failed["error_code"] == "job_no_progress_timeout"
    assert failed["heartbeat_at"] >= heartbeat_before
    assert killed == [33333]


def test_watchdog_detects_worker_heartbeat_timeout(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_waiting_request(store)
    _enqueue_api_job(
        store,
        request_id=request.request_id,
        business_key="heartbeat-d",
        heartbeat_timeout_seconds=1.0,
    )
    claimed = _claim_api_job(store, worker_id="api-worker-heartbeat", worker_pid=0)
    now = time.time()
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=str(claimed["job_id"]),
        heartbeat_at=now - 5.0,
        last_progress_at=now,
    )

    payload = watchdog_scanner.execute_watchdog_scan_once({"apply_actions": True}, store=store)

    failed = store.load_api_worker_job(job_id=str(claimed["job_id"]))
    assert payload["applied_count"] == 1
    assert failed["status"] == "finished"
    assert failed["result_status"] == "failed"
    assert failed["error_code"] == "worker_heartbeat_timeout"
    assert payload["outcomes"][0]["store_result"]["kill_result"]["reason"] == "missing_worker_pid"


def test_queue_recovers_after_watchdog_failed_job_and_new_worker_claims_next(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = _submit_waiting_request(store)
    _enqueue_api_job(store, request_id=request.request_id, business_key="a-normal")
    _enqueue_api_job(
        store,
        request_id=request.request_id,
        business_key="b-timeout",
        max_execution_seconds=1.0,
    )
    _enqueue_api_job(store, request_id=request.request_id, business_key="c-normal")

    job_a = _claim_api_job(store, worker_id="api-worker-a", worker_pid=11111)
    store.mark_api_worker_job_success(
        job_id=str(job_a["job_id"]),
        run_id=str(job_a["run_id"]),
        summary={"handler_status": "success"},
        result={"case_id": "a-normal"},
    )

    job_b = _claim_api_job(store, worker_id="api-worker-b", worker_pid=22222)
    _set_row_fields(
        store,
        table_name="api_worker_job",
        id_column="job_id",
        id_value=str(job_b["job_id"]),
        started_at=time.time() - 10.0,
    )
    killed: list[int] = []
    monkeypatch.setattr(
        watchdog_scanner,
        "kill_worker_process",
        lambda worker_pid, **_: killed.append(int(worker_pid)) or {"killed": True, "worker_pid": int(worker_pid)},
    )

    watchdog_scanner.execute_watchdog_scan_once({"apply_actions": True}, store=store)

    job_c = _claim_api_job(store, worker_id="api-worker-c", worker_pid=33333)
    store.mark_api_worker_job_success(
        job_id=str(job_c["job_id"]),
        run_id=str(job_c["run_id"]),
        summary={"handler_status": "success"},
        result={"case_id": "c-normal"},
    )

    stored_a = store.load_api_worker_job(job_id=str(job_a["job_id"]))
    stored_b = store.load_api_worker_job(job_id=str(job_b["job_id"]))
    stored_c = store.load_api_worker_job(job_id=str(job_c["job_id"]))
    assert stored_a["status"] == "finished"
    assert stored_a["result_status"] == "success"
    assert stored_b["error_code"] == "job_total_timeout"
    assert stored_c["status"] == "finished"
    assert stored_c["result_status"] == "success"
    assert killed == [22222]
    assert job_c["worker_pid"] == 33333
    assert job_c["worker_pid"] != job_b["worker_pid"]
