from __future__ import annotations

from typing import Any


STAGE_CODE = "refresh_selection_rows"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_refresh_selection_rows(
        store=store,
        request=request,
        workflow=workflow,
    )
