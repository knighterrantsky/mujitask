"""Remove derived remote URI from TikTok media facts.

Revision ID: 20260727_0009
Revises: 20260723_0008
Create Date: 2026-07-27 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260727_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tk_media_assets", "remote_uri")


def downgrade() -> None:
    op.add_column(
        "tk_media_assets",
        sa.Column("remote_uri", sa.Text(), nullable=False, server_default=""),
    )
