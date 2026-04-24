"""Acceptance-only helpers for rewrite verification.

This package is intentionally outside ``automation_business_scaffold.business``.
Runtime workflow code must not import it.
"""

from .comparator import (
    AcceptanceArtifactWriter,
    AchieveComparator,
    JsonRefResolver,
    compare_achieve_payload,
)
from .runtime_projection import (
    RuntimeAcceptanceArtifacts,
    build_fact_projection_from_store,
    build_feishu_projection,
    build_outbox_projection_from_store,
    build_runtime_acceptance_artifacts,
    build_runtime_trace_projection,
)

__all__ = [
    "AcceptanceArtifactWriter",
    "AchieveComparator",
    "JsonRefResolver",
    "RuntimeAcceptanceArtifacts",
    "build_fact_projection_from_store",
    "build_feishu_projection",
    "build_outbox_projection_from_store",
    "build_runtime_acceptance_artifacts",
    "build_runtime_trace_projection",
    "compare_achieve_payload",
]
