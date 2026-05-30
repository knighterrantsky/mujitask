"""Add video creator identifiers and metric snapshots.

Revision ID: 20260528_0006
Revises: 20260509_0005
Create Date: 2026-05-28 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260528_0006"
down_revision = "20260509_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tk_videos
        ADD COLUMN IF NOT EXISTS creator_uid TEXT NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        ALTER TABLE tk_videos
        ADD COLUMN IF NOT EXISTS creator_unique_id TEXT NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tk_videos_creator_unique_id
        ON tk_videos(creator_unique_id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tk_video_product_unique
        ON tk_video_product_relations(video_key, product_id, source_platform)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tk_video_product_product_video
        ON tk_video_product_relations(product_id, video_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tk_video_product_video_product
        ON tk_video_product_relations(video_key, product_id)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tk_video_metric_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            video_key TEXT NOT NULL,
            video_id TEXT NOT NULL DEFAULT '',
            creator_key TEXT NOT NULL DEFAULT '',
            source_platform TEXT NOT NULL DEFAULT '',
            source_endpoint TEXT NOT NULL DEFAULT '',
            play_count DOUBLE PRECISION NOT NULL DEFAULT 0,
            digg_count DOUBLE PRECISION NOT NULL DEFAULT 0,
            comment_count DOUBLE PRECISION NOT NULL DEFAULT 0,
            share_count DOUBLE PRECISION NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            collected_at DOUBLE PRECISION NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tk_video_metric_snapshots_video_collected
        ON tk_video_metric_snapshots(video_key, collected_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tk_video_metric_snapshots_creator_collected
        ON tk_video_metric_snapshots(creator_key, collected_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tk_video_metric_snapshots_creator_collected")
    op.execute("DROP INDEX IF EXISTS idx_tk_video_metric_snapshots_video_collected")
    op.execute("DROP TABLE IF EXISTS tk_video_metric_snapshots")
    op.execute("DROP INDEX IF EXISTS idx_tk_video_product_video_product")
    op.execute("DROP INDEX IF EXISTS idx_tk_video_product_product_video")
    op.execute("DROP INDEX IF EXISTS idx_tk_video_product_unique")
    op.execute("DROP INDEX IF EXISTS idx_tk_videos_creator_unique_id")
    op.execute("ALTER TABLE tk_videos DROP COLUMN IF EXISTS creator_unique_id")
    op.execute("ALTER TABLE tk_videos DROP COLUMN IF EXISTS creator_uid")
