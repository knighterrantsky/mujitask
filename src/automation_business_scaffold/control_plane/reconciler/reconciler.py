from __future__ import annotations

from typing import Any

from automation_business_scaffold.control_plane.executor.runner import (
    _release_request_after_child_completion,
)
from automation_business_scaffold.control_plane.reconciler.views import aggregate_request_child_counts
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def reconcile_parent_after_child_completion(
    *,
    store: RuntimeStore,
    request_id: str,
) -> list[dict[str, Any]]:
    return _release_request_after_child_completion(store, request_id=request_id)


__all__ = ["aggregate_request_child_counts", "reconcile_parent_after_child_completion"]
