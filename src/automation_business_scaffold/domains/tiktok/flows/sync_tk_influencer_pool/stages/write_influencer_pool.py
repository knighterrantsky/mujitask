from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import render_job_keys

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403

STAGE_CODE = "write_influencer_pool"


def _advance_stage_write_influencer_pool(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    write_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITE_POOL_STAGE_CODE,
        job_code="feishu_table_write",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in write_jobs):
        return _waiting_stage_result(
            current_stage=WRITE_POOL_STAGE_CODE,
            message="Influencer pool write jobs are still running.",
        )
    return _advance_stage_result(next_stage=FINALIZE_PRODUCT_STAGE_CODE)


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_write_influencer_pool(store=store, request=request)
