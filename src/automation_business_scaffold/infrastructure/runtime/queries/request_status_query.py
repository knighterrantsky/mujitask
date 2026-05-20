from __future__ import annotations

from typing import Any

from automation_business_scaffold.models.artifact_object import ArtifactObjectRecord
from automation_business_scaffold.infrastructure.runtime.persistence_primitives import (
    coerce_non_negative_float as _coerce_non_negative_float,
    load_json_dict as _load_json_dict,
)
from automation_business_scaffold.infrastructure.runtime.runtime_records import (
    NotificationOutboxRecord,
    RuntimeTaskExecutionRecord,
    RuntimeTaskRequestRecord,
)


class RequestStatusQuery:
    def __init__(self, store: Any):
        self._store = store

    def load_task_request(self, *, request_id: str) -> RuntimeTaskRequestRecord:
        store = self._store
        with store._engine.connect() as connection:
            row = (
                connection.execute(
                    store._text("SELECT * FROM task_request WHERE request_id = :request_id LIMIT 1"),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Task request not found.")
            return store._request_from_row(row)

    def list_task_executions(self, *, request_id: str) -> list[RuntimeTaskExecutionRecord]:
        store = self._store
        with store._engine.connect() as connection:
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT *
                        FROM task_execution
                        WHERE request_id = :request_id
                        ORDER BY queue_seq ASC, created_at ASC
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
            return [store._execution_from_row(row) for row in rows]

    def list_task_execution_summaries_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
        store = self._store
        with store._engine.connect() as connection:
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT
                            execution_id, request_id, item_code, workflow_code, business_key,
                            dedupe_key, resource_code, status, result_status, queue_seq,
                            progress_stage, attempt_count, max_attempts, payload_json,
                            summary_json, error_text, error_type, error_code,
                            dead_letter_reason, run_id, created_at, updated_at, started_at,
                            finished_at, heartbeat_at, last_progress_at, progress_seq,
                            progress_message
                        FROM task_execution
                        WHERE request_id = :request_id
                        ORDER BY queue_seq ASC, created_at ASC
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
        return [
            {
                "execution_id": str(row["execution_id"]),
                "request_id": str(row["request_id"] or ""),
                "item_code": str(row["item_code"] or ""),
                "workflow_code": str(row["workflow_code"] or ""),
                "business_key": str(row["business_key"] or ""),
                "dedupe_key": str(row["dedupe_key"] or ""),
                "resource_code": str(row["resource_code"] or ""),
                "status": str(row["status"] or ""),
                "result_status": str(row["result_status"] or ""),
                "queue_seq": int(row["queue_seq"] or 0),
                "progress_stage": str(row["progress_stage"] or ""),
                "attempt_count": int(row["attempt_count"] or 0),
                "max_attempts": int(row["max_attempts"] or 0),
                "payload": _load_json_dict(row["payload_json"]),
                "summary": _load_json_dict(row["summary_json"]),
                "result": {},
                "error_text": str(row["error_text"] or ""),
                "error_type": str(row["error_type"] or ""),
                "error_code": str(row["error_code"] or ""),
                "dead_letter_reason": str(row["dead_letter_reason"] or ""),
                "run_id": str(row["run_id"] or ""),
                "created_at": _coerce_non_negative_float(row["created_at"]),
                "updated_at": _coerce_non_negative_float(row["updated_at"]),
                "started_at": _coerce_non_negative_float(row["started_at"]),
                "finished_at": _coerce_non_negative_float(row["finished_at"]),
                "heartbeat_at": _coerce_non_negative_float(row["heartbeat_at"]),
                "last_progress_at": _coerce_non_negative_float(row["last_progress_at"]),
                "progress_seq": int(row["progress_seq"] or 0),
                "progress_message": str(row["progress_message"] or ""),
            }
            for row in rows
        ]

    def summarize_task_executions_for_request(self, *, request_id: str) -> dict[str, Any]:
        store = self._store
        with store._engine.connect() as connection:
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT
                            status,
                            COALESCE(NULLIF(result_status, ''), status) AS effective_status,
                            COUNT(*) AS count
                        FROM task_execution
                        WHERE request_id = :request_id
                        GROUP BY status, COALESCE(NULLIF(result_status, ''), status)
                        """
                    ),
                    {"request_id": request_id},
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
            if status in {"pending", "running", "waiting"}:
                active_count += row_count
        total = sum(counts.values())
        return {
            "total": total,
            "counts": counts,
            "active_count": active_count,
            "terminal_count": max(total - active_count, 0),
            "success_count": counts.get("success", 0) + counts.get("partial_success", 0),
            "failed_count": counts.get("failed", 0) + counts.get("cancelled", 0),
            "skipped_count": counts.get("skipped", 0),
            "fallback_required_count": counts.get("fallback_required", 0),
        }

    def list_request_outbox(self, *, request_id: str) -> list[NotificationOutboxRecord]:
        store = self._store
        with store._engine.connect() as connection:
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT *
                        FROM notification_outbox
                        WHERE ref_type = 'task_request'
                          AND ref_id = :request_id
                        ORDER BY created_at ASC
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
            return [store._outbox_from_row(row) for row in rows]

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        store = self._store
        with store._engine.connect() as connection:
            rows = (
                connection.execute(
                    store._text(
                        """
                        SELECT *
                        FROM artifact_object
                        WHERE run_id = :run_id
                        ORDER BY created_at ASC, kind ASC
                        """
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .all()
            )
            return [store._artifact_from_row(row) for row in rows]
