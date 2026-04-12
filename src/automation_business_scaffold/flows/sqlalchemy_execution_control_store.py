from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping

from automation_business_scaffold.models import (
    ArtifactObjectRecord,
    ControlledExecutionSnapshot,
    ResourceLeaseRecord,
    TaskExecutionRecord,
    TaskRequestRecord,
)

TERMINAL_STATUSES = {"success", "failed"}


def _load_json_dict(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class SqlAlchemyExecutionControlStore:
    def __init__(self, db_url: str):
        self._db_url = db_url.strip()
        if not self._db_url:
            raise ValueError("db_url is required for SqlAlchemyExecutionControlStore.")
        try:
            from sqlalchemy import create_engine, text
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "SqlAlchemyExecutionControlStore requires SQLAlchemy. "
                "Install project dependencies before using BUSINESS_EXECUTION_CONTROL_DB_URL."
            ) from exc
        self._text = text
        self._engine = create_engine(self._db_url, future=True, pool_pre_ping=True)
        self._dialect_name = str(self._engine.dialect.name or "").lower()
        self._ensure_schema()

    @property
    def _supports_for_update(self) -> bool:
        return self._dialect_name.startswith("postgres")

    def _ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS task_request (
                request_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                resource_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
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
                request_id TEXT NOT NULL UNIQUE,
                task_name TEXT NOT NULL,
                resource_code TEXT NOT NULL,
                status TEXT NOT NULL,
                queue_seq INTEGER NOT NULL,
                worker_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                heartbeat_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_execution_status_queue_seq
                ON task_execution(status, queue_seq)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_task_execution_resource_queue_seq
                ON task_execution(resource_code, queue_seq)
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_lease (
                resource_code TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                status TEXT NOT NULL,
                lease_until REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifact_object (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                bucket TEXT NOT NULL,
                object_key TEXT NOT NULL,
                etag TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
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

    @staticmethod
    def _request_from_row(row: Mapping[str, Any]) -> TaskRequestRecord:
        return TaskRequestRecord(
            request_id=str(row["request_id"]),
            task_name=str(row["task_name"]),
            resource_code=str(row["resource_code"]),
            status=str(row["request_status"]),
            payload=_load_json_dict(row["payload_json"]),
            requested_by=str(row["requested_by"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            created_at=_coerce_float(row["request_created_at"]),
            updated_at=_coerce_float(row["request_updated_at"]),
            started_at=_coerce_float(row["request_started_at"]),
            finished_at=_coerce_float(row["request_finished_at"]),
        )

    @staticmethod
    def _execution_from_row(row: Mapping[str, Any]) -> TaskExecutionRecord:
        return TaskExecutionRecord(
            execution_id=str(row["execution_id"]),
            request_id=str(row["request_id"]),
            task_name=str(row["task_name"]),
            resource_code=str(row["resource_code"]),
            status=str(row["execution_status"]),
            queue_seq=int(row["queue_seq"]),
            worker_id=str(row["worker_id"] or ""),
            run_id=str(row["run_id"] or ""),
            summary=_load_json_dict(row["summary_json"]),
            result=_load_json_dict(row["result_json"]),
            error_text=str(row["error_text"] or ""),
            created_at=_coerce_float(row["execution_created_at"]),
            updated_at=_coerce_float(row["execution_updated_at"]),
            started_at=_coerce_float(row["execution_started_at"]),
            finished_at=_coerce_float(row["execution_finished_at"]),
            heartbeat_at=_coerce_float(row["heartbeat_at"]),
        )

    @staticmethod
    def _lease_from_row(row: Mapping[str, Any] | None) -> ResourceLeaseRecord | None:
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

    @staticmethod
    def _artifact_from_row(row: Mapping[str, Any]) -> ArtifactObjectRecord:
        return ArtifactObjectRecord(
            artifact_id=str(row["artifact_id"]),
            run_id=str(row["run_id"]),
            step_id=str(row["step_id"]),
            kind=str(row["kind"]),
            bucket=str(row["bucket"]),
            object_key=str(row["object_key"]),
            etag=str(row["etag"]),
            size=int(row["size"]),
            content_type=str(row["content_type"]),
            source_path=str(row["source_path"]),
            created_at=_coerce_float(row["created_at"]),
        )

    def _load_snapshot_with_connection(
        self,
        connection: Any,
        *,
        request_id: str = "",
        execution_id: str = "",
    ) -> ControlledExecutionSnapshot:
        if not request_id and not execution_id:
            raise ValueError("request_id or execution_id is required.")
        filters: list[str] = []
        values: dict[str, Any] = {}
        if request_id:
            filters.append("r.request_id = :request_id")
            values["request_id"] = request_id
        if execution_id:
            filters.append("e.execution_id = :execution_id")
            values["execution_id"] = execution_id
        row = (
            connection.execute(
                self._text(
                    f"""
                    SELECT
                        r.request_id,
                        r.task_name,
                        r.resource_code,
                        r.payload_json,
                        r.idempotency_key,
                        r.status AS request_status,
                        r.requested_by,
                        r.created_at AS request_created_at,
                        r.updated_at AS request_updated_at,
                        r.started_at AS request_started_at,
                        r.finished_at AS request_finished_at,
                        e.execution_id,
                        e.status AS execution_status,
                        e.queue_seq,
                        e.worker_id,
                        e.run_id,
                        e.summary_json,
                        e.result_json,
                        e.error_text,
                        e.created_at AS execution_created_at,
                        e.updated_at AS execution_updated_at,
                        e.started_at AS execution_started_at,
                        e.finished_at AS execution_finished_at,
                        e.heartbeat_at
                    FROM task_request r
                    JOIN task_execution e ON e.request_id = r.request_id
                    WHERE {" AND ".join(filters)}
                    LIMIT 1
                    """
                ),
                values,
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ValueError("Execution snapshot not found.")
        lease_row = (
            connection.execute(
                self._text(
                    """
                    SELECT resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                    FROM resource_lease
                    WHERE resource_code = :resource_code
                    """
                ),
                {"resource_code": row["resource_code"]},
            )
            .mappings()
            .first()
        )
        queue_position = 0
        if str(row["execution_status"]) not in TERMINAL_STATUSES:
            queue_position = int(
                connection.execute(
                    self._text(
                        """
                        SELECT COUNT(*)
                        FROM task_execution
                        WHERE resource_code = :resource_code
                          AND queue_seq <= :queue_seq
                          AND status IN ('queued', 'running')
                        """
                    ),
                    {
                        "resource_code": row["resource_code"],
                        "queue_seq": row["queue_seq"],
                    },
                ).scalar_one()
            )
        return ControlledExecutionSnapshot(
            request=self._request_from_row(row),
            execution=self._execution_from_row(row),
            lease=self._lease_from_row(lease_row),
            queue_position=queue_position,
        )

    def submit_request(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        resource_code: str,
        requested_by: str,
        idempotency_key: str = "",
    ) -> ControlledExecutionSnapshot:
        request_id = uuid.uuid4().hex
        execution_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            now = time.time()
            existing_row: Mapping[str, Any] | None = None
            if idempotency_key:
                existing_row = (
                    connection.execute(
                        self._text(
                            """
                            SELECT
                                r.request_id,
                                r.task_name,
                                r.resource_code,
                                r.payload_json,
                                r.idempotency_key,
                                r.status AS request_status,
                                r.requested_by,
                                r.created_at AS request_created_at,
                                r.updated_at AS request_updated_at,
                                r.started_at AS request_started_at,
                                r.finished_at AS request_finished_at,
                                e.execution_id,
                                e.status AS execution_status,
                                e.queue_seq,
                                e.worker_id,
                                e.run_id,
                                e.summary_json,
                                e.result_json,
                                e.error_text,
                                e.created_at AS execution_created_at,
                                e.updated_at AS execution_updated_at,
                                e.started_at AS execution_started_at,
                                e.finished_at AS execution_finished_at,
                                e.heartbeat_at
                            FROM task_request r
                            JOIN task_execution e ON e.request_id = r.request_id
                            WHERE r.task_name = :task_name AND r.idempotency_key = :idempotency_key
                            ORDER BY r.created_at DESC
                            LIMIT 1
                            """
                        ),
                        {
                            "task_name": task_name,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    .mappings()
                    .first()
                )
                if existing_row is not None:
                    request_id = str(existing_row["request_id"])
            if existing_row is not None:
                return self._load_snapshot_with_connection(connection, request_id=request_id)

            queue_seq = int(
                connection.execute(
                    self._text("SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM task_execution")
                ).scalar_one()
            )
            connection.execute(
                self._text(
                    """
                    INSERT INTO task_request (
                        request_id, task_name, resource_code, payload_json, idempotency_key, status,
                        requested_by, created_at, updated_at, started_at, finished_at
                    ) VALUES (
                        :request_id, :task_name, :resource_code, :payload_json, :idempotency_key, :status,
                        :requested_by, :created_at, :updated_at, NULL, NULL
                    )
                    """
                ),
                {
                    "request_id": request_id,
                    "task_name": task_name,
                    "resource_code": resource_code,
                    "payload_json": _json_dumps(payload),
                    "idempotency_key": idempotency_key,
                    "status": "queued",
                    "requested_by": requested_by,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                self._text(
                    """
                    INSERT INTO task_execution (
                        execution_id, request_id, task_name, resource_code, status, queue_seq,
                        worker_id, run_id, summary_json, result_json, error_text,
                        created_at, updated_at, started_at, finished_at, heartbeat_at
                    ) VALUES (
                        :execution_id, :request_id, :task_name, :resource_code, :status, :queue_seq,
                        '', '', '{}', '{}', '', :created_at, :updated_at, NULL, NULL, NULL
                    )
                    """
                ),
                {
                    "execution_id": execution_id,
                    "request_id": request_id,
                    "task_name": task_name,
                    "resource_code": resource_code,
                    "status": "queued",
                    "queue_seq": queue_seq,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return self._load_snapshot_with_connection(connection, request_id=request_id)

    def load_snapshot(
        self,
        *,
        request_id: str = "",
        execution_id: str = "",
    ) -> ControlledExecutionSnapshot:
        with self._engine.connect() as connection:
            return self._load_snapshot_with_connection(
                connection,
                request_id=request_id,
                execution_id=execution_id,
            )

    def _mark_expired_leases_failed(self, connection: Any, now: float) -> None:
        expired_rows = (
            connection.execute(
                self._text(
                    """
                    SELECT l.resource_code, l.execution_id, e.request_id
                    FROM resource_lease l
                    JOIN task_execution e ON e.execution_id = l.execution_id
                    WHERE l.lease_until <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in expired_rows:
            error_text = "Execution lease expired before the worker completed."
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'failed',
                        error_text = :error_text,
                        updated_at = :now,
                        finished_at = COALESCE(finished_at, :now)
                    WHERE execution_id = :execution_id
                      AND status = 'running'
                    """
                ),
                {
                    "error_text": error_text,
                    "now": now,
                    "execution_id": row["execution_id"],
                },
            )
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'failed',
                        updated_at = :now,
                        finished_at = COALESCE(finished_at, :now)
                    WHERE request_id = :request_id
                      AND status = 'running'
                    """
                ),
                {
                    "now": now,
                    "request_id": row["request_id"],
                },
            )
            connection.execute(
                self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": row["resource_code"]},
            )

    def claim_request(
        self,
        *,
        request_id: str,
        worker_id: str,
        lease_seconds: float,
        run_id: str = "",
    ) -> ControlledExecutionSnapshot | None:
        request_lock_clause = " FOR UPDATE" if self._supports_for_update else ""
        lease_lock_clause = " FOR UPDATE" if self._supports_for_update else ""
        with self._engine.begin() as connection:
            now = time.time()
            self._mark_expired_leases_failed(connection, now)
            row = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT r.request_id, r.resource_code, e.execution_id, e.status AS execution_status
                        FROM task_request r
                        JOIN task_execution e ON e.request_id = r.request_id
                        WHERE r.request_id = :request_id
                        LIMIT 1{request_lock_clause}
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
            if row is None or str(row["execution_status"]) != "queued":
                return None
            live_lease = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT execution_id, lease_until
                        FROM resource_lease
                        WHERE resource_code = :resource_code
                        LIMIT 1{lease_lock_clause}
                        """
                    ),
                    {"resource_code": row["resource_code"]},
                )
                .mappings()
                .first()
            )
            if live_lease is not None and _coerce_float(live_lease["lease_until"]) > now:
                return None
            if live_lease is not None:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": row["resource_code"]},
                )
            resolved_run_id = run_id or f"managed-{row['execution_id']}"
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'running',
                        updated_at = :now,
                        started_at = COALESCE(started_at, :now)
                    WHERE request_id = :request_id
                    """
                ),
                {"now": now, "request_id": request_id},
            )
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'running',
                        worker_id = :worker_id,
                        run_id = CASE WHEN run_id = '' THEN :run_id ELSE run_id END,
                        updated_at = :now,
                        started_at = COALESCE(started_at, :now),
                        heartbeat_at = :now
                    WHERE request_id = :request_id
                    """
                ),
                {
                    "worker_id": worker_id,
                    "run_id": resolved_run_id,
                    "now": now,
                    "request_id": request_id,
                },
            )
            connection.execute(
                self._text(
                    """
                    INSERT INTO resource_lease (
                        resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                    ) VALUES (
                        :resource_code, :execution_id, 'active', :lease_until, :heartbeat_at, :created_at, :updated_at
                    )
                    ON CONFLICT(resource_code) DO UPDATE SET
                        execution_id = excluded.execution_id,
                        status = excluded.status,
                        lease_until = excluded.lease_until,
                        heartbeat_at = excluded.heartbeat_at,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "resource_code": row["resource_code"],
                    "execution_id": row["execution_id"],
                    "lease_until": now + lease_seconds,
                    "heartbeat_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return self._load_snapshot_with_connection(connection, request_id=request_id)

    def claim_next_request(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        task_name: str = "",
    ) -> ControlledExecutionSnapshot | None:
        task_clause = " AND r.task_name = :task_name" if task_name else ""
        queue_lock_clause = " FOR UPDATE OF e SKIP LOCKED" if self._supports_for_update else ""
        lease_lock_clause = " FOR UPDATE" if self._supports_for_update else ""
        with self._engine.begin() as connection:
            now = time.time()
            self._mark_expired_leases_failed(connection, now)
            queued_rows = (
                connection.execute(
                    self._text(
                        f"""
                        SELECT r.request_id, r.resource_code
                        FROM task_request r
                        JOIN task_execution e ON e.request_id = r.request_id
                        WHERE e.status = 'queued'{task_clause}
                        ORDER BY e.queue_seq ASC{queue_lock_clause}
                        """
                    ),
                    {"task_name": task_name} if task_name else {},
                )
                .mappings()
                .all()
            )
            for row in queued_rows:
                live_lease = (
                    connection.execute(
                        self._text(
                            f"""
                            SELECT execution_id, lease_until
                            FROM resource_lease
                            WHERE resource_code = :resource_code
                            LIMIT 1{lease_lock_clause}
                            """
                        ),
                        {"resource_code": row["resource_code"]},
                    )
                    .mappings()
                    .first()
                )
                if live_lease is not None and _coerce_float(live_lease["lease_until"]) > now:
                    continue
                if live_lease is not None:
                    connection.execute(
                        self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                        {"resource_code": row["resource_code"]},
                    )
                candidate_request_id = str(row["request_id"])
                connection.execute(
                    self._text(
                        """
                        UPDATE task_request
                        SET status = 'running',
                            updated_at = :now,
                            started_at = COALESCE(started_at, :now)
                        WHERE request_id = :request_id
                        """
                    ),
                    {"now": now, "request_id": candidate_request_id},
                )
                execution_row = (
                    connection.execute(
                        self._text(
                            "SELECT execution_id FROM task_execution WHERE request_id = :request_id LIMIT 1"
                        ),
                        {"request_id": candidate_request_id},
                    )
                    .mappings()
                    .first()
                )
                if execution_row is None:
                    continue
                resolved_run_id = f"managed-{execution_row['execution_id']}"
                connection.execute(
                    self._text(
                        """
                        UPDATE task_execution
                        SET status = 'running',
                            worker_id = :worker_id,
                            run_id = CASE WHEN run_id = '' THEN :run_id ELSE run_id END,
                            updated_at = :now,
                            started_at = COALESCE(started_at, :now),
                            heartbeat_at = :now
                        WHERE request_id = :request_id
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "run_id": resolved_run_id,
                        "now": now,
                        "request_id": candidate_request_id,
                    },
                )
                connection.execute(
                    self._text(
                        """
                        INSERT INTO resource_lease (
                            resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                        ) VALUES (
                            :resource_code, :execution_id, 'active', :lease_until, :heartbeat_at, :created_at, :updated_at
                        )
                        ON CONFLICT(resource_code) DO UPDATE SET
                            execution_id = excluded.execution_id,
                            status = excluded.status,
                            lease_until = excluded.lease_until,
                            heartbeat_at = excluded.heartbeat_at,
                            updated_at = excluded.updated_at
                        """
                    ),
                    {
                        "resource_code": row["resource_code"],
                        "execution_id": execution_row["execution_id"],
                        "lease_until": now + lease_seconds,
                        "heartbeat_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                return self._load_snapshot_with_connection(connection, request_id=candidate_request_id)
            return None

    def heartbeat(self, *, execution_id: str, lease_seconds: float) -> None:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET heartbeat_at = :now, updated_at = :now
                    WHERE execution_id = :execution_id
                      AND status = 'running'
                    """
                ),
                {"now": now, "execution_id": execution_id},
            )
            connection.execute(
                self._text(
                    """
                    UPDATE resource_lease
                    SET heartbeat_at = :now, lease_until = :lease_until, updated_at = :now
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "now": now,
                    "lease_until": now + lease_seconds,
                    "execution_id": execution_id,
                },
            )

    def mark_success(
        self,
        *,
        execution_id: str,
        result: dict[str, Any],
        summary: dict[str, Any],
        run_id: str,
    ) -> ControlledExecutionSnapshot:
        with self._engine.begin() as connection:
            now = time.time()
            request_row = (
                connection.execute(
                    self._text(
                        """
                        SELECT request_id, resource_code
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
            if request_row is None:
                raise ValueError("Execution not found.")
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'success',
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = '',
                        updated_at = :now,
                        finished_at = :now,
                        heartbeat_at = :now
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "now": now,
                    "execution_id": execution_id,
                },
            )
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'success',
                        updated_at = :now,
                        finished_at = :now
                    WHERE request_id = :request_id
                    """
                ),
                {"now": now, "request_id": request_row["request_id"]},
            )
            connection.execute(
                self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": request_row["resource_code"]},
            )
            return self._load_snapshot_with_connection(connection, execution_id=execution_id)

    def mark_failed(
        self,
        *,
        execution_id: str,
        error_text: str,
        run_id: str,
        result: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> ControlledExecutionSnapshot:
        with self._engine.begin() as connection:
            now = time.time()
            request_row = (
                connection.execute(
                    self._text(
                        """
                        SELECT request_id, resource_code
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
            if request_row is None:
                raise ValueError("Execution not found.")
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'failed',
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        updated_at = :now,
                        finished_at = :now,
                        heartbeat_at = :now
                    WHERE execution_id = :execution_id
                    """
                ),
                {
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "now": now,
                    "execution_id": execution_id,
                },
            )
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'failed',
                        updated_at = :now,
                        finished_at = :now
                    WHERE request_id = :request_id
                    """
                ),
                {"now": now, "request_id": request_row["request_id"]},
            )
            connection.execute(
                self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": request_row["resource_code"]},
            )
            return self._load_snapshot_with_connection(connection, execution_id=execution_id)

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
                            artifact_id, run_id, step_id, kind, bucket, object_key, etag,
                            size, content_type, source_path, created_at
                        ) VALUES (
                            :artifact_id, :run_id, :step_id, :kind, :bucket, :object_key, :etag,
                            :size, :content_type, :source_path, :created_at
                        )
                        """
                    ),
                    {
                        "artifact_id": record.artifact_id,
                        "run_id": record.run_id,
                        "step_id": record.step_id,
                        "kind": record.kind,
                        "bucket": record.bucket,
                        "object_key": record.object_key,
                        "etag": record.etag,
                        "size": record.size,
                        "content_type": record.content_type,
                        "source_path": record.source_path,
                        "created_at": record.created_at,
                    },
                )

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT artifact_id, run_id, step_id, kind, bucket, object_key, etag,
                               size, content_type, source_path, created_at
                        FROM artifact_object
                        WHERE run_id = :run_id
                        ORDER BY object_key ASC
                        """
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .all()
            )
            return [self._artifact_from_row(row) for row in rows]
