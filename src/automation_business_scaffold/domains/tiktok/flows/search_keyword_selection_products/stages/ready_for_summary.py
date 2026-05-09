from __future__ import annotations

from typing import Any


STAGE_CODE = "ready_for_summary"


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    from ..summary import finalize_request

    return finalize_request(store=store, request=request, workflow=workflow)
