from __future__ import annotations

import time
import uuid
from typing import Any

from automation_business_scaffold.infrastructure.runtime.persistence_primitives import (
    coerce_non_negative_float,
    json_dumps,
)
from automation_business_scaffold.infrastructure.runtime.runtime_records import NotificationOutboxRecord


class NotificationOutboxRepository:
    def __init__(self, store: Any):
        self._store = store

    def create(
        self,
        *,
        channel_code: str,
        event_type: str,
        ref_id: str,
        reply_target: str,
        payload: dict[str, Any],
        dedupe_key: str,
        max_execution_seconds: float = 0.0,
    ) -> NotificationOutboxRecord:
        store = self._store
        outbox_id = uuid.uuid4().hex
        now = time.time()
        with store._engine.begin() as connection:
            if dedupe_key:
                existing = (
                    connection.execute(
                        store._text(
                            """
                            SELECT *
                            FROM notification_outbox
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
                    return store._outbox_from_row(existing)
            connection.execute(
                store._text(
                    """
                    INSERT INTO notification_outbox (
                        outbox_id, channel_code, event_type, ref_type, ref_id,
                        reply_target, dedupe_key, payload_json, status, progress_stage, retry_count,
                        max_retry_count, max_execution_seconds, next_retry_at, last_error_text,
                        error_type, error_code, dead_letter_reason, sent_at, last_progress_at,
                        created_at, updated_at
                    ) VALUES (
                        :outbox_id, :channel_code, :event_type, 'task_request', :ref_id,
                        :reply_target, :dedupe_key, :payload_json, 'pending', 'queued', 0,
                        10, :max_execution_seconds, NULL, '',
                        '', '', '', NULL, :last_progress_at,
                        :created_at, :updated_at
                    )
                    """
                ),
                {
                    "outbox_id": outbox_id,
                    "channel_code": channel_code,
                    "event_type": event_type,
                    "ref_id": ref_id,
                    "reply_target": reply_target,
                    "dedupe_key": dedupe_key,
                    "payload_json": json_dumps(payload),
                    "max_execution_seconds": coerce_non_negative_float(max_execution_seconds),
                    "last_progress_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return self.load(outbox_id=outbox_id)

    def load(self, *, outbox_id: str) -> NotificationOutboxRecord:
        store = self._store
        with store._engine.connect() as connection:
            row = (
                connection.execute(
                    store._text("SELECT * FROM notification_outbox WHERE outbox_id = :outbox_id LIMIT 1"),
                    {"outbox_id": outbox_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Outbox record not found.")
            return store._outbox_from_row(row)
