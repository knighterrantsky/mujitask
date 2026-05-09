"""Add runtime result status columns.

Revision ID: 20260509_0005
Revises: 20260422_0004
Create Date: 2026-05-09 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260509_0005"
down_revision = "20260422_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("task_request", "task_execution", "api_worker_job"):
        op.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN IF NOT EXISTS result_status TEXT NOT NULL DEFAULT ''
            """
        )


def downgrade() -> None:
    for table_name in ("api_worker_job", "task_execution", "task_request"):
        op.execute(
            f"""
            ALTER TABLE {table_name}
            DROP COLUMN IF EXISTS result_status
            """
        )
