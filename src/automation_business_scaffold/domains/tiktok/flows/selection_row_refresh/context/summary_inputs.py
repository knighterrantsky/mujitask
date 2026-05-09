from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RowSummaryInput:
    source_record_id: str
    business_key: str
    row_status: str
    browser_fallback_used: bool = False
    warnings: tuple[str, ...] = ()
    payload: dict[str, Any] | None = None
