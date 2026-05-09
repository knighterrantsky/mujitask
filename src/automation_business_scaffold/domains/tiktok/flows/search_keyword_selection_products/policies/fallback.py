from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FallbackDecision:
    required: bool
    handler_code: str = ""
    reason: str = ""
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ResumeDecision:
    resumable: bool
    source_record_id: str
    handler_code: str
    payload: Mapping[str, Any] | None = None


def row_fallback_key(*, source_record_id: str, fallback_handler: str) -> str:
    return f"{fallback_handler}:{source_record_id}"
