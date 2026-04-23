"""Create FastMoss cookie cache table.

Revision ID: 20260422_0004
Revises: 20260422_0003
Create Date: 2026-04-22 02:10:00
"""

from __future__ import annotations

from alembic import op

revision = "20260422_0004"
down_revision = "20260422_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
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
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fastmoss_session_cookie_cache_account
            ON fastmoss_session_cookie_cache(namespace, account_key, region)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fastmoss_session_cookie_cache_expires
            ON fastmoss_session_cookie_cache(expires_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_fastmoss_session_cookie_cache_expires")
    op.execute("DROP INDEX IF EXISTS idx_fastmoss_session_cookie_cache_account")
    op.execute("DROP TABLE IF EXISTS fastmoss_session_cookie_cache")
