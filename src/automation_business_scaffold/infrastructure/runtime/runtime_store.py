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
from automation_business_scaffold.infrastructure.facts.tk_fact_store import ensure_tk_fact_schema


ACTIVE_EXECUTION_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_EXECUTION_STATUSES = {"success", "failed", "skipped", "cancelled"}
ACTIVE_API_WORKER_JOB_STATUSES = {"pending", "running", "retry_wait"}
TERMINAL_API_WORKER_JOB_STATUSES = {"success", "failed", "skipped", "cancelled"}
TERMINAL_REQUEST_STATUSES = {"success", "failed", "cancelled"}
DEFAULT_ACTIVE_REQUEST_SCAN_STATUSES = ("running", "waiting_children")
DEFAULT_ACTIVE_JOB_SCAN_STATUSES = ("running",)
DEFAULT_OUTBOX_SCAN_STATUSES = ("sending",)
DEFAULT_WATCHDOG_STALE_AFTER_SECONDS = 300.0
POSTGRES_SCHEMA_LOCK_KEY = 426319877301


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
                progress_stage TEXT NOT NULL DEFAULT '',
                stage_cursor_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                dead_letter_reason TEXT NOT NULL DEFAULT '',
                child_total_count INTEGER NOT NULL DEFAULT 0,
                child_terminal_count INTEGER NOT NULL DEFAULT 0,
                child_success_count INTEGER NOT NULL DEFAULT 0,
                child_failed_count INTEGER NOT NULL DEFAULT 0,
                child_skipped_count INTEGER NOT NULL DEFAULT 0,
                requested_by TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                lease_until DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION,
                last_progress_at DOUBLE PRECISION,
                max_execution_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION
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
                progress_stage TEXT NOT NULL DEFAULT '',
                available_at DOUBLE PRECISION NOT NULL,
                worker_id TEXT NOT NULL DEFAULT '',
                worker_pid INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                max_execution_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                max_idle_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                heartbeat_timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                dead_letter_reason TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION,
                last_progress_at DOUBLE PRECISION,
                progress_seq INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT ''
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
            CREATE TABLE IF NOT EXISTS api_worker_job (
                job_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL DEFAULT '',
                task_code TEXT NOT NULL DEFAULT '',
                job_code TEXT NOT NULL DEFAULT '',
                business_key TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                progress_stage TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                max_execution_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                dead_letter_reason TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                worker_pid INTEGER NOT NULL DEFAULT 0,
                lease_until DOUBLE PRECISION,
                available_at DOUBLE PRECISION NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                max_idle_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                heartbeat_timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION,
                last_progress_at DOUBLE PRECISION,
                progress_seq INTEGER NOT NULL DEFAULT 0,
                progress_message TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_api_worker_job_status_available_created
                ON api_worker_job(status, available_at, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_api_worker_job_request_created
                ON api_worker_job(request_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_api_worker_job_job_code_status_available
                ON api_worker_job(job_code, status, available_at)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_api_worker_job_dedupe_key
                ON api_worker_job(dedupe_key)
                WHERE dedupe_key <> ''
            """,
            """
            CREATE TABLE IF NOT EXISTS fastmoss_session_cookie_cache (
                cache_key TEXT PRIMARY KEY,
                namespace TEXT NOT NULL DEFAULT '',
                account_key TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                cookies_json TEXT NOT NULL DEFAULT '[]',
                cookie_count INTEGER NOT NULL DEFAULT 0,
                has_fd_tk INTEGER NOT NULL DEFAULT 0,
                fd_tk_digest TEXT NOT NULL DEFAULT '',
                expires_at DOUBLE PRECISION,
                last_auth_failed_at DOUBLE PRECISION,
                last_login_at DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_fastmoss_session_cookie_cache_account
                ON fastmoss_session_cookie_cache(namespace, account_key, region)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_fastmoss_session_cookie_cache_expires
                ON fastmoss_session_cookie_cache(expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_lease (
                resource_code TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                request_id TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                lease_until DOUBLE PRECISION NOT NULL,
                heartbeat_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
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
                progress_stage TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retry_count INTEGER NOT NULL DEFAULT 10,
                max_execution_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                next_retry_at DOUBLE PRECISION,
                worker_id TEXT NOT NULL DEFAULT '',
                lease_until DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION,
                last_error_text TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                dead_letter_reason TEXT NOT NULL DEFAULT '',
                sent_at DOUBLE PRECISION,
                last_progress_at DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
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
                created_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_artifact_object_run_id
                ON artifact_object(run_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS influencer_pool_product_job (
                job_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL DEFAULT '',
                source_record_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                source_record_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                matched_author_count INTEGER NOT NULL DEFAULT 0,
                queued_author_job_count INTEGER NOT NULL DEFAULT 0,
                last_error_text TEXT NOT NULL DEFAULT '',
                last_error_type TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_path TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                lease_until DOUBLE PRECISION,
                available_at DOUBLE PRECISION NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_influencer_pool_product_job_status
                ON influencer_pool_product_job(status, available_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS influencer_pool_author_job (
                job_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL DEFAULT '',
                source_record_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                influencer_id TEXT NOT NULL,
                uid TEXT NOT NULL DEFAULT '',
                sold_count DOUBLE PRECISION NOT NULL DEFAULT 0,
                follower_count DOUBLE PRECISION NOT NULL DEFAULT 0,
                holiday_name TEXT NOT NULL DEFAULT '',
                source_images_json TEXT NOT NULL DEFAULT '{}',
                author_row_json TEXT NOT NULL DEFAULT '{}',
                force_refresh INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                target_record_id TEXT NOT NULL DEFAULT '',
                snapshot_id TEXT NOT NULL DEFAULT '',
                last_error_text TEXT NOT NULL DEFAULT '',
                last_error_type TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_path TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                lease_until DOUBLE PRECISION,
                available_at DOUBLE PRECISION NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                started_at DOUBLE PRECISION,
                finished_at DOUBLE PRECISION,
                heartbeat_at DOUBLE PRECISION
            )
            """,
            """
            DROP INDEX IF EXISTS idx_influencer_pool_author_job_product_influencer
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_influencer_pool_author_job_product_status
                ON influencer_pool_author_job(product_id, status, available_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_influencer_pool_author_job_source_status
                ON influencer_pool_author_job(source_record_id, status, available_at)
            """,
        ]
        with self._engine.begin() as connection:
            dialect_name = str(connection.dialect.name or "").lower()
            if dialect_name.startswith("postgres"):
                connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({POSTGRES_SCHEMA_LOCK_KEY})")
            for statement in statements:
                connection.exec_driver_sql(statement)
            ensure_tk_fact_schema(connection)
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="worker_id",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )

            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="lease_until",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="heartbeat_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="progress_stage",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="last_progress_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="max_execution_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="error_type",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="error_code",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_request",
                column_name="dead_letter_reason",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="progress_stage",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="worker_pid",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="last_progress_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="max_execution_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="max_idle_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="heartbeat_timeout_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="progress_seq",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="progress_message",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="error_type",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="error_code",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="task_execution",
                column_name="dead_letter_reason",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="progress_stage",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="worker_pid",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="last_progress_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="max_execution_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="max_idle_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="heartbeat_timeout_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="progress_seq",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="progress_message",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="error_type",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="error_code",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="api_worker_job",
                column_name="dead_letter_reason",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="worker_id",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="lease_until",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="heartbeat_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="progress_stage",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="last_progress_at",
                column_definition="DOUBLE PRECISION",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="max_execution_seconds",
                column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="error_type",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="error_code",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="notification_outbox",
                column_name="dead_letter_reason",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="influencer_pool_author_job",
                column_name="force_refresh",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                table_name="influencer_pool_product_job",
                column_name="request_id",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                table_name="influencer_pool_author_job",
                column_name="request_id",
                column_definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_postgres_double_precision_columns(connection)
            connection.exec_driver_sql(
                "DROP INDEX IF EXISTS idx_influencer_pool_product_job_source_product"
            )
            connection.exec_driver_sql(
                "DROP INDEX IF EXISTS idx_influencer_pool_author_job_source_product_influencer"
            )
            connection.exec_driver_sql(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_influencer_pool_product_job_request_source_product
                    ON influencer_pool_product_job(request_id, source_record_id, product_id)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_influencer_pool_author_job_request_source_product_influencer
                    ON influencer_pool_author_job(request_id, source_record_id, product_id, influencer_id)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_task_request_status_lease_until
                    ON task_request(status, lease_until)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_task_request_status_last_progress_at
                    ON task_request(status, last_progress_at)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_task_execution_status_last_progress_at
                    ON task_execution(status, last_progress_at)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_api_worker_job_status_last_progress_at
                    ON api_worker_job(status, last_progress_at)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_lease_until
                    ON notification_outbox(status, lease_until)
                """
            )
            connection.exec_driver_sql(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_status_last_progress_at
                    ON notification_outbox(status, last_progress_at)
                """
            )

    def collect_db_connection_health(
        self,
        *,
        max_connection_ratio: float = 0.8,
        max_idle_in_transaction: int = -1,
    ) -> dict[str, Any]:
        threshold_ratio = min(max(float(max_connection_ratio or 0.8), 0.1), 1.0)
        idle_tx_threshold = int(max_idle_in_transaction if max_idle_in_transaction is not None else -1)
        with self._engine.connect() as connection:
            max_connections = int(
                connection.execute(
                    self._text("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
                ).scalar_one()
                or 0
            )
            state_rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT COALESCE(state, '') AS state, count(*)::int AS count
                        FROM pg_stat_activity
                        GROUP BY COALESCE(state, '')
                        """
                    )
                )
                .mappings()
                .all()
            )
            source_rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT COALESCE(application_name, '') AS application_name,
                               COALESCE(state, '') AS state,
                               count(*)::int AS count
                        FROM pg_stat_activity
                        GROUP BY COALESCE(application_name, ''), COALESCE(state, '')
                        ORDER BY count(*) DESC, application_name, state
                        LIMIT 20
                        """
                    )
                )
                .mappings()
                .all()
            )
        counts_by_state = {str(row["state"] or "unknown"): int(row["count"] or 0) for row in state_rows}
        total_connections = sum(counts_by_state.values())
        connection_ratio = (total_connections / max_connections) if max_connections else 0.0
        idle_in_transaction_count = counts_by_state.get("idle in transaction", 0)
        warnings: list[str] = []
        if max_connections and connection_ratio >= threshold_ratio:
            warnings.append("connection_ratio_exceeded")
        if idle_tx_threshold >= 0 and idle_in_transaction_count > idle_tx_threshold:
            warnings.append("idle_in_transaction_exceeded")
        return {
            "status": "warning" if warnings else "ok",
            "healthy": not warnings,
            "max_connections": max_connections,
            "total_connections": total_connections,
            "connection_ratio": connection_ratio,
            "max_connection_ratio": threshold_ratio,
            "idle_in_transaction_count": idle_in_transaction_count,
            "max_idle_in_transaction": idle_tx_threshold,
            "counts_by_state": counts_by_state,
            "top_sources": [
                {
                    "application_name": str(row["application_name"] or ""),
                    "state": str(row["state"] or ""),
                    "count": int(row["count"] or 0),
                }
                for row in source_rows
            ],
            "warnings": warnings,
        }

    def _has_column(self, connection: Any, *, table_name: str, column_name: str) -> bool:
        row = (
            connection.execute(
                self._text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = :table_name
                      AND column_name = :column_name
                    LIMIT 1
                    """
                ),
                {"table_name": table_name, "column_name": column_name},
            )
            .first()
        )
        return row is not None

    def _ensure_column(
        self,
        connection: Any,
        *,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        if self._has_column(connection, table_name=table_name, column_name=column_name):
            return
        connection.exec_driver_sql(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )

    def _ensure_postgres_double_precision_columns(self, connection: Any) -> None:
        if not str(connection.dialect.name or "").lower().startswith("postgres"):
            return
        columns_by_table = {
            "task_request": [
                "lease_until",
                "heartbeat_at",
                "last_progress_at",
                "max_execution_seconds",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
            ],
            "task_execution": [
                "available_at",
                "max_execution_seconds",
                "max_idle_seconds",
                "heartbeat_timeout_seconds",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "heartbeat_at",
                "last_progress_at",
            ],
            "resource_lease": ["lease_until", "heartbeat_at", "created_at", "updated_at"],
            "notification_outbox": [
                "next_retry_at",
                "lease_until",
                "heartbeat_at",
                "sent_at",
                "last_progress_at",
                "max_execution_seconds",
                "created_at",
                "updated_at",
            ],
            "artifact_object": ["created_at"],
            "api_worker_job": [
                "lease_until",
                "available_at",
                "max_execution_seconds",
                "max_idle_seconds",
                "heartbeat_timeout_seconds",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "heartbeat_at",
                "last_progress_at",
            ],
            "fastmoss_session_cookie_cache": [
                "expires_at",
                "last_auth_failed_at",
                "last_login_at",
                "created_at",
                "updated_at",
            ],
            "influencer_pool_product_job": [
                "lease_until",
                "available_at",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "heartbeat_at",
            ],
            "influencer_pool_author_job": [
                "sold_count",
                "follower_count",
                "lease_until",
                "available_at",
                "created_at",
                "updated_at",
                "started_at",
                "finished_at",
                "heartbeat_at",
            ],
        }
        for table_name, column_names in columns_by_table.items():
            for column_name in column_names:
                row = (
                    connection.execute(
                        self._text(
                            """
                            SELECT data_type
                            FROM information_schema.columns
                            WHERE table_schema = current_schema()
                              AND table_name = :table_name
                              AND column_name = :column_name
                            LIMIT 1
                            """
                        ),
                        {"table_name": table_name, "column_name": column_name},
                    )
                    .mappings()
                    .first()
                )
                if row is None or str(row["data_type"]).lower() == "double precision":
                    continue
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                    f"TYPE DOUBLE PRECISION USING {column_name}::double precision"
                )

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

    def list_task_executions(self, *, request_id: str) -> list[RuntimeTaskExecutionRecord]:
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
        created_records: list[RuntimeTaskExecutionRecord] = []
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
                max_execution_seconds = _coerce_non_negative_float(item.get("max_execution_seconds"))
                max_idle_seconds = _resolve_runtime_seconds(
                    item.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    item.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                )
                connection.execute(
                    self._text(
                        """
                        INSERT INTO task_execution (
                            execution_id, request_id, task_name, item_code, workflow_code,
                            business_key, dedupe_key, resource_code, status, queue_seq,
                            progress_stage, available_at, worker_id, worker_pid, attempt_count, max_attempts,
                            max_execution_seconds, max_idle_seconds, heartbeat_timeout_seconds,
                            payload_json, summary_json, result_json, error_text,
                            error_type, error_code, dead_letter_reason, run_id,
                            created_at, updated_at, started_at, finished_at, heartbeat_at, last_progress_at
                            , progress_seq, progress_message
                        ) VALUES (
                            :execution_id, :request_id, :task_name, :item_code, :workflow_code,
                            :business_key, :dedupe_key, :resource_code, 'pending', :queue_seq,
                            'queued', :available_at, '', 0, 0, :max_attempts,
                            :max_execution_seconds, :max_idle_seconds, :heartbeat_timeout_seconds,
                            :payload_json, '{}', '{}', '',
                            '', '', '', '',
                            :created_at, :updated_at, NULL, NULL, NULL, :last_progress_at
                            , 0, ''
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
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "payload_json": _json_dumps(payload),
                        "created_at": now,
                        "updated_at": now,
                        "last_progress_at": now,
                    },
                )
                created_records.append(
                    RuntimeTaskExecutionRecord(
                        execution_id=execution_id,
                        request_id=request_id,
                        item_code=item_code,
                        workflow_code=workflow_code,
                        business_key=business_key,
                        dedupe_key=dedupe_key,
                        resource_code=resource_code,
                        status="pending",
                        queue_seq=next_queue_seq,
                        progress_stage="queued",
                        available_at=now,
                        max_attempts=int(item.get("max_attempts", 3) or 3),
                        max_execution_seconds=max_execution_seconds,
                        max_idle_seconds=max_idle_seconds,
                        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                        payload=payload,
                        last_progress_at=now,
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

    def enqueue_api_worker_jobs(
        self,
        *,
        request_id: str,
        task_code: str,
        job_code: str,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_records: list[dict[str, Any]] = []
        updated_records: list[dict[str, Any]] = []
        skipped_records: list[dict[str, Any]] = []
        now = time.time()
        with self._engine.begin() as connection:
            for job in jobs:
                business_key = str(job.get("business_key", "") or "")
                dedupe_key = str(job.get("dedupe_key", "") or "")
                payload = dict(job.get("payload") or {})
                max_attempts = int(job.get("max_attempts", 3) or 3)
                max_execution_seconds = _coerce_non_negative_float(job.get("max_execution_seconds"))
                max_idle_seconds = _resolve_runtime_seconds(
                    job.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    job.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                )
                existing = None
                if dedupe_key:
                    existing = (
                        connection.execute(
                            self._text(
                                """
                                SELECT *
                                FROM api_worker_job
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
                    existing_status = str(existing["status"] or "")
                    if existing_status in TERMINAL_API_WORKER_JOB_STATUSES and not force_refresh:
                        skipped_records.append(
                            {
                                "business_key": business_key,
                                "dedupe_key": dedupe_key,
                                "existing_job_id": str(existing["job_id"]),
                                "status": existing_status,
                            }
                        )
                        continue
                    connection.execute(
                        self._text(
                            """
                            UPDATE api_worker_job
                            SET request_id = :request_id,
                                task_code = :task_code,
                                job_code = :job_code,
                                business_key = :business_key,
                                status = 'pending',
                                stage = 'queued',
                                progress_stage = 'queued',
                                payload_json = :payload_json,
                                summary_json = '{}',
                                result_json = '{}',
                                error_text = '',
                                error_type = '',
                                error_code = '',
                                dead_letter_reason = '',
                                worker_id = '',
                                worker_pid = 0,
                                lease_until = NULL,
                                available_at = :available_at,
                                max_attempts = :max_attempts,
                                max_execution_seconds = :max_execution_seconds,
                                max_idle_seconds = :max_idle_seconds,
                                heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                                progress_seq = 0,
                                progress_message = '',
                                last_progress_at = :last_progress_at,
                                updated_at = :updated_at,
                                finished_at = NULL,
                                heartbeat_at = NULL
                            WHERE job_id = :job_id
                            """
                        ),
                        {
                            "job_id": existing["job_id"],
                            "request_id": request_id,
                            "task_code": task_code,
                            "job_code": job_code,
                            "business_key": business_key,
                            "payload_json": _json_dumps(payload),
                            "available_at": now,
                            "max_attempts": max_attempts,
                            "max_execution_seconds": max_execution_seconds,
                            "max_idle_seconds": max_idle_seconds,
                            "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                            "last_progress_at": now,
                            "updated_at": now,
                        },
                    )
                    updated = (
                        connection.execute(
                            self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                            {"job_id": existing["job_id"]},
                        )
                        .mappings()
                        .first()
                    )
                    if updated is not None:
                        updated_records.append(self._api_worker_job_from_row(updated))
                    continue

                job_id = uuid.uuid4().hex
                connection.execute(
                    self._text(
                        """
                        INSERT INTO api_worker_job (
                            job_id, request_id, task_code, job_code, business_key, dedupe_key,
                            status, stage, progress_stage, attempt_count, max_attempts, max_execution_seconds,
                            max_idle_seconds, heartbeat_timeout_seconds,
                            payload_json, summary_json, result_json, error_text, error_type, error_code,
                            dead_letter_reason, worker_id, worker_pid, lease_until,
                            available_at, run_id, created_at, updated_at, started_at,
                            finished_at, heartbeat_at, last_progress_at, progress_seq, progress_message
                        ) VALUES (
                            :job_id, :request_id, :task_code, :job_code, :business_key, :dedupe_key,
                            'pending', 'queued', 'queued', 0, :max_attempts, :max_execution_seconds,
                            :max_idle_seconds, :heartbeat_timeout_seconds, :payload_json,
                            '{}', '{}', '', '', '', '', '', 0, NULL,
                            :available_at, '', :created_at, :updated_at, NULL,
                            NULL, NULL, :last_progress_at, 0, ''
                        )
                        """
                    ),
                    {
                        "job_id": job_id,
                        "request_id": request_id,
                        "task_code": task_code,
                        "job_code": job_code,
                        "business_key": business_key,
                        "dedupe_key": dedupe_key,
                        "max_attempts": max_attempts,
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "payload_json": _json_dumps(payload),
                        "available_at": now,
                        "created_at": now,
                        "updated_at": now,
                        "last_progress_at": now,
                    },
                )
                created = (
                    connection.execute(
                        self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                        {"job_id": job_id},
                    )
                    .mappings()
                    .first()
                )
                if created is not None:
                    created_records.append(self._api_worker_job_from_row(created))
        return {
            "created_count": len(created_records),
            "updated_count": len(updated_records),
            "skipped_count": len(skipped_records),
            "created_records": created_records,
            "updated_records": updated_records,
            "skipped_records": skipped_records,
        }

    def _requeue_expired_api_worker_job_claims(self, connection: Any, *, now: float) -> None:
        del connection, now
        return

    def claim_next_api_worker_job(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        job_code: str = "",
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            self._requeue_expired_api_worker_job_claims(connection, now=now)
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT job.*
                        FROM api_worker_job job
                        JOIN task_request request ON request.request_id = job.request_id
                        WHERE (:request_id = '' OR job.request_id = :request_id)
                          AND (:job_code = '' OR job.job_code = :job_code)
                          AND request.status = 'waiting_children'
                          AND job.status IN ('pending', 'retry_wait')
                          AND job.available_at <= :available_at
                        ORDER BY job.available_at ASC, job.created_at ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "request_id": request_id,
                        "job_code": job_code,
                        "available_at": now,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            payload = _load_json_dict(row.get("payload_json"))
            run_id = f"api-worker-{row['job_id']}-{uuid.uuid4().hex}"
            max_execution_seconds = _resolve_runtime_seconds(
                row.get("max_execution_seconds"),
                payload,
                "max_execution_seconds",
            )
            max_idle_seconds = _resolve_runtime_seconds(
                row.get("max_idle_seconds"),
                payload,
                "max_idle_seconds",
                "max_no_progress_seconds",
                default=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
            )
            heartbeat_timeout_seconds = _resolve_runtime_seconds(
                row.get("heartbeat_timeout_seconds"),
                payload,
                "heartbeat_timeout_seconds",
                default=max(lease_seconds * 2.0, 30.0),
            )
            result = connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = 'running',
                        stage = 'running',
                        progress_stage = 'job_claimed',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        worker_pid = :worker_pid,
                        lease_until = :lease_until,
                        run_id = :run_id,
                        max_execution_seconds = :max_execution_seconds,
                        max_idle_seconds = :max_idle_seconds,
                        heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                        updated_at = :updated_at,
                        started_at = :updated_at,
                        finished_at = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE job_id = :job_id
                      AND status IN ('pending', 'retry_wait')
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "worker_pid": int(worker_pid or 0),
                    "lease_until": now + max(lease_seconds, 5.0),
                    "run_id": run_id,
                    "max_execution_seconds": max_execution_seconds,
                    "max_idle_seconds": max_idle_seconds,
                    "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker claimed job.",
                },
            )
            if int(result.rowcount or 0) <= 0:
                return None
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._api_worker_job_from_row(claimed) if claimed is not None else None

    def heartbeat_api_worker_job(self, *, job_id: str, run_id: str, lease_seconds: float) -> bool:
        with self._engine.begin() as connection:
            now = time.time()
            result = connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET heartbeat_at = :heartbeat_at,
                        lease_until = :lease_until,
                        updated_at = :updated_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "heartbeat_at": now,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "updated_at": now,
                },
            )
        return int(result.rowcount or 0) > 0

    def update_api_worker_job_progress(
        self,
        *,
        job_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> dict[str, Any]:
        del lease_seconds
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET progress_stage = :progress_stage,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "progress_stage": progress_stage,
                    "progress_message": str(message or ""),
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def mark_api_worker_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        summary: dict[str, Any],
        result: dict[str, Any],
        stage: str = "completed",
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = time.time()
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = 'success',
                        stage = :stage,
                        progress_stage = :progress_stage,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = '',
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at,
                        finished_at = :finished_at
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "stage": stage,
                    "progress_stage": stage,
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "API worker job succeeded.",
                    "updated_at": now,
                    "finished_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

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
        with self._engine.begin() as connection:
            now = time.time()
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT attempt_count, max_attempts
                        FROM api_worker_job
                        WHERE job_id = :job_id
                          AND run_id = :run_id
                          AND status = 'running'
                        LIMIT 1
                        """
                    ),
                    {"job_id": job_id, "run_id": run_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return self.load_api_worker_job(job_id=job_id)
            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            status = "retry_wait" if attempt_count < max_attempts else "failed"
            available_at = now + max(retry_delay_seconds, 0.1) if status == "retry_wait" else now
            connection.execute(
                self._text(
                    """
                    UPDATE api_worker_job
                    SET status = :status,
                        stage = :stage,
                        progress_stage = :progress_stage,
                        run_id = :run_id,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        worker_id = '',
                        worker_pid = 0,
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at,
                        finished_at = CASE WHEN :status = 'failed' THEN :updated_at ELSE finished_at END
                    WHERE job_id = :job_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "stage": "retry_wait" if status == "retry_wait" else "failed",
                    "progress_stage": "retry_wait" if status == "retry_wait" else "failed",
                    "run_id": run_id,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "dead_letter_reason": dead_letter_reason or ("max_attempts_exhausted" if status == "failed" else ""),
                    "available_at": available_at,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": error_text,
                    "updated_at": now,
                },
            )
        return self.load_api_worker_job(job_id=job_id)

    def load_api_worker_job(self, *, job_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    self._text("SELECT * FROM api_worker_job WHERE job_id = :job_id LIMIT 1"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError("API worker job not found.")
            return self._api_worker_job_from_row(row)

    def list_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM api_worker_job
                        WHERE request_id = :request_id
                          AND (:job_code = '' OR job_code = :job_code)
                        ORDER BY created_at ASC, updated_at ASC
                        """
                    ),
                    {"request_id": request_id, "job_code": job_code},
                )
                .mappings()
                .all()
            )
        return [self._api_worker_job_from_row(row) for row in rows]

    def summarize_api_worker_jobs_for_request(
        self,
        *,
        request_id: str,
        job_code: str = "",
    ) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM api_worker_job
                        WHERE request_id = :request_id
                          AND (:job_code = '' OR job_code = :job_code)
                        GROUP BY status
                        """
                    ),
                    {"request_id": request_id, "job_code": job_code},
                )
                .mappings()
                .all()
            )
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        total = sum(counts.values())
        active_count = sum(counts.get(status, 0) for status in ACTIVE_API_WORKER_JOB_STATUSES)
        success_count = counts.get("success", 0) + counts.get("skipped", 0)
        failed_count = counts.get("failed", 0) + counts.get("cancelled", 0)
        return {
            "total": total,
            "counts": counts,
            "active_count": active_count,
            "terminal_count": max(total - active_count, 0),
            "success_count": success_count,
            "failed_count": failed_count,
        }

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
        expired_rows = (
            connection.execute(
                self._text(
                    """
                    SELECT lease.resource_code, lease.execution_id, lease.request_id, execution.status AS execution_status
                    FROM resource_lease lease
                    LEFT JOIN task_execution execution ON execution.execution_id = lease.execution_id
                    WHERE lease.lease_until <= :now
                    """
                ),
                {"now": now},
            )
            .mappings()
            .all()
        )
        for row in expired_rows:
            if str(row["execution_status"] or "") == "running":
                continue
            connection.execute(
                self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                {"resource_code": row["resource_code"]},
            )

    def claim_next_browser_execution(
        self,
        *,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
        request_id: str = "",
        item_codes: tuple[str, ...] = (),
    ) -> RuntimeTaskExecutionRecord | None:
        normalized_request_id = str(request_id or "").strip()
        allowed_item_codes = {str(item or "").strip() for item in item_codes if str(item or "").strip()}
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
                          AND (:request_id = '' OR request_id = :request_id)
                        ORDER BY queue_seq ASC, created_at ASC
                        """
                    ),
                    {"available_at": now, "request_id": normalized_request_id},
                )
                .mappings()
                .all()
            )
            for row in rows:
                if allowed_item_codes and str(row["item_code"] or "") not in allowed_item_codes:
                    continue
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
                payload = _load_json_dict(row.get("payload_json"))
                run_id = f"browser-{row['execution_id']}-{uuid.uuid4().hex}"
                max_execution_seconds = _resolve_runtime_seconds(
                    row.get("max_execution_seconds"),
                    payload,
                    "max_execution_seconds",
                )
                max_idle_seconds = _resolve_runtime_seconds(
                    row.get("max_idle_seconds"),
                    payload,
                    "max_idle_seconds",
                    "max_no_progress_seconds",
                    default=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
                )
                heartbeat_timeout_seconds = _resolve_runtime_seconds(
                    row.get("heartbeat_timeout_seconds"),
                    payload,
                    "heartbeat_timeout_seconds",
                    default=max(lease_seconds * 2.0, 30.0),
                )
                result = connection.execute(
                    self._text(
                        """
                        UPDATE task_execution
                        SET status = 'running',
                            worker_id = :worker_id,
                            worker_pid = :worker_pid,
                            attempt_count = COALESCE(attempt_count, 0) + 1,
                            run_id = :run_id,
                            progress_stage = 'job_claimed',
                            max_execution_seconds = :max_execution_seconds,
                            max_idle_seconds = :max_idle_seconds,
                            heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                            updated_at = :updated_at,
                            started_at = :updated_at,
                            finished_at = NULL,
                            heartbeat_at = :heartbeat_at,
                            last_progress_at = :last_progress_at,
                            progress_seq = COALESCE(progress_seq, 0) + 1,
                            progress_message = :progress_message
                        WHERE execution_id = :execution_id
                          AND status IN ('pending', 'retry_wait')
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "worker_pid": int(worker_pid or 0),
                        "run_id": run_id,
                        "max_execution_seconds": max_execution_seconds,
                        "max_idle_seconds": max_idle_seconds,
                        "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                        "updated_at": now,
                        "heartbeat_at": now,
                        "last_progress_at": now,
                        "progress_message": "Browser worker claimed execution.",
                        "execution_id": row["execution_id"],
                    },
                )
                if int(result.rowcount or 0) <= 0:
                    continue
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

    def claim_browser_execution(
        self,
        *,
        execution_id: str,
        worker_id: str,
        worker_pid: int | None = None,
        lease_seconds: float,
    ) -> RuntimeTaskExecutionRecord | None:
        with self._engine.begin() as connection:
            now = time.time()
            self._requeue_expired_leases(connection, now=now)
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
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
            if row is None or str(row["status"] or "") not in {"pending", "retry_wait"}:
                return None
            if _coerce_float(row["available_at"]) > now:
                return None
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
                    return None
                if lease_row is not None:
                    connection.execute(
                        self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                        {"resource_code": resource_code},
                    )
            payload = _load_json_dict(row.get("payload_json"))
            run_id = f"browser-{row['execution_id']}-{uuid.uuid4().hex}"
            max_execution_seconds = _resolve_runtime_seconds(
                row.get("max_execution_seconds"),
                payload,
                "max_execution_seconds",
            )
            max_idle_seconds = _resolve_runtime_seconds(
                row.get("max_idle_seconds"),
                payload,
                "max_idle_seconds",
                "max_no_progress_seconds",
                default=DEFAULT_WATCHDOG_STALE_AFTER_SECONDS,
            )
            heartbeat_timeout_seconds = _resolve_runtime_seconds(
                row.get("heartbeat_timeout_seconds"),
                payload,
                "heartbeat_timeout_seconds",
                default=max(lease_seconds * 2.0, 30.0),
            )
            result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = 'running',
                        worker_id = :worker_id,
                        worker_pid = :worker_pid,
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        run_id = :run_id,
                        progress_stage = 'job_claimed',
                        max_execution_seconds = :max_execution_seconds,
                        max_idle_seconds = :max_idle_seconds,
                        heartbeat_timeout_seconds = :heartbeat_timeout_seconds,
                        updated_at = :updated_at,
                        started_at = :updated_at,
                        finished_at = NULL,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND status IN ('pending', 'retry_wait')
                    """
                ),
                {
                    "worker_id": worker_id,
                    "worker_pid": int(worker_pid or 0),
                    "run_id": run_id,
                    "max_execution_seconds": max_execution_seconds,
                    "max_idle_seconds": max_idle_seconds,
                    "heartbeat_timeout_seconds": heartbeat_timeout_seconds,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": "Browser worker claimed execution.",
                    "execution_id": execution_id,
                },
            )
            if int(result.rowcount or 0) <= 0:
                return None
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
                    {"execution_id": execution_id},
                )
                .mappings()
                .first()
            )
            if execution is None:
                return None
            return self._execution_from_row(execution)

    def heartbeat_browser_execution(self, *, execution_id: str, run_id: str, lease_seconds: float) -> bool:
        with self._engine.begin() as connection:
            now = time.time()
            result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET heartbeat_at = :heartbeat_at,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "heartbeat_at": now,
                    "updated_at": now,
                    "execution_id": execution_id,
                    "run_id": run_id,
                },
            )
            if int(result.rowcount or 0) <= 0:
                return False
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
        return True

    def update_task_execution_progress(
        self,
        *,
        execution_id: str,
        run_id: str,
        progress_stage: str,
        message: str = "",
        lease_seconds: float | None = None,
    ) -> RuntimeTaskExecutionRecord:
        del lease_seconds
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET progress_stage = :progress_stage,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message,
                        updated_at = :updated_at
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "execution_id": execution_id,
                    "run_id": run_id,
                    "progress_stage": progress_stage,
                    "progress_message": str(message or ""),
                    "last_progress_at": now,
                    "updated_at": now,
                },
            )
        return self.load_task_execution(execution_id=execution_id)

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
            update_result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = :status,
                        run_id = :run_id,
                        progress_stage = :progress_stage,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = '',
                        error_code = '',
                        dead_letter_reason = '',
                        worker_id = '',
                        worker_pid = 0,
                        updated_at = :updated_at,
                        finished_at = :finished_at,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "status": status,
                    "run_id": run_id,
                    "progress_stage": status,
                    "summary_json": _json_dumps(summary),
                    "result_json": _json_dumps(result),
                    "error_text": error_text,
                    "updated_at": now,
                    "finished_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": f"Browser execution {status}.",
                    "execution_id": execution_id,
                },
            )
            applied = int(update_result.rowcount or 0) > 0
            if applied and execution_row["resource_code"]:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": execution_row["resource_code"]},
                )
            if applied:
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
    ) -> RuntimeTaskExecutionRecord:
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
    ) -> RuntimeTaskExecutionRecord:
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
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> RuntimeTaskExecutionRecord:
        with self._engine.begin() as connection:
            now = time.time()
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM task_execution
                        WHERE execution_id = :execution_id
                          AND run_id = :run_id
                          AND status = 'running'
                        LIMIT 1
                        """
                    ),
                    {"execution_id": execution_id, "run_id": run_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return self.load_task_execution(execution_id=execution_id)
            status = "retry_wait"
            available_at = now + max(retry_delay_seconds, 0.1)
            if int(row["attempt_count"] or 0) >= int(row["max_attempts"] or 1):
                status = "failed"
                available_at = now
            update_result = connection.execute(
                self._text(
                    """
                    UPDATE task_execution
                    SET status = :status,
                        run_id = :run_id,
                        progress_stage = :progress_stage,
                        summary_json = :summary_json,
                        result_json = :result_json,
                        error_text = :error_text,
                        error_type = :error_type,
                        error_code = :error_code,
                        dead_letter_reason = :dead_letter_reason,
                        worker_id = '',
                        worker_pid = 0,
                        available_at = :available_at,
                        updated_at = :updated_at,
                        finished_at = CASE WHEN :status = 'failed' THEN :updated_at ELSE finished_at END,
                        heartbeat_at = :heartbeat_at,
                        last_progress_at = :last_progress_at,
                        progress_seq = COALESCE(progress_seq, 0) + 1,
                        progress_message = :progress_message
                    WHERE execution_id = :execution_id
                      AND run_id = :run_id
                      AND status = 'running'
                    """
                ),
                {
                    "status": status,
                    "run_id": run_id,
                    "progress_stage": status,
                    "summary_json": _json_dumps(summary or {}),
                    "result_json": _json_dumps(result or {}),
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "dead_letter_reason": dead_letter_reason or ("max_attempts_exhausted" if status == "failed" else ""),
                    "available_at": available_at,
                    "updated_at": now,
                    "heartbeat_at": now,
                    "last_progress_at": now,
                    "progress_message": error_text,
                    "execution_id": execution_id,
                },
            )
            applied = int(update_result.rowcount or 0) > 0
            resource_code = str(row["resource_code"] or "")
            if applied and resource_code:
                connection.execute(
                    self._text("DELETE FROM resource_lease WHERE resource_code = :resource_code"),
                    {"resource_code": resource_code},
                )
            if applied:
                self._refresh_request_child_counts(
                    connection,
                    request_id=str(row["request_id"]),
                    now=now,
                )
        return self.load_task_execution(execution_id=execution_id)

    def load_task_execution(self, *, execution_id: str) -> RuntimeTaskExecutionRecord:
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
        normalized_statuses = tuple(str(status or "").strip() for status in statuses if str(status or "").strip())
        if not normalized_statuses:
            return []
        placeholders, status_params = _build_bind_placeholders("status", normalized_statuses)
        query = f"""
            SELECT *
            FROM {table_name}
            WHERE status IN ({placeholders})
              AND {predicate_sql}
            ORDER BY {order_by_sql}
            LIMIT :limit
        """
        params = dict(predicate_params)
        params.update(status_params)
        params["limit"] = max(int(limit or 1), 1)
        with self._engine.connect() as connection:
            rows = (
                connection.execute(self._text(query), params)
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

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
                    "payload_json": _json_dumps(payload),
                    "max_execution_seconds": _coerce_non_negative_float(max_execution_seconds),
                    "last_progress_at": now,
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

    def upsert_influencer_pool_author_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_count = 0
        updated_count = 0
        kept_terminal_count = 0
        now = time.time()
        with self._engine.begin() as connection:
            for job in jobs:
                request_id = str(job.get("request_id", "") or "").strip()
                product_id = str(job.get("product_id", "") or "").strip()
                influencer_id = str(job.get("influencer_id", "") or "").strip()
                source_record_id = str(job.get("source_record_id", "") or "").strip()
                if not product_id or not influencer_id:
                    continue
                existing = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM influencer_pool_author_job
                            WHERE request_id = :request_id
                              AND source_record_id = :source_record_id
                              AND product_id = :product_id
                              AND influencer_id = :influencer_id
                            LIMIT 1
                            """
                        ),
                        {
                            "request_id": request_id,
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                            "influencer_id": influencer_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                payload = {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "influencer_id": influencer_id,
                    "uid": str(job.get("uid", "") or ""),
                    "sold_count": _coerce_float(job.get("sold_count")),
                    "follower_count": _coerce_float(job.get("follower_count")),
                    "holiday_name": str(job.get("holiday_name", "") or ""),
                    "source_images_json": _json_dumps({"value": job.get("source_images")}),
                    "author_row_json": _json_dumps(
                        job.get("author_row") if isinstance(job.get("author_row"), dict) else {}
                    ),
                    "force_refresh": 1 if bool(job.get("force_refresh")) else 0,
                    "max_attempts": int(job.get("max_attempts", 3) or 3),
                }
                if existing is None:
                    connection.execute(
                        self._text(
                            """
                            INSERT INTO influencer_pool_author_job (
                                job_id, request_id, source_record_id, product_id, influencer_id, uid,
                                sold_count, follower_count, holiday_name, source_images_json,
                                author_row_json, force_refresh, status, stage, attempt_count, max_attempts,
                                available_at, created_at, updated_at
                            ) VALUES (
                                :job_id, :request_id, :source_record_id, :product_id, :influencer_id, :uid,
                                :sold_count, :follower_count, :holiday_name, :source_images_json,
                                :author_row_json, :force_refresh, 'pending', 'queued', 0, :max_attempts,
                                :available_at, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            **payload,
                            "job_id": uuid.uuid4().hex,
                            "available_at": now,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    created_count += 1
                    continue

                existing_status = str(existing["status"] or "")
                should_keep_terminal = (
                    existing_status in {"succeeded", "skipped"}
                    and not force_refresh
                )
                next_status = existing_status if should_keep_terminal else "pending"
                if should_keep_terminal:
                    kept_terminal_count += 1
                else:
                    updated_count += 1
                connection.execute(
                    self._text(
                        """
                        UPDATE influencer_pool_author_job
                        SET request_id = :request_id,
                            source_record_id = :source_record_id,
                            uid = :uid,
                            sold_count = :sold_count,
                            follower_count = :follower_count,
                            holiday_name = :holiday_name,
                            source_images_json = :source_images_json,
                            author_row_json = :author_row_json,
                            force_refresh = :force_refresh,
                            status = :status,
                            stage = CASE WHEN :status = status THEN stage ELSE 'queued' END,
                            max_attempts = :max_attempts,
                            available_at = CASE WHEN :status = status THEN available_at ELSE :available_at END,
                            worker_id = CASE WHEN :status = status THEN worker_id ELSE '' END,
                            lease_until = CASE WHEN :status = status THEN lease_until ELSE NULL END,
                            updated_at = :updated_at
                        WHERE job_id = :job_id
                        """
                    ),
                    {
                        **payload,
                        "job_id": existing["job_id"],
                        "status": next_status,
                        "available_at": now,
                        "updated_at": now,
                    },
                )
        return {
            "created_count": created_count,
            "updated_count": updated_count,
            "kept_terminal_count": kept_terminal_count,
        }

    def upsert_influencer_pool_product_jobs(
        self,
        *,
        jobs: list[dict[str, Any]],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        created_count = 0
        updated_count = 0
        kept_terminal_count = 0
        now = time.time()
        with self._engine.begin() as connection:
            for job in jobs:
                request_id = str(job.get("request_id", "") or "").strip()
                source_record_id = str(job.get("source_record_id", "") or "").strip()
                product_id = str(job.get("product_id", "") or "").strip()
                if not source_record_id:
                    continue
                existing = (
                    connection.execute(
                        self._text(
                            """
                            SELECT *
                            FROM influencer_pool_product_job
                            WHERE request_id = :request_id
                              AND source_record_id = :source_record_id
                              AND product_id = :product_id
                            LIMIT 1
                            """
                        ),
                        {
                            "request_id": request_id,
                            "source_record_id": source_record_id,
                            "product_id": product_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                payload = {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "source_record_json": _json_dumps(
                        job.get("source_record") if isinstance(job.get("source_record"), dict) else {}
                    ),
                    "max_attempts": int(job.get("max_attempts", 3) or 3),
                }
                if existing is None:
                    connection.execute(
                        self._text(
                            """
                            INSERT INTO influencer_pool_product_job (
                                job_id, request_id, source_record_id, product_id, source_record_json,
                                status, stage, attempt_count, max_attempts,
                                available_at, created_at, updated_at
                            ) VALUES (
                                :job_id, :request_id, :source_record_id, :product_id, :source_record_json,
                                'pending', 'queued', 0, :max_attempts,
                                :available_at, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            **payload,
                            "job_id": uuid.uuid4().hex,
                            "available_at": now,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    created_count += 1
                    continue

                existing_status = str(existing["status"] or "")
                should_keep_terminal = existing_status in {"completed", "skipped"} and not force_refresh
                next_status = existing_status if should_keep_terminal else "pending"
                if should_keep_terminal:
                    kept_terminal_count += 1
                else:
                    updated_count += 1
                connection.execute(
                    self._text(
                        """
                        UPDATE influencer_pool_product_job
                        SET request_id = :request_id,
                            product_id = :product_id,
                            source_record_json = :source_record_json,
                            status = :status,
                            stage = CASE WHEN :status = status THEN stage ELSE 'queued' END,
                            max_attempts = :max_attempts,
                            available_at = CASE WHEN :status = status THEN available_at ELSE :available_at END,
                            worker_id = CASE WHEN :status = status THEN worker_id ELSE '' END,
                            lease_until = CASE WHEN :status = status THEN lease_until ELSE NULL END,
                            updated_at = :updated_at
                        WHERE job_id = :job_id
                        """
                    ),
                    {
                        **payload,
                        "job_id": existing["job_id"],
                        "status": next_status,
                        "available_at": now,
                        "updated_at": now,
                    },
                )
        return {
            "created_count": created_count,
            "updated_count": updated_count,
            "kept_terminal_count": kept_terminal_count,
        }

    def claim_influencer_pool_product_job(
        self,
        *,
        request_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'failed_retry',
                        stage = 'lease_expired',
                        worker_id = '',
                        lease_until = NULL,
                        updated_at = :updated_at
                    WHERE status IN ('discovering')
                      AND lease_until IS NOT NULL
                      AND lease_until <= :now
                    """
                ),
                {"now": now, "updated_at": now},
            )
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND status IN ('pending', 'failed_retry')
                          AND available_at <= :available_at
                        ORDER BY created_at ASC, updated_at ASC
                        LIMIT 1
                        """
                    ),
                    {"request_id": request_id, "available_at": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'discovering',
                        stage = 'product_author_list',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        started_at = CASE WHEN started_at IS NULL THEN :now ELSE started_at END,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "now": now,
                },
            )
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM influencer_pool_product_job WHERE job_id = :job_id"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._influencer_pool_product_job_from_row(claimed) if claimed is not None else None

    def mark_influencer_pool_product_job_discovered(
        self,
        *,
        job_id: str,
        run_id: str,
        matched_author_count: int = 0,
        queued_author_job_count: int = 0,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'detail_pending',
                        stage = 'author_jobs_queued',
                        matched_author_count = :matched_author_count,
                        queued_author_job_count = :queued_author_job_count,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "matched_author_count": max(int(matched_author_count or 0), 0),
                    "queued_author_job_count": max(int(queued_author_job_count or 0), 0),
                    "now": now,
                },
            )

    def mark_influencer_pool_product_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str = "completed",
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'completed',
                        stage = :stage,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "run_id": run_id, "stage": stage, "now": now},
            )

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
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'author_failed_retry',
                        stage = 'waiting_author_retry',
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "now": now,
                },
            )

    def reactivate_influencer_pool_product_job_finalizer(
        self,
        *,
        request_id: str = "",
        source_record_id: str,
        product_id: str,
        run_id: str,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = 'detail_pending',
                        stage = 'author_job_updated',
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE source_record_id = :source_record_id
                      AND product_id = :product_id
                      AND (:request_id = '' OR request_id = :request_id)
                      AND status IN ('detail_pending', 'author_failed_retry')
                    """
                ),
                {
                    "request_id": request_id,
                    "source_record_id": source_record_id,
                    "product_id": product_id,
                    "run_id": run_id,
                    "now": now,
                },
            )

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
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text("SELECT attempt_count, max_attempts FROM influencer_pool_product_job WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            attempt_count = int(row["attempt_count"] or 0) if row is not None else 0
            max_attempts = int(row["max_attempts"] or 1) if row is not None else 1
            status = "hard_stopped" if hard_stop else ("failed_retry" if attempt_count < max_attempts else "hard_failed")
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_product_job
                    SET status = :status,
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = CASE WHEN :status IN ('hard_failed', 'hard_stopped') THEN :now ELSE finished_at END
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "stage": stage,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "available_at": now + max(retry_delay_seconds, 0.1),
                    "now": now,
                },
            )

    def list_influencer_pool_product_jobs_for_finalizer(
        self,
        *,
        request_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND status = 'detail_pending'
                        ORDER BY updated_at ASC, created_at ASC
                        LIMIT :limit
                        """
                    ),
                    {"request_id": request_id, "limit": max(int(limit or 1), 1)},
                )
                .mappings()
                .all()
            )
        return [self._influencer_pool_product_job_from_row(row) for row in rows]

    def list_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        ORDER BY created_at ASC, updated_at ASC
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
        return [self._influencer_pool_product_job_from_row(row) for row in rows]

    def summarize_influencer_pool_product_jobs_for_request(self, *, request_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        GROUP BY status
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .all()
            )
            aggregate = (
                connection.execute(
                    self._text(
                        """
                        SELECT
                            COALESCE(SUM(matched_author_count), 0) AS matched_author_count,
                            COALESCE(SUM(queued_author_job_count), 0) AS queued_author_job_count
                        FROM influencer_pool_product_job
                        WHERE request_id = :request_id
                        """
                    ),
                    {"request_id": request_id},
                )
                .mappings()
                .first()
            )
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        active_statuses = {
            "pending",
            "failed_retry",
            "discovering",
            "detail_pending",
            "author_failed_retry",
        }
        failed_statuses = {"hard_failed", "hard_stopped"}
        success_statuses = {"completed", "skipped"}
        total = sum(counts.values())
        active_count = sum(counts.get(status, 0) for status in active_statuses)
        failed_count = sum(counts.get(status, 0) for status in failed_statuses)
        success_count = sum(counts.get(status, 0) for status in success_statuses)
        return {
            "total": total,
            "counts": counts,
            "active_count": active_count,
            "terminal_count": max(total - active_count, 0),
            "success_count": success_count,
            "failed_count": failed_count,
            "matched_author_count": int((aggregate or {}).get("matched_author_count") or 0),
            "queued_author_job_count": int((aggregate or {}).get("queued_author_job_count") or 0),
        }

    def find_next_influencer_pool_work_request_id(
        self,
        *,
        task_code: str = "sync_tk_influencer_pool",
    ) -> str:
        now = time.time()
        queries = [
            """
            SELECT job.request_id
            FROM influencer_pool_product_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting_children'
              AND job.status IN ('pending', 'failed_retry')
              AND job.available_at <= :available_at
            ORDER BY job.available_at ASC, job.created_at ASC
            LIMIT 1
            """,
            """
            SELECT job.request_id
            FROM influencer_pool_author_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting_children'
              AND job.status IN ('pending', 'failed_retry')
              AND job.available_at <= :available_at
            ORDER BY job.available_at ASC, job.created_at ASC
            LIMIT 1
            """,
            """
            SELECT job.request_id
            FROM influencer_pool_product_job job
            JOIN task_request request ON request.request_id = job.request_id
            WHERE request.task_code = :task_code
              AND request.status = 'waiting_children'
              AND job.status = 'detail_pending'
            ORDER BY job.updated_at ASC, job.created_at ASC
            LIMIT 1
            """,
        ]
        with self._engine.connect() as connection:
            for query in queries:
                row = (
                    connection.execute(
                        self._text(query),
                        {"task_code": task_code, "available_at": now},
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    return str(row["request_id"] or "")
        return ""

    def _influencer_pool_product_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row["job_id"]),
            "request_id": str(row["request_id"] or ""),
            "source_record_id": str(row["source_record_id"] or ""),
            "product_id": str(row["product_id"] or ""),
            "source_record": _load_json_dict(row["source_record_json"]),
            "status": str(row["status"] or ""),
            "stage": str(row["stage"] or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "matched_author_count": int(row["matched_author_count"] or 0),
            "queued_author_job_count": int(row["queued_author_job_count"] or 0),
            "last_error_text": str(row["last_error_text"] or ""),
            "last_error_type": str(row["last_error_type"] or ""),
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error_path": str(row["last_error_path"] or ""),
            "run_id": str(row["run_id"] or ""),
        }

    def claim_influencer_pool_author_job(
        self,
        *,
        request_id: str = "",
        product_id: str = "",
        source_record_id: str = "",
        worker_id: str,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'failed_retry',
                        stage = 'lease_expired',
                        worker_id = '',
                        lease_until = NULL,
                        updated_at = :updated_at
                    WHERE status = 'running'
                      AND lease_until IS NOT NULL
                      AND lease_until <= :now
                    """
                ),
                {"now": now, "updated_at": now},
            )
            row = (
                connection.execute(
                    self._text(
                        """
                        SELECT *
                        FROM influencer_pool_author_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND (:product_id = '' OR product_id = :product_id)
                          AND (:source_record_id = '' OR source_record_id = :source_record_id)
                          AND status IN ('pending', 'failed_retry')
                          AND available_at <= :available_at
                        ORDER BY created_at ASC, updated_at ASC
                        LIMIT 1
                        """
                    ),
                    {
                        "request_id": request_id,
                        "product_id": product_id,
                        "source_record_id": source_record_id,
                        "available_at": now,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'running',
                        stage = 'author_detail',
                        attempt_count = COALESCE(attempt_count, 0) + 1,
                        worker_id = :worker_id,
                        lease_until = :lease_until,
                        started_at = CASE WHEN started_at IS NULL THEN :now ELSE started_at END,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": row["job_id"],
                    "worker_id": worker_id,
                    "lease_until": now + max(lease_seconds, 5.0),
                    "now": now,
                },
            )
            claimed = (
                connection.execute(
                    self._text("SELECT * FROM influencer_pool_author_job WHERE job_id = :job_id"),
                    {"job_id": row["job_id"]},
                )
                .mappings()
                .first()
            )
            return self._influencer_pool_author_job_from_row(claimed) if claimed is not None else None

    def mark_influencer_pool_author_job_success(
        self,
        *,
        job_id: str,
        run_id: str,
        target_record_id: str = "",
        snapshot_id: str = "",
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'succeeded',
                        stage = 'completed',
                        target_record_id = :target_record_id,
                        snapshot_id = :snapshot_id,
                        run_id = :run_id,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "target_record_id": target_record_id,
                    "snapshot_id": snapshot_id,
                    "now": now,
                },
            )

    def mark_influencer_pool_author_job_skipped(
        self,
        *,
        job_id: str,
        run_id: str,
        stage: str,
        reason: str,
    ) -> None:
        now = time.time()
        with self._engine.begin() as connection:
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = 'skipped',
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :reason,
                        worker_id = '',
                        lease_until = NULL,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "run_id": run_id, "stage": stage, "reason": reason, "now": now},
            )

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
        now = time.time()
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    self._text("SELECT attempt_count, max_attempts FROM influencer_pool_author_job WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            attempt_count = int(row["attempt_count"] or 0) if row is not None else 0
            max_attempts = int(row["max_attempts"] or 1) if row is not None else 1
            status = "failed_retry" if attempt_count < max_attempts else "hard_failed"
            connection.execute(
                self._text(
                    """
                    UPDATE influencer_pool_author_job
                    SET status = :status,
                        stage = :stage,
                        run_id = :run_id,
                        last_error_text = :error_text,
                        last_error_type = :error_type,
                        last_error_code = :error_code,
                        last_error_path = :error_path,
                        worker_id = '',
                        lease_until = NULL,
                        available_at = :available_at,
                        heartbeat_at = :now,
                        updated_at = :now,
                        finished_at = CASE WHEN :status = 'hard_failed' THEN :now ELSE finished_at END
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "stage": stage,
                    "run_id": run_id,
                    "error_text": error_text,
                    "error_type": error_type,
                    "error_code": error_code,
                    "error_path": error_path,
                    "available_at": now + max(retry_delay_seconds, 0.1),
                    "now": now,
                },
            )

    def summarize_influencer_pool_author_jobs(
        self,
        *,
        request_id: str = "",
        product_id: str,
        source_record_id: str,
    ) -> dict[str, Any]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    self._text(
                        """
                        SELECT status, COUNT(*) AS count
                        FROM influencer_pool_author_job
                        WHERE (:request_id = '' OR request_id = :request_id)
                          AND product_id = :product_id
                          AND source_record_id = :source_record_id
                        GROUP BY status
                        """
                    ),
                    {
                        "request_id": request_id,
                        "product_id": product_id,
                        "source_record_id": source_record_id,
                    },
                )
                .mappings()
                .all()
            )
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        return {
            "total": sum(counts.values()),
            "counts": counts,
            "pending_count": counts.get("pending", 0),
            "running_count": counts.get("running", 0),
            "failed_retry_count": counts.get("failed_retry", 0),
            "succeeded_count": counts.get("succeeded", 0),
            "skipped_count": counts.get("skipped", 0),
            "hard_failed_count": counts.get("hard_failed", 0),
        }

    def _influencer_pool_author_job_from_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row["job_id"]),
            "request_id": str(row["request_id"] or ""),
            "source_record_id": str(row["source_record_id"] or ""),
            "product_id": str(row["product_id"] or ""),
            "influencer_id": str(row["influencer_id"] or ""),
            "uid": str(row["uid"] or ""),
            "sold_count": _coerce_float(row["sold_count"]),
            "follower_count": _coerce_float(row["follower_count"]),
            "holiday_name": str(row["holiday_name"] or ""),
            "source_images": _load_json_dict(row["source_images_json"]).get("value"),
            "author_row": _load_json_dict(row["author_row_json"]),
            "force_refresh": bool(int(row["force_refresh"] or 0)),
            "status": str(row["status"] or ""),
            "stage": str(row["stage"] or ""),
            "attempt_count": int(row["attempt_count"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "target_record_id": str(row["target_record_id"] or ""),
            "snapshot_id": str(row["snapshot_id"] or ""),
            "last_error_text": str(row["last_error_text"] or ""),
            "last_error_type": str(row["last_error_type"] or ""),
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error_path": str(row["last_error_path"] or ""),
            "run_id": str(row["run_id"] or ""),
        }

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
