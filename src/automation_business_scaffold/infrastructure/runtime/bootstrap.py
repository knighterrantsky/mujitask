from __future__ import annotations

from typing import Any

from automation_business_scaffold.infrastructure.schemas.runtime_schema import ensure_runtime_schema


def bootstrap_runtime_schema(engine: Any) -> None:
    ensure_runtime_schema(engine)
