from __future__ import annotations

import time
import uuid
from typing import Any

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import (
    coerce_float as _coerce_float,
    coerce_non_negative_float as _coerce_non_negative_float,
    json_dumps as _json_dumps,
    load_json_dict as _load_json_dict,
    resolve_runtime_seconds as _resolve_runtime_seconds,
)
from automation_business_scaffold.infrastructure.runtime.runtime_records import RuntimeTaskExecutionRecord

DEFAULT_WATCHDOG_STALE_AFTER_SECONDS = 300.0
FINAL_RESULT_STATUSES = {"success", "partial_success", "failed", "skipped"}


def _result_status_from_handler_result(result: dict[str, Any], *, default: str) -> str:
    handler_result = result.get("handler_result")
    if isinstance(handler_result, dict):
        handler_status = str(handler_result.get("status") or "")
        if handler_status in FINAL_RESULT_STATUSES:
            return handler_status
    return default


class TaskExecutionRepository:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def enqueue_task_executions(
        self,
        *,
        request_id: str,
        item_code: str,
        workflow_code: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        created_records: list[RuntimeTaskExecutionRecord] = []
        skipped_records: list[dict[str, Any]] = []
        with self._engine.begin() as connection:
            parent_row = (
                connection.execute(
                    self._text(
                        """
                        SELECT status
                        FROM task_request
                        WHERE request_id = :request_id
                        LIMIT 1
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if parent_row is not None and str(parent_row["status"] or "") in {"cancelling", "cancelled", "finished"}:
                return {
                    "created_count": 0,
                    "skipped_count": len(items),
                    "created_records": [],
                    "skipped_records": [
                        {
                            "business_key": str(item.get("business_key", "") or ""),
                            "dedupe_key": str(item.get("dedupe_key", "") or ""),
                            "status": "parent_not_active",
                        }
                        for item in items
                    ],
                }
            next_queue_seq = int(
                connection.execute(
                    self._text("SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM task_execution")
                ).scalar_one()
            )
            now = time.time()
            for item in items:
                business_key = str(item.get("business_key", "") or "")
                dedupe_key = str(item.get("dedupe_key", "") or "")
                resource_code = str(item.get("resource_code", "") or "")
                if dedupe_key:
                    existing = (
                        connection.execute(
                            self._text(
                                """
                                SELECT execution_id, request_id, status
                                FROM task_execution
                                WHERE dedupe_key = :dedupe_key
                                  AND status IN ('pending', 'running')
                                LIMIT 1
                                """
                            ),
                            {"dedupe_key": dedupe_key},
                        )
                        .mappings()
                        .first()
                    )
                    if existing is not None:
                        skipped_records.append(
                            {
                                "business_key": business_key,
                                "dedupe_key": dedupe_key,
                                "existing_execution_id": str(existing["execution_id"]),
                                "existing_request_id": str(existing["request_id"]),
                                "status": str(existing["status"]),
                            }
                        )
                        continue
                execution_id = uuid.uuid4().hex
                payload = dict(item.get("payload") or {})
                max_execution_seconds = _coerce_non_negative_float(item.get("max_execution_seconds"))
                max_idle_seconds = _resolve_runtime_seconds(
                    item.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    item.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                )
                connection.execute(
                    self._text(
                        """
                        INSERT INTO task_execution (
                            execution_id, request_id, task_name, item_code, workflow_code,
                            business_key, dedupe_key, resource_code, status, queue_seq,
                            progress_stage, available_at, worker_id, worker_pid, attempt_count, max_attempts,
                            max_execution_seconds, max_idle_seconds, heartbeat_timeout_seconds,
                            payload_json, summary_json, result_json, error_text,
                            error_type, error_code, dead_letter_reason, run_id,
                            created_at, updated_at, started_at, finished_at, heartbeat_at, last_progress_at
                            , progress_seq, progress_message
                        ) VALUES (
                            :execution_id, :request_id, :task_name, :item_code, :workflow_code,
                            :business_key, :dedupe_key, :resource_code, 'pending', :queue_seq,
                            'queued', :available_at, '', 0, 0, :max_attempts,
                            :max_execution_seconds, :max_idle_seconds, :heartbeat_timeout_seconds,
                            :payload_json, '{}', '{}', '',
                            '', '', '', '',
                            :created_at, :updated_at, NULL, NULL, NULL, :last_progress_at
                            , 0, ''
                        )
                        """
                    ),
                    {
                        "execution_id": execution_id,
                        "request_id": request_id,
                        "task_name": item_code,
                        "item_code": item_code,
                        "workflow_code": workflow_code,
                        "business_key": business_key,
                        "dedupe_key": dedupe_key,
                        "resource_code": resource_code,
                        "queue_seq": next_queue_seq,
                        "available_at": now,
                        "max_attempts": int(item.get("max_attempts", 3) or 3),
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "payload_json": _json_dumps(payload),
                        "created_at": now,
                        "updated_at": now,
                        "last_progress_at": now,
                    },
                )
                created_records.append(
                    RuntimeTaskExecutionRecord(
                        execution_id=execution_id,
                        request_id=request_id,
                        item_code=item_code,
                        workflow_code=workflow_code,
                        business_key=business_key,
                        dedupe_key=dedupe_key,
                        resource_code=resource_code,
                        status="pending",
                        queue_seq=next_queue_seq,
                        progress_stage="queued",
                        available_at=now,
                        max_attempts=int(item.get("max_attempts", 3) or 3),
                        max_execution_seconds=max_execution_seconds,
                        max_idle_seconds=max_idle_seconds,
                        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                        payload=payload,
                        last_progress_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                next_queue_seq += 1
            self._refresh_request_child_counts(connection, request_id=request_id, now=now)
        return {
            "created_count": len(created_records),
            "skipped_count": len(skipped_records),
            "created_records": [record.to_dict() for record in created_records],
            "skipped_records": skipped_records,
        }

    def claim_next_browser_execution(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        item_codes: tuple[str, ...] = (),
    ) -> RuntimeTaskExecutionRecord | None:
        normalized_request_id = str(request_id or "").strip()
        allowed_item_codes = {str(item or "").strip() for item in item_codes if str(item or "").strip()}
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_leases(connection, now=now)
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT execution.*
                        FROM task_execution execution
                        JOIN task_request request ON request.request_id = execution.request_id
                        WHERE execution.status = 'pending'
                          AND execution.available_at <= :available_at
                          AND (:request_id = '' OR execution.request_id = :request_id)
                          AND request.status = 'waiting'
                          AND (
                              :request_id <> ''
                              OR NOT EXISTS (
                                  SELECT 1
                                  FROM task_request older_request
                                  WHERE older_request.status NOT IN ('finished', 'cancelled')
                                    AND (
                                        older_request.created_at < request.created_at
                                        OR (
                                            older_request.created_at = request.created_at
                                            AND older_request.request_id < request.request_id
                                        )
                                    )
                              )
                          )
                        ORDER BY execution.queue_seq ASC, execution.created_at ASC
                        """
                    ),
                    {"available_at": now, "request_id": normalized_request_id},
                )
                .mappings()
                .all()
            )
            for row in rows:
                if allowed_item_codes and str(row["item_code"] or "") not in allowed_item_codes:
                    continue
                resource_code = str(row["resource_code"] or "")
                if resource_code:
                    lease_row = (
                        connection.execute(
                            self._text(
                                """
                                SELECT *
                                FROM resource_lease
                                WHERE resource_code = :resource_code
                                LIMIT 1
                                """
                            ),
                            {"resource_code": resource_code},
                        )
                        .mappings()
                        .first()
                    )
                    if lease_row is not None and _coerce_float(lease_row["lease_until"]) > now:
                        continue
                    if lease_row is not None:
                        connection.execute(
                            self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                            {"resource_code": resource_code},
                        )
                payload = _load_json_dict(row.get("payload_json"))
                run_id = f"browser-{row['execution_id']}-{uuid.uuid4().hex}"
                max_execution_seconds = _resolve_runtime_seconds(
                    row.get("max_execution_seconds"),
                    payload,
                    "max_execution_seconds",
                )
                max_idle_seconds = _resolve_runtime_seconds(
                    row.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                    default=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    row.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                    default=max(lease_seconds * 2.0, 30.0),
                )
                result = connection.execute(
                    self._text(
                        """
                        UPDATE task_execution
                        SET status = 'running',
                            worker_id = :worker_id,
                            worker_pid = :worker_pid,
                            attempt_count = COALESCE(attempt_count, 0) + 1,
                            run_id = :run_id,
                            progress_stage = 'job_claimed',
                            max_execution_seconds = :max_execution_seconds,
                            max_idle_seconds = :max_idle_seconds,
                            heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                            updated_at = :updated_at,
                            started_at = :updated_at,
                            finished_at = NULL,
                            heartbeat_at = :heartbeat_at,
                            last_progress_at = :last_progress_at,
                            progress_seq = COALESCE(progress_seq, 0) + 1,
                            progress_message = :progress_message
                        WHERE execution_id = :execution_id
                          AND status = 'pending'
                          AND EXISTS (
                              SELECT 1
                              FROM task_request request
                              WHERE request.request_id = task_execution.request_id
                                AND request.status = 'waiting'
                                AND (
                                    :request_id <> ''
                                    OR NOT EXISTS (
                                        SELECT 1
                                        FROM task_request older_request
                                        WHERE older_request.status NOT IN ('finished', 'cancelled')
                                          AND (
                                              older_request.created_at < request.created_at
                                              OR (
                                                  older_request.created_at = request.created_at
                                                  AND older_request.request_id < request.request_id
                                              )
                                          )
                                    )
                                )
                          )
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "worker_pid": int(worker_pid or 0),
                        "run_id": run_id,
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "updated_at": now,
                        "heartbeat_at": now,
                        "last_progress_at": now,
                        "progress_message": "Browser worker claimed execution.",
                        "execution_id": row["execution_id"],
                        "request_id": normalized_request_id,
                    },
                )
                if int(result.rowcount or 0) <= 0:
                    continue
                if resource_code:
                    connection.execute(
                        self._text(
                            """
                            INSERT INTO resource_lease (
                                resource_code, execution_id, request_id, worker_id, status,
                                lease_until, heartbeat_at, created_at, updated_at
                            ) VALUES (
                                :resource_code, :execution_id, :request_id, :worker_id, 'active',
                                :lease_until, :heartbeat_at, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            "resource_code": resource_code,
                            "execution_id": row["execution_id"],
                            "request_id": row["request_id"],
                            "worker_id": worker_id,
                            "lease_until": now + lease_seconds,
                            "heartbeat_at": now,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                execution = (
                    connection.execute(
                        self._text("SELECT * FROM task_execution WHERE execution_id = :execution_id"),
                        {"execution_id": row["execution_id"]},
                    )
                    .mappings()
                    .first()
                )
                if execution is None:
                    return None
                return self._execution_from_row(execution)
            return None

    def claim_browser_execution(
        self,
        *,
        execution_id: str,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
    ) -> RuntimeTaskExecutionRecord | None:
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_leases(connection, now=now)
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM task_execution
                        WHERE execution_id = :execution_id
                        LIMIT 1
                        """
                    ),
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if row is None or str(row["status"] or "") != "pending":
                return None
            if _coerce_float(row["available_at"]) > now:
                return None
            resource_code = str(row["resource_code"] or "")
            if resource_code:
                lease_row = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM resource_lease
                            WHERE resource_code = :resource_code
                            LIMIT 1
                            """
                        ),
                        {"resource_code": resource_code},
                    )
                    .mappings()
                    .first()
                )
                if lease_row is not None and _coerce_float(lease_row["lease_until"]) > now:
                    return None
                if lease_row is not None:
                    connection.execute(
                        self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                        {"resource_code": resource_code},
                    )
            payload = _load_json_dict(row.get("payload_json"))
            run_id = f"browser-{row['execution_id']}-{uuid.uuid4().hex}"
            max_execution_seconds = _resolve_runtime_seconds(
                row.get("max_execution_seconds"),
                payload,
                "max_execution_seconds",
            )
            max_idle_seconds = _resolve_runtime_seconds(
                row.get("max_idle_seconds"),
                payload,
                "max_idle_seconds",
                "max_no_progress_seconds",
                default=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
            )
            heartbeat_timeout_seconds = _resolve_runtime_seconds(
                row.get("heartbeat_timeout_seconds"),
                payload,
                "heartbeat_timeout_seconds",
                default=max(lease_seconds * 2.0, 30.0),
            )
            result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'running',
                        worker_id = :worker_id,
                        worker_pid = :worker_pid,
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        run_id = :run_id,
                        progress_stage = 'job_claimed',
                        max_execution_seconds = :max_execution_seconds,
                        max_idle_seconds = :max_idle_seconds,
                        heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                        updated_at = :updated_at,
                        started_at = :updated_at,
                        finished_at = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND status = 'pending'
                    """
                ),
                {
                    "worker_id": worker_id,
                    "worker_pid": int(worker_pid or 0),
                    "run_id": run_id,
                    "max_execution_seconds": max_execution_seconds,
                    "max_idle_seconds": max_idle_seconds,
                    "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "Browser worker claimed execution.",
                    "execution_id": execution_id,
                },
            )
            if int(result.rowcount or 0) <= 0:
                return None
            if resource_code:
                connection.execute(
                    self._text(
                        """
                        INSERT INTO resource_lease (
                            resource_code, execution_id, request_id, worker_id, status,
                            lease_until, heartbeat_at, created_at, updated_at
                        ) VALUES (
                            :resource_code, :execution_id, :request_id, :worker_id, 'active',
                            :lease_until, :heartbeat_at, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "resource_code": resource_code,
                        "execution_id": row["execution_id"],
                        "request_id": row["request_id"],
                        "worker_id": worker_id,
                        "lease_until": now + lease_seconds,
                        "heartbeat_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            execution = (
                connection.execute(
                    self._text("SELECT * FROM task_execution WHERE execution_id = :execution_id"),
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if execution is None:
                return None
            return self._execution_from_row(execution)

    def heartbeat_browser_execution(self, *, execution_id: str, run_id: str, lease_seconds: float) -> bool:
        with self._engine.begin() as connection:
            now = time.time()
            result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET heartbeat_at = :heartbeat_at,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "heartbeat_at": now,
                    "updated_at": now,
                    "execution_id": execution_id,
                    "run_id": run_id,
                },
            )
            if int(result.rowcount or 0) <= 0:
                return False
            connection.execute(
                self._text(
                    """
                    UPDATE resource_lease
                    SET heartbeat_at = :heartbeat_at,
                        lease_until = :lease_until,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "heartbeat_at": now,
                    "lease_until": now + lease_seconds,
                    "updated_at": now,
                    "execution_id": execution_id,
                },
            )
        return True

    def update_task_execution_progress(
        self,
        *,
        execution_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> RuntimeTaskExecutionRecord:
        del lease_seconds
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET progress_stage = :progress_stage,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "execution_id": execution_id,
                    "run_id": run_id,
                    "progress_stage": progress_stage,
                    "progress_message": str(message or ""),
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
        return self.load_task_execution(execution_id=execution_id)

    def _finalize_browser_execution(
        self,
        *,
        execution_id: str,
        status: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        error_text: str,
    ) -> RuntimeTaskExecutionRecord:
        with self._engine.begin() as connection:
            now = time.time()
            execution_row = (
                connection.execute(
                    self._text(
                        """
                        SELECT execution_id, request_id, resource_code
                        FROM task_execution
                        WHERE execution_id = :execution_id
                        LIMIT 1
                        """
                    ),
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if execution_row is None:
                raise ValueError("Task execution not found.")
            update_result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'finished',
                        result_status = :result_status,
                        run_id = :run_id,
                        progress_stage = :progress_stage,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        updated_at = :updated_at,
                        finished_at = :finished_at,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "result_status": _result_status_from_handler_result(result, default=status),
                    "run_id": run_id,
                    "progress_stage": status,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "error_text": error_text,
                    "updated_at": now,
                    "finished_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": f"Browser execution {status}.",
                    "execution_id": execution_id,
                },
            )
            applied = int(update_result.rowcount or 0) > 0
            if applied and execution_row["resource_code"]:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": execution_row["resource_code"]},
                )
            if applied:
                self._refresh_request_child_counts(
                    connection,
                    request_id=str(execution_row["request_id"]),
                    now=now,
                )
            execution = (
                connection.execute(
                    self._text("SELECT * FROM task_execution WHERE execution_id = :execution_id"),
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if execution is None:
                raise ValueError("Task execution not found after update.")
            return self._execution_from_row(execution)

    def mark_browser_execution_success(
        self,
        *,
        execution_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
    ) -> RuntimeTaskExecutionRecord:
        return self._finalize_browser_execution(
            execution_id=execution_id,
            status="success",
            run_id=run_id,
            summary=summary,
            result=result,
            error_text="",
        )

    def mark_browser_execution_skipped(
        self,
        *,
        execution_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
    ) -> RuntimeTaskExecutionRecord:
        return self._finalize_browser_execution(
            execution_id=execution_id,
            status="skipped",
            run_id=run_id,
            summary=summary,
            result=result,
            error_text="",
        )

    def mark_browser_execution_retry_or_failed(
        self,
        *,
        execution_id: str,
        run_id: str,
        error_text: str,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        retry_delay_seconds: float = 30.0,
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> RuntimeTaskExecutionRecord:
        with self._engine.begin() as connection:
            now = time.time()
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT execution.*, request.status AS request_status
                        FROM task_execution execution
                        JOIN task_request request ON request.request_id = execution.request_id
                        WHERE execution.execution_id = :execution_id
                          AND execution.run_id = :run_id
                          AND execution.status = 'running'
                        LIMIT 1
                        """
                    ),
                    {"execution_id": execution_id, "run_id": run_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return self.load_task_execution(execution_id=execution_id)
            parent_cancelling = str(row["request_status"] or "") == "cancelling"
            status = "cancelled" if parent_cancelling else "pending"
            result_status = ""
            available_at = now if parent_cancelling else now + max(retry_delay_seconds, 0.1)
            if not parent_cancelling and int(row["attempt_count"] or 0) >= int(row["max_attempts"] or 1):
                status = "finished"
                result_status = "failed"
                available_at = now
            update_result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = :status,
                        result_status = :result_status,
                        run_id = :run_id,
                        progress_stage = :progress_stage,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        worker_id = '',
                        worker_pid = 0,
                        available_at = :available_at,
                        updated_at = :updated_at,
                        finished_at = CASE WHEN :result_status = 'failed' THEN :updated_at ELSE finished_at END,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "status": status,
                    "result_status": result_status,
                    "run_id": run_id,
                    "progress_stage": status,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "dead_letter_reason": dead_letter_reason or ("parent_request_cancelling" if parent_cancelling else ("max_attempts_exhausted" if result_status == "failed" else "")),
                    "available_at": available_at,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": error_text,
                    "execution_id": execution_id,
                },
            )
            applied = int(update_result.rowcount or 0) > 0
            resource_code = str(row["resource_code"] or "")
            if applied and resource_code:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": resource_code},
                )
            if applied:
                self._refresh_request_child_counts(
                    connection,
                    request_id=str(row["request_id"]),
                    now=now,
                )
        return self.load_task_execution(execution_id=execution_id)

    def load_task_execution(self, *, execution_id: str) -> RuntimeTaskExecutionRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text("SELECT * FROM task_execution WHERE execution_id = :execution_id LIMIT 1"),
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Task execution not found.")
            return self._execution_from_row(row)
