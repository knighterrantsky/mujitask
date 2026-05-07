from __future__ import annotations

from typing import Any, Mapping

from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_api_jobs_active as _any_api_jobs_active,
    api_jobs_for_stage as _api_jobs_for_stage,
    extract_effective_result_payload,
    extract_handler_result_status,
    render_job_keys,
    timeout_seconds_for_workflow as _timeout_seconds,
    update_request_stage_cursor as _update_request_cursor,
)

from ..context import (
    RUNTIME_DB_PASSTHROUGH_KEYS,
    _fastmoss_search_settings_from_request_payload,
    _fastmoss_security_browser_fallback_attempted,
    _fastmoss_security_browser_fallback_cursor,
    _fastmoss_security_fallback_payload_from_job,
    _finalize_fastmoss_security_required,
    _first_text,
    _keyword_seed_import_retry_after_fastmoss_browser_exists,
    _keyword_seed_import_search_request,
    _latest_job,
    _payload_subset,
    _waiting,
)


STAGE_CODE = "keyword_seed_import"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "keyword_seed_import"
    jobs = _api_jobs_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    job_def = workflow.require_job("keyword_seed_import")
    if _any_api_jobs_active(jobs):
        return _waiting(stage_code=stage_code, message="Waiting for keyword seed import to finish.")

    retry_after_fastmoss_browser = False
    latest_import_job = _latest_job(jobs, job_code="keyword_seed_import") if jobs else None
    if latest_import_job and extract_handler_result_status(latest_import_job) == "fallback_required":
        if _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs):
            return _finalize_fastmoss_security_required(
                latest_import_job,
                details={"reason": "retry_after_browser_fallback_still_requires_security_verification"},
            )
        fallback_cursor = _fastmoss_security_browser_fallback_cursor(store=store, request_id=request.request_id)
        if fallback_cursor.get("status") == "success":
            retry_after_fastmoss_browser = True
        elif fallback_cursor.get("status") in {"failed", "partial_success"}:
            return _finalize_fastmoss_security_required(latest_import_job, details={"reason": "browser_fallback_failed"})
        elif _fastmoss_security_browser_fallback_attempted(store=store, request_id=request.request_id):
            return _finalize_fastmoss_security_required(latest_import_job, details={"reason": "browser_fallback_attempted"})
        else:
            fallback_payload = _fastmoss_security_fallback_payload_from_job(latest_import_job)
            _update_request_cursor(
                store=store,
                request=request,
                stage_code=stage_code,
                payload={
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_search_security_verification",
                    "fastmoss_security_fallback": fallback_payload,
                },
            )
            return {
                "action": "advance",
                "next_stage": "fastmoss_security_browser_fallback",
                "details": {
                    "fallback_required": True,
                    "fallback_reason": "fastmoss_search_security_verification",
                },
            }

    if not jobs or retry_after_fastmoss_browser:
        search_request = _keyword_seed_import_search_request(
            {
                **dict(request.payload or {}),
                "keyword_workflow_mode": "selection",
                "sales_7d_threshold": _first_text(request.payload.get("sales_7d_threshold"), request.payload.get("min_day7_sold_count"), "500"),
                "product_price_threshold": _first_text(request.payload.get("product_price_threshold"), request.payload.get("price_threshold"), "10.99"),
            },
            latest_import_job=latest_import_job,
            retry_after_fastmoss_browser=retry_after_fastmoss_browser,
        )
        search_query = str(search_request.get("search_query") or "")
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            search_request["fastmoss"] = fastmoss_settings

        seed_table_ref = str(request.payload.get("selection_table_ref") or request.payload.get("seed_table_ref") or request.payload.get("target_table_ref") or request.payload.get("table_url") or "")
        payload = {
            "stage_code": stage_code,
            "keyword_workflow_mode": "selection",
            "search_query": search_query,
            "search_digest": search_request["search_digest"],
            "search_request": search_request,
            "seed_write": {
                "target_table_ref": seed_table_ref,
                "write_mode": "insert_if_absent",
                "mapper_code": "selection_seed_projection_mapper",
            },
            **_payload_subset(
                request.payload,
                ("table_refs", "access_token", "access_token_env", "validate_schema")
                + RUNTIME_DB_PASSTHROUGH_KEYS,
            ),
        }
        if retry_after_fastmoss_browser:
            payload["fastmoss_security_browser_fallback_attempt"] = 1
            payload["fallback_source_job_id"] = str((latest_import_job or {}).get("job_id") or "")
        keys = render_job_keys(
            job_def,
            request.payload,
            payload,
            request_id=request.request_id,
            task_code=request.task_code,
            workflow_code=workflow.workflow_code,
            stage_code=stage_code,
            job_code=job_def.job_code,
        )
        dedupe_key = keys["dedupe_key"]
        if retry_after_fastmoss_browser:
            dedupe_key = f"{dedupe_key}:after-fastmoss-security-browser-fallback"
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=request.task_code,
            job_code=job_def.job_code,
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": dedupe_key,
                    "payload": payload,
                    "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                }
            ],
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued keyword seed import.",
            details={"dispatch_payload": {"keyword_seed_import": dispatch}},
        )

    import_job = latest_import_job
    payload = extract_effective_result_payload(import_job)
    candidates = [dict(item) for item in payload.get("normalized_candidates", []) if isinstance(item, Mapping)]
    seeds = [dict(item) for item in payload.get("seed_contexts", []) if isinstance(item, Mapping)]
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "candidate_contexts": candidates,
            "candidate_total_count": len(candidates),
            "seed_contexts": seeds,
            "seed_total_count": len(seeds),
            "successful_seed_count": sum(1 for item in seeds if item.get("seed_status") == "success"),
            "search_parameters": dict(payload.get("search_parameters") or {}),
            "search_filter_info": {
                "filters": dict((payload.get("search_parameters") or {}).get("filters") or {}),
                "output_conditions": dict((payload.get("search_parameters") or {}).get("output_conditions") or {}),
                "condition_context": dict((payload.get("search_parameters") or {}).get("condition_context") or {}),
                "sort": dict((payload.get("search_parameters") or {}).get("sort") or {}),
                "pagination": dict((payload.get("search_parameters") or {}).get("pagination") or {}),
            },
            "seed_write_results": [dict(item) for item in payload.get("seed_write_results", []) if isinstance(item, Mapping)],
        },
    )
    return {
        "action": "advance",
        "next_stage": "dispatch_selection_row_refresh_jobs",
        "details": {
            "candidate_total_count": len(candidates),
            "seed_total_count": len(seeds),
        },
    }
