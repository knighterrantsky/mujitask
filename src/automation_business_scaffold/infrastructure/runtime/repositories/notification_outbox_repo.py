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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

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

    def _requeue_expired_outbox_claims(self, connection: Any, *, now: float) -> None:
        rows = (
            connection.execute(
                self._text(
                    """
                    SELECT outbox_id, retry_count, max_retry_count
                    FROM notification_outbox
                    WHERE status = 'sending'
                      AND COALESCE(lease_until, 0) <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in rows:
            retry_count = int(row["retry_count"] or 0) + 1
            max_retry_count = int(row["max_retry_count"] or 0)
            status = "retry_wait" if retry_count < max_retry_count else "failed"
            next_retry_at = now if status == "retry_wait" else None
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = :status,
                        progress_stage = CASE
                            WHEN :status = 'failed' THEN 'failed'
                            ELSE 'retry_wait'
                        END,
                        retry_count = :retry_count,
                        next_retry_at = :next_retry_at,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = NULL,
                        last_error_text = :last_error_text,
                        error_type = 'timeout',
                        error_code = 'outbox_lease_expired',
                        dead_letter_reason = CASE
                            WHEN :status = 'failed' THEN 'lease_expired'
                            ELSE dead_letter_reason
                        END,
                        last_progress_at = :last_progress_at,
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {
                    "outbox_id": row["outbox_id"],
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at,
                    "last_error_text": "Outbox sending lease expired and was reclaimed.",
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )

    def claim_next_outbox(self, *, worker_id: str, lease_seconds: float) -> NotificationOutboxRecord | None:
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_outbox_claims(connection, now=now)
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM notification_outbox
                        WHERE status = 'pending'
                           OR (status = 'retry_wait' AND COALESCE(next_retry_at, 0) <= :now)
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"now": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = 'sending',
                        progress_stage = 'sending',
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {
                    "outbox_id": row["outbox_id"],
                    "worker_id": worker_id,
                    "lease_until": now + lease_seconds,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
            updated_row = (
                connection.execute(
                    self._text("SELECT * FROM notification_outbox WHERE outbox_id = :outbox_id LIMIT 1"),
                    {"outbox_id": row["outbox_id"]},
                )
                .mappings()
                .first()
            )
            if updated_row is None:
                return None
            return self._outbox_from_row(updated_row)

    def heartbeat_outbox(self, *, outbox_id: str, lease_seconds: float) -> None:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET heartbeat_at = :heartbeat_at,
                        lease_until = :lease_until,
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                      AND status = 'sending'
                    """
                ),
                {
                    "outbox_id": outbox_id,
                    "heartbeat_at": now,
                    "lease_until": now + lease_seconds,
                    "updated_at": now,
                },
            )

    def update_outbox_progress(
        self,
        *,
        outbox_id: str,
        progress_stage: str,
        lease_seconds: float | None = None,
    ) -> NotificationOutboxRecord:
        now = time.time()
        with self._engine.begin() as connection:
            assignments = [
                "progress_stage = :progress_stage",
                "last_progress_at = :last_progress_at",
                "updated_at = :updated_at",
            ]
            values: dict[str, Any] = {
                "outbox_id": outbox_id,
                "progress_stage": progress_stage,
                "last_progress_at": now,
                "updated_at": now,
            }
            if lease_seconds is not None:
                assignments.extend(["heartbeat_at = :heartbeat_at", "lease_until = :lease_until"])
                values["heartbeat_at"] = now
                values["lease_until"] = now + max(lease_seconds, 0.1)
            connection.execute(
                self._text(
                    f"""
                    UPDATE notification_outbox
                    SET {", ".join(assignments)}
                    WHERE outbox_id = :outbox_id
                    """
                ),
                values,
            )
        return self.load_outbox(outbox_id=outbox_id)

    def mark_outbox_sent(self, *, outbox_id: str) -> NotificationOutboxRecord:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = 'sent',
                        progress_stage = 'sent',
                        sent_at = :sent_at,
                        updated_at = :updated_at,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = NULL,
                        last_error_text = '',
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        last_progress_at = :last_progress_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {"outbox_id": outbox_id, "sent_at": now, "updated_at": now, "last_progress_at": now},
            )
        return self.load_outbox(outbox_id=outbox_id)

    def mark_outbox_retry_or_failed(
        self,
        *,
        outbox_id: str,
        error_text: str,
        retry_delay_seconds: float = 30.0,
        retryable: bool = True,
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> NotificationOutboxRecord:
        with self._engine.begin() as connection:
            now = time.time()
            row = (
                connection.execute(
                    self._text("SELECT * FROM notification_outbox WHERE outbox_id = :outbox_id LIMIT 1"),
                    {"outbox_id": outbox_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Outbox record not found.")
            retry_count = int(row["retry_count"] or 0) + 1
            max_retry_count = int(row["max_retry_count"] or 0)
            status = "retry_wait" if retryable and retry_count < max_retry_count else "failed"
            next_retry_at = now + max(retry_delay_seconds, 0.1) if status == "retry_wait" else None
            resolved_dead_letter_reason = dead_letter_reason
            if status == "failed" and not resolved_dead_letter_reason:
                resolved_dead_letter_reason = "max_retry_exhausted" if retryable else "terminal_dispatch_failure"
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = :status,
                        progress_stage = :progress_stage,
                        retry_count = :retry_count,
                        next_retry_at = :next_retry_at,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = NULL,
                        last_error_text = :last_error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        last_progress_at = :last_progress_at,
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {
                    "outbox_id": outbox_id,
                    "status": status,
                    "progress_stage": status,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at,
                    "last_error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "dead_letter_reason": resolved_dead_letter_reason,
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
        return self.load_outbox(outbox_id=outbox_id)

    def reclaim_expired_outbox_claims(self, *, limit: int = 100) -> list[NotificationOutboxRecord]:
        candidates = self.scan_expired_outbox_leases(limit=limit)
        if not candidates:
            return []
        now = time.time()
        with self._engine.begin() as connection:
            for candidate in candidates:
                row = (
                    connection.execute(
                        self._text(
                            """
                            SELECT retry_count, max_retry_count
                            FROM notification_outbox
                            WHERE outbox_id = :outbox_id
                              AND status = 'sending'
                            LIMIT 1
                            """
                        ),
                        {"outbox_id": candidate.outbox_id},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                retry_count = int(row["retry_count"] or 0) + 1
                max_retry_count = int(row["max_retry_count"] or 0)
                status = "retry_wait" if retry_count < max_retry_count else "failed"
                next_retry_at = now if status == "retry_wait" else None
                connection.execute(
                    self._text(
                        """
                        UPDATE notification_outbox
                        SET status = :status,
                            progress_stage = :progress_stage,
                            retry_count = :retry_count,
                            next_retry_at = :next_retry_at,
                            worker_id = '',
                            lease_until = NULL,
                            heartbeat_at = NULL,
                            last_error_text = :last_error_text,
                            error_type = 'timeout',
                            error_code = 'outbox_lease_expired',
                            dead_letter_reason = :dead_letter_reason,
                            last_progress_at = :last_progress_at,
                            updated_at = :updated_at
                        WHERE outbox_id = :outbox_id
                        """
                    ),
                    {
                        "outbox_id": candidate.outbox_id,
                        "status": status,
                        "progress_stage": "failed" if status == "failed" else "retry_wait",
                        "retry_count": retry_count,
                        "next_retry_at": next_retry_at,
                        "last_error_text": "Outbox sending lease expired and was reclaimed.",
                        "dead_letter_reason": "lease_expired" if status == "failed" else "",
                        "last_progress_at": now,
                        "updated_at": now,
                    },
                )
        return [self.load_outbox(outbox_id=item.outbox_id) for item in candidates]
