"""Create execution control tables.

Revision ID: 20260412_0001
Revises:
Create Date: 2026-04-12 18:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260412_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_request",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("resource_code", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    op.create_table(
        "task_execution",
        sa.Column("execution_id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False, unique=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("resource_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("queue_seq", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("run_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("heartbeat_at", sa.Float(), nullable=True),
    )
    op.create_index(
        "idx_task_execution_status_queue_seq",
        "task_execution",
        ["status", "queue_seq"],
        unique=False,
    )
    op.create_index(
        "idx_task_execution_resource_queue_seq",
        "task_execution",
        ["resource_code", "queue_seq"],
        unique=False,
    )
    op.create_table(
        "resource_lease",
        sa.Column("resource_code", sa.Text(), primary_key=True),
        sa.Column("execution_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("heartbeat_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "artifact_object",
        sa.Column("artifact_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("idx_artifact_object_run_id", "artifact_object", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_artifact_object_run_id", table_name="artifact_object")
    op.drop_table("artifact_object")
    op.drop_table("resource_lease")
    op.drop_index("idx_task_execution_resource_queue_seq", table_name="task_execution")
    op.drop_index("idx_task_execution_status_queue_seq", table_name="task_execution")
    op.drop_table("task_execution")
    op.drop_table("task_request")
