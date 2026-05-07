from __future__ import annotations

from typing import Any

from automation_business_scaffold.models.artifact_object import ArtifactObjectRecord
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
