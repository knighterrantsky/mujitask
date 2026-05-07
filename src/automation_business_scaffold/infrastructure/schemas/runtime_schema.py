from __future__ import annotations

from typing import Any

from sqlalchemy import text as sql_text

POSTGRES_SCHEMA_LOCK_KEY = 426319877301

def ensure_runtime_schema(engine: Any) -> None:
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
    with engine.begin() as connection:
        dialect_name = str(connection.dialect.name or "").lower()
        if dialect_name.startswith("postgres"):
            connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({POSTGRES_SCHEMA_LOCK_KEY})")
        for statement in statements:
            connection.exec_driver_sql(statement)
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="worker_id",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )

        _ensure_column(
            connection,
            table_name="task_request",
            column_name="lease_until",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="heartbeat_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="progress_stage",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="last_progress_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="max_execution_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="error_type",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="error_code",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_request",
            column_name="dead_letter_reason",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="progress_stage",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="worker_pid",
            column_definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="last_progress_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="max_execution_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="max_idle_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="heartbeat_timeout_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="progress_seq",
            column_definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="progress_message",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="error_type",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="error_code",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="task_execution",
            column_name="dead_letter_reason",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="progress_stage",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="worker_pid",
            column_definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="last_progress_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="max_execution_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="max_idle_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="heartbeat_timeout_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="progress_seq",
            column_definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="progress_message",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="error_type",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="error_code",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="api_worker_job",
            column_name="dead_letter_reason",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="worker_id",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="lease_until",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="heartbeat_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="progress_stage",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="last_progress_at",
            column_definition="DOUBLE PRECISION",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="max_execution_seconds",
            column_definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="error_type",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="error_code",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="notification_outbox",
            column_name="dead_letter_reason",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="influencer_pool_author_job",
            column_name="force_refresh",
            column_definition="INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            connection,
            table_name="influencer_pool_product_job",
            column_name="request_id",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(
            connection,
            table_name="influencer_pool_author_job",
            column_name="request_id",
            column_definition="TEXT NOT NULL DEFAULT ''",
        )
        _ensure_postgres_double_precision_columns(connection)
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


def _has_column(connection: Any, *, table_name: str, column_name: str) -> bool:
    row = (
        connection.execute(
            sql_text(
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
    connection: Any,
    *,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    if _has_column(connection, table_name=table_name, column_name=column_name):
        return
    connection.exec_driver_sql(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
    )

def _ensure_postgres_double_precision_columns(connection: Any) -> None:
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
                    sql_text(
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

