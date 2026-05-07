from __future__ import annotations

from typing import Any


STAGE_CODE = "dispatch_selection_row_refresh_jobs"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_dispatch_selection_row_refresh_jobs(
        store=store,
        request=request,
        workflow=workflow,
    )
