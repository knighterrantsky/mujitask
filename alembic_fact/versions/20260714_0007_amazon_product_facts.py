"""Add isolated Amazon product fact tables.

Revision ID: 20260714_0007
Revises:
Create Date: 2026-07-14 00:00:00
"""

from __future__ import annotations

import os
import re

from alembic import op

from automation_business_scaffold.infrastructure.schemas.amazon_fact_schema import (
    AMAZON_FACT_INDEX_NAMES,
    AMAZON_FACT_SCHEMA_STATEMENTS,
    AMAZON_FACT_TABLES,
    AMAZON_FACT_VERSION_TABLE,
)

revision = "20260714_0007"
down_revision = None
branch_labels = None
depends_on = None

_RUNTIME_ROLE_ENV = "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE"
_POSTGRES_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VERSION_TABLE = AMAZON_FACT_VERSION_TABLE


def _runtime_role() -> str:
    role = os.getenv(_RUNTIME_ROLE_ENV, "").strip()
    if not role:
        return ""
    if len(role) > 63 or _POSTGRES_IDENTIFIER.fullmatch(role) is None:
        raise RuntimeError(
            f"{_RUNTIME_ROLE_ENV} must be an unquoted PostgreSQL identifier of at most "
            "63 characters."
        )
    return role


def _grant_runtime_privileges(role: str) -> None:
    bind = op.get_bind()
    preparer = bind.dialect.identifier_preparer
    schema = str(bind.dialect.default_schema_name or "public")
    quoted_schema = preparer.quote_identifier(schema)
    quoted_role = preparer.quote_identifier(role)
    qualified_tables = ", ".join(
        f"{quoted_schema}.{preparer.quote_identifier(table_name)}"
        for table_name in reversed(AMAZON_FACT_TABLES)
    )
    qualified_version_table = f"{quoted_schema}.{preparer.quote_identifier(_VERSION_TABLE)}"

    op.execute(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {qualified_tables} TO {quoted_role}")
    op.execute(f"GRANT SELECT ON TABLE {qualified_version_table} TO {quoted_role}")


def upgrade() -> None:
    runtime_role = _runtime_role()
    for statement in AMAZON_FACT_SCHEMA_STATEMENTS:
        op.execute(statement)
    if runtime_role:
        _grant_runtime_privileges(runtime_role)


def downgrade() -> None:
    for index_name in reversed(AMAZON_FACT_INDEX_NAMES):
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    for table_name in AMAZON_FACT_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
