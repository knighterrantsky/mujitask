from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FallbackDecision:
    required: bool
    handler_code: str = ""
    reason: str = ""
    payload: Mapping[str, Any] | None = None


def row_fallback_key(*, source_record_id: str, business_entity_key: str, fallback_handler: str) -> str:
    row_key = str(source_record_id or business_entity_key or "").strip()
    return f"{fallback_handler}:{row_key}"
