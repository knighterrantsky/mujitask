from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from automation_business_scaffold.config import get_execution_control_defaults
from automation_business_scaffold.flows.feishu_competitor_flow import run_feishu_single_row_update
from automation_business_scaffold.flows.sqlalchemy_execution_control_store import (
    SqlAlchemyExecutionControlStore,
)
from automation_business_scaffold.models import (
    ArtifactObjectRecord,
    ControlledExecutionSnapshot,
    ResourceLeaseRecord,
    TaskExecutionRecord,
    TaskRequestRecord,
)

CONTROLLED_TASK_NAME = "feishu_single_row_update"
CONTROLLED_STEP_ID = "execute_controlled_single_row_update"
DEFAULT_RESOURCE_CODE = "browser.tiktok.main"
TERMINAL_STATUSES = {"success", "failed"}


def _read_float_param(params: dict[str, Any], key: str, default: float) -> float:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_int_param(params: dict[str, Any], key: str, default: int) -> int:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _read_bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    raw = params.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


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


def _json_pretty(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_controlled_params(params: dict[str, Any]) -> dict[str, Any]:
    control_keys = {
        "control_action",
        "request_id",
        "execution_id",
        "execution_control_db_url",
        "execution_control_db_path",
        "execution_requested_by",
        "execution_worker_id",
        "execution_lease_seconds",
        "execution_heartbeat_interval_seconds",
        "execution_poll_interval_seconds",
        "execution_wait_timeout_seconds",
        "execution_run_id",
        "execution_control_max_iterations",
        "execution_control_max_idle_cycles",
        "execution_control_stop_when_idle",
        "execution_control_artifact_root",
        "execution_control_artifact_bucket",
    }
    return {key: value for key, value in params.items() if key not in control_keys}


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json_file(path: Path, payload: Any) -> None:
    _write_text(path, f"{_json_pretty(payload)}\n")


def build_controlled_resource_code(params: dict[str, Any]) -> str:
    profile_ref = str(params.get("profile_ref", "") or "").strip()
    if not profile_ref:
        return DEFAULT_RESOURCE_CODE
    safe_profile = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in profile_ref
    ).strip("-")
    return f"browser.tiktok.{safe_profile or 'main'}"


def _controlled_settings(params: dict[str, Any]) -> dict[str, Any]:
    defaults = get_execution_control_defaults()
    configured_db_url = str(params.get("execution_control_db_url") or defaults.db_url).strip()
    configured_db_path = str(params.get("execution_control_db_path") or defaults.db_path)
    if not configured_db_url and "://" in configured_db_path:
        configured_db_url = configured_db_path
        configured_db_path = defaults.db_path
    return {
        "db_url": configured_db_url,
        "db_path": configured_db_path,
        "artifact_root": str(params.get("execution_control_artifact_root") or defaults.artifact_root),
        "artifact_bucket": str(
            params.get("execution_control_artifact_bucket") or defaults.artifact_bucket
        ),
        "requested_by": str(params.get("execution_requested_by") or defaults.requested_by),
        "worker_id": str(params.get("execution_worker_id") or defaults.worker_id),
        "lease_seconds": max(
            _read_float_param(params, "execution_lease_seconds", defaults.lease_seconds),
            5.0,
        ),
        "heartbeat_interval_seconds": max(
            _read_float_param(
                params,
                "execution_heartbeat_interval_seconds",
                defaults.heartbeat_interval_seconds,
            ),
            0.2,
        ),
        "poll_interval_seconds": max(
            _read_float_param(
                params,
                "execution_poll_interval_seconds",
                defaults.poll_interval_seconds,
            ),
            0.05,
        ),
        "wait_timeout_seconds": max(
            _read_float_param(
                params,
                "execution_wait_timeout_seconds",
                defaults.wait_timeout_seconds,
            ),
            1.0,
        ),
    }


def _create_execution_control_store(settings: dict[str, Any]) -> Any:
    db_url = str(settings.get("db_url", "") or "").strip()
    if db_url:
        return SqlAlchemyExecutionControlStore(db_url)
    return ExecutionControlStore(str(settings["db_path"]))


