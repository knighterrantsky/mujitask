"""Add complete durable object coordinates to TikTok media facts.

Revision ID: 20260723_0008
Revises: 20260714_0007
Create Date: 2026-07-23 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260723_0008"
down_revision = "20260714_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tk_media_assets",
        sa.Column("bucket", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "tk_media_assets",
        sa.Column("content_digest", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "tk_media_assets",
        sa.Column("remote_uri", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "tk_media_assets",
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("tk_media_assets", "size_bytes")
    op.drop_column("tk_media_assets", "remote_uri")
    op.drop_column("tk_media_assets", "content_digest")
    op.drop_column("tk_media_assets", "bucket")
