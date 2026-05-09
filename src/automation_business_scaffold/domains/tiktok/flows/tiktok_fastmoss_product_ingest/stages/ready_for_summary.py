from __future__ import annotations

from typing import Any

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "ready_for_summary"

def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del store, request, workflow
    return {"action": "finalize"}
