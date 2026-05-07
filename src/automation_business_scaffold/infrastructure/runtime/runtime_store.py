"""Execution control and TK fact database runtime store."""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from contextlib import contextmanager
from typing import Any, Mapping

from automation_business_scaffold.models.artifact_object import ArtifactObjectRecord
from automation_business_scaffold.infrastructure.runtime.runtime_records import (
    NotificationOutboxRecord,
    ResourceLeaseRecord,
    RuntimeTaskExecutionRecord,
    RuntimeTaskRequestRecord,
)
from automation_business_scaffold.infrastructure.runtime.bootstrap import bootstrap_runtime_schema
from automation_business_scaffold.infrastructure.runtime.schema_version import (
    missing_runtime_schema_message,
)
from automation_business_scaffold.infrastructure.runtime.queries import DbHealthQuery, RequestStatusQuery, WatchdogQuery
from automation_business_scaffold.infrastructure.runtime.repositories import (
    ApiWorkerJobRepository,
    ArtifactObjectRepository,
    InfluencerPoolJobRepository,
    NotificationOutboxRepository,
    ResourceLeaseRepository,
    TaskExecutionRepository,
    TaskRequestRepository,
)


ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_EXECUTION_STATUSES = {"success", "failed", "skipped", "cancelled"}
ACTIVE_API_WORKER_JOB_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_API_WORKER_JOB_STATUSES = {"success", "failed", "skipped", "cancelled"}
TERMINAL_REQUEST_STATUSES = {"success", "failed", "cancelled"}
DEFAULT_ACTIVE_REQUEST_SCAN_STATUSES = ("running", "waiting_children")
DEFAULT_ACTIVE_JOB_SCAN_STATUSES = ("running",)
DEFAULT_OUTBOX_SCAN_STATUSES = ("sending",)
DEFAULT_WATCHDOG_STALE_AFTER_SECONDS = 300.0


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


def _load_json_list(raw_value: str | None) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_non_negative_float(value: Any) -> float:
    return max(_coerce_float(value), 0.0)


def _resolve_runtime_seconds(
    row_value: Any,
    payload: Mapping[str, Any],
    *payload_keys: str,
    default: float = 0.0,
) -> float:
    row_seconds = _coerce_non_negative_float(row_value)
    if row_seconds > 0:
        return row_seconds
    for key in payload_keys:
        payload_seconds = _coerce_non_negative_float(payload.get(key))
        if payload_seconds > 0:
            return payload_seconds
    return _coerce_non_negative_float(default)


