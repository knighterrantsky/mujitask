"""Add complete durable object coordinates to TikTok media facts.

Revision ID: 20260723_0008
Revises: 20260714_0007
Create Date: 2026-07-23 12:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260723_0008"
down_revision = "20260714_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tk_media_assets ADD COLUMN IF NOT EXISTS "
        "bucket TEXT DEFAULT '' NOT NULL"
    )
    op.execute(
        "ALTER TABLE tk_media_assets ADD COLUMN IF NOT EXISTS "
        "content_digest TEXT DEFAULT '' NOT NULL"
    )
    op.execute(
        "ALTER TABLE tk_media_assets ADD COLUMN IF NOT EXISTS "
        "remote_uri TEXT DEFAULT '' NOT NULL"
    )
    op.execute(
        "ALTER TABLE tk_media_assets ADD COLUMN IF NOT EXISTS "
        "size_bytes BIGINT DEFAULT 0 NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tk_media_assets DROP COLUMN IF EXISTS size_bytes")
    op.execute("ALTER TABLE tk_media_assets DROP COLUMN IF EXISTS remote_uri")
    op.execute("ALTER TABLE tk_media_assets DROP COLUMN IF EXISTS content_digest")
    op.execute("ALTER TABLE tk_media_assets DROP COLUMN IF EXISTS bucket")
