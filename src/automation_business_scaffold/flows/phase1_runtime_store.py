from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from automation_business_scaffold.models.artifact_object import ArtifactObjectRecord
from automation_business_scaffold.models.execution_control import ResourceLeaseRecord
from automation_business_scaffold.models.phase1_runtime import (
    NotificationOutboxRecord,
    Phase1TaskExecutionRecord,
    Phase1TaskRequestRecord,
)


ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_EXECUTION_STATUSES = {"success", "failed", "skipped", "cancelled"}
TERMINAL_REQUEST_STATUSES = {"success", "failed", "cancelled"}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _load_json_dict(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class Phase1RuntimeStore:
    def __init__(self, *, db_url: str = "", db_path: str | Path = ""):
        try:
            from sqlalchemy import create_engine, text
        except ModuleNotFoundError as exc:
            raise RuntimeError("Phase1RuntimeStore requires SQLAlchemy.") from exc

        resolved_db_url = str(db_url or "").strip()
        if not resolved_db_url:
            resolved_path = Path(db_path or "runtime/execution_control/control_plane.sqlite3").expanduser()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_db_url = f"sqlite:///{resolved_path.resolve()}"
        self._db_url = resolved_db_url
        self._text = text
        self._engine = create_engine(self._db_url, future=True, pool_pre_ping=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS task_request (
                request_id TEXT PRIMARY KEY,
                project_code TEXT NOT NULL DEFAULT 'automation-business-scaffold',
                skill_code TEXT NOT NULL DEFAULT '',
                task_name TEXT NOT NULL DEFAULT '',
                task_code TEXT NOT NULL DEFAULT '',
                resource_code TEXT NOT NULL DEFAULT '',
                trigger_mode TEXT NOT NULL DEFAULT 'manual',
                source_channel_code TEXT NOT NULL DEFAULT '',
                source_session_id TEXT NOT NULL DEFAULT '',
                reply_target TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                current_stage TEXT NOT NULL DEFAULT '',
                stage_cursor_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                child_total_count INTEGER NOT NULL DEFAULT 0,
                child_terminal_count INTEGER NOT NULL DEFAULT 0,
                child_success_count INTEGER NOT NULL DEFAULT 0,
                child_failed_count INTEGER NOT NULL DEFAULT 0,
                child_skipped_count INTEGER NOT NULL DEFAULT 0,
                requested_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS task_execution (
                execution_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                task_name TEXT NOT NULL DEFAULT '',
                item_code TEXT NOT NULL DEFAULT '',
                workflow_code TEXT NOT NULL DEFAULT '',
                business_key TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                resource_code TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                queue_seq INTEGER NOT NULL,
                available_at REAL NOT NULL,
                worker_id TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                payload_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                heartbeat_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_request_status_created_at
                ON task_request(status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_request_task_code_created_at
                ON task_request(task_code, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_execution_request_created_at
                ON task_execution(request_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_execution_status_available_queue_seq
                ON task_execution(status, available_at, queue_seq)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_execution_resource_status_available_queue_seq
                ON task_execution(resource_code, status, available_at, queue_seq)
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_lease (
                resource_code TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                lease_until REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_lease_lease_until
                ON resource_lease(lease_until)
            """,
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
                outbox_id TEXT PRIMARY KEY,
                channel_code TEXT NOT NULL,
                event_type TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                reply_target TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retry_count INTEGER NOT NULL DEFAULT 10,
                next_retry_at REAL,
                last_error_text TEXT NOT NULL DEFAULT '',
                sent_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_outbox_dedupe_key
                ON notification_outbox(dedupe_key)
                WHERE dedupe_key <> ''
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_next_retry_at
                ON notification_outbox(status, next_retry_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_notification_outbox_ref_type_ref_id
                ON notification_outbox(ref_type, ref_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS artifact_object (
                artifact_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL DEFAULT '',
                execution_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                bucket TEXT NOT NULL,
                object_key TEXT NOT NULL,
                etag TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_artifact_object_run_id
                ON artifact_object(run_id)
            """,
        ]
        with self._engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    def _request_from_row(self, row: Mapping[str, Any]) -> Phase1TaskRequestRecord:
        return Phase1TaskRequestRecord(
            request_id=str(row["request_id"]),
            project_code=str(row["project_code"]),
            task_code=str(row["task_code"] or row["task_name"] or ""),
            status=str(row["status"]),
            payload=_load_json_dict(row["payload_json"]),
            current_stage=str(row["current_stage"] or ""),
            trigger_mode=str(row["trigger_mode"] or "manual"),
            source_channel_code=str(row["source_channel_code"] or ""),
            source_session_id=str(row["source_session_id"] or ""),
            reply_target=str(row["reply_target"] or ""),
            requested_by=str(row["requested_by"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            summary=_load_json_dict(row["summary_json"]),
            result=_load_json_dict(row["result_json"]),
            stage_cursor=_load_json_dict(row["stage_cursor_json"]),
            error_text=str(row["error_text"] or ""),
            child_total_count=int(row["child_total_count"] or 0),
            child_terminal_count=int(row["child_terminal_count"] or 0),
            child_success_count=int(row["child_success_count"] or 0),
            child_failed_count=int(row["child_failed_count"] or 0),
            child_skipped_count=int(row["child_skipped_count"] or 0),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
            started_at=_coerce_float(row["started_at"]),
            finished_at=_coerce_float(row["finished_at"]),
        )

    def _execution_from_row(self, row: Mapping[str, Any]) -> Phase1TaskExecutionRecord:
        return Phase1TaskExecutionRecord(
            execution_id=str(row["execution_id"]),
            request_id=str(row["request_id"]),
            item_code=str(row["item_code"] or row["task_name"] or ""),
            workflow_code=str(row["workflow_code"] or ""),
            business_key=str(row["business_key"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            resource_code=str(row["resource_code"] or ""),
            status=str(row["status"]),
            queue_seq=int(row["queue_seq"]),
            available_at=_coerce_float(row["available_at"]),
            worker_id=str(row["worker_id"] or ""),
            attempt_count=int(row["attempt_count"] or 0),
            max_attempts=int(row["max_attempts"] or 0),
            payload=_load_json_dict(row["payload_json"]),
            summary=_load_json_dict(row["summary_json"]),
            result=_load_json_dict(row["result_json"]),
            error_text=str(row["error_text"] or ""),
            run_id=str(row["run_id"] or ""),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
            started_at=_coerce_float(row["started_at"]),
            finished_at=_coerce_float(row["finished_at"]),
            heartbeat_at=_coerce_float(row["heartbeat_at"]),
        )

    def _lease_from_row(self, row: Mapping[str, Any] | None) -> ResourceLeaseRecord | None:
        if row is None:
            return None
        return ResourceLeaseRecord(
            resource_code=str(row["resource_code"]),
            execution_id=str(row["execution_id"]),
            status=str(row["status"]),
            lease_until=_coerce_float(row["lease_until"]),
            heartbeat_at=_coerce_float(row["heartbeat_at"]),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
        )

    def _outbox_from_row(self, row: Mapping[str, Any]) -> NotificationOutboxRecord:
        return NotificationOutboxRecord(
            outbox_id=str(row["outbox_id"]),
            channel_code=str(row["channel_code"]),
            event_type=str(row["event_type"]),
            ref_type=str(row["ref_type"]),
            ref_id=str(row["ref_id"]),
            status=str(row["status"]),
            payload=_load_json_dict(row["payload_json"]),
            reply_target=str(row["reply_target"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            retry_count=int(row["retry_count"] or 0),
            max_retry_count=int(row["max_retry_count"] or 0),
            next_retry_at=_coerce_float(row["next_retry_at"]),
            last_error_text=str(row["last_error_text"] or ""),
            sent_at=_coerce_float(row["sent_at"]),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
        )

    def _artifact_from_row(self, row: Mapping[str, Any]) -> ArtifactObjectRecord:
        return ArtifactObjectRecord(
            artifact_id=str(row["artifact_id"]),
            request_id=str(row["request_id"] or ""),
            execution_id=str(row["execution_id"] or ""),
            run_id=str(row["run_id"]),
            step_id=str(row["step_id"]),
            kind=str(row["kind"]),
            bucket=str(row["bucket"]),
            object_key=str(row["object_key"]),
            etag=str(row["etag"]),
            size=int(row["size"] or 0),
            content_type=str(row["content_type"] or ""),
            source_path=str(row["source_path"] or ""),
            metadata=_load_json_dict(row["metadata_json"]),
            created_at=_coerce_float(row["created_at"]),
        )

    def submit_task_request(
        self,
        *,
        project_code: str,
        task_code: str,
        payload: dict[str, Any],
        requested_by: str,
        trigger_mode: str = "manual",
        source_channel_code: str = "",
        source_session_id: str = "",
        reply_target: str = "",
        idempotency_key: str = "",
    ) -> Phase1TaskRequestRecord:
        request_id = uuid.uuid4().hex
        now = time.time()
        with self._engine.begin() as connection:
            if idempotency_key:
                existing = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM task_request
                            WHERE project_code = :project_code
                              AND task_code = :task_code
                              AND idempotency_key = :idempotency_key
                            ORDER BY created_at DESC
                            LIMIT 1
                            """
                        ),
                        {
                            "project_code": project_code,
                            "task_code": task_code,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    return self._request_from_row(existing)
            connection.execute(
                self._text(
                    """
                    INSERT INTO task_request (
                        request_id, project_code, skill_code, task_name, task_code, resource_code,
                        trigger_mode, source_channel_code, source_session_id, reply_target,
                        payload_json, idempotency_key, status, current_stage, stage_cursor_json,
                        summary_json, result_json, error_text,
                        child_total_count, child_terminal_count, child_success_count,
                        child_failed_count, child_skipped_count,
                        requested_by, created_at, updated_at, started_at, finished_at
                    ) VALUES (
                        :request_id, :project_code, '', :task_name, :task_code, '',
                        :trigger_mode, :source_channel_code, :source_session_id, :reply_target,
                        :payload_json, :idempotency_key, 'pending', '', '{}',
                        '{}', '{}', '',
                        0, 0, 0, 0, 0,
                        :requested_by, :created_at, :updated_at, NULL, NULL
                    )
                    """
                ),
                {
                    "request_id": request_id,
                    "project_code": project_code,
                    "task_name": task_code,
                    "task_code": task_code,
                    "trigger_mode": trigger_mode,
                    "source_channel_code": source_channel_code,
                    "source_session_id": source_session_id,
                    "reply_target": reply_target,
                    "payload_json": _json_dumps(payload),
                    "idempotency_key": idempotency_key,
                    "requested_by": requested_by,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return self.load_task_request(request_id=request_id)

    def load_task_request(self, *, request_id: str) -> Phase1TaskRequestRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text("SELECT * FROM task_request WHERE request_id = :request_id LIMIT 1"),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("Task request not found.")
            return self._request_from_row(row)

    def list_task_executions(self, *, request_id: str) -> list[Phase1TaskExecutionRecord]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
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
            return [self._execution_from_row(row) for row in rows]

    def list_request_outbox(self, *, request_id: str) -> list[NotificationOutboxRecord]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
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
            return [self._outbox_from_row(row) for row in rows]

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
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
            return [self._artifact_from_row(row) for row in rows]

    def claim_next_task_request(self, *, worker_id: str) -> Phase1TaskRequestRecord | None:
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM task_request
                        WHERE status IN ('pending', 'ready_for_summary')
                        ORDER BY created_at ASC
                        LIMIT 1
                        """
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            now = time.time()
            request = self._request_from_row(row)
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'running',
                        updated_at = :updated_at,
                        started_at = CASE WHEN started_at IS NULL THEN :updated_at ELSE started_at END
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request.request_id, "updated_at": now},
            )
            return self.load_task_request(request_id=request.request_id)

    def update_task_request(
        self,
        *,
        request_id: str,
        status: str | None = None,
        current_stage: str | None = None,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        stage_cursor: dict[str, Any] | None = None,
        error_text: str | None = None,
        child_total_count: int | None = None,
        child_terminal_count: int | None = None,
        child_success_count: int | None = None,
        child_failed_count: int | None = None,
        child_skipped_count: int | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> Phase1TaskRequestRecord:
        assignments = ["updated_at = :updated_at"]
        values: dict[str, Any] = {"request_id": request_id, "updated_at": time.time()}
        if status is not None:
            assignments.append("status = :status")
            values["status"] = status
        if current_stage is not None:
            assignments.append("current_stage = :current_stage")
            values["current_stage"] = current_stage
        if summary is not None:
            assignments.append("summary_json = :summary_json")
            values["summary_json"] = _json_dumps(summary)
        if result is not None:
            assignments.append("result_json = :result_json")
            values["result_json"] = _json_dumps(result)
        if stage_cursor is not None:
            assignments.append("stage_cursor_json = :stage_cursor_json")
            values["stage_cursor_json"] = _json_dumps(stage_cursor)
        if error_text is not None:
            assignments.append("error_text = :error_text")
            values["error_text"] = error_text
        if child_total_count is not None:
            assignments.append("child_total_count = :child_total_count")
            values["child_total_count"] = child_total_count
        if child_terminal_count is not None:
            assignments.append("child_terminal_count = :child_terminal_count")
            values["child_terminal_count"] = child_terminal_count
        if child_success_count is not None:
            assignments.append("child_success_count = :child_success_count")
            values["child_success_count"] = child_success_count
        if child_failed_count is not None:
            assignments.append("child_failed_count = :child_failed_count")
            values["child_failed_count"] = child_failed_count
        if child_skipped_count is not None:
            assignments.append("child_skipped_count = :child_skipped_count")
            values["child_skipped_count"] = child_skipped_count
        if started_at is not None:
            assignments.append("started_at = :started_at")
            values["started_at"] = started_at
        if finished_at is not None:
            assignments.append("finished_at = :finished_at")
            values["finished_at"] = finished_at
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    f"""
                    UPDATE task_request
                    SET {", ".join(assignments)}
                    WHERE request_id = :request_id
                    """
                ),
                values,
            )
        return self.load_task_request(request_id=request_id)

    def enqueue_task_executions(
        self,
        *,
        request_id: str,
        item_code: str,
        workflow_code: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        created_records: list[Phase1TaskExecutionRecord] = []
        skipped_records: list[dict[str, Any]] = []
        with self._engine.begin() as connection:
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
                                  AND status IN ('pending', 'running', 'retry_wait')
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
                connection.execute(
                    self._text(
                        """
                        INSERT INTO task_execution (
                            execution_id, request_id, task_name, item_code, workflow_code,
                            business_key, dedupe_key, resource_code, status, queue_seq,
                            available_at, worker_id, attempt_count, max_attempts,
                            payload_json, summary_json, result_json, error_text, run_id,
                            created_at, updated_at, started_at, finished_at, heartbeat_at
                        ) VALUES (
                            :execution_id, :request_id, :task_name, :item_code, :workflow_code,
                            :business_key, :dedupe_key, :resource_code, 'pending', :queue_seq,
                            :available_at, '', 0, :max_attempts,
                            :payload_json, '{}', '{}', '', '',
                            :created_at, :updated_at, NULL, NULL, NULL
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
                        "payload_json": _json_dumps(payload),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                created_records.append(
                    Phase1TaskExecutionRecord(
                        execution_id=execution_id,
                        request_id=request_id,
                        item_code=item_code,
                        workflow_code=workflow_code,
                        business_key=business_key,
                        dedupe_key=dedupe_key,
                        resource_code=resource_code,
                        status="pending",
                        queue_seq=next_queue_seq,
                        available_at=now,
                        max_attempts=int(item.get("max_attempts", 3) or 3),
                        payload=payload,
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

    def _requeue_expired_leases(self, connection: Any, *, now: float) -> None:
        expired_rows = (
            connection.execute(
                self._text(
                    """
                    SELECT resource_code, execution_id, request_id
                    FROM resource_lease
                    WHERE lease_until <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in expired_rows:
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'pending',
                        updated_at = :updated_at,
                        heartbeat_at = NULL,
                        worker_id = ''
                    WHERE execution_id = :execution_id
                      AND status = 'running'
                    """
                ),
                {
                    "updated_at": now,
                    "execution_id": row["execution_id"],
                },
            )
            connection.execute(
                self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": row["resource_code"]},
            )
            self._refresh_request_child_counts(
                connection,
                request_id=str(row["request_id"]),
                now=now,
            )

    def claim_next_browser_execution(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> Phase1TaskExecutionRecord | None:
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_leases(connection, now=now)
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM task_execution
                        WHERE status IN ('pending', 'retry_wait')
                          AND available_at <= :available_at
                        ORDER BY queue_seq ASC, created_at ASC
                        """
                    ),
                    {"available_at": now},
                )
                .mappings()
                .all()
            )
            for row in rows:
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
                run_id = str(row["run_id"] or f"managed-{row['execution_id']}")
                connection.execute(
                    self._text(
                        """
                        UPDATE task_execution
                        SET status = 'running',
                            worker_id = :worker_id,
                            attempt_count = COALESCE(attempt_count, 0) + 1,
                            run_id = CASE WHEN run_id = '' THEN :run_id ELSE run_id END,
                            updated_at = :updated_at,
                            started_at = CASE WHEN started_at IS NULL THEN :updated_at ELSE started_at END,
                            heartbeat_at = :heartbeat_at
                        WHERE execution_id = :execution_id
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "run_id": run_id,
                        "updated_at": now,
                        "heartbeat_at": now,
                        "execution_id": row["execution_id"],
                    },
                )
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

    def heartbeat_browser_execution(self, *, execution_id: str, lease_seconds: float) -> None:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET heartbeat_at = :heartbeat_at,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                      AND status = 'running'
                    """
                ),
                {
                    "heartbeat_at": now,
                    "updated_at": now,
                    "execution_id": execution_id,
                },
            )
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

    def _finalize_browser_execution(
        self,
        *,
        execution_id: str,
        status: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        error_text: str,
    ) -> Phase1TaskExecutionRecord:
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
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = :status,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        updated_at = :updated_at,
                        finished_at = :finished_at,
                        heartbeat_at = :heartbeat_at
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "status": status,
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "error_text": error_text,
                    "updated_at": now,
                    "finished_at": now,
                    "heartbeat_at": now,
                    "execution_id": execution_id,
                },
            )
            if execution_row["resource_code"]:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": execution_row["resource_code"]},
                )
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
    ) -> Phase1TaskExecutionRecord:
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
    ) -> Phase1TaskExecutionRecord:
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
    ) -> Phase1TaskExecutionRecord:
        with self._engine.begin() as connection:
            now = time.time()
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
            status = "retry_wait"
            available_at = now + max(retry_delay_seconds, 0.1)
            if int(row["attempt_count"] or 0) >= int(row["max_attempts"] or 1):
                status = "failed"
                available_at = now
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = :status,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        available_at = :available_at,
                        updated_at = :updated_at,
                        finished_at = CASE WHEN :status = 'failed' THEN :updated_at ELSE finished_at END,
                        heartbeat_at = :heartbeat_at
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "status": status,
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "available_at": available_at,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "execution_id": execution_id,
                },
            )
            resource_code = str(row["resource_code"] or "")
            if resource_code:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": resource_code},
                )
            self._refresh_request_child_counts(
                connection,
                request_id=str(row["request_id"]),
                now=now,
            )
        return self.load_task_execution(execution_id=execution_id)

    def load_task_execution(self, *, execution_id: str) -> Phase1TaskExecutionRecord:
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

    def _refresh_request_child_counts(self, connection: Any, *, request_id: str, now: float) -> None:
        stats = (
            connection.execute(
                self._text(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN status IN ('success', 'failed', 'skipped', 'cancelled') THEN 1 ELSE 0 END) AS terminal_count,
                        SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                        SUM(CASE WHEN status IN ('pending', 'running', 'retry_wait') THEN 1 ELSE 0 END) AS active_count
                    FROM task_execution
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id},
            )
            .mappings()
            .first()
        )
        if stats is None:
            stats = {
                "total_count": 0,
                "terminal_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "active_count": 0,
            }
        connection.execute(
            self._text(
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
        if request_row is None:
            return
        if (
            str(request_row["status"]) == "waiting_children"
            and int(stats["total_count"] or 0) > 0
            and int(stats["active_count"] or 0) == 0
        ):
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'ready_for_summary',
                        current_stage = 'ready_for_summary',
                        updated_at = :updated_at
                    WHERE request_id = :request_id
                    """
                ),
                {"request_id": request_id, "updated_at": now},
            )

    def create_notification_outbox(
        self,
        *,
        channel_code: str,
        event_type: str,
        ref_id: str,
        reply_target: str,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> NotificationOutboxRecord:
        outbox_id = uuid.uuid4().hex
        now = time.time()
        with self._engine.begin() as connection:
            if dedupe_key:
                existing = (
                    connection.execute(
                        self._text(
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
                    return self._outbox_from_row(existing)
            connection.execute(
                self._text(
                    """
                    INSERT INTO notification_outbox (
                        outbox_id, channel_code, event_type, ref_type, ref_id,
                        reply_target, dedupe_key, payload_json, status, retry_count,
                        max_retry_count, next_retry_at, last_error_text, sent_at,
                        created_at, updated_at
                    ) VALUES (
                        :outbox_id, :channel_code, :event_type, 'task_request', :ref_id,
                        :reply_target, :dedupe_key, :payload_json, 'pending', 0,
                        10, NULL, '', NULL,
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
                    "payload_json": _json_dumps(payload),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return self.load_outbox(outbox_id=outbox_id)

    def load_outbox(self, *, outbox_id: str) -> NotificationOutboxRecord:
        with self._engine.connect() as connection:
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
            return self._outbox_from_row(row)

    def claim_next_outbox(self) -> NotificationOutboxRecord | None:
        with self._engine.begin() as connection:
            now = time.time()
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
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {"outbox_id": row["outbox_id"], "updated_at": now},
            )
        return self.load_outbox(outbox_id=str(row["outbox_id"]))

    def mark_outbox_sent(self, *, outbox_id: str) -> NotificationOutboxRecord:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = 'sent',
                        sent_at = :sent_at,
                        updated_at = :updated_at,
                        last_error_text = ''
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {"outbox_id": outbox_id, "sent_at": now, "updated_at": now},
            )
        return self.load_outbox(outbox_id=outbox_id)

    def mark_outbox_retry_or_failed(
        self,
        *,
        outbox_id: str,
        error_text: str,
        retry_delay_seconds: float = 30.0,
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
            status = "retry_wait" if retry_count < max_retry_count else "failed"
            next_retry_at = now + max(retry_delay_seconds, 0.1) if status == "retry_wait" else None
            connection.execute(
                self._text(
                    """
                    UPDATE notification_outbox
                    SET status = :status,
                        retry_count = :retry_count,
                        next_retry_at = :next_retry_at,
                        last_error_text = :last_error_text,
                        updated_at = :updated_at
                    WHERE outbox_id = :outbox_id
                    """
                ),
                {
                    "outbox_id": outbox_id,
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at,
                    "last_error_text": error_text,
                    "updated_at": now,
                },
            )
        return self.load_outbox(outbox_id=outbox_id)

    def replace_artifacts(self, *, run_id: str, records: list[ArtifactObjectRecord]) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                self._text("DELETE FROM artifact_object WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            for record in records:
                connection.execute(
                    self._text(
                        """
                        INSERT INTO artifact_object (
                            artifact_id, request_id, execution_id, run_id, step_id, kind,
                            bucket, object_key, etag, size, content_type, source_path,
                            metadata_json, created_at
                        ) VALUES (
                            :artifact_id, :request_id, :execution_id, :run_id, :step_id, :kind,
                            :bucket, :object_key, :etag, :size, :content_type, :source_path,
                            :metadata_json, :created_at
                        )
                        """
                    ),
                    {
                        "artifact_id": record.artifact_id,
                        "request_id": record.request_id,
                        "execution_id": record.execution_id,
                        "run_id": record.run_id,
                        "step_id": record.step_id,
                        "kind": record.kind,
                        "bucket": record.bucket,
                        "object_key": record.object_key,
                        "etag": record.etag,
                        "size": record.size,
                        "content_type": record.content_type,
                        "source_path": record.source_path,
                        "metadata_json": _json_dumps(record.metadata),
                        "created_at": record.created_at,
                    },
                )