class ExecutionControlStore:
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._db_path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
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
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_task_execution_status_queue_seq
                    ON task_execution(status, queue_seq);

                CREATE INDEX IF NOT EXISTS idx_task_execution_resource_queue_seq
                    ON task_execution(resource_code, queue_seq);

                CREATE TABLE IF NOT EXISTS resource_lease (
                    resource_code TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_artifact_object_run_id
                    ON artifact_object(run_id);
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> TaskRequestRecord:
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
    def _execution_from_row(row: sqlite3.Row) -> TaskExecutionRecord:
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
    def _lease_from_row(row: sqlite3.Row | None) -> ResourceLeaseRecord | None:
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
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactObjectRecord:
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

    def submit_request(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        resource_code: str,
        requested_by: str,
        idempotency_key: str = "",
    ) -> ControlledExecutionSnapshot:
        connection = self._connect()
        try:
            now = time.time()
            request_id = uuid.uuid4().hex
            execution_id = uuid.uuid4().hex
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing_row = connection.execute(
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
                    WHERE r.task_name = ? AND r.idempotency_key = ?
                    ORDER BY r.created_at DESC
                    LIMIT 1
                    """,
                    (task_name, idempotency_key),
                ).fetchone()
                if existing_row is not None:
                    connection.commit()
                    return self.load_snapshot(request_id=str(existing_row["request_id"]))

            queue_seq = int(
                connection.execute("SELECT COALESCE(MAX(queue_seq), 0) + 1 FROM task_execution").fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO task_request (
                    request_id, task_name, resource_code, payload_json, idempotency_key, status,
                    requested_by, created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    request_id,
                    task_name,
                    resource_code,
                    _json_dumps(payload),
                    idempotency_key,
                    "queued",
                    requested_by,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_execution (
                    execution_id, request_id, task_name, resource_code, status, queue_seq,
                    worker_id, run_id, summary_json, result_json, error_text,
                    created_at, updated_at, started_at, finished_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', '', '{}', '{}', '', ?, ?, NULL, NULL, NULL)
                """,
                (
                    execution_id,
                    request_id,
                    task_name,
                    resource_code,
                    "queued",
                    queue_seq,
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return self.load_snapshot(request_id=request_id)

    def load_snapshot(
        self,
        *,
        request_id: str = "",
        execution_id: str = "",
    ) -> ControlledExecutionSnapshot:
        if not request_id and not execution_id:
            raise ValueError("request_id or execution_id is required.")
        connection = self._connect()
        try:
            filters: list[str] = []
            values: list[Any] = []
            if request_id:
                filters.append("r.request_id = ?")
                values.append(request_id)
            if execution_id:
                filters.append("e.execution_id = ?")
                values.append(execution_id)
            row = connection.execute(
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
                """,
                values,
            ).fetchone()
            if row is None:
                raise ValueError("Execution snapshot not found.")
            lease_row = connection.execute(
                """
                SELECT resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                FROM resource_lease
                WHERE resource_code = ?
                """,
                (row["resource_code"],),
            ).fetchone()
            queue_position = 0
            if str(row["execution_status"]) not in TERMINAL_STATUSES:
                queue_position = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM task_execution
                        WHERE resource_code = ?
                          AND queue_seq <= ?
                          AND status IN ('queued', 'running')
                        """,
                        (row["resource_code"], row["queue_seq"]),
                    ).fetchone()[0]
                )
            return ControlledExecutionSnapshot(
                request=self._request_from_row(row),
                execution=self._execution_from_row(row),
                lease=self._lease_from_row(lease_row),
                queue_position=queue_position,
            )
        finally:
            connection.close()

    def _mark_expired_leases_failed(self, connection: sqlite3.Connection, now: float) -> None:
        expired_rows = connection.execute(
            """
            SELECT l.resource_code, l.execution_id, e.request_id
            FROM resource_lease l
            JOIN task_execution e ON e.execution_id = l.execution_id
            WHERE l.lease_until <= ?
            """,
            (now,),
        ).fetchall()
        for row in expired_rows:
            error_text = "Execution lease expired before the worker completed."
            connection.execute(
                """
                UPDATE task_execution
                SET status = 'failed',
                    error_text = ?,
                    updated_at = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE execution_id = ?
                  AND status = 'running'
                """,
                (error_text, now, now, row["execution_id"]),
            )
            connection.execute(
                """
                UPDATE task_request
                SET status = 'failed',
                    updated_at = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE request_id = ?
                  AND status = 'running'
                """,
                (now, now, row["request_id"]),
            )
            connection.execute(
                "DELETE FROM resource_lease WHERE resource_code = ?",
                (row["resource_code"],),
            )

    def claim_request(
        self,
        *,
        request_id: str,
        worker_id: str,
        lease_seconds: float,
        run_id: str = "",
    ) -> ControlledExecutionSnapshot | None:
        connection = self._connect()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            self._mark_expired_leases_failed(connection, now)
            row = connection.execute(
                """
                SELECT r.request_id, r.resource_code, e.execution_id, e.status AS execution_status
                FROM task_request r
                JOIN task_execution e ON e.request_id = r.request_id
                WHERE r.request_id = ?
                LIMIT 1
                """,
                (request_id,),
            ).fetchone()
            if row is None or str(row["execution_status"]) != "queued":
                connection.commit()
                return None
            live_lease = connection.execute(
                """
                SELECT execution_id, lease_until
                FROM resource_lease
                WHERE resource_code = ?
                LIMIT 1
                """,
                (row["resource_code"],),
            ).fetchone()
            if live_lease is not None and _coerce_float(live_lease["lease_until"]) > now:
                connection.commit()
                return None
            if live_lease is not None:
                connection.execute(
                    "DELETE FROM resource_lease WHERE resource_code = ?",
                    (row["resource_code"],),
                )
            resolved_run_id = run_id or f"managed-{row['execution_id']}"
            connection.execute(
                """
                UPDATE task_request
                SET status = 'running',
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE request_id = ?
                """,
                (now, now, request_id),
            )
            connection.execute(
                """
                UPDATE task_execution
                SET status = 'running',
                    worker_id = ?,
                    run_id = CASE WHEN run_id = '' THEN ? ELSE run_id END,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?
                WHERE request_id = ?
                """,
                (worker_id, resolved_run_id, now, now, now, request_id),
            )
            connection.execute(
                """
                INSERT INTO resource_lease (
                    resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?)
                ON CONFLICT(resource_code) DO UPDATE SET
                    execution_id = excluded.execution_id,
                    status = excluded.status,
                    lease_until = excluded.lease_until,
                    heartbeat_at = excluded.heartbeat_at,
                    updated_at = excluded.updated_at
                """,
                (
                    row["resource_code"],
                    row["execution_id"],
                    now + lease_seconds,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return self.load_snapshot(request_id=request_id)

    def claim_next_request(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        task_name: str = "",
    ) -> ControlledExecutionSnapshot | None:
        connection = self._connect()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            self._mark_expired_leases_failed(connection, now)
            query = """
                SELECT r.request_id, r.resource_code
                FROM task_request r
                JOIN task_execution e ON e.request_id = r.request_id
                WHERE e.status = 'queued'
            """
            values: list[Any] = []
            if task_name:
                query += " AND r.task_name = ?"
                values.append(task_name)
            query += " ORDER BY e.queue_seq ASC"
            queued_rows = connection.execute(query, values).fetchall()
            for row in queued_rows:
                live_lease = connection.execute(
                    """
                    SELECT execution_id, lease_until
                    FROM resource_lease
                    WHERE resource_code = ?
                    LIMIT 1
                    """,
                    (row["resource_code"],),
                ).fetchone()
                if live_lease is not None and _coerce_float(live_lease["lease_until"]) > now:
                    continue
                if live_lease is not None:
                    connection.execute(
                        "DELETE FROM resource_lease WHERE resource_code = ?",
                        (row["resource_code"],),
                    )
                candidate_request_id = str(row["request_id"])
                connection.execute(
                    """
                    UPDATE task_request
                    SET status = 'running',
                        updated_at = ?,
                        started_at = COALESCE(started_at, ?)
                    WHERE request_id = ?
                    """,
                    (now, now, candidate_request_id),
                )
                execution_row = connection.execute(
                    "SELECT execution_id FROM task_execution WHERE request_id = ? LIMIT 1",
                    (candidate_request_id,),
                ).fetchone()
                if execution_row is None:
                    continue
                resolved_run_id = f"managed-{execution_row['execution_id']}"
                connection.execute(
                    """
                    UPDATE task_execution
                    SET status = 'running',
                        worker_id = ?,
                        run_id = CASE WHEN run_id = '' THEN ? ELSE run_id END,
                        updated_at = ?,
                        started_at = COALESCE(started_at, ?),
                        heartbeat_at = ?
                    WHERE request_id = ?
                    """,
                    (worker_id, resolved_run_id, now, now, now, candidate_request_id),
                )
                connection.execute(
                    """
                    INSERT INTO resource_lease (
                        resource_code, execution_id, status, lease_until, heartbeat_at, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?)
                    ON CONFLICT(resource_code) DO UPDATE SET
                        execution_id = excluded.execution_id,
                        status = excluded.status,
                        lease_until = excluded.lease_until,
                        heartbeat_at = excluded.heartbeat_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["resource_code"],
                        execution_row["execution_id"],
                        now + lease_seconds,
                        now,
                        now,
                        now,
                    ),
                )
                connection.commit()
                return self.load_snapshot(request_id=candidate_request_id)
            connection.commit()
            return None
        finally:
            connection.close()

    def heartbeat(self, *, execution_id: str, lease_seconds: float) -> None:
        connection = self._connect()
        try:
            now = time.time()
            connection.execute(
                """
                UPDATE task_execution
                SET heartbeat_at = ?, updated_at = ?
                WHERE execution_id = ?
                  AND status = 'running'
                """,
                (now, now, execution_id),
            )
            connection.execute(
                """
                UPDATE resource_lease
                SET heartbeat_at = ?, lease_until = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (now, now + lease_seconds, now, execution_id),
            )
        finally:
            connection.close()

    def mark_success(
        self,
        *,
        execution_id: str,
        result: dict[str, Any],
        summary: dict[str, Any],
        run_id: str,
    ) -> ControlledExecutionSnapshot:
        connection = self._connect()
        try:
            now = time.time()
            request_row = connection.execute(
                "SELECT request_id, resource_code FROM task_execution WHERE execution_id = ? LIMIT 1",
                (execution_id,),
            ).fetchone()
            if request_row is None:
                raise ValueError("Execution not found.")
            connection.execute(
                """
                UPDATE task_execution
                SET status = 'success',
                    run_id = ?,
                    summary_json = ?,
                    result_json = ?,
                    error_text = '',
                    updated_at = ?,
                    finished_at = ?,
                    heartbeat_at = ?
                WHERE execution_id = ?
                """,
                (
                    run_id,
                    _json_dumps(summary),
                    _json_dumps(result),
                    now,
                    now,
                    now,
                    execution_id,
                ),
            )
            connection.execute(
                """
                UPDATE task_request
                SET status = 'success',
                    updated_at = ?,
                    finished_at = ?
                WHERE request_id = ?
                """,
                (now, now, request_row["request_id"]),
            )
            connection.execute(
                "DELETE FROM resource_lease WHERE resource_code = ?",
                (request_row["resource_code"],),
            )
        finally:
            connection.close()
        return self.load_snapshot(execution_id=execution_id)

    def mark_failed(
        self,
        *,
        execution_id: str,
        error_text: str,
        run_id: str,
        result: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> ControlledExecutionSnapshot:
        connection = self._connect()
        try:
            now = time.time()
            request_row = connection.execute(
                "SELECT request_id, resource_code FROM task_execution WHERE execution_id = ? LIMIT 1",
                (execution_id,),
            ).fetchone()
            if request_row is None:
                raise ValueError("Execution not found.")
            connection.execute(
                """
                UPDATE task_execution
                SET status = 'failed',
                    run_id = ?,
                    summary_json = ?,
                    result_json = ?,
                    error_text = ?,
                    updated_at = ?,
                    finished_at = ?,
                    heartbeat_at = ?
                WHERE execution_id = ?
                """,
                (
                    run_id,
                    _json_dumps(summary or {}),
                    _json_dumps(result or {}),
                    error_text,
                    now,
                    now,
                    now,
                    execution_id,
                ),
            )
            connection.execute(
                """
                UPDATE task_request
                SET status = 'failed',
                    updated_at = ?,
                    finished_at = ?
                WHERE request_id = ?
                """,
                (now, now, request_row["request_id"]),
            )
            connection.execute(
                "DELETE FROM resource_lease WHERE resource_code = ?",
                (request_row["resource_code"],),
            )
        finally:
            connection.close()
        return self.load_snapshot(execution_id=execution_id)

    def replace_artifacts(self, *, run_id: str, records: list[ArtifactObjectRecord]) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM artifact_object WHERE run_id = ?", (run_id,))
            for record in records:
                connection.execute(
                    """
                    INSERT INTO artifact_object (
                        artifact_id, run_id, step_id, kind, bucket, object_key, etag,
                        size, content_type, source_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.artifact_id,
                        record.run_id,
                        record.step_id,
                        record.kind,
                        record.bucket,
                        record.object_key,
                        record.etag,
                        record.size,
                        record.content_type,
                        record.source_path,
                        record.created_at,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT artifact_id, run_id, step_id, kind, bucket, object_key, etag,
                       size, content_type, source_path, created_at
                FROM artifact_object
                WHERE run_id = ?
                ORDER BY object_key ASC
                """,
                (run_id,),
            ).fetchall()
            return [self._artifact_from_row(row) for row in rows]
        finally:
            connection.close()


class LeaseHeartbeat:
    def __init__(
        self,
        *,
        store: ExecutionControlStore,
        execution_id: str,
        lease_seconds: float,
        interval_seconds: float,
    ):
        self._store = store
        self._execution_id = execution_id
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval_seconds + 1.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self._store.heartbeat(
                execution_id=self._execution_id,
                lease_seconds=self._lease_seconds,
            )


class TeeStream:
    def __init__(self, *targets):
        self._targets = targets

    def write(self, data: str) -> int:
        for target in self._targets:
            target.write(data)
            target.flush()
        return len(data)

    def flush(self) -> None:
        for target in self._targets:
            target.flush()


def _artifact_content_type(kind: str, path: Path) -> str:
    if kind.endswith("_json") or path.suffix == ".json":
        return "application/json"
    if kind.endswith("_log") or path.suffix == ".log":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _runtime_object_key(run_id: str, relative_name: str) -> str:
    return f"runs/{run_id}/{relative_name}"


def _build_artifact_record(
    *,
    run_id: str,
    step_id: str,
    kind: str,
    bucket: str,
    object_key: str,
    path: Path,
    created_at: float,
) -> ArtifactObjectRecord:
    return ArtifactObjectRecord(
        artifact_id=uuid.uuid4().hex,
        run_id=run_id,
        step_id=step_id,
        kind=kind,
        bucket=bucket,
        object_key=object_key,
        etag=_sha256_of_file(path),
        size=path.stat().st_size,
        content_type=_artifact_content_type(kind, path),
        source_path=str(path.resolve()),
        created_at=created_at,
    )


def _artifact_payload_from_records(
    *,
    artifact_root: Path,
    run_id: str,
    records: list[ArtifactObjectRecord],
) -> dict[str, Any]:
    by_kind = {record.kind: record for record in records}
    run_prefix = artifact_root / "runs" / run_id
    return {
        "artifact_count": len(records),
        "artifacts": [record.to_dict() for record in records],
        "artifact_uri_prefix": run_prefix.resolve().as_uri() if records else "",
        "run_object_key": by_kind.get("run_json").object_key if "run_json" in by_kind else "",
        "steps_object_key": by_kind.get("steps_json").object_key if "steps_json" in by_kind else "",
        "signals_object_key": by_kind.get("signals_json").object_key if "signals_json" in by_kind else "",
        "stdout_object_key": by_kind.get("stdout_log").object_key if "stdout_log" in by_kind else "",
        "artifacts_dir": str((run_prefix / "artifacts").resolve()) if records else "",
    }


def _load_artifact_payload(
    *,
    store: ExecutionControlStore,
    settings: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    if not run_id:
        return {
            "artifact_count": 0,
            "artifacts": [],
            "artifact_uri_prefix": "",
            "run_object_key": "",
            "steps_object_key": "",
            "signals_object_key": "",
            "stdout_object_key": "",
            "artifacts_dir": "",
        }
    records = store.list_artifacts(run_id=run_id)
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    return _artifact_payload_from_records(
        artifact_root=artifact_root,
        run_id=run_id,
        records=records,
    )


def _sync_controlled_artifacts(
    *,
    store: ExecutionControlStore,
    settings: dict[str, Any],
    snapshot: ControlledExecutionSnapshot,
    result_payload: dict[str, Any],
    error_text: str,
    stdout_path: Path,
) -> dict[str, Any]:
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    run_root = artifact_root / "runs" / snapshot.execution.run_id
    artifacts_root = run_root / "artifacts" / CONTROLLED_STEP_ID
    created_at = time.time()

    step_status = snapshot.execution.status
    run_payload = {
        "run_id": snapshot.execution.run_id,
        "task_name": snapshot.request.task_name,
        "request_id": snapshot.request.request_id,
        "execution_id": snapshot.execution.execution_id,
        "status": step_status,
        "worker_id": snapshot.execution.worker_id,
        "resource_code": snapshot.request.resource_code,
        "summary": snapshot.execution.summary,
        "result": result_payload,
        "error": error_text,
        "created_at": snapshot.execution.created_at,
        "started_at": snapshot.execution.started_at,
        "finished_at": snapshot.execution.finished_at,
    }
    steps_payload = [
        {
            "step_id": CONTROLLED_STEP_ID,
            "status": step_status,
            "started_at": snapshot.execution.started_at,
            "finished_at": snapshot.execution.finished_at,
            "artifacts": {
                "state_dump": str((artifacts_root / "state.json").resolve()),
            },
            "summary": snapshot.execution.summary,
            "error": error_text,
        }
    ]
    signals_payload = [
        {
            "signal_type": "execution.claimed",
            "execution_id": snapshot.execution.execution_id,
            "run_id": snapshot.execution.run_id,
            "at": snapshot.execution.started_at,
        },
        {
            "signal_type": "step.completed" if step_status == "success" else "step.failed",
            "execution_id": snapshot.execution.execution_id,
            "step_id": CONTROLLED_STEP_ID,
            "run_id": snapshot.execution.run_id,
            "at": snapshot.execution.finished_at,
        },
        {
            "signal_type": "execution.completed" if step_status == "success" else "execution.failed",
            "execution_id": snapshot.execution.execution_id,
            "run_id": snapshot.execution.run_id,
            "at": snapshot.execution.finished_at,
        },
    ]
    state_payload = {
        "request": snapshot.request.to_dict(),
        "execution": snapshot.execution.to_dict(),
        "result": result_payload,
        "error": error_text,
    }

    run_file = run_root / "run.json"
    steps_file = run_root / "steps.json"
    signals_file = run_root / "signals.json"
    state_file = artifacts_root / "state.json"

    _write_json_file(run_file, run_payload)
    _write_json_file(steps_file, steps_payload)
    _write_json_file(signals_file, signals_payload)
    _write_json_file(state_file, state_payload)

    records = [
        _build_artifact_record(
            run_id=snapshot.execution.run_id,
            step_id=CONTROLLED_STEP_ID,
            kind="run_json",
            bucket=str(settings["artifact_bucket"]),
            object_key=_runtime_object_key(snapshot.execution.run_id, "run.json"),
            path=run_file,
            created_at=created_at,
        ),
        _build_artifact_record(
            run_id=snapshot.execution.run_id,
            step_id=CONTROLLED_STEP_ID,
            kind="steps_json",
            bucket=str(settings["artifact_bucket"]),
            object_key=_runtime_object_key(snapshot.execution.run_id, "steps.json"),
            path=steps_file,
            created_at=created_at,
        ),
        _build_artifact_record(
            run_id=snapshot.execution.run_id,
            step_id=CONTROLLED_STEP_ID,
            kind="signals_json",
            bucket=str(settings["artifact_bucket"]),
            object_key=_runtime_object_key(snapshot.execution.run_id, "signals.json"),
            path=signals_file,
            created_at=created_at,
        ),
        _build_artifact_record(
            run_id=snapshot.execution.run_id,
            step_id=CONTROLLED_STEP_ID,
            kind="stdout_log",
            bucket=str(settings["artifact_bucket"]),
            object_key=_runtime_object_key(snapshot.execution.run_id, "stdout.log"),
            path=stdout_path,
            created_at=created_at,
        ),
        _build_artifact_record(
            run_id=snapshot.execution.run_id,
            step_id=CONTROLLED_STEP_ID,
            kind="state_json",
            bucket=str(settings["artifact_bucket"]),
            object_key=_runtime_object_key(
                snapshot.execution.run_id,
                f"artifacts/{CONTROLLED_STEP_ID}/state.json",
            ),
            path=state_file,
            created_at=created_at,
        ),
    ]
    store.replace_artifacts(run_id=snapshot.execution.run_id, records=records)
    return _artifact_payload_from_records(
        artifact_root=artifact_root,
        run_id=snapshot.execution.run_id,
        records=records,
    )


class ExecutionDaemon:
    def __init__(
        self,
        *,
        store: ExecutionControlStore,
        worker_id: str,
        lease_seconds: float,
        poll_interval_seconds: float,
        artifact_root: str,
        artifact_bucket: str,
    ):
        self._store = store
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._artifact_root = artifact_root
        self._artifact_bucket = artifact_bucket

    def run_once(self) -> dict[str, Any]:
        snapshot = self._store.claim_next_request(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            task_name=CONTROLLED_TASK_NAME,
        )
        if snapshot is None:
            return {
                "daemon_status": "idle",
                "message": "No queued controlled execution is ready to run.",
                "worker_id": self._worker_id,
                "processed_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "idle_cycles": 1,
                "processed_executions": [],
                "last_execution": {},
            }
        finished_snapshot, artifact_payload = _run_controlled_snapshot(
            store=self._store,
            snapshot=snapshot,
            settings={
                "lease_seconds": self._lease_seconds,
                "heartbeat_interval_seconds": min(
                    self._poll_interval_seconds,
                    self._lease_seconds,
                ),
                "artifact_root": self._artifact_root,
                "artifact_bucket": self._artifact_bucket,
            },
        )
        execution_payload = _snapshot_to_payload(
            finished_snapshot,
            control_action="daemon_once",
            message="Queued controlled execution finished.",
            artifact_payload=artifact_payload,
        )
        is_success = execution_payload["execution_status"] == "success"
        return {
            "daemon_status": "processed",
            "message": "Executor daemon processed one queued controlled execution.",
            "worker_id": self._worker_id,
            "processed_count": 1,
            "success_count": 1 if is_success else 0,
            "failed_count": 0 if is_success else 1,
            "idle_cycles": 0,
            "processed_executions": [execution_payload],
            "last_execution": execution_payload,
        }

    def run_loop(
        self,
        *,
        stop_when_idle: bool,
        max_iterations: int,
        max_idle_cycles: int,
    ) -> dict[str, Any]:
        processed_executions: list[dict[str, Any]] = []
        success_count = 0
        failed_count = 0
        idle_cycles = 0
        iterations = 0

        while True:
            iteration_payload = self.run_once()
            iterations += 1
            if iteration_payload["daemon_status"] == "processed":
                idle_cycles = 0
                processed_executions.extend(iteration_payload["processed_executions"])
                success_count += int(iteration_payload["success_count"])
                failed_count += int(iteration_payload["failed_count"])
            else:
                idle_cycles += 1

            if max_iterations > 0 and iterations >= max_iterations:
                break
            if stop_when_idle and idle_cycles >= max(max_idle_cycles, 1):
                break
            if iteration_payload["daemon_status"] != "processed":
                time.sleep(self._poll_interval_seconds)

        last_execution = processed_executions[-1] if processed_executions else {}
        if processed_executions:
            message = "Executor daemon stopped after draining available queued executions."
            daemon_status = "completed"
        else:
            message = "Executor daemon exited without finding queued controlled executions."
            daemon_status = "idle"
        return {
            "daemon_status": daemon_status,
            "message": message,
            "worker_id": self._worker_id,
            "processed_count": len(processed_executions),
            "success_count": success_count,
            "failed_count": failed_count,
            "idle_cycles": idle_cycles,
            "iterations": iterations,
            "processed_executions": processed_executions,
            "last_execution": last_execution,
        }


def _snapshot_to_payload(
    snapshot: ControlledExecutionSnapshot,
    *,
    control_action: str,
    message: str = "",
    artifact_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = snapshot.to_dict()
    payload.update(
        {
            "control_action": control_action,
            "message": message,
            "task_name": snapshot.request.task_name,
            "resource_code": snapshot.request.resource_code,
            "request_id": snapshot.request.request_id,
            "execution_id": snapshot.execution.execution_id,
            "request_status": snapshot.request.status,
            "execution_status": snapshot.execution.status,
            "run_id": snapshot.execution.run_id,
            "queue_seq": snapshot.execution.queue_seq,
            "queue_position": snapshot.queue_position,
            "worker_id": snapshot.execution.worker_id,
            "summary": snapshot.execution.summary,
            "result": snapshot.execution.result,
            "error": snapshot.execution.error_text,
        }
    )
    if artifact_payload:
        payload.update(artifact_payload)
    return payload


def _ensure_controlled_workflow_contract(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    summary = normalized.get("summary")
    if not isinstance(summary, dict) or "total" not in summary:
        counts: dict[str, int] = {}
        if isinstance(summary, dict):
            existing_counts = summary.get("counts")
            if isinstance(existing_counts, dict):
                counts.update(
                    {
                        str(key): int(value)
                        for key, value in existing_counts.items()
                        if isinstance(value, int | float)
                    }
                )
        execution_status = str(normalized.get("execution_status", "") or "").strip()
        daemon_status = str(normalized.get("daemon_status", "") or "").strip()
        processed_count = max(int(normalized.get("processed_count", 0) or 0), 0)
        total = processed_count
        if execution_status and execution_status != "idle":
            counts.setdefault(execution_status, 1)
            total = max(total, 1)
        elif daemon_status:
            counts.setdefault(daemon_status, processed_count)
        normalized["summary"] = {
            "total": total,
            "counts": counts,
        }
    result_payload = normalized.get("result")
    if "item" not in normalized:
        normalized["item"] = result_payload.get("item", {}) if isinstance(result_payload, dict) else {}
    if "items" not in normalized:
        normalized["items"] = (
            result_payload.get("items", [])
            if isinstance(result_payload, dict) and isinstance(result_payload.get("items"), list)
            else []
        )
    return normalized


def submit_controlled_feishu_single_row_update(params: dict[str, Any]) -> dict[str, Any]:
    settings = _controlled_settings(params)
    store = _create_execution_control_store(settings)
    idempotency_key = str(params.get("idempotency_key", "") or "").strip()
    snapshot = store.submit_request(
        task_name=CONTROLLED_TASK_NAME,
        payload=_sanitize_controlled_params(params),
        resource_code=build_controlled_resource_code(params),
        requested_by=settings["requested_by"],
        idempotency_key=idempotency_key,
    )
    return _snapshot_to_payload(
        snapshot,
        control_action="submit",
        message="Request accepted and queued for controlled execution.",
        artifact_payload=_load_artifact_payload(
            store=store,
            settings=settings,
            run_id=snapshot.execution.run_id,
        ),
    )


def get_controlled_feishu_single_row_update_status(params: dict[str, Any]) -> dict[str, Any]:
    settings = _controlled_settings(params)
    store = _create_execution_control_store(settings)
    snapshot = store.load_snapshot(
        request_id=str(params.get("request_id", "") or ""),
        execution_id=str(params.get("execution_id", "") or ""),
    )
    return _snapshot_to_payload(
        snapshot,
        control_action="status",
        message="Loaded controlled execution status.",
        artifact_payload=_load_artifact_payload(
            store=store,
            settings=settings,
            run_id=snapshot.execution.run_id,
        ),
    )


def _run_controlled_snapshot(
    *,
    store: ExecutionControlStore,
    snapshot: ControlledExecutionSnapshot,
    settings: dict[str, Any],
) -> tuple[ControlledExecutionSnapshot, dict[str, Any]]:
    run_id = str(snapshot.execution.run_id or f"managed-{snapshot.execution.execution_id}")
    artifact_root = Path(str(settings["artifact_root"])).expanduser()
    stdout_path = artifact_root / "runs" / run_id / "stdout.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    result_payload: dict[str, Any] = {}
    error_text = ""
    finalized_snapshot: ControlledExecutionSnapshot
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_handle:
            tee_stdout = TeeStream(sys.stdout, stdout_handle)
            tee_stderr = TeeStream(sys.stderr, stdout_handle)
            with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
                print(
                    f"[execution-control] execution_id={snapshot.execution.execution_id} "
                    f"run_id={run_id} status=running"
                )
                with LeaseHeartbeat(
                    store=store,
                    execution_id=snapshot.execution.execution_id,
                    lease_seconds=float(settings["lease_seconds"]),
                    interval_seconds=float(
                        min(settings["heartbeat_interval_seconds"], settings["lease_seconds"])
                    ),
                ):
                    result = run_feishu_single_row_update(dict(snapshot.request.payload))
                result_payload = result if isinstance(result, dict) else {}
                summary = result_payload.get("summary", {})
                finalized_snapshot = store.mark_success(
                    execution_id=snapshot.execution.execution_id,
                    result=result_payload,
                    summary=summary if isinstance(summary, dict) else {},
                    run_id=run_id,
                )
                print(
                    f"[execution-control] execution_id={snapshot.execution.execution_id} "
                    f"run_id={run_id} status=success"
                )
    except Exception as exc:
        error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        result_payload = {"error": error_text}
        finalized_snapshot = store.mark_failed(
            execution_id=snapshot.execution.execution_id,
            error_text=error_text,
            run_id=run_id,
        )
        with stdout_path.open("a", encoding="utf-8") as stdout_handle:
            stdout_handle.write(f"\n[execution-control] execution failed\n{error_text}\n")
    artifact_payload = _sync_controlled_artifacts(
        store=store,
        settings=settings,
        snapshot=finalized_snapshot,
        result_payload=result_payload,
        error_text=error_text,
        stdout_path=stdout_path,
    )
    return finalized_snapshot, artifact_payload


def execute_next_controlled_feishu_single_row_update(params: dict[str, Any]) -> dict[str, Any]:
    settings = _controlled_settings(params)
    store = _create_execution_control_store(settings)
    daemon = ExecutionDaemon(
        store=store,
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
        poll_interval_seconds=float(settings["poll_interval_seconds"]),
        artifact_root=str(settings["artifact_root"]),
        artifact_bucket=str(settings["artifact_bucket"]),
    )
    daemon_payload = daemon.run_once()
    if daemon_payload["processed_executions"]:
        payload = dict(daemon_payload["last_execution"])
        payload.update(
            {
                "daemon_status": daemon_payload["daemon_status"],
                "processed_count": daemon_payload["processed_count"],
                "success_count": daemon_payload["success_count"],
                "failed_count": daemon_payload["failed_count"],
                "idle_cycles": daemon_payload["idle_cycles"],
                "processed_executions": daemon_payload["processed_executions"],
                "last_execution": daemon_payload["last_execution"],
                "worker_id": daemon_payload["worker_id"],
                "message": daemon_payload["message"],
                "control_action": "execute_next",
            }
        )
        return payload
    return {
        "control_action": "execute_next",
        "message": daemon_payload["message"],
        "task_name": CONTROLLED_TASK_NAME,
        "request_id": "",
        "execution_id": "",
        "request_status": "idle",
        "execution_status": "idle",
        "queue_position": 0,
        "resource_code": "",
        "run_id": "",
        "summary": {},
        "result": {},
        "error": "",
        "request": {},
        "execution": {},
        "lease": {},
        "daemon_status": daemon_payload["daemon_status"],
        "processed_count": daemon_payload["processed_count"],
        "success_count": daemon_payload["success_count"],
        "failed_count": daemon_payload["failed_count"],
        "idle_cycles": daemon_payload["idle_cycles"],
        "processed_executions": daemon_payload["processed_executions"],
        "last_execution": daemon_payload["last_execution"],
        "worker_id": daemon_payload["worker_id"],
        "artifact_count": 0,
        "artifacts": [],
        "artifact_uri_prefix": "",
        "run_object_key": "",
        "steps_object_key": "",
        "signals_object_key": "",
        "stdout_object_key": "",
        "artifacts_dir": "",
    }


def run_controlled_executor_daemon(params: dict[str, Any]) -> dict[str, Any]:
    settings = _controlled_settings(params)
    store = _create_execution_control_store(settings)
    daemon = ExecutionDaemon(
        store=store,
        worker_id=str(settings["worker_id"]),
        lease_seconds=float(settings["lease_seconds"]),
        poll_interval_seconds=float(settings["poll_interval_seconds"]),
        artifact_root=str(settings["artifact_root"]),
        artifact_bucket=str(settings["artifact_bucket"]),
    )
    payload = daemon.run_loop(
        stop_when_idle=_read_bool_param(params, "execution_control_stop_when_idle", False),
        max_iterations=max(_read_int_param(params, "execution_control_max_iterations", 0), 0),
        max_idle_cycles=max(_read_int_param(params, "execution_control_max_idle_cycles", 1), 1),
    )
    payload["control_action"] = "daemon_loop"
    return payload


def run_controlled_feishu_single_row_update(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("control_action", "run") or "run").strip().lower()
    if action == "submit":
        return _ensure_controlled_workflow_contract(
            submit_controlled_feishu_single_row_update(params)
        )
    if action in {"status", "result"}:
        status_payload = get_controlled_feishu_single_row_update_status(params)
        status_payload["control_action"] = action
        if action == "result":
            status_payload["message"] = "Loaded controlled execution result."
        return _ensure_controlled_workflow_contract(status_payload)
    if action == "daemon_once":
        payload = execute_next_controlled_feishu_single_row_update(params)
        payload["control_action"] = "daemon_once"
        return _ensure_controlled_workflow_contract(payload)
    if action == "daemon_loop":
        return _ensure_controlled_workflow_contract(run_controlled_executor_daemon(params))
    if action == "execute_next":
        return _ensure_controlled_workflow_contract(
            execute_next_controlled_feishu_single_row_update(params)
        )
    if action != "run":
        raise ValueError(f"Unsupported control_action '{action}'.")

    settings = _controlled_settings(params)
    store = _create_execution_control_store(settings)
    submitted_payload = submit_controlled_feishu_single_row_update(params)
    request_id = str(submitted_payload["request_id"])
    deadline = time.monotonic() + float(settings["wait_timeout_seconds"])

    while True:
        snapshot = store.load_snapshot(request_id=request_id)
        if snapshot.execution.status in TERMINAL_STATUSES:
            return _ensure_controlled_workflow_contract(
                _snapshot_to_payload(
                    snapshot,
                    control_action="run",
                    message="Controlled execution finished.",
                    artifact_payload=_load_artifact_payload(
                        store=store,
                        settings=settings,
                        run_id=snapshot.execution.run_id,
                    ),
                )
            )
        claimed_snapshot = store.claim_request(
            request_id=request_id,
            worker_id=settings["worker_id"],
            lease_seconds=float(settings["lease_seconds"]),
            run_id=str(params.get("execution_run_id", "") or ""),
        )
        if claimed_snapshot is not None:
            finished_snapshot, artifact_payload = _run_controlled_snapshot(
                store=store,
                snapshot=claimed_snapshot,
                settings=settings,
            )
            return _ensure_controlled_workflow_contract(
                _snapshot_to_payload(
                    finished_snapshot,
                    control_action="run",
                    message="Controlled execution finished.",
                    artifact_payload=artifact_payload,
                )
            )
        if time.monotonic() >= deadline:
            timeout_snapshot = store.load_snapshot(request_id=request_id)
            payload = _ensure_controlled_workflow_contract(
                _snapshot_to_payload(
                    timeout_snapshot,
                    control_action="run",
                    message="Controlled execution is still queued or running.",
                    artifact_payload=_load_artifact_payload(
                        store=store,
                        settings=settings,
                        run_id=timeout_snapshot.execution.run_id,
                    ),
                )
            )
            payload["wait_timed_out"] = True
            return payload
        time.sleep(float(settings["poll_interval_seconds"]))
