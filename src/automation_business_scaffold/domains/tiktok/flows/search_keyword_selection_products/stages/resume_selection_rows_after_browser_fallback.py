from __future__ import annotations

from typing import Any


STAGE_CODE = "resume_selection_rows_after_browser_fallback"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_resume_selection_rows_after_browser_fallback(
        store=store,
        request=request,
        workflow=workflow,
    )
