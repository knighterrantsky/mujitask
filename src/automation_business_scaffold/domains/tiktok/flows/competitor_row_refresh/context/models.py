from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RowIdentity:
    source_record_id: str
    source_table_ref: str
    business_key: str
    product_identity: dict[str, Any]


@dataclass
class RowPipelineState:
    warnings: list[str] = field(default_factory=list)
    optional_step_failed: bool = False
    runtime_evidence: dict[str, Any] = field(default_factory=dict)
    step_timeline: list[dict[str, Any]] = field(default_factory=list)
