"""Remove derived remote URI from Amazon media facts.

Revision ID: 20260727_0009
Revises: 20260714_0007
Create Date: 2026-07-27 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260727_0009"
down_revision = "20260714_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE amazon_media_assets DROP COLUMN IF EXISTS remote_uri")


def downgrade() -> None:
    op.add_column(
        "amazon_media_assets",
        sa.Column("remote_uri", sa.Text(), nullable=False, server_default=""),
    )
