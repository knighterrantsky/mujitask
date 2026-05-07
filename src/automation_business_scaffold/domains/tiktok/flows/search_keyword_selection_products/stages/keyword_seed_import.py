from __future__ import annotations

from typing import Any


STAGE_CODE = "keyword_seed_import"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from .. import orchestrator

    return orchestrator._advance_keyword_seed_import(
        store=store,
        request=request,
        workflow=workflow,
    )
