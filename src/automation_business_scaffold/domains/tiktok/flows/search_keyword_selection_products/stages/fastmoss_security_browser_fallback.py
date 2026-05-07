from __future__ import annotations

from typing import Any


STAGE_CODE = "fastmoss_security_browser_fallback"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_fastmoss_security_browser_fallback(
        store=store,
        request=request,
        workflow=workflow,
    )
