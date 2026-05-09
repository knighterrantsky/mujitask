from __future__ import annotations

import time
from typing import Any, Mapping

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import coerce_float, coerce_int

DEFAULT_WATCHDOG_STALE_AFTER_SECONDS = 300.0


class WatchdogRecoveryCoordinator:
    def __init__(self, store: Any):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def scan_waiting_children_reconciliation(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        del now
        max_limit = max(int(limit or 100), 1)
        rows = self._scan_runtime_rows(
            table_name="task_request",
            statuses=("waiting_children",),
            predicate_sql="1 = 1",
            predicate_params={},
            limit=max_limit,
            order_by_sql="updated_at ASC, created_at ASC",
        )
        candidates: list[dict[str, Any]] = []
        with self._engine.connect() as connection:
            for row in rows:
                request = self._request_from_row(row)
                counts = self._aggregate_runtime_request_children(connection, request_id=request.request_id)
                if (
                    counts["total_count"] <= 0
                    or counts["active_count"] > 0
                    or counts.get("fallback_required_count", 0) > 0
                ):
                    continue
                candidates.append(
                    self._watchdog_payload(
                        target_table="task_request",
                        target_id=request.request_id,
                        request_id=request.request_id,
                        status=request.status,
                        record=request.to_dict(),
                        reason="Parent request is still waiting_children even though all child work is terminal.",
                        metadata=counts,
                    )
                )
        return candidates

    def scan_expired_outbox_sending(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        del now
        max_limit = max(int(limit or 100), 1)
        candidates_by_id: dict[str, dict[str, Any]] = {}

        for outbox in self.scan_expired_outbox_leases(limit=max_limit):
            candidates_by_id[outbox.outbox_id] = self._watchdog_payload(
                target_table="notification_outbox",
                target_id=outbox.outbox_id,
                request_id=outbox.ref_id if outbox.ref_type == "task_request" else "",
                status=outbox.status,
                record=outbox.to_dict(),
                reason="Outbox sending lease expired while dispatch was still running.",
            )
        for outbox in self.scan_outbox_execution_timeouts(limit=max_limit):
            candidates_by_id[outbox.outbox_id] = self._watchdog_payload(
                target_table="notification_outbox",
                target_id=outbox.outbox_id,
                request_id=outbox.ref_id if outbox.ref_type == "task_request" else "",
                status=outbox.status,
                record=outbox.to_dict(),
                reason="Outbox sending exceeded max_execution_seconds.",
            )
        for outbox in self.scan_stale_outbox_items(
            stale_after_seconds=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
            statuses=("sending",),
            limit=max_limit,
        ):
            candidates_by_id.setdefault(
                outbox.outbox_id,
                self._watchdog_payload(
                    target_table="notification_outbox",
                    target_id=outbox.outbox_id,
                    request_id=outbox.ref_id if outbox.ref_type == "task_request" else "",
                    status=outbox.status,
                    record=outbox.to_dict(),
                    reason="Outbox sending heartbeat is alive but progress is stale.",
                ),
            )
        return list(candidates_by_id.values())[:max_limit]

    def apply_watchdog_action(self, *, action: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(action)
        action_type = str(normalized.get("action_type") or "").strip()
        target_table = str(normalized.get("target_table") or "").strip()
        target_id = str(normalized.get("target_id") or "").strip()
        target_status = str(normalized.get("target_status") or "").strip()
        next_status = str(normalized.get("next_status") or "").strip()
        error_type = str(normalized.get("error_type") or "").strip()
        error_code = str(normalized.get("error_code") or normalized.get("rule_code") or "").strip()
        reason = str(normalized.get("reason") or "").strip()
        action_metadata = normalized.get("metadata")
        if not isinstance(action_metadata, Mapping):
            action_metadata = {}
        observed_attempt_count = coerce_int(action_metadata.get("observed_attempt_count"))
        observed_retry_count = coerce_int(action_metadata.get("observed_retry_count"))
        observed_lease_until = coerce_float(action_metadata.get("observed_lease_until"))
        observed_started_at = coerce_float(action_metadata.get("observed_started_at"))
        observed_last_progress_at = coerce_float(action_metadata.get("observed_last_progress_at"))
        observed_max_execution_seconds = coerce_float(action_metadata.get("observed_max_execution_seconds"))
        observed_run_id = str(action_metadata.get("observed_run_id") or "").strip()
        observed_worker_id = str(action_metadata.get("observed_worker_id") or "").strip()
        observed_worker_pid = coerce_int(action_metadata.get("observed_worker_pid"))
        observed_heartbeat_at = coerce_float(action_metadata.get("observed_heartbeat_at"))
        guard_attempt_count = 1 if observed_attempt_count > 0 else 0
        guard_retry_count = 1 if "observed_retry_count" in action_metadata else 0
        guard_lease_until = 1 if observed_lease_until > 0 else 0
        guard_started_at = 1 if observed_started_at > 0 else 0
        guard_last_progress_at = 1 if observed_last_progress_at > 0 else 0
        guard_max_execution_seconds = 1 if observed_max_execution_seconds > 0 else 0
        guard_run_id = 1 if observed_run_id else 0
        guard_heartbeat_at = 1 if observed_heartbeat_at > 0 else 0
        dead_letter_reason = "watchdog_failed" if action_type == "fail" else ""
        now = time.time()

        if target_table == "task_request":
            if action_type == "repair":
                repaired = self.reconcile_request_waiting_children(request_id=target_id)
                applied = bool(repaired.get("transitioned"))
                return {
                    "target_table": target_table,
                    "target_id": target_id,
                    "action_type": action_type,
                    "status": str(repaired["request"].status),
                    "applied": applied,
                    "transitioned": applied,
                }
            status = next_status or ("failed" if action_type == "fail" else "pending")
            with self._engine.begin() as connection:
                result = connection.execute(
                    self._text(
                        """
                        UPDATE task_request
                        SET status = CASE
                                WHEN :action_type = 'retry' AND current_stage = 'ready_for_summary'
                                    THEN 'ready_for_summary'
                                ELSE :status
                            END,
                            current_stage = CASE
                                WHEN :action_type = 'retry' AND current_stage <> 'ready_for_summary'
                                    THEN ''
                                ELSE current_stage
                            END,
                            progress_stage = CASE
                                WHEN :action_type = 'retry' AND current_stage = 'ready_for_summary'
                                    THEN 'ready_for_summary'
                                ELSE :progress_stage
                            END,
                            stage_cursor_json = CASE
                                WHEN :action_type = 'retry' AND current_stage <> 'ready_for_summary'
                                    THEN '{}'
                                ELSE stage_cursor_json
                            END,
                            error_text = :error_text,
                            error_type = :error_type,
                            error_code = :error_code,
                            dead_letter_reason = :dead_letter_reason,
                            worker_id = '',
                            lease_until = NULL,
                            heartbeat_at = NULL,
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at,
                            finished_at = CASE WHEN :action_type = 'fail' THEN :updated_at ELSE finished_at END
                        WHERE request_id = :request_id
                          AND (:target_status = '' OR status = :target_status)
                          AND (:guard_lease_until = 0 OR COALESCE(lease_until, 0) = :observed_lease_until)
                          AND (:guard_started_at = 0 OR COALESCE(started_at, 0) = :observed_started_at)
                          AND (:guard_last_progress_at = 0 OR COALESCE(last_progress_at, 0) = :observed_last_progress_at)
                          AND (:guard_max_execution_seconds = 0 OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds)
                        """
                    ),
                    {
                        "request_id": target_id,
                        "target_status": target_status,
                        "action_type": action_type,
                        "status": status,
                        "progress_stage": status,
                        "error_text": reason,
                        "error_type": error_type,
                        "error_code": error_code,
                        "dead_letter_reason": dead_letter_reason,
                        "last_progress_at": now,
                        "updated_at": now,
                        "guard_lease_until": guard_lease_until,
                        "observed_lease_until": observed_lease_until,
                        "guard_started_at": guard_started_at,
                        "observed_started_at": observed_started_at,
                        "guard_last_progress_at": guard_last_progress_at,
                        "observed_last_progress_at": observed_last_progress_at,
                        "guard_max_execution_seconds": guard_max_execution_seconds,
                        "observed_max_execution_seconds": observed_max_execution_seconds,
                    },
                )
                applied = int(result.rowcount or 0) > 0
            updated = self.load_task_request(request_id=target_id)
            return {"target_table": target_table, "target_id": target_id, "action_type": action_type, "applied": applied, "status": updated.status}

        if target_table == "api_worker_job":
            status = next_status or ("failed" if action_type == "fail" else "retry_wait")
            with self._engine.begin() as connection:
                result = connection.execute(
                    self._text(
                        """
                        UPDATE api_worker_job
                        SET status = :status,
                            stage = :stage,
                            progress_stage = :progress_stage,
                            worker_id = '',
                            worker_pid = 0,
                            lease_until = NULL,
                            available_at = :available_at,
                            error_text = :error_text,
                            error_type = :error_type,
                            error_code = :error_code,
                            dead_letter_reason = :dead_letter_reason,
                            heartbeat_at = :heartbeat_at,
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at,
                            finished_at = CASE WHEN :status = 'failed' THEN :updated_at ELSE finished_at END
                        WHERE job_id = :job_id
                          AND (:target_status = '' OR status = :target_status)
                          AND (:guard_run_id = 0 OR run_id = :observed_run_id)
                          AND (:guard_attempt_count = 0 OR COALESCE(attempt_count, 0) = :observed_attempt_count)
                          AND (:guard_lease_until = 0 OR COALESCE(lease_until, 0) = :observed_lease_until)
                          AND (:guard_started_at = 0 OR COALESCE(started_at, 0) = :observed_started_at)
                          AND (:guard_last_progress_at = 0 OR COALESCE(last_progress_at, 0) = :observed_last_progress_at)
                          AND (:guard_heartbeat_at = 0 OR COALESCE(heartbeat_at, 0) = :observed_heartbeat_at)
                          AND (:guard_max_execution_seconds = 0 OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds)
                        """
                    ),
                    {
                        "job_id": target_id,
                        "target_status": target_status,
                        "status": status,
                        "stage": status,
                        "progress_stage": status,
                        "available_at": now,
                        "error_text": reason,
                        "error_type": error_type,
                        "error_code": error_code,
                        "dead_letter_reason": dead_letter_reason,
                        "heartbeat_at": now,
                        "last_progress_at": now,
                        "updated_at": now,
                        "guard_run_id": guard_run_id,
                        "observed_run_id": observed_run_id,
                        "guard_attempt_count": guard_attempt_count,
                        "observed_attempt_count": observed_attempt_count,
                        "guard_lease_until": guard_lease_until,
                        "observed_lease_until": observed_lease_until,
                        "guard_started_at": guard_started_at,
                        "observed_started_at": observed_started_at,
                        "guard_last_progress_at": guard_last_progress_at,
                        "observed_last_progress_at": observed_last_progress_at,
                        "guard_heartbeat_at": guard_heartbeat_at,
                        "observed_heartbeat_at": observed_heartbeat_at,
                        "guard_max_execution_seconds": guard_max_execution_seconds,
                        "observed_max_execution_seconds": observed_max_execution_seconds,
                    },
                )
                applied = int(result.rowcount or 0) > 0
            updated = self.load_api_worker_job(job_id=target_id)
            if applied:
                request_id = str(updated.get("request_id") or "").strip()
                if request_id:
                    self.reconcile_request_waiting_children(request_id=request_id)
            return {
                "target_table": target_table,
                "target_id": target_id,
                "action_type": action_type,
                "applied": applied,
                "status": str(updated["status"]),
                "run_id": observed_run_id or str(updated.get("run_id") or ""),
                "worker_id": observed_worker_id or str(updated.get("worker_id") or ""),
                "worker_pid": observed_worker_pid or coerce_int(updated.get("worker_pid")),
            }

        if target_table == "task_execution":
            execution = self.load_task_execution(execution_id=target_id)
            status = next_status or ("failed" if action_type == "fail" else "retry_wait")
            with self._engine.begin() as connection:
                result = connection.execute(
                    self._text(
                        """
                        UPDATE task_execution
                        SET status = :status,
                            progress_stage = :progress_stage,
                            worker_id = '',
                            worker_pid = 0,
                            error_text = :error_text,
                            error_type = :error_type,
                            error_code = :error_code,
                            dead_letter_reason = :dead_letter_reason,
                            available_at = :available_at,
                            heartbeat_at = :heartbeat_at,
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at,
                            finished_at = CASE WHEN :status = 'failed' THEN :updated_at ELSE finished_at END
                        WHERE execution_id = :execution_id
                          AND (:target_status = '' OR status = :target_status)
                          AND (:guard_run_id = 0 OR run_id = :observed_run_id)
                          AND (:guard_attempt_count = 0 OR COALESCE(attempt_count, 0) = :observed_attempt_count)
                          AND (
                              :guard_lease_until = 0
                              OR EXISTS (
                                  SELECT 1
                                  FROM resource_lease lease
                                  WHERE lease.execution_id = :execution_id
                                    AND COALESCE(lease.lease_until, 0) = :observed_lease_until
                              )
                          )
                          AND (:guard_started_at = 0 OR COALESCE(started_at, 0) = :observed_started_at)
                          AND (:guard_last_progress_at = 0 OR COALESCE(last_progress_at, 0) = :observed_last_progress_at)
                          AND (:guard_heartbeat_at = 0 OR COALESCE(heartbeat_at, 0) = :observed_heartbeat_at)
                          AND (:guard_max_execution_seconds = 0 OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds)
                        """
                    ),
                    {
                        "execution_id": target_id,
                        "target_status": target_status,
                        "status": status,
                        "progress_stage": status,
                        "error_text": reason,
                        "error_type": error_type,
                        "error_code": error_code,
                        "dead_letter_reason": dead_letter_reason,
                        "available_at": now,
                        "heartbeat_at": now,
                        "last_progress_at": now,
                        "updated_at": now,
                        "guard_run_id": guard_run_id,
                        "observed_run_id": observed_run_id,
                        "guard_attempt_count": guard_attempt_count,
                        "observed_attempt_count": observed_attempt_count,
                        "guard_lease_until": guard_lease_until,
                        "observed_lease_until": observed_lease_until,
                        "guard_started_at": guard_started_at,
                        "observed_started_at": observed_started_at,
                        "guard_last_progress_at": guard_last_progress_at,
                        "observed_last_progress_at": observed_last_progress_at,
                        "guard_heartbeat_at": guard_heartbeat_at,
                        "observed_heartbeat_at": observed_heartbeat_at,
                        "guard_max_execution_seconds": guard_max_execution_seconds,
                        "observed_max_execution_seconds": observed_max_execution_seconds,
                    },
                )
                applied = int(result.rowcount or 0) > 0
                if applied:
                    connection.execute(
                        self._text("DELETE FROM resource_lease WHERE execution_id = :execution_id"),
                        {"execution_id": target_id},
                    )
                    self._refresh_request_child_counts(connection, request_id=execution.request_id, now=now)
            updated = self.load_task_execution(execution_id=target_id)
            if applied:
                self.reconcile_request_waiting_children(request_id=execution.request_id)
            return {
                "target_table": target_table,
                "target_id": target_id,
                "action_type": action_type,
                "applied": applied,
                "status": updated.status,
                "run_id": observed_run_id or updated.run_id,
                "worker_id": observed_worker_id or updated.worker_id,
                "worker_pid": observed_worker_pid or updated.worker_pid,
            }

        if target_table == "notification_outbox":
            status = next_status or ("failed" if action_type == "fail" else "retry_wait")
            with self._engine.begin() as connection:
                result = connection.execute(
                    self._text(
                        """
                        UPDATE notification_outbox
                        SET status = :status,
                            progress_stage = :progress_stage,
                            retry_count = CASE
                                WHEN :action_type = 'retry' THEN retry_count + 1
                                WHEN :action_type = 'fail'
                                     AND max_retry_count > 0
                                     AND retry_count < max_retry_count THEN retry_count + 1
                                ELSE retry_count
                            END,
                            worker_id = '',
                            lease_until = NULL,
                            heartbeat_at = NULL,
                            next_retry_at = :next_retry_at,
                            last_error_text = :last_error_text,
                            error_type = :error_type,
                            error_code = :error_code,
                            dead_letter_reason = :dead_letter_reason,
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at
                        WHERE outbox_id = :outbox_id
                          AND (:target_status = '' OR status = :target_status)
                          AND (:guard_retry_count = 0 OR COALESCE(retry_count, 0) = :observed_retry_count)
                          AND (:guard_lease_until = 0 OR COALESCE(lease_until, 0) = :observed_lease_until)
                          AND (:guard_last_progress_at = 0 OR COALESCE(last_progress_at, 0) = :observed_last_progress_at)
                          AND (:guard_max_execution_seconds = 0 OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds)
                        """
                    ),
                    {
                        "outbox_id": target_id,
                        "target_status": target_status,
                        "action_type": action_type,
                        "status": status,
                        "progress_stage": status,
                        "next_retry_at": now if status == "retry_wait" else None,
                        "last_error_text": reason,
                        "error_type": error_type,
                        "error_code": error_code,
                        "dead_letter_reason": dead_letter_reason,
                        "last_progress_at": now,
                        "updated_at": now,
                        "guard_retry_count": guard_retry_count,
                        "observed_retry_count": observed_retry_count,
                        "guard_lease_until": guard_lease_until,
                        "observed_lease_until": observed_lease_until,
                        "guard_last_progress_at": guard_last_progress_at,
                        "observed_last_progress_at": observed_last_progress_at,
                        "guard_max_execution_seconds": guard_max_execution_seconds,
                        "observed_max_execution_seconds": observed_max_execution_seconds,
                    },
                )
                applied = int(result.rowcount or 0) > 0
            updated = self.load_outbox(outbox_id=target_id)
            return {
                "target_table": target_table,
                "target_id": target_id,
                "action_type": action_type,
                "applied": applied,
                "status": updated.status,
                "retry_count": updated.retry_count,
            }

        raise ValueError(f"Unsupported watchdog target_table: {target_table}")
