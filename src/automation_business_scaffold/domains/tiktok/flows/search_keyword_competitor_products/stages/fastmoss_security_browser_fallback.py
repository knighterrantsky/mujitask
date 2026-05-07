from __future__ import annotations

from typing import Any

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_browser_executions_active as _any_browser_executions_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    extract_handler_result_status,
    render_job_keys,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context import (
    FASTMOSS_BROWSER_PASSTHROUGH_KEYS,
    RUNTIME_DB_PASSTHROUGH_KEYS,
    _fastmoss_browser_resource_code,
    _fastmoss_search_settings_from_request_payload,
    _fastmoss_security_fallback_payload_from_job,
    _latest_job,
    _payload_subset,
    _waiting,
)


STAGE_CODE = "fastmoss_security_browser_fallback"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "fastmoss_security_browser_fallback"
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    if not executions:
        import_job = _latest_job(
            _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code="keyword_seed_import"),
            job_code="keyword_seed_import",
        )
        if not import_job or extract_handler_result_status(import_job) != "fallback_required":
            return {"action": "advance", "next_stage": "keyword_seed_import", "details": {"fallback_candidate_count": 0}}

        fallback_payload = _fastmoss_security_fallback_payload_from_job(import_job)
        job_def = workflow.require_job("fastmoss_security_browser_resolve")
        payload = {
            **_payload_subset(request.payload, FASTMOSS_BROWSER_PASSTHROUGH_KEYS + RUNTIME_DB_PASSTHROUGH_KEYS),
            "stage_code": stage_code,
            "search_query": str(fallback_payload.get("search_query") or request.payload.get("search_query") or ""),
            "search_digest": str(fallback_payload.get("search_digest") or (import_job.get("payload") or {}).get("search_digest") or ""),
            "search_request": dict(fallback_payload.get("search_request") or {}),
            "security_context": dict(fallback_payload.get("security_context") or {}),
            "fallback_source_job_id": str(import_job.get("job_id") or ""),
            "request_payload": dict(request.payload or {}),
        }
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
        keys = render_job_keys(
            job_def,
            request.payload,
            fallback_payload,
            payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
            item_code=job_def.job_code,
        )
        dispatch = store.enqueue_task_executions(
            request_id=request.request_id,
            item_code=job_def.job_code,
            workflow_code=workflow.workflow_code,
            items=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": build_stage_local_dedupe_key(
                        keys["dedupe_key"],
                        job_def.job_code,
                        stage_scope=stage_code,
                    ),
                    "resource_code": _fastmoss_browser_resource_code(payload),
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "status": "pending",
                "browser_dispatch": dispatch,
                "fallback_source_job_id": str(import_job.get("job_id") or ""),
                "search_request": dict(fallback_payload.get("search_request") or {}),
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued FastMoss security browser fallback.",
            details={"created_count": int(dispatch["created_count"])},
        )
    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for FastMoss security browser fallback to finish.")

    execution = executions[-1]
    handler_status = extract_handler_result_status(execution)
    if handler_status == "success":
        execution_payload = extract_effective_result_payload(execution)
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "status": "success",
                "verified_path": str(execution_payload.get("verified_path") or "/api/goods/V2/search"),
                "cookie_cache": dict(execution_payload.get("cookie_cache") or {}),
                "fallback_source_job_id": str((execution.payload or {}).get("fallback_source_job_id") or ""),
            },
        )
        return {
            "action": "advance",
            "next_stage": "keyword_seed_import",
            "details": {"fastmoss_security_browser_fallback": "success"},
        }

    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={"status": "failed", "execution_count": len(executions)},
    )
    return {
        "action": "finalize",
        "final_status": "failed",
        "details": {
            "error_code": "fastmoss_security_verification_required",
            "reason": "fastmoss_security_browser_fallback_failed",
        },
    }
