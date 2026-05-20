from __future__ import annotations

import time
import uuid
from typing import Any

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import (
    coerce_non_negative_float as _coerce_non_negative_float,
    json_dumps as _json_dumps,
    load_json_dict as _load_json_dict,
    resolve_runtime_seconds as _resolve_runtime_seconds,
)

ACTIVE_API_WORKER_JOB_STATUSES = {"pending", "running", "waiting"}
TERMINAL_API_WORKER_JOB_STATUSES = {"finished", "cancelled", "success", "failed", "skipped"}
FINAL_RESULT_STATUSES = {"success", "partial_success", "failed", "skipped"}
DEFAULT_WATCHDOG_STALE_AFTER_SECONDS = 300.0


def _result_status_from_handler_result(result: dict[str, Any], *, default: str) -> str:
    handler_result = result.get("handler_result")
    if isinstance(handler_result, dict):
        handler_status = str(handler_result.get("status") or "")
        if handler_status in FINAL_RESULT_STATUSES:
            return handler_status
        if handler_status == "fallback_required":
            return ""
    return default


class ApiWorkerJobRepository:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def enqueue_api_worker_jobs(
        self,
        *,
        request_id: str,
        task_code: str,
        job_code: str,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_records: list[dict[str, Any]] = []
        updated_records: list[dict[str, Any]] = []
        skipped_records: list[dict[str, Any]] = []
        now = time.time()
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
                    "updated_count": 0,
                    "skipped_count": len(jobs),
                    "created_records": [],
                    "updated_records": [],
                    "skipped_records": [
                        {
                            "business_key": str(job.get("business_key", "") or ""),
                            "dedupe_key": str(job.get("dedupe_key", "") or ""),
                            "status": "parent_not_active",
                        }
                        for job in jobs
                    ],
                }
            for job in jobs:
                business_key = str(job.get("business_key", "") or "")
                dedupe_key = str(job.get("dedupe_key", "") or "")
                payload = dict(job.get("payload") or {})
                max_attempts = int(job.get("max_attempts", 3) or 3)
                max_execution_seconds = _coerce_non_negative_float(job.get("max_execution_seconds"))
                max_idle_seconds = _resolve_runtime_seconds(
                    job.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    job.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                )
                existing = None
                if dedupe_key:
                    existing = (
                        connection.execute(
                            self._text(
                                """
                                SELECT *
                                FROM api_worker_job
                                WHERE dedupe_key = :dedupe_key
                                LIMIT 1
                                """
                            ),
                            {"dedupe_key": dedupe_key},
                        )
                        .mappings()
                        .first()
                    )
                if existing is not None:
                    existing_status = str(existing["status"] or "")
                    if existing_status in TERMINAL_API_WORKER_JOB_STATUSES and not force_refresh:
                        skipped_records.append(
                            {
                                "business_key": business_key,
                                "dedupe_key": dedupe_key,
                                "existing_job_id": str(existing["job_id"]),
                                "status": existing_status,
                            }
                        )
                        continue
                    connection.execute(
                        self._text(
                            """
                            UPDATE api_worker_job
                            SET request_id = :request_id,
                                task_code = :task_code,
                                job_code = :job_code,
                                business_key = :business_key,
                                status = 'pending',
                                stage = 'queued',
                                progress_stage = 'queued',
                                payload_json = :payload_json,
                                summary_json = '{}',
                                result_json = '{}',
                                error_text = '',
                                error_type = '',
                                error_code = '',
                                dead_letter_reason = '',
                                worker_id = '',
                                worker_pid = 0,
                                lease_until = NULL,
                                available_at = :available_at,
                                max_attempts = :max_attempts,
                                max_execution_seconds = :max_execution_seconds,
                                max_idle_seconds = :max_idle_seconds,
                                heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                                progress_seq = 0,
                                progress_message = '',
                                last_progress_at = :last_progress_at,
                                updated_at = :updated_at,
                                finished_at = NULL,
                                heartbeat_at = NULL
                            WHERE job_id = :job_id
                            """
                        ),
                        {
                            "job_id": existing["job_id"],
                            "request_id": request_id,
                            "task_code": task_code,
                            "job_code": job_code,
                            "business_key": business_key,
                            "payload_json": _json_dumps(payload),
                            "available_at": now,
                            "max_attempts": max_attempts,
                            "max_execution_seconds": max_execution_seconds,
                            "max_idle_seconds": max_idle_seconds,
                            "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                            "last_progress_at": now,
                            "updated_at": now,
                        },
                    )
                    updated = (
                        connection.execute(
                            self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                            {"job_id": existing["job_id"]},
                        )
                        .mappings()
                        .first()
                    )
                    if updated is not None:
                        updated_records.append(self._api_worker_job_from_row(updated))
                    continue

                job_id = uuid.uuid4().hex
                connection.execute(
                    self._text(
                        """
                        INSERT INTO api_worker_job (
                            job_id, request_id, task_code, job_code, business_key, dedupe_key,
                            status, stage, progress_stage, attempt_count, max_attempts, max_execution_seconds,
                            max_idle_seconds, heartbeat_timeout_seconds,
                            payload_json, summary_json, result_json, error_text, error_type, error_code,
                            dead_letter_reason, worker_id, worker_pid, lease_until,
                            available_at, run_id, created_at, updated_at, started_at,
                            finished_at, heartbeat_at, last_progress_at, progress_seq, progress_message
                        ) VALUES (
                            :job_id, :request_id, :task_code, :job_code, :business_key, :dedupe_key,
                            'pending', 'queued', 'queued', 0, :max_attempts, :max_execution_seconds,
                            :max_idle_seconds, :heartbeat_timeout_seconds, :payload_json,
                            '{}', '{}', '', '', '', '', '', 0, NULL,
                            :available_at, '', :created_at, :updated_at, NULL,
                            NULL, NULL, :last_progress_at, 0, ''
                        )
                        """
                    ),
                    {
                        "job_id": job_id,
                        "request_id": request_id,
                        "task_code": task_code,
                        "job_code": job_code,
                        "business_key": business_key,
                        "dedupe_key": dedupe_key,
                        "max_attempts": max_attempts,
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "payload_json": _json_dumps(payload),
                        "available_at": now,
                        "created_at": now,
                        "updated_at": now,
                        "last_progress_at": now,
                    },
                )
                created = (
                    connection.execute(
                        self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                        {"job_id": job_id},
                    )
                    .mappings()
                    .first()
                )
                if created is not None:
                    created_records.append(self._api_worker_job_from_row(created))
        return {
            "created_count": len(created_records),
            "updated_count": len(updated_records),
            "skipped_count": len(skipped_records),
            "created_records": created_records,
            "updated_records": updated_records,
            "skipped_records": skipped_records,
        }

    def _requeue_expired_api_worker_job_claims(self, connection: Any, *, now: float) -> None:
        del connection, now
        return

    def claim_next_api_worker_job(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        job_code: str = "",
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            self._requeue_expired_api_worker_job_claims(connection, now=now)
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT job.*
                        FROM api_worker_job job
                        JOIN task_request request ON request.request_id = job.request_id
                        WHERE (:request_id = '' OR job.request_id = :request_id)
                          AND (:job_code = '' OR job.job_code = :job_code)
                          AND request.status = 'waiting'
                          AND job.status = 'pending'
                          AND job.available_at <= :available_at
                          AND (
                              COALESCE(NULLIF(request.current_stage, ''), '') <> 'fastmoss_security_browser_fallback'
                              OR COALESCE(NULLIF(job.payload_json::jsonb ->> 'stage_code', ''), '') = ''
                              OR job.payload_json::jsonb ->> 'stage_code' = request.current_stage
                          )
                          AND NOT EXISTS (
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
                        ORDER BY job.available_at ASC, job.created_at ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "request_id": request_id,
                        "job_code": job_code,
                        "available_at": now,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            payload = _load_json_dict(row.get("payload_json"))
            run_id = f"api-worker-{row['job_id']}-{uuid.uuid4().hex}"
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
                    UPDATE api_worker_job
                    SET status = 'running',
                        stage = 'running',
                        progress_stage = 'job_claimed',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        worker_pid = :worker_pid,
                        lease_until = :lease_until,
                        run_id = :run_id,
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
                    WHERE job_id = :job_id
                      AND status = 'pending'
                      AND EXISTS (
                          SELECT 1
                          FROM task_request request
                          WHERE request.request_id = api_worker_job.request_id
                            AND request.status = 'waiting'
                            AND (
                                COALESCE(NULLIF(request.current_stage, ''), '') <> 'fastmoss_security_browser_fallback'
                                OR COALESCE(NULLIF(api_worker_job.payload_json::jsonb ->> 'stage_code', ''), '') = ''
                                OR api_worker_job.payload_json::jsonb ->> 'stage_code' = request.current_stage
                            )
                            AND NOT EXISTS (
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
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "worker_pid": int(worker_pid or 0),
                    "lease_until": now + max(lease_seconds, 5.0),
                    "run_id": run_id,
                    "max_execution_seconds": max_execution_seconds,
                    "max_idle_seconds": max_idle_seconds,
                    "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker claimed job.",
                },
            )
            if int(result.rowcount or 0) <= 0:
                return None
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._api_worker_job_from_row(claimed) if claimed is not None else None

    def heartbeat_api_worker_job(self, *, job_id: str, run_id: str, lease_seconds: float) -> bool:
        with self._engine.begin() as connection:
            now = time.time()
            result = connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET heartbeat_at = :heartbeat_at,
                        lease_until = :lease_until,
                        updated_at = :updated_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "heartbeat_at": now,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "updated_at": now,
                },
            )
        return int(result.rowcount or 0) > 0

    def update_api_worker_job_progress(
        self,
        *,
        job_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        del lease_seconds
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET progress_stage = :progress_stage,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "progress_stage": progress_stage,
                    "progress_message": str(message or ""),
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def mark_api_worker_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        stage: str = "completed",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = 'finished',
                        result_status = :result_status,
                        stage = :stage,
                        progress_stage = :progress_stage,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = '',
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at,
                        finished_at = :finished_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "result_status": _result_status_from_handler_result(result, default="success"),
                    "stage": stage,
                    "progress_stage": stage,
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker job succeeded.",
                    "updated_at": now,
                    "finished_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def mark_api_worker_job_waiting(
        self,
        *,
        job_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        stage: str,
        error_text: str = "",
        error_type: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM task_request request
                                WHERE request.request_id = api_worker_job.request_id
                                  AND request.status = 'cancelling'
                            ) THEN 'cancelled'
                            ELSE 'waiting'
                        END,
                        result_status = '',
                        stage = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM task_request request
                                WHERE request.request_id = api_worker_job.request_id
                                  AND request.status = 'cancelling'
                            ) THEN 'cancelled'
                            ELSE :stage
                        END,
                        progress_stage = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM task_request request
                                WHERE request.request_id = api_worker_job.request_id
                                  AND request.status = 'cancelling'
                            ) THEN 'cancelled'
                            ELSE :progress_stage
                        END,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at,
                        finished_at = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM task_request request
                                WHERE request.request_id = api_worker_job.request_id
                                  AND request.status = 'cancelling'
                            ) THEN :updated_at
                            ELSE NULL
                        END
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "stage": stage,
                    "progress_stage": stage,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "error_text": str(error_text or ""),
                    "error_type": str(error_type or ""),
                    "error_code": str(error_code or ""),
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker job is waiting for external work.",
                    "updated_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def requeue_waiting_api_worker_job(
        self,
        *,
        job_id: str,
        payload: dict[str, Any],
        stage: str = "queued",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = 'pending',
                        result_status = '',
                        stage = :stage,
                        progress_stage = :progress_stage,
                        payload_json = :payload_json,
                        summary_json = '{}',
                        result_json = '{}',
                        error_text = '',
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        available_at = :available_at,
                        run_id = '',
                        updated_at = :updated_at,
                        started_at = NULL,
                        finished_at = NULL,
                        heartbeat_at = NULL,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE job_id = :job_id
                      AND status = 'waiting'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM task_request request
                          WHERE request.request_id = api_worker_job.request_id
                            AND request.status = 'cancelling'
                      )
                    """
                ),
                {
                    "job_id": job_id,
                    "stage": stage,
                    "progress_stage": stage,
                    "payload_json": _json_dumps(payload),
                    "available_at": now,
                    "updated_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker job requeued after waiting work completed.",
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def mark_waiting_api_worker_job_failed(
        self,
        *,
        job_id: str,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_text: str = "",
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = 'finished',
                        result_status = 'failed',
                        stage = 'failed',
                        progress_stage = 'failed',
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        run_id = '',
                        updated_at = :updated_at,
                        finished_at = :updated_at,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE job_id = :job_id
                      AND status = 'waiting'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM task_request request
                          WHERE request.request_id = api_worker_job.request_id
                            AND request.status = 'cancelling'
                      )
                    """
                ),
                {
                    "job_id": job_id,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": str(error_text or ""),
                    "error_type": str(error_type or ""),
                    "error_code": str(error_code or ""),
                    "dead_letter_reason": str(dead_letter_reason or "browser_fallback_failed"),
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": str(error_text or "API worker job failed after waiting work failed."),
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def mark_api_worker_job_retry_or_failed(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        retry_delay_seconds: float = 30.0,
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT job.attempt_count, job.max_attempts, job.request_id, request.status AS request_status
                        FROM api_worker_job job
                        JOIN task_request request ON request.request_id = job.request_id
                        WHERE job.job_id = :job_id
                          AND job.run_id = :run_id
                          AND job.status = 'running'
                        LIMIT 1
                        """
                    ),
                    {"job_id": job_id, "run_id": run_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return self.load_api_worker_job(job_id=job_id)
            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            parent_cancelling = str(row["request_status"] or "") == "cancelling"
            will_retry = attempt_count < max_attempts and not parent_cancelling
            status = "cancelled" if parent_cancelling else ("pending" if will_retry else "finished")
            result_status = "" if will_retry or parent_cancelling else "failed"
            available_at = now if parent_cancelling else (now + max(retry_delay_seconds, 0.1) if will_retry else now)
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = :status,
                        result_status = :result_status,
                        stage = :stage,
                        progress_stage = :progress_stage,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at,
                        finished_at = CASE WHEN :result_status = 'failed' THEN :updated_at ELSE finished_at END
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "result_status": result_status,
                    "stage": "cancelled" if parent_cancelling else ("retry_wait" if will_retry else "failed"),
                    "progress_stage": "cancelled" if parent_cancelling else ("retry_wait" if will_retry else "failed"),
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "dead_letter_reason": dead_letter_reason or ("parent_request_cancelling" if parent_cancelling else ("max_attempts_exhausted" if not will_retry else "")),
                    "available_at": available_at,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": error_text,
                    "updated_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def load_api_worker_job(self, *, job_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("API worker job not found.")
            return self._api_worker_job_from_row(row)

    def list_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM api_worker_job
                        WHERE request_id = :request_id
                          AND (:job_code = '' OR job_code = :job_code)
                        ORDER BY created_at ASC, updated_at ASC
                        """
                    ),
                    {"request_id": request_id, "job_code": job_code},
                )
                .mappings()
                .all()
            )
        return [self._api_worker_job_from_row(row) for row in rows]

    def list_api_worker_job_summaries_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT
                            job_id, request_id, task_code, job_code, business_key, dedupe_key,
                            status, result_status, stage, progress_stage, attempt_count, max_attempts,
                            payload_json, summary_json, error_text, error_type, error_code,
                            created_at, updated_at, started_at, finished_at
                        FROM api_worker_job
                        WHERE request_id = :request_id
                          AND (:job_code = '' OR job_code = :job_code)
                        ORDER BY created_at ASC, updated_at ASC
                        """
                    ),
                    {"request_id": request_id, "job_code": job_code},
                )
                .mappings()
                .all()
            )
        return [
            {
                "job_id": str(row["job_id"]),
                "request_id": str(row["request_id"] or ""),
                "task_code": str(row["task_code"] or ""),
                "job_code": str(row["job_code"] or ""),
                "business_key": str(row["business_key"] or ""),
                "dedupe_key": str(row["dedupe_key"] or ""),
                "status": str(row["status"] or ""),
                "result_status": str(row.get("result_status", "") or ""),
                "stage": str(row["stage"] or ""),
                "progress_stage": str(row.get("progress_stage", "") or ""),
                "attempt_count": int(row["attempt_count"] or 0),
                "max_attempts": int(row["max_attempts"] or 0),
                "payload": _load_json_dict(row["payload_json"]),
                "summary": _load_json_dict(row["summary_json"]),
                "result": {},
                "error_text": str(row["error_text"] or ""),
                "error_type": str(row.get("error_type", "") or ""),
                "error_code": str(row.get("error_code", "") or ""),
                "created_at": _coerce_non_negative_float(row.get("created_at")),
                "updated_at": _coerce_non_negative_float(row.get("updated_at")),
                "started_at": _coerce_non_negative_float(row.get("started_at")),
                "finished_at": _coerce_non_negative_float(row.get("finished_at")),
            }
            for row in rows
        ]

    def summarize_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT
                            status,
                            COALESCE(NULLIF(result_status, ''), status) AS effective_status,
                            COUNT(*) AS count
                        FROM api_worker_job
                        WHERE request_id = :request_id
                          AND (:job_code = '' OR job_code = :job_code)
                        GROUP BY status, COALESCE(NULLIF(result_status, ''), status)
                        """
                    ),
                    {"request_id": request_id, "job_code": job_code},
                )
                .mappings()
                .all()
            )
        counts: dict[str, int] = {}
        active_count = 0
        for row in rows:
            row_count = int(row["count"] or 0)
            status = str(row["status"] or "")
            effective_status = str(row["effective_status"] or status or "unknown")
            counts[effective_status] = counts.get(effective_status, 0) + row_count
            if status in ACTIVE_API_WORKER_JOB_STATUSES:
                active_count += row_count
        total = sum(counts.values())
        success_count = counts.get("success", 0) + counts.get("partial_success", 0)
        failed_count = counts.get("failed", 0) + counts.get("cancelled", 0)
        return {
            "total": total,
            "counts": counts,
            "active_count": active_count,
            "terminal_count": max(total - active_count, 0),
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": counts.get("skipped", 0),
            "fallback_required_count": counts.get("fallback_required", 0),
        }
