"""Explicit database schema bootstrap entrypoints."""

from .fact_schema import ensure_tk_fact_schema
from .runtime_schema import ensure_runtime_schema

__all__ = ["ensure_runtime_schema", "ensure_tk_fact_schema"]
