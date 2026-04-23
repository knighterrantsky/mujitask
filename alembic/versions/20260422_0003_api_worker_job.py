"""Create generic API worker job queue table.

Revision ID: 20260422_0003
Revises: 20260421_0002
Create Date: 2026-04-22 00:40:00
"""

from __future__ import annotations

from alembic import op

revision = "20260422_0003"
down_revision = "20260421_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
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
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            payload_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error_text TEXT NOT NULL DEFAULT '',
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_api_worker_job_status_available_created
            ON api_worker_job(status, available_at, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_api_worker_job_request_created
            ON api_worker_job(request_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_api_worker_job_job_code_status_available
            ON api_worker_job(job_code, status, available_at)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_worker_job_dedupe_key
            ON api_worker_job(dedupe_key)
            WHERE dedupe_key <> ''
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_api_worker_job_dedupe_key")
    op.execute("DROP INDEX IF EXISTS idx_api_worker_job_job_code_status_available")
    op.execute("DROP INDEX IF EXISTS idx_api_worker_job_request_created")
    op.execute("DROP INDEX IF EXISTS idx_api_worker_job_status_available_created")
    op.execute("DROP TABLE IF EXISTS api_worker_job")