def _build_bind_placeholders(prefix: str, values: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    placeholders: list[str] = []
    params: dict[str, Any] = {}
    for index, value in enumerate(values):
        key = f"{prefix}_{index}"
        placeholders.append(f":{key}")
        params[key] = value
    return ", ".join(placeholders), params


def _postgres_advisory_lock_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    raw_value = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if raw_value >= 2**63:
        raw_value -= 2**64
    return raw_value


class RuntimeStore:
    def __init__(self, *, db_url: str = ""):
        try:
            from sqlalchemy import create_engine, text
            from sqlalchemy.pool import NullPool
        except ModuleNotFoundError as exc:
            raise RuntimeError("RuntimeStore requires SQLAlchemy.") from exc

        resolved_db_url = str(db_url or "").strip()
        if not resolved_db_url:
            raise RuntimeError(
                "RuntimeStore requires BUSINESS_EXECUTION_CONTROL_DB_URL / "
                "EXECUTION_CONTROL_DB_URL. Fill scripts/execution_control/executor.local.env "
                "or skills/mujitask-tiktok-feishu-sync/skill.local.env, or pass "
                "execution_control_db_url explicitly. SQLite/db_path fallback has been removed."
            )
        if resolved_db_url.lower().startswith("sqlite"):
            raise RuntimeError("SQLite is no longer supported for RuntimeStore; use Postgres.")
        self._db_url = resolved_db_url
        self._text = text
        self._engine = create_engine(
            self._db_url,
            future=True,
            pool_pre_ping=True,
            poolclass=NullPool,
        )
        self._request_status_query = RequestStatusQuery(self)
        self._watchdog_query = WatchdogQuery(self)
        self._db_health_query = DbHealthQuery(self)
        self._task_request_repo = TaskRequestRepository(self)
        self._api_worker_job_repo = ApiWorkerJobRepository(self)
        self._task_execution_repo = TaskExecutionRepository(self)
        self._notification_outbox_repo = NotificationOutboxRepository(self)
        self._resource_lease_repo = ResourceLeaseRepository(self)
        self._artifact_object_repo = ArtifactObjectRepository(self)
        self._influencer_pool_job_repo = InfluencerPoolJobRepository(self)

    def bootstrap_schema(self) -> None:
        bootstrap_runtime_schema(self._engine)

    def _ensure_runtime_schema_ready(self) -> None:
        with self._engine.connect() as connection:
            task_request_table = connection.execute(
                self._text("SELECT to_regclass('task_request')")
            ).scalar_one_or_none()
        if not task_request_table:
            raise RuntimeError(missing_runtime_schema_message())

    def collect_db_connection_health(
        self,
        *,
        max_connection_ratio: float = 0.8,
        max_idle_in_transaction: int = -1,
    ) -> dict[str, Any]:
        return self._db_health_query.collect_db_connection_health(max_connection_ratio=max_connection_ratio, max_idle_in_transaction=max_idle_in_transaction)

    def _request_from_row(self, row: Mapping[str, Any]) -> RuntimeTaskRequestRecord:
        return RuntimeTaskRequestRecord(
            request_id=str(row["request_id"]),
            project_code=str(row["project_code"]),
            task_code=str(row["task_code"] or row["task_name"] or ""),
            status=str(row["status"]),
            payload=_load_json_dict(row["payload_json"]),
            current_stage=str(row["current_stage"] or ""),
            progress_stage=str(row.get("progress_stage", "") or ""),
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
            error_type=str(row.get("error_type", "") or ""),
            error_code=str(row.get("error_code", "") or ""),
            dead_letter_reason=str(row.get("dead_letter_reason", "") or ""),
            child_total_count=int(row["child_total_count"] or 0),
            child_terminal_count=int(row["child_terminal_count"] or 0),
            child_success_count=int(row["child_success_count"] or 0),
            child_failed_count=int(row["child_failed_count"] or 0),
            child_skipped_count=int(row["child_skipped_count"] or 0),
            worker_id=str(row.get("worker_id", "") or ""),
            lease_until=_coerce_float(row.get("lease_until")),
            heartbeat_at=_coerce_float(row.get("heartbeat_at")),
            last_progress_at=_coerce_float(row.get("last_progress_at")),
            max_execution_seconds=_coerce_non_negative_float(row.get("max_execution_seconds")),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
            started_at=_coerce_float(row["started_at"]),
            finished_at=_coerce_float(row["finished_at"]),
        )

    def _execution_from_row(self, row: Mapping[str, Any]) -> RuntimeTaskExecutionRecord:
        return RuntimeTaskExecutionRecord(
            execution_id=str(row["execution_id"]),
            request_id=str(row["request_id"]),
            item_code=str(row["item_code"] or row["task_name"] or ""),
            workflow_code=str(row["workflow_code"] or ""),
            business_key=str(row["business_key"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            resource_code=str(row["resource_code"] or ""),
            status=str(row["status"]),
            queue_seq=int(row["queue_seq"]),
            progress_stage=str(row.get("progress_stage", "") or ""),
            available_at=_coerce_float(row["available_at"]),
            worker_id=str(row["worker_id"] or ""),
            worker_pid=_coerce_int(row.get("worker_pid")),
            attempt_count=int(row["attempt_count"] or 0),
            max_attempts=int(row["max_attempts"] or 0),
            payload=_load_json_dict(row["payload_json"]),
            summary=_load_json_dict(row["summary_json"]),
            result=_load_json_dict(row["result_json"]),
            error_text=str(row["error_text"] or ""),
            error_type=str(row.get("error_type", "") or ""),
            error_code=str(row.get("error_code", "") or ""),
            dead_letter_reason=str(row.get("dead_letter_reason", "") or ""),
            run_id=str(row["run_id"] or ""),
            created_at=_coerce_float(row["created_at"]),
            updated_at=_coerce_float(row["updated_at"]),
            started_at=_coerce_float(row["started_at"]),
            finished_at=_coerce_float(row["finished_at"]),
            heartbeat_at=_coerce_float(row["heartbeat_at"]),
            last_progress_at=_coerce_float(row.get("last_progress_at")),
            max_execution_seconds=_coerce_non_negative_float(row.get("max_execution_seconds")),
            max_idle_seconds=_coerce_non_negative_float(row.get("max_idle_seconds")),
            heartbeat_timeout_seconds=_coerce_non_negative_float(row.get("heartbeat_timeout_seconds")),
            progress_seq=_coerce_int(row.get("progress_seq")),
            progress_message=str(row.get("progress_message", "") or ""),
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
            progress_stage=str(row.get("progress_stage", "") or ""),
            payload=_load_json_dict(row["payload_json"]),
            reply_target=str(row["reply_target"] or ""),
            dedupe_key=str(row["dedupe_key"] or ""),
            retry_count=int(row["retry_count"] or 0),
            max_retry_count=int(row["max_retry_count"] or 0),
            next_retry_at=_coerce_float(row["next_retry_at"]),
            worker_id=str(row.get("worker_id", "") or ""),
            lease_until=_coerce_float(row.get("lease_until")),
            heartbeat_at=_coerce_float(row.get("heartbeat_at")),
            last_error_text=str(row["last_error_text"] or ""),
            error_type=str(row.get("error_type", "") or ""),
            error_code=str(row.get("error_code", "") or ""),
            dead_letter_reason=str(row.get("dead_letter_reason", "") or ""),
            sent_at=_coerce_float(row["sent_at"]),
            last_progress_at=_coerce_float(row.get("last_progress_at")),
            max_execution_seconds=_coerce_non_negative_float(row.get("max_execution_seconds")),
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

    def _api_worker_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row["job_id"]),
            "request_id": str(row["request_id"] or ""),
            "task_code": str(row["task_code"] or ""),
            "job_code": str(row["job_code"] or ""),
            "business_key": str(row["business_key"] or ""),
            "dedupe_key": str(row["dedupe_key"] or ""),
            "status": str(row["status"] or ""),
            "stage": str(row["stage"] or ""),
            "progress_stage": str(row.get("progress_stage", "") or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "max_execution_seconds": _coerce_non_negative_float(row.get("max_execution_seconds")),
            "payload": _load_json_dict(row["payload_json"]),
            "summary": _load_json_dict(row["summary_json"]),
            "result": _load_json_dict(row["result_json"]),
            "error_text": str(row["error_text"] or ""),
            "error_type": str(row.get("error_type", "") or ""),
            "error_code": str(row.get("error_code", "") or ""),
            "dead_letter_reason": str(row.get("dead_letter_reason", "") or ""),
            "worker_id": str(row.get("worker_id", "") or ""),
            "worker_pid": _coerce_int(row.get("worker_pid")),
            "lease_until": _coerce_float(row.get("lease_until")),
            "available_at": _coerce_float(row["available_at"]),
            "run_id": str(row["run_id"] or ""),
            "max_idle_seconds": _coerce_non_negative_float(row.get("max_idle_seconds")),
            "heartbeat_timeout_seconds": _coerce_non_negative_float(row.get("heartbeat_timeout_seconds")),
            "created_at": _coerce_float(row["created_at"]),
            "updated_at": _coerce_float(row["updated_at"]),
            "started_at": _coerce_float(row["started_at"]),
            "finished_at": _coerce_float(row["finished_at"]),
            "heartbeat_at": _coerce_float(row["heartbeat_at"]),
            "last_progress_at": _coerce_float(row.get("last_progress_at")),
            "progress_seq": _coerce_int(row.get("progress_seq")),
            "progress_message": str(row.get("progress_message", "") or ""),
        }

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
        max_execution_seconds: float = 0.0,
    ) -> RuntimeTaskRequestRecord:
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
                        payload_json, idempotency_key, status, current_stage, progress_stage, stage_cursor_json,
                        summary_json, result_json, error_text, error_type, error_code, dead_letter_reason,
                        child_total_count, child_terminal_count, child_success_count,
                        child_failed_count, child_skipped_count,
                        requested_by, created_at, updated_at, started_at, finished_at,
                        last_progress_at, max_execution_seconds
                    ) VALUES (
                        :request_id, :project_code, '', :task_name, :task_code, '',
                        :trigger_mode, :source_channel_code, :source_session_id, :reply_target,
                        :payload_json, :idempotency_key, 'pending', '', 'submitted', '{}',
                        '{}', '{}', '', '', '', '',
                        0, 0, 0, 0, 0,
                        :requested_by, :created_at, :updated_at, NULL, NULL,
                        :last_progress_at, :max_execution_seconds
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
                    "last_progress_at": now,
                    "max_execution_seconds": _coerce_non_negative_float(max_execution_seconds),
                },
            )
        return self.load_task_request(request_id=request_id)

    def load_task_request(self, *, request_id: str) -> RuntimeTaskRequestRecord:
        return self._task_request_repo.load(request_id=request_id)

    def list_task_executions(self, *, request_id: str) -> list[RuntimeTaskExecutionRecord]:
        return self._request_status_query.list_task_executions(request_id=request_id)

    def list_request_outbox(self, *, request_id: str) -> list[NotificationOutboxRecord]:
        return self._request_status_query.list_request_outbox(request_id=request_id)

    def list_artifacts(self, *, run_id: str) -> list[ArtifactObjectRecord]:
        return self._request_status_query.list_artifacts(run_id=run_id)

    def _requeue_expired_task_request_claims(self, connection: Any, *, now: float) -> None:
        expired_rows = (
            connection.execute(
                self._text(
                    """
                    SELECT request_id, current_stage
                    FROM task_request
                    WHERE status = 'running'
                      AND COALESCE(lease_until, 0) <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in expired_rows:
            request_id = str(row["request_id"])
            current_stage = str(row["current_stage"] or "").strip()
            reset_status = "ready_for_summary" if current_stage == "ready_for_summary" else "pending"
            reset_stage = "ready_for_summary" if current_stage == "ready_for_summary" else ""
            reset_cursor = None if current_stage == "ready_for_summary" else "{}"
            values: dict[str, Any] = {
                "request_id": request_id,
                "status": reset_status,
                "current_stage": reset_stage,
                "progress_stage": reset_stage,
                "last_progress_at": now,
                "updated_at": now,
            }
            assignments = [
                "status = :status",
                "current_stage = :current_stage",
                "progress_stage = :progress_stage",
                "last_progress_at = :last_progress_at",
                "updated_at = :updated_at",
                "worker_id = ''",
                "lease_until = NULL",
                "heartbeat_at = NULL",
            ]
            if reset_cursor is not None:
                assignments.append("stage_cursor_json = :stage_cursor_json")
                values["stage_cursor_json"] = reset_cursor
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

    def claim_next_task_request(self, *, worker_id: str, lease_seconds: float) -> RuntimeTaskRequestRecord | None:
        self._ensure_runtime_schema_ready()
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_task_request_claims(connection, now=now)
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
            request = self._request_from_row(row)
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET status = 'running',
                        progress_stage = CASE
                            WHEN current_stage <> '' THEN current_stage
                            ELSE 'claimed'
                        END,
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        updated_at = :updated_at,
                        started_at = CASE WHEN started_at IS NULL THEN :updated_at ELSE started_at END
                    WHERE request_id = :request_id
                    """
                ),
                {
                    "request_id": request.request_id,
                    "worker_id": worker_id,
                    "lease_until": now + lease_seconds,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
            updated_row = (
                connection.execute(
                    self._text("SELECT * FROM task_request WHERE request_id = :request_id LIMIT 1"),
                    {"request_id": request.request_id},
                )
                .mappings()
                .first()
            )
            if updated_row is None:
                return None
            return self._request_from_row(updated_row)

    def update_task_request(
        self,
        *,
        request_id: str,
        status: str | None = None,
        current_stage: str | None = None,
        progress_stage: str | None = None,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        stage_cursor: dict[str, Any] | None = None,
        error_text: str | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        dead_letter_reason: str | None = None,
        child_total_count: int | None = None,
        child_terminal_count: int | None = None,
        child_success_count: int | None = None,
        child_failed_count: int | None = None,
        child_skipped_count: int | None = None,
        worker_id: str | None = None,
        lease_until: float | None = None,
        heartbeat_at: float | None = None,
        last_progress_at: float | None = None,
        max_execution_seconds: float | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> RuntimeTaskRequestRecord:
        assignments = ["updated_at = :updated_at"]
        values: dict[str, Any] = {"request_id": request_id, "updated_at": time.time()}
        if status is not None:
            assignments.append("status = :status")
            values["status"] = status
        if current_stage is not None:
            assignments.append("current_stage = :current_stage")
            values["current_stage"] = current_stage
        if progress_stage is not None:
            assignments.append("progress_stage = :progress_stage")
            values["progress_stage"] = progress_stage
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
        if error_type is not None:
            assignments.append("error_type = :error_type")
            values["error_type"] = error_type
        if error_code is not None:
            assignments.append("error_code = :error_code")
            values["error_code"] = error_code
        if dead_letter_reason is not None:
            assignments.append("dead_letter_reason = :dead_letter_reason")
            values["dead_letter_reason"] = dead_letter_reason
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
        if worker_id is not None:
            assignments.append("worker_id = :worker_id")
            values["worker_id"] = worker_id
        if lease_until is not None:
            assignments.append("lease_until = :lease_until")
            values["lease_until"] = lease_until
        if heartbeat_at is not None:
            assignments.append("heartbeat_at = :heartbeat_at")
            values["heartbeat_at"] = heartbeat_at
        if last_progress_at is not None:
            assignments.append("last_progress_at = :last_progress_at")
            values["last_progress_at"] = last_progress_at
        if max_execution_seconds is not None:
            assignments.append("max_execution_seconds = :max_execution_seconds")
            values["max_execution_seconds"] = _coerce_non_negative_float(max_execution_seconds)
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

    def update_task_request_progress(
        self,
        *,
        request_id: str,
        progress_stage: str,
        lease_seconds: float | None = None,
    ) -> RuntimeTaskRequestRecord:
        now = time.time()
        lease_until = None
        heartbeat_at = None
        if lease_seconds is not None:
            heartbeat_at = now
            lease_until = now + max(lease_seconds, 0.1)
        return self.update_task_request(
            request_id=request_id,
            progress_stage=progress_stage,
            last_progress_at=now,
            heartbeat_at=heartbeat_at,
            lease_until=lease_until,
        )

    def heartbeat_task_request(self, *, request_id: str, lease_seconds: float) -> None:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE task_request
                    SET heartbeat_at = :heartbeat_at,
                        lease_until = :lease_until,
                        updated_at = :updated_at
                    WHERE request_id = :request_id
                      AND status = 'running'
                    """
                ),
                {
                    "request_id": request_id,
                    "heartbeat_at": now,
                    "lease_until": now + lease_seconds,
                    "updated_at": now,
                },
            )

    def enqueue_task_executions(
        self,
        *,
        request_id: str,
        item_code: str,
        workflow_code: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._task_execution_repo.enqueue_task_executions(request_id=request_id, item_code=item_code, workflow_code=workflow_code, items=items)

    def enqueue_api_worker_jobs(
        self,
        *,
        request_id: str,
        task_code: str,
        job_code: str,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._api_worker_job_repo.enqueue_api_worker_jobs(request_id=request_id, task_code=task_code, job_code=job_code, jobs=jobs, force_refresh=force_refresh)

    def _requeue_expired_api_worker_job_claims(self, connection: Any, *, now: float) -> None:
        return self._api_worker_job_repo._requeue_expired_api_worker_job_claims(connection, now=now)

    def claim_next_api_worker_job(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        job_code: str = "",
    ) -> dict[str, Any] | None:
        self._ensure_runtime_schema_ready()
        return self._api_worker_job_repo.claim_next_api_worker_job(worker_id=worker_id, worker_pid=worker_pid, lease_seconds=lease_seconds, request_id=request_id, job_code=job_code)

    def heartbeat_api_worker_job(self, *, job_id: str, run_id: str, lease_seconds: float) -> bool:
        return self._api_worker_job_repo.heartbeat_api_worker_job(job_id=job_id, run_id=run_id, lease_seconds=lease_seconds)

    def update_api_worker_job_progress(
        self,
        *,
        job_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._api_worker_job_repo.update_api_worker_job_progress(job_id=job_id, run_id=run_id, progress_stage=progress_stage, message=message, lease_seconds=lease_seconds)

    def mark_api_worker_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        stage: str = "completed",
    ) -> dict[str, Any]:
        return self._api_worker_job_repo.mark_api_worker_job_success(job_id=job_id, run_id=run_id, summary=summary, result=result, stage=stage)

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
        return self._api_worker_job_repo.mark_api_worker_job_retry_or_failed(job_id=job_id, run_id=run_id, error_text=error_text, summary=summary, result=result, retry_delay_seconds=retry_delay_seconds, error_type=error_type, error_code=error_code, dead_letter_reason=dead_letter_reason)

    def load_api_worker_job(self, *, job_id: str) -> dict[str, Any]:
        return self._api_worker_job_repo.load_api_worker_job(job_id=job_id)

    def list_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> list[dict[str, Any]]:
        return self._api_worker_job_repo.list_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)

    def summarize_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> dict[str, Any]:
        return self._api_worker_job_repo.summarize_api_worker_jobs_for_request(request_id=request_id, job_code=job_code)

    @contextmanager
    def fastmoss_cookie_cache_lock(self, *, cache_key: str) -> Any:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            raise ValueError("cache_key is required for FastMoss cookie cache lock.")
        lock_key = _postgres_advisory_lock_key(f"fastmoss_cookie_cache:{normalized_cache_key}")
        with self._engine.begin() as connection:
            connection.execute(
                self._text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            yield

    def load_fastmoss_cookie_cache(self, *, cache_key: str) -> dict[str, Any] | None:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            return None
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM fastmoss_session_cookie_cache
                        WHERE cache_key = :cache_key
                        LIMIT 1
                        """
                    ),
                    {"cache_key": normalized_cache_key},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {
            "cache_key": str(row["cache_key"] or ""),
            "namespace": str(row["namespace"] or ""),
            "account_key": str(row["account_key"] or ""),
            "base_url": str(row["base_url"] or ""),
            "region": str(row["region"] or ""),
            "cookies": _load_json_list(row.get("cookies_json")),
            "cookie_count": int(row["cookie_count"] or 0),
            "has_fd_tk": bool(int(row["has_fd_tk"] or 0)),
            "fd_tk_digest": str(row["fd_tk_digest"] or ""),
            "expires_at": _coerce_float(row.get("expires_at")),
            "last_auth_failed_at": _coerce_float(row.get("last_auth_failed_at")),
            "last_login_at": _coerce_float(row.get("last_login_at")),
            "created_at": _coerce_float(row["created_at"]),
            "updated_at": _coerce_float(row["updated_at"]),
        }

    def save_fastmoss_cookie_cache(
        self,
        *,
        cache_key: str,
        namespace: str = "",
        account_key: str,
        base_url: str,
        region: str,
        cookies: list[dict[str, Any]],
        cookie_count: int,
        has_fd_tk: bool,
        fd_tk_digest: str,
        expires_at: float | None,
        last_login_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            raise ValueError("cache_key is required for FastMoss cookie cache.")
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    INSERT INTO fastmoss_session_cookie_cache (
                        cache_key, namespace, account_key, base_url, region,
                        cookies_json, cookie_count, has_fd_tk, fd_tk_digest,
                        expires_at, last_auth_failed_at, last_login_at, created_at, updated_at
                    )
                    VALUES (
                        :cache_key, :namespace, :account_key, :base_url, :region,
                        :cookies_json, :cookie_count, :has_fd_tk, :fd_tk_digest,
                        :expires_at, NULL, :last_login_at, :created_at, :updated_at
                    )
                    ON CONFLICT (cache_key) DO UPDATE SET
                        namespace = EXCLUDED.namespace,
                        account_key = EXCLUDED.account_key,
                        base_url = EXCLUDED.base_url,
                        region = EXCLUDED.region,
                        cookies_json = EXCLUDED.cookies_json,
                        cookie_count = EXCLUDED.cookie_count,
                        has_fd_tk = EXCLUDED.has_fd_tk,
                        fd_tk_digest = EXCLUDED.fd_tk_digest,
                        expires_at = EXCLUDED.expires_at,
                        last_auth_failed_at = NULL,
                        last_login_at = COALESCE(EXCLUDED.last_login_at, fastmoss_session_cookie_cache.last_login_at),
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "cache_key": normalized_cache_key,
                    "namespace": str(namespace or ""),
                    "account_key": str(account_key or ""),
                    "base_url": str(base_url or ""),
                    "region": str(region or ""),
                    "cookies_json": json.dumps(cookies, ensure_ascii=False, separators=(",", ":")),
                    "cookie_count": int(cookie_count),
                    "has_fd_tk": 1 if has_fd_tk else 0,
                    "fd_tk_digest": str(fd_tk_digest or ""),
                    "expires_at": expires_at,
                    "last_login_at": last_login_at,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        saved = self.load_fastmoss_cookie_cache(cache_key=normalized_cache_key)
        if saved is None:
            raise RuntimeError("Failed to save FastMoss cookie cache.")
        return saved

    def mark_fastmoss_cookie_cache_auth_failed(self, *, cache_key: str) -> dict[str, Any] | None:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            return None
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE fastmoss_session_cookie_cache
                    SET last_auth_failed_at = :last_auth_failed_at,
                        updated_at = :updated_at
                    WHERE cache_key = :cache_key
                    """
                ),
                {
                    "cache_key": normalized_cache_key,
                    "last_auth_failed_at": now,
                    "updated_at": now,
                },
            )
        return self.load_fastmoss_cookie_cache(cache_key=normalized_cache_key)

    def delete_fastmoss_cookie_cache(self, *, cache_key: str) -> None:
        normalized_cache_key = str(cache_key or "").strip()
        if not normalized_cache_key:
            return
        with self._engine.begin() as connection:
            connection.execute(
                self._text("DELETE FROM fastmoss_session_cookie_cache WHERE cache_key = :cache_key"),
                {"cache_key": normalized_cache_key},
            )

    def _requeue_expired_leases(self, connection: Any, *, now: float) -> None:
        self._resource_lease_repo.requeue_expired_leases(connection, now=now)

    def claim_next_browser_execution(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        item_codes: tuple[str, ...] = (),
    ) -> RuntimeTaskExecutionRecord | None:
        self._ensure_runtime_schema_ready()
        return self._task_execution_repo.claim_next_browser_execution(worker_id=worker_id, worker_pid=worker_pid, lease_seconds=lease_seconds, request_id=request_id, item_codes=item_codes)

    def claim_browser_execution(
        self,
        *,
        execution_id: str,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
    ) -> RuntimeTaskExecutionRecord | None:
        self._ensure_runtime_schema_ready()
        return self._task_execution_repo.claim_browser_execution(execution_id=execution_id, worker_id=worker_id, worker_pid=worker_pid, lease_seconds=lease_seconds)

    def heartbeat_browser_execution(self, *, execution_id: str, run_id: str, lease_seconds: float) -> bool:
        return self._task_execution_repo.heartbeat_browser_execution(execution_id=execution_id, run_id=run_id, lease_seconds=lease_seconds)

    def update_task_execution_progress(
        self,
        *,
        execution_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo.update_task_execution_progress(execution_id=execution_id, run_id=run_id, progress_stage=progress_stage, message=message, lease_seconds=lease_seconds)

    def _finalize_browser_execution(
        self,
        *,
        execution_id: str,
        status: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        error_text: str,
    ) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo._finalize_browser_execution(execution_id=execution_id, status=status, run_id=run_id, summary=summary, result=result, error_text=error_text)

    def mark_browser_execution_success(
        self,
        *,
        execution_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
    ) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo.mark_browser_execution_success(execution_id=execution_id, run_id=run_id, summary=summary, result=result)

    def mark_browser_execution_skipped(
        self,
        *,
        execution_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
    ) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo.mark_browser_execution_skipped(execution_id=execution_id, run_id=run_id, summary=summary, result=result)

    def mark_browser_execution_retry_or_failed(
        self,
        *,
        execution_id: str,
        run_id: str,
        error_text: str,
        summary: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        retry_delay_seconds: float = 30.0,
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo.mark_browser_execution_retry_or_failed(execution_id=execution_id, run_id=run_id, error_text=error_text, summary=summary, result=result, retry_delay_seconds=retry_delay_seconds, error_type=error_type, error_code=error_code, dead_letter_reason=dead_letter_reason)

    def load_task_execution(self, *, execution_id: str) -> RuntimeTaskExecutionRecord:
        return self._task_execution_repo.load_task_execution(execution_id=execution_id)

    def _scan_runtime_rows(
        self,
        *,
        table_name: str,
        statuses: tuple[str, ...],
        predicate_sql: str,
        predicate_params: Mapping[str, Any],
        limit: int,
        order_by_sql: str,
    ) -> list[dict[str, Any]]:
        return self._watchdog_query.scan_runtime_rows(
            table_name=table_name,
            statuses=statuses,
            predicate_sql=predicate_sql,
            predicate_params=predicate_params,
            limit=limit,
            order_by_sql=order_by_sql,
        )

    def scan_stale_task_requests(
        self,
        *,
        stale_after_seconds: float,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_REQUEST_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[RuntimeTaskRequestRecord]:
        now = time.time()
        rows = self._scan_runtime_rows(
            table_name="task_request",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) <= :threshold"
            ),
            predicate_params={"threshold": now - max(stale_after_seconds, 0.0)},
            limit=limit,
            order_by_sql="COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) ASC, created_at ASC",
        )
        return [self._request_from_row(row) for row in rows]

    def scan_stale_api_worker_jobs(
        self,
        *,
        stale_after_seconds: float,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_JOB_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        now = time.time()
        rows = self._scan_runtime_rows(
            table_name="api_worker_job",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) <= :threshold"
            ),
            predicate_params={"threshold": now - max(stale_after_seconds, 0.0)},
            limit=limit,
            order_by_sql="COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) ASC, created_at ASC",
        )
        return [self._api_worker_job_from_row(row) for row in rows]

    def scan_stale_task_executions(
        self,
        *,
        stale_after_seconds: float,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_JOB_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[RuntimeTaskExecutionRecord]:
        now = time.time()
        rows = self._scan_runtime_rows(
            table_name="task_execution",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) <= :threshold"
            ),
            predicate_params={"threshold": now - max(stale_after_seconds, 0.0)},
            limit=limit,
            order_by_sql="COALESCE(last_progress_at, heartbeat_at, started_at, updated_at, created_at) ASC, created_at ASC",
        )
        return [self._execution_from_row(row) for row in rows]

    def scan_stale_outbox_items(
        self,
        *,
        stale_after_seconds: float,
        statuses: tuple[str, ...] = DEFAULT_OUTBOX_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[NotificationOutboxRecord]:
        now = time.time()
        rows = self._scan_runtime_rows(
            table_name="notification_outbox",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(last_progress_at, heartbeat_at, updated_at, created_at) <= :threshold"
            ),
            predicate_params={"threshold": now - max(stale_after_seconds, 0.0)},
            limit=limit,
            order_by_sql="COALESCE(last_progress_at, heartbeat_at, updated_at, created_at) ASC, created_at ASC",
        )
        return [self._outbox_from_row(row) for row in rows]

    def scan_task_request_execution_timeouts(
        self,
        *,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_REQUEST_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[RuntimeTaskRequestRecord]:
        rows = self._scan_runtime_rows(
            table_name="task_request",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(max_execution_seconds, 0) > 0 "
                "AND COALESCE(started_at, created_at) + max_execution_seconds <= :now"
            ),
            predicate_params={"now": time.time()},
            limit=limit,
            order_by_sql="COALESCE(started_at, created_at) ASC, created_at ASC",
        )
        return [self._request_from_row(row) for row in rows]

    def scan_api_worker_job_execution_timeouts(
        self,
        *,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_JOB_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = self._scan_runtime_rows(
            table_name="api_worker_job",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(max_execution_seconds, 0) > 0 "
                "AND COALESCE(started_at, created_at) + max_execution_seconds <= :now"
            ),
            predicate_params={"now": time.time()},
            limit=limit,
            order_by_sql="COALESCE(started_at, created_at) ASC, created_at ASC",
        )
        return [self._api_worker_job_from_row(row) for row in rows]

    def scan_task_execution_timeouts(
        self,
        *,
        statuses: tuple[str, ...] = DEFAULT_ACTIVE_JOB_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[RuntimeTaskExecutionRecord]:
        rows = self._scan_runtime_rows(
            table_name="task_execution",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(max_execution_seconds, 0) > 0 "
                "AND COALESCE(started_at, created_at) + max_execution_seconds <= :now"
            ),
            predicate_params={"now": time.time()},
            limit=limit,
            order_by_sql="COALESCE(started_at, created_at) ASC, created_at ASC",
        )
        return [self._execution_from_row(row) for row in rows]

    def scan_outbox_execution_timeouts(
        self,
        *,
        statuses: tuple[str, ...] = DEFAULT_OUTBOX_SCAN_STATUSES,
        limit: int = 100,
    ) -> list[NotificationOutboxRecord]:
        rows = self._scan_runtime_rows(
            table_name="notification_outbox",
            statuses=statuses,
            predicate_sql=(
                "COALESCE(max_execution_seconds, 0) > 0 "
                "AND COALESCE(last_progress_at, heartbeat_at, updated_at, created_at) + max_execution_seconds <= :now"
            ),
            predicate_params={"now": time.time()},
            limit=limit,
            order_by_sql="COALESCE(last_progress_at, heartbeat_at, updated_at, created_at) ASC, created_at ASC",
        )
        return [self._outbox_from_row(row) for row in rows]

    def scan_expired_outbox_leases(self, *, limit: int = 100) -> list[NotificationOutboxRecord]:
        rows = self._scan_runtime_rows(
            table_name="notification_outbox",
            statuses=("sending",),
            predicate_sql="COALESCE(lease_until, 0) <= :now",
            predicate_params={"now": time.time()},
            limit=limit,
            order_by_sql="COALESCE(lease_until, 0) ASC, created_at ASC",
        )
        return [self._outbox_from_row(row) for row in rows]

    def _watchdog_payload(
        self,
        *,
        target_table: str,
        target_id: str,
        status: str,
        record: Mapping[str, Any],
        request_id: str = "",
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(record)
        payload["target_table"] = target_table
        payload["target_id"] = target_id
        payload["status"] = status
        if request_id:
            payload["request_id"] = request_id
        if reason:
            payload["reason"] = reason
        payload["metadata"] = dict(metadata or {})
        return payload

    def scan_expired_running_leases(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        max_limit = max(int(limit or 100), 1)
        candidates: list[dict[str, Any]] = []

        request_rows = self._scan_runtime_rows(
            table_name="task_request",
            statuses=("running",),
            predicate_sql="COALESCE(lease_until, 0) <= :now",
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(lease_until, 0) ASC, created_at ASC",
        )
        for row in request_rows:
            request = self._request_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="task_request",
                    target_id=request.request_id,
                    request_id=request.request_id,
                    status=request.status,
                    record=request.to_dict(),
                    reason="Task request lease expired while still running.",
                )
            )

        job_rows = self._scan_runtime_rows(
            table_name="api_worker_job",
            statuses=("running",),
            predicate_sql="COALESCE(lease_until, 0) <= :now",
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(lease_until, 0) ASC, created_at ASC",
        )
        for row in job_rows:
            job = self._api_worker_job_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="api_worker_job",
                    target_id=str(job["job_id"]),
                    request_id=str(job.get("request_id") or ""),
                    status=str(job.get("status") or ""),
                    record=job,
                    reason="API worker job lease expired while still running.",
                )
            )

        with self._engine.connect() as connection:
            execution_rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT execution.*, lease.lease_until
                        FROM task_execution execution
                        JOIN resource_lease lease ON lease.execution_id = execution.execution_id
                        WHERE execution.status = 'running'
                          AND lease.lease_until <= :now
                        ORDER BY lease.lease_until ASC, execution.created_at ASC
                        LIMIT :limit
                        """
                    ),
                    {"now": now, "limit": max_limit},
                )
                .mappings()
                .all()
            )
        for row in execution_rows:
            execution = self._execution_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="task_execution",
                    target_id=execution.execution_id,
                    request_id=execution.request_id,
                    status=execution.status,
                    record=execution.to_dict(),
                    reason="Browser execution resource lease expired while still running.",
                )
            )
        return candidates[:max_limit]

    def scan_stale_progress(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        max_limit = max(int(limit or 100), 1)
        stale_after_seconds = DEFAULT_WATCHDOG_STALE_AFTER_SECONDS
        candidates: list[dict[str, Any]] = []

        for request in self.scan_stale_task_requests(
            stale_after_seconds=stale_after_seconds,
            statuses=("running",),
            limit=max_limit,
        ):
            candidates.append(
                self._watchdog_payload(
                    target_table="task_request",
                    target_id=request.request_id,
                    request_id=request.request_id,
                    status=request.status,
                    record=request.to_dict(),
                    reason="Task request heartbeat is alive but progress is stale.",
                )
            )

        job_rows = self._scan_runtime_rows(
            table_name="api_worker_job",
            statuses=("running",),
            predicate_sql=(
                "COALESCE(max_idle_seconds, 0) > 0 "
                "AND COALESCE(last_progress_at, started_at, created_at) + max_idle_seconds <= :now"
            ),
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(last_progress_at, started_at, created_at) ASC, created_at ASC",
        )
        for row in job_rows:
            job = self._api_worker_job_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="api_worker_job",
                    target_id=str(job["job_id"]),
                    request_id=str(job.get("request_id") or ""),
                    status=str(job.get("status") or ""),
                    record=job,
                    reason="API worker job heartbeat is alive but progress is stale.",
                )
            )

        execution_rows = self._scan_runtime_rows(
            table_name="task_execution",
            statuses=("running",),
            predicate_sql=(
                "COALESCE(max_idle_seconds, 0) > 0 "
                "AND COALESCE(last_progress_at, started_at, created_at) + max_idle_seconds <= :now"
            ),
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(last_progress_at, started_at, created_at) ASC, created_at ASC",
        )
        for row in execution_rows:
            execution = self._execution_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="task_execution",
                    target_id=execution.execution_id,
                    request_id=execution.request_id,
                    status=execution.status,
                    record=execution.to_dict(),
                    reason="Browser execution heartbeat is alive but progress is stale.",
                )
            )
        return candidates[:max_limit]

    def scan_worker_heartbeat_timeouts(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        max_limit = max(int(limit or 100), 1)
        candidates: list[dict[str, Any]] = []

        job_rows = self._scan_runtime_rows(
            table_name="api_worker_job",
            statuses=("running",),
            predicate_sql=(
                "COALESCE(heartbeat_timeout_seconds, 0) > 0 "
                "AND COALESCE(heartbeat_at, started_at, created_at) + heartbeat_timeout_seconds <= :now"
            ),
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(heartbeat_at, started_at, created_at) ASC, created_at ASC",
        )
        for row in job_rows:
            job = self._api_worker_job_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="api_worker_job",
                    target_id=str(job["job_id"]),
                    request_id=str(job.get("request_id") or ""),
                    status=str(job.get("status") or ""),
                    record=job,
                    reason="API worker heartbeat exceeded heartbeat_timeout_seconds.",
                )
            )

        execution_rows = self._scan_runtime_rows(
            table_name="task_execution",
            statuses=("running",),
            predicate_sql=(
                "COALESCE(heartbeat_timeout_seconds, 0) > 0 "
                "AND COALESCE(heartbeat_at, started_at, created_at) + heartbeat_timeout_seconds <= :now"
            ),
            predicate_params={"now": now},
            limit=max_limit,
            order_by_sql="COALESCE(heartbeat_at, started_at, created_at) ASC, created_at ASC",
        )
        for row in execution_rows:
            execution = self._execution_from_row(row)
            candidates.append(
                self._watchdog_payload(
                    target_table="task_execution",
                    target_id=execution.execution_id,
                    request_id=execution.request_id,
                    status=execution.status,
                    record=execution.to_dict(),
                    reason="Browser worker heartbeat exceeded heartbeat_timeout_seconds.",
                )
            )
        return candidates[:max_limit]

    def scan_execution_timeouts(self, *, now: float, limit: int | None = None) -> list[dict[str, Any]]:
        max_limit = max(int(limit or 100), 1)
        candidates: list[dict[str, Any]] = []

        for request in self.scan_task_request_execution_timeouts(limit=max_limit):
            candidates.append(
                self._watchdog_payload(
                    target_table="task_request",
                    target_id=request.request_id,
                    request_id=request.request_id,
                    status=request.status,
                    record=request.to_dict(),
                    reason="Task request exceeded max_execution_seconds.",
                )
            )
        for job in self.scan_api_worker_job_execution_timeouts(limit=max_limit):
            candidates.append(
                self._watchdog_payload(
                    target_table="api_worker_job",
                    target_id=str(job["job_id"]),
                    request_id=str(job.get("request_id") or ""),
                    status=str(job.get("status") or ""),
                    record=job,
                    reason="API worker job exceeded max_execution_seconds.",
                )
            )
        for execution in self.scan_task_execution_timeouts(limit=max_limit):
            candidates.append(
                self._watchdog_payload(
                    target_table="task_execution",
                    target_id=execution.execution_id,
                    request_id=execution.request_id,
                    status=execution.status,
                    record=execution.to_dict(),
                    reason="Browser execution exceeded max_execution_seconds.",
                )
            )
        return candidates[:max_limit]

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
                if counts["total_count"] <= 0 or counts["active_count"] > 0:
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
        observed_attempt_count = _coerce_int(action_metadata.get("observed_attempt_count"))
        observed_retry_count = _coerce_int(action_metadata.get("observed_retry_count"))
        observed_lease_until = _coerce_float(action_metadata.get("observed_lease_until"))
        observed_started_at = _coerce_float(action_metadata.get("observed_started_at"))
        observed_last_progress_at = _coerce_float(action_metadata.get("observed_last_progress_at"))
        observed_max_execution_seconds = _coerce_float(
            action_metadata.get("observed_max_execution_seconds")
        )
        observed_run_id = str(action_metadata.get("observed_run_id") or "").strip()
        observed_worker_id = str(action_metadata.get("observed_worker_id") or "").strip()
        observed_worker_pid = _coerce_int(action_metadata.get("observed_worker_pid"))
        observed_heartbeat_at = _coerce_float(action_metadata.get("observed_heartbeat_at"))
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
                          AND (
                              :guard_started_at = 0
                              OR COALESCE(started_at, 0) = :observed_started_at
                          )
                          AND (
                              :guard_last_progress_at = 0
                              OR COALESCE(last_progress_at, 0) = :observed_last_progress_at
                          )
                          AND (
                              :guard_max_execution_seconds = 0
                              OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds
                          )
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
            return {
                "target_table": target_table,
                "target_id": target_id,
                "action_type": action_type,
                "applied": applied,
                "status": updated.status,
            }

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
                          AND (
                              :guard_attempt_count = 0
                              OR COALESCE(attempt_count, 0) = :observed_attempt_count
                          )
                          AND (:guard_lease_until = 0 OR COALESCE(lease_until, 0) = :observed_lease_until)
                          AND (
                              :guard_started_at = 0
                              OR COALESCE(started_at, 0) = :observed_started_at
                          )
                          AND (
                              :guard_last_progress_at = 0
                              OR COALESCE(last_progress_at, 0) = :observed_last_progress_at
                          )
                          AND (
                              :guard_heartbeat_at = 0
                              OR COALESCE(heartbeat_at, 0) = :observed_heartbeat_at
                          )
                          AND (
                              :guard_max_execution_seconds = 0
                              OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds
                          )
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
                "worker_pid": observed_worker_pid or _coerce_int(updated.get("worker_pid")),
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
                          AND (
                              :guard_attempt_count = 0
                              OR COALESCE(attempt_count, 0) = :observed_attempt_count
                          )
                          AND (
                              :guard_lease_until = 0
                              OR EXISTS (
                                  SELECT 1
                                  FROM resource_lease lease
                                  WHERE lease.execution_id = :execution_id
                                    AND COALESCE(lease.lease_until, 0) = :observed_lease_until
                              )
                          )
                          AND (
                              :guard_started_at = 0
                              OR COALESCE(started_at, 0) = :observed_started_at
                          )
                          AND (
                              :guard_last_progress_at = 0
                              OR COALESCE(last_progress_at, 0) = :observed_last_progress_at
                          )
                          AND (
                              :guard_heartbeat_at = 0
                              OR COALESCE(heartbeat_at, 0) = :observed_heartbeat_at
                          )
                          AND (
                              :guard_max_execution_seconds = 0
                              OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds
                          )
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
                          AND (
                              :guard_retry_count = 0
                              OR COALESCE(retry_count, 0) = :observed_retry_count
                          )
                          AND (:guard_lease_until = 0 OR COALESCE(lease_until, 0) = :observed_lease_until)
                          AND (
                              :guard_last_progress_at = 0
                              OR COALESCE(last_progress_at, 0) = :observed_last_progress_at
                          )
                          AND (
                              :guard_max_execution_seconds = 0
                              OR COALESCE(max_execution_seconds, 0) = :observed_max_execution_seconds
                          )
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

    def reclaim_expired_outbox_claims(self, *, limit: int = 100) -> list[NotificationOutboxRecord]:
        return self._notification_outbox_repo.reclaim_expired_outbox_claims(limit=limit)

    def _aggregate_runtime_request_children(self, connection: Any, *, request_id: str) -> dict[str, int]:
        task_stats = (
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
        ) or {}
        api_stats = (
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
                    FROM api_worker_job
                    WHERE request_id = :request_id
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
            "active_count": int(task_stats.get("active_count") or 0) + int(api_stats.get("active_count") or 0),
        }

    def reconcile_request_waiting_children(self, *, request_id: str) -> dict[str, Any]:
        now = time.time()
        with self._engine.begin() as connection:
            request_row = (
                connection.execute(
                    self._text(
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

            counts = self._aggregate_runtime_request_children(connection, request_id=request_id)
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
                    "child_total_count": counts["total_count"],
                    "child_terminal_count": counts["terminal_count"],
                    "child_success_count": counts["success_count"],
                    "child_failed_count": counts["failed_count"],
                    "child_skipped_count": counts["skipped_count"],
                    "updated_at": now,
                },
            )

            transitioned = False
            if str(request_row["status"] or "") == "waiting_children" and counts["active_count"] == 0:
                connection.execute(
                    self._text(
                        """
                        UPDATE task_request
                        SET status = 'ready_for_summary',
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
                    self._text(
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
            "request": self._request_from_row(updated_row),
            "transitioned": transitioned,
            "child_total_count": counts["total_count"],
            "child_terminal_count": counts["terminal_count"],
            "child_success_count": counts["success_count"],
            "child_failed_count": counts["failed_count"],
            "child_skipped_count": counts["skipped_count"],
            "active_count": counts["active_count"],
        }

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
        max_execution_seconds: float = 0.0,
    ) -> NotificationOutboxRecord:
        return self._notification_outbox_repo.create(
            channel_code=channel_code,
            event_type=event_type,
            ref_id=ref_id,
            reply_target=reply_target,
            payload=payload,
            dedupe_key=dedupe_key,
            max_execution_seconds=max_execution_seconds,
        )

    def load_outbox(self, *, outbox_id: str) -> NotificationOutboxRecord:
        return self._notification_outbox_repo.load(outbox_id=outbox_id)

    def _requeue_expired_outbox_claims(self, connection: Any, *, now: float) -> None:
        return self._notification_outbox_repo._requeue_expired_outbox_claims(connection, now=now)

    def claim_next_outbox(self, *, worker_id: str, lease_seconds: float) -> NotificationOutboxRecord | None:
        self._ensure_runtime_schema_ready()
        return self._notification_outbox_repo.claim_next_outbox(worker_id=worker_id, lease_seconds=lease_seconds)

    def heartbeat_outbox(self, *, outbox_id: str, lease_seconds: float) -> None:
        return self._notification_outbox_repo.heartbeat_outbox(outbox_id=outbox_id, lease_seconds=lease_seconds)

    def update_outbox_progress(
        self,
        *,
        outbox_id: str,
        progress_stage: str,
        lease_seconds: float | None = None,
    ) -> NotificationOutboxRecord:
        return self._notification_outbox_repo.update_outbox_progress(outbox_id=outbox_id, progress_stage=progress_stage, lease_seconds=lease_seconds)

    def mark_outbox_sent(self, *, outbox_id: str) -> NotificationOutboxRecord:
        return self._notification_outbox_repo.mark_outbox_sent(outbox_id=outbox_id)

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
        return self._notification_outbox_repo.mark_outbox_retry_or_failed(outbox_id=outbox_id, error_text=error_text, retry_delay_seconds=retry_delay_seconds, retryable=retryable, error_type=error_type, error_code=error_code, dead_letter_reason=dead_letter_reason)

    def upsert_influencer_pool_author_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._influencer_pool_job_repo.upsert_influencer_pool_author_jobs(jobs=jobs, force_refresh=force_refresh)

    def upsert_influencer_pool_product_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._influencer_pool_job_repo.upsert_influencer_pool_product_jobs(jobs=jobs, force_refresh=force_refresh)

    def claim_influencer_pool_product_job(
        self,
        *,
        request_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        return self._influencer_pool_job_repo.claim_influencer_pool_product_job(request_id=request_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def mark_influencer_pool_product_job_discovered(
        self,
        *,
        job_id: str,
        run_id: str,
        matched_author_count: int = 0,
        queued_author_job_count: int = 0,
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_product_job_discovered(job_id=job_id, run_id=run_id, matched_author_count=matched_author_count, queued_author_job_count=queued_author_job_count)

    def mark_influencer_pool_product_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str = "completed",
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_product_job_success(job_id=job_id, run_id=run_id, stage=stage)

    def mark_influencer_pool_product_job_author_retry_wait(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str = "",
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_product_job_author_retry_wait(job_id=job_id, run_id=run_id, error_text=error_text, error_type=error_type, error_code=error_code, error_path=error_path)

    def reactivate_influencer_pool_product_job_finalizer(
        self,
        *,
        request_id: str = "",
        source_record_id: str,
        product_id: str,
        run_id: str,
    ) -> None:
        return self._influencer_pool_job_repo.reactivate_influencer_pool_product_job_finalizer(request_id=request_id, source_record_id=source_record_id, product_id=product_id, run_id=run_id)

    def mark_influencer_pool_product_job_failed(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str,
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
        stage: str = "",
        retry_delay_seconds: float = 30.0,
        hard_stop: bool = False,
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_product_job_failed(job_id=job_id, run_id=run_id, error_text=error_text, error_type=error_type, error_code=error_code, error_path=error_path, stage=stage, retry_delay_seconds=retry_delay_seconds, hard_stop=hard_stop)

    def list_influencer_pool_product_jobs_for_finalizer(
        self,
        *,
        request_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._influencer_pool_job_repo.list_influencer_pool_product_jobs_for_finalizer(request_id=request_id, limit=limit)

    def list_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
        return self._influencer_pool_job_repo.list_influencer_pool_product_jobs_for_request(request_id=request_id)

    def summarize_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> dict[str, Any]:
        return self._influencer_pool_job_repo.summarize_influencer_pool_product_jobs_for_request(request_id=request_id)

    def find_next_influencer_pool_work_request_id(
        self,
        *,
        task_code: str = "sync_tk_influencer_pool",
    ) -> str:
        return self._influencer_pool_job_repo.find_next_influencer_pool_work_request_id(task_code=task_code)

    def _influencer_pool_product_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return self._influencer_pool_job_repo._influencer_pool_product_job_from_row(row)

    def claim_influencer_pool_author_job(
        self,
        *,
        request_id: str = "",
        product_id: str = "",
        source_record_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        return self._influencer_pool_job_repo.claim_influencer_pool_author_job(request_id=request_id, product_id=product_id, source_record_id=source_record_id, worker_id=worker_id, lease_seconds=lease_seconds)

    def mark_influencer_pool_author_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        target_record_id: str = "",
        snapshot_id: str = "",
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_author_job_success(job_id=job_id, run_id=run_id, target_record_id=target_record_id, snapshot_id=snapshot_id)

    def mark_influencer_pool_author_job_skipped(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str,
        reason: str,
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_author_job_skipped(job_id=job_id, run_id=run_id, stage=stage, reason=reason)

    def mark_influencer_pool_author_job_failed(
        self,
        *,
        job_id: str,
        run_id: str,
        error_text: str,
        error_type: str = "",
        error_code: str = "",
        error_path: str = "",
        stage: str = "",
        retry_delay_seconds: float = 30.0,
    ) -> None:
        return self._influencer_pool_job_repo.mark_influencer_pool_author_job_failed(job_id=job_id, run_id=run_id, error_text=error_text, error_type=error_type, error_code=error_code, error_path=error_path, stage=stage, retry_delay_seconds=retry_delay_seconds)

    def summarize_influencer_pool_author_jobs(
        self,
        *,
        request_id: str = "",
        product_id: str,
        source_record_id: str,
    ) -> dict[str, Any]:
        return self._influencer_pool_job_repo.summarize_influencer_pool_author_jobs(request_id=request_id, product_id=product_id, source_record_id=source_record_id)

    def _influencer_pool_author_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return self._influencer_pool_job_repo._influencer_pool_author_job_from_row(row)

    def replace_artifacts(self, *, run_id: str, records: list[ArtifactObjectRecord]) -> None:
        return self._artifact_object_repo.replace_artifacts(run_id=run_id, records=records)
