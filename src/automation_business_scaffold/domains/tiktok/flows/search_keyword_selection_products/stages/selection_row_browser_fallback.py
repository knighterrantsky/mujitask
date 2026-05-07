from __future__ import annotations

from typing import Any


STAGE_CODE = "selection_row_browser_fallback"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_selection_row_browser_fallback(
        store=store,
        request=request,
        workflow=workflow,
    )
