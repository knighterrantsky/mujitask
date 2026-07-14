"""Add isolated Amazon product fact tables.

Revision ID: 20260714_0007
Revises: 20260528_0006
Create Date: 2026-07-14 00:00:00
"""

from __future__ import annotations

from alembic import op

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_INDEX_NAMES,
    AMAZON_FACT_SCHEMA_STATEMENTS,
    AMAZON_FACT_TABLES,
)

revision = "20260714_0007"
down_revision = "20260528_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in AMAZON_FACT_SCHEMA_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for index_name in reversed(AMAZON_FACT_INDEX_NAMES):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    for table_name in AMAZON_FACT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
