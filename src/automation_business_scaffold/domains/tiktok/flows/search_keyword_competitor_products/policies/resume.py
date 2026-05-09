from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ResumeDecision:
    resumable: bool
    source_record_id: str
    handler_code: str
    payload: Mapping[str, Any] | None = None
