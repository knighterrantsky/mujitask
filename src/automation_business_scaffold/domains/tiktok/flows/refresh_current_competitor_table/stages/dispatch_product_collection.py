from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    update_request_stage_cursor as _update_request_cursor,
)

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "dispatch_product_collection"

def _advance_dispatch_product_collection(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "dispatch_product_collection"
    row_contexts = _row_contexts(store, request_id=request.request_id)
    source_table_ref = _source_table_ref_from_request_payload(request.payload)
    if not row_contexts:
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={"dispatched_row_count": 0},
        )
        return {
            "action": "advance",
            "next_stage": "collect_product_data",
            "details": {"dispatched_row_count": 0},
        }

    row_job_def = workflow.require_job("competitor_row_refresh")
    row_jobs: list[dict[str, Any]] = []
    for row in row_contexts:
        if not str(row.get("business_key") or ""):
            continue
        row_payload = {
            **_runtime_child_context(
                request=request,
                workflow=workflow,
                stage_code="collect_product_data",
            ),
            **_payload_subset(
                request.payload,
                FEISHU_WRITE_PASSTHROUGH_KEYS
                + TIKTOK_REQUEST_PASSTHROUGH_KEYS
                + FASTMOSS_PRODUCT_PASSTHROUGH_KEYS
                + FACT_PERSISTENCE_PASSTHROUGH_KEYS
                + ARTIFACT_PASSTHROUGH_KEYS,
            ),
            "request_payload": dict(request.payload or {}),
            "stage_code": "collect_product_data",
            "source_record_id": row["source_record_id"],
            "source_record_id_or_product_id": _first_text(row.get("source_record_id"), row.get("product_id")),
            "product_identity": dict(row["product_identity"]),
            "normalized_product_url": row.get("normalized_product_url") or "",
            "source_table_ref": source_table_ref,
            "source_context": dict(row["source_context"]),
            "fallback_allowed": bool(request.payload.get("fallback_allowed", True)),
        }
        row_keys = render_job_keys(
            row_job_def,
            request.payload,
            row,
            row_payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code="collect_product_data",
            job_code=row_job_def.job_code,
        )
        row_jobs.append(
            {
                "business_key": row_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
                "payload": row_payload,
                "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
            }
        )

    row_dispatch = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=row_jobs,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "dispatched_row_count": len(row_contexts),
            "row_dispatch": row_dispatch,
        },
    )
    return {
        "action": "advance",
        "next_stage": "collect_product_data",
        "details": {
            "dispatched_row_count": len(row_contexts),
            "row_refresh_created_count": int(row_dispatch["created_count"]),
        },
    }


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    return _advance_dispatch_product_collection(store=store, request=request, workflow=workflow)
