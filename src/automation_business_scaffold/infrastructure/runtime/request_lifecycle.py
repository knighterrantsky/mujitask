from __future__ import annotations

import time
from typing import Any


class RuntimeRequestLifecycle:
    def __init__(self, store: Any):
        self._store = store

    def aggregate_children(self, connection: Any, *, request_id: str) -> dict[str, int]:
        task_stats = (
            connection.execute(
                self._store._text(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN status IN ('finished', 'cancelled') THEN 1 ELSE 0 END) AS terminal_count,
                        SUM(CASE WHEN effective_status IN ('success', 'partial_success') THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN effective_status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN effective_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                        SUM(CASE WHEN effective_status = 'fallback_required' THEN 1 ELSE 0 END) AS fallback_required_count,
                        SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active_count
                    FROM (
                        SELECT
                            status,
                            COALESCE(
                                NULLIF(result_json, '')::jsonb #>> '{handler_result,status}',
                                NULLIF(result_status, ''),
                                status
                            ) AS effective_status
                        FROM task_execution
                        WHERE request_id = :request_id
                    ) child
                    """
                ),
                {"request_id": request_id},
            )
            .mappings()
            .first()
        ) or {}
        api_stats = (
            connection.execute(
                self._store._text(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN status IN ('finished', 'cancelled') THEN 1 ELSE 0 END) AS terminal_count,
                        SUM(CASE WHEN effective_status IN ('success', 'partial_success') THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN effective_status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN effective_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                        SUM(CASE WHEN effective_status = 'fallback_required' THEN 1 ELSE 0 END) AS fallback_required_count,
                        SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active_count
                    FROM (
                        SELECT
                            status,
                            COALESCE(
                                NULLIF(result_json, '')::jsonb #>> '{handler_result,status}',
                                NULLIF(result_status, ''),
                                status
                            ) AS effective_status
                        FROM api_worker_job
                        WHERE request_id = :request_id
                    ) child
                    """
                ),
                {"request_id": request_id},
            )
            .mappings()
            .first()
        ) or {}
        return {
            "total_count": int(task_stats.get("total_count") or 0) + int(api_stats.get("total_count") or 0),
            "terminal_count": int(task_stats.get("terminal_count") or 0) + int(api_stats.get("terminal_count") or 0),
            "success_count": int(task_stats.get("success_count") or 0) + int(api_stats.get("success_count") or 0),
            "failed_count": int(task_stats.get("failed_count") or 0) + int(api_stats.get("failed_count") or 0),
            "skipped_count": int(task_stats.get("skipped_count") or 0) + int(api_stats.get("skipped_count") or 0),
            "fallback_required_count": int(task_stats.get("fallback_required_count") or 0)
            + int(api_stats.get("fallback_required_count") or 0),
            "active_count": int(task_stats.get("active_count") or 0) + int(api_stats.get("active_count") or 0),
        }

    def reconcile_waiting_children(self, *, request_id: str) -> dict[str, Any]:
        now = time.time()
        store = self._store
        with store._engine.begin() as connection:
            request_row = (
                connection.execute(
                    store._text(
                        """
                        SELECT *
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
            if request_row is None:
                raise ValueError("Task request not found.")

            counts = self.aggregate_children(connection, request_id=request_id)
            connection.execute(
                store._text(
                    """
                    UPDATE task_request
                    SET child_total_count = :child_total_count,
                        child_terminal_count = :child_terminal_count,
                        child_success_count = :child_success_count,
                        child_failed_count = :child_failed_count,
                        child_skipped_count = :child_skipped_count,
                        updated_at = :updated_at
                    WHERE request_id = :request_id
                    """
                ),
                {
                    "request_id": request_id,
                    "child_total_count": counts["total_count"],
                    "child_terminal_count": counts["terminal_count"],
                    "child_success_count": counts["success_count"],
                    "child_failed_count": counts["failed_count"],
                    "child_skipped_count": counts["skipped_count"],
                    "updated_at": now,
                },
            )

            transitioned = False
            if (
                str(request_row["status"] or "") == "waiting"
                and counts["active_count"] == 0
                and counts["fallback_required_count"] == 0
            ):
                connection.execute(
                    store._text(
                        """
                        UPDATE task_request
                        SET status = 'pending',
                            result_status = '',
                            current_stage = 'ready_for_summary',
                            progress_stage = 'ready_for_summary',
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at
                        WHERE request_id = :request_id
                        """
                    ),
                    {
                        "request_id": request_id,
                        "last_progress_at": now,
                        "updated_at": now,
                    },
                )
                transitioned = True

            updated_row = (
                connection.execute(
                    store._text(
                        """
                        SELECT *
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

        if updated_row is None:
            raise ValueError("Task request not found after reconcile.")
        return {
            "request": store._request_from_row(updated_row),
            "transitioned": transitioned,
            "child_total_count": counts["total_count"],
            "child_terminal_count": counts["terminal_count"],
            "child_success_count": counts["success_count"],
            "child_failed_count": counts["failed_count"],
            "child_skipped_count": counts["skipped_count"],
            "fallback_required_count": counts["fallback_required_count"],
            "active_count": counts["active_count"],
        }

    def refresh_child_counts(self, connection: Any, *, request_id: str, now: float) -> None:
        stats = self._task_execution_stats(connection, request_id=request_id)
        connection.execute(
            self._store._text(
                """
                UPDATE task_request
                SET child_total_count = :child_total_count,
                    child_terminal_count = :child_terminal_count,
                    child_success_count = :child_success_count,
                    child_failed_count = :child_failed_count,
                    child_skipped_count = :child_skipped_count,
                    updated_at = :updated_at
                WHERE request_id = :request_id
                """
            ),
            {
                "request_id": request_id,
                "child_total_count": int(stats["total_count"] or 0),
                "child_terminal_count": int(stats["terminal_count"] or 0),
                "child_success_count": int(stats["success_count"] or 0),
                "child_failed_count": int(stats["failed_count"] or 0),
                "child_skipped_count": int(stats["skipped_count"] or 0),
                "updated_at": now,
            },
        )
        request_row = (
            connection.execute(
                self._store._text(
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
        if request_row is None:
            return
        if (
            str(request_row["status"]) == "waiting"
            and int(stats["total_count"] or 0) > 0
            and int(stats["active_count"] or 0) == 0
            and int(stats["fallback_required_count"] or 0) == 0
        ):
            connection.execute(
                self._store._text(
                    """
                    UPDATE task_request
                    SET status = 'pending',
                        result_status = '',
                        current_stage = 'ready_for_summary',
                        updated_at = :updated_at
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id, "updated_at": now},
            )

    def _task_execution_stats(self, connection: Any, *, request_id: str) -> dict[str, int]:
        stats = (
            connection.execute(
                self._store._text(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN status IN ('finished', 'cancelled') THEN 1 ELSE 0 END) AS terminal_count,
                        SUM(CASE WHEN effective_status IN ('success', 'partial_success') THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN effective_status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN effective_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                        SUM(CASE WHEN effective_status = 'fallback_required' THEN 1 ELSE 0 END) AS fallback_required_count,
                        SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active_count
                    FROM (
                        SELECT
                            status,
                            COALESCE(
                                NULLIF(result_json, '')::jsonb #>> '{handler_result,status}',
                                NULLIF(result_status, ''),
                                status
                            ) AS effective_status
                        FROM task_execution
                        WHERE request_id = :request_id
                    ) child
                    """
                ),
                {"request_id": request_id},
            )
            .mappings()
            .first()
        )
        if stats is None:
            return {
                "total_count": 0,
                "terminal_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "fallback_required_count": 0,
                "active_count": 0,
            }
        return dict(stats)
