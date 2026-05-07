from __future__ import annotations

from typing import Any

from ..context import *

STAGE_CODE = "writeback_competitor_status"

def _advance_stage_writeback_competitor_status(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    writeback_jobs = _stage_api_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=WRITEBACK_STAGE_CODE,
        job_code="feishu_table_write",
    )
    if any(str(job.get("status") or "") in ACTIVE_STATUSES for job in writeback_jobs):
        return _waiting_stage_result(
            current_stage=WRITEBACK_STAGE_CODE,
            message="Competitor status writeback jobs are still running.",
        )
    return _advance_stage_result(next_stage=SUMMARY_STAGE_CODE)


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_writeback_competitor_status(store=store, request=request)
