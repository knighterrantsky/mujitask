from __future__ import annotations

import hashlib
from typing import Any, Mapping

from ..context.models import *  # noqa: F403
from ..context.runtime_views import *  # noqa: F403
from ..context.stage_inputs import *  # noqa: F403
from ..context.decision_models import *  # noqa: F403
from ..context.summary_inputs import *  # noqa: F403

STAGE_CODE = "fastmoss_security_browser_fallback"


def _advance_stage_fastmoss_security_browser_fallback(*, store: RuntimeStore, request: Any) -> dict[str, Any]:
    candidates = _fastmoss_browser_fallback_candidates(store=store, request_id=request.request_id)
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=STAGE_CODE)
    if not candidates:
        if _any_browser_executions_active(executions):
            return _waiting_stage_result(
                current_stage=STAGE_CODE,
                message="Waiting for FastMoss security browser fallback to finish.",
            )
        return _advance_stage_result(
            next_stage=DISCOVER_CREATORS_STAGE_CODE,
            details={"fallback_candidate_count": 0},
        )

    fallback_digest = _fallback_digest(candidates)
    relevant_executions = [
        execution
        for execution in executions
        if _execution_payload(execution).get("fallback_digest") == fallback_digest
    ]
    if not relevant_executions:
        dispatch = _dispatch_fastmoss_browser_fallback(
            store=store,
            request=request,
            candidates=candidates,
            fallback_digest=fallback_digest,
        )
        return _waiting_stage_result(
            current_stage=STAGE_CODE,
            message="Enqueued FastMoss auth/security browser fallback.",
            details={"created_count": int(dispatch.get("created_count") or 0), "fallback_candidate_count": len(candidates)},
        )
    if _any_browser_executions_active(relevant_executions):
        return _waiting_stage_result(
            current_stage=STAGE_CODE,
            message="Waiting for FastMoss auth/security browser fallback to finish.",
        )

    execution = relevant_executions[-1]
    handler_status = extract_handler_result_status(execution)
    if handler_status in SUCCESSFUL_HANDLER_STATUSES:
        requeued = _requeue_fastmoss_waiting_jobs_after_browser(
            store=store,
            candidates=candidates,
            execution=execution,
        )
        source_stage = _source_stage_from_candidates(candidates)
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=STAGE_CODE,
            payload={
                "status": "success",
                "fallback_digest": fallback_digest,
                "source_stage_code": source_stage,
                "requeued_job_ids": [str(job.get("job_id") or "") for job in requeued],
                "browser_execution_id": str(getattr(execution, "execution_id", "") or ""),
            },
        )
        return _waiting_stage_result(
            current_stage=source_stage,
            message="Requeued FastMoss API jobs after browser fallback.",
            details={"requeued_count": len(requeued), "fallback_candidate_count": len(candidates)},
        )

    _update_request_cursor(
        store=store,
        request=request,
        stage_code=STAGE_CODE,
        payload={"status": "failed", "fallback_digest": fallback_digest, "execution_count": len(relevant_executions)},
    )
    failed_jobs = _fail_fastmoss_waiting_jobs_after_browser_failure(
        store=store,
        candidates=candidates,
        execution=execution,
    )
    source_stage = _source_stage_from_candidates(candidates)
    return _advance_stage_result(
        next_stage=source_stage,
        details={
            "error_code": "fastmoss_security_browser_fallback_failed",
            "reason": "fastmoss_auth_security_recovery_failed",
            "failed_waiting_job_count": len(failed_jobs),
        },
    )


def _dispatch_fastmoss_browser_fallback(
    *,
    store: RuntimeStore,
    request: Any,
    candidates: list[dict[str, Any]],
    fallback_digest: str,
) -> dict[str, Any]:
    job_def = SYNC_TK_INFLUENCER_POOL_WORKFLOW.require_job("fastmoss_security_browser_resolve")
    source_job = candidates[0]
    fallback_payload = _fastmoss_security_fallback_payload_from_job(source_job)
    payload = {
        **_payload_subset(request.payload, FASTMOSS_BROWSER_PASSTHROUGH_KEYS + RUNTIME_DB_PASSTHROUGH_KEYS),
        "stage_code": STAGE_CODE,
        "fallback_digest": fallback_digest,
        "source_stage_code": _source_stage_from_candidates(candidates),
        "source_job_ids": [str(job.get("job_id") or "") for job in candidates],
        "search_query": str(fallback_payload.get("search_query") or fallback_digest),
        "search_digest": fallback_digest,
        "search_request": dict(fallback_payload.get("search_request") or {}),
        "security_context": dict(fallback_payload.get("security_context") or {}),
        "verification_request": dict(fallback_payload.get("verification_request") or {}),
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
        workflow_code=WORKFLOW_CODE,
        stage_code=STAGE_CODE,
        item_code=job_def.job_code,
    )
    dispatch = store.enqueue_task_executions(
        request_id=request.request_id,
        item_code=job_def.job_code,
        workflow_code=WORKFLOW_CODE,
        items=[
            {
                "business_key": keys["business_key"] or f"fastmoss-security:{fallback_digest}",
                "dedupe_key": build_stage_local_dedupe_key(
                    keys["dedupe_key"],
                    job_def.job_code,
                    stage_scope=STAGE_CODE,
                ),
                "resource_code": _fastmoss_browser_resource_code(payload),
                "payload": payload,
                "max_execution_seconds": _timeout_seconds(SYNC_TK_INFLUENCER_POOL_WORKFLOW, job_def.job_code),
            }
        ],
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=STAGE_CODE,
        payload={
            "status": "pending",
            "fallback_digest": fallback_digest,
            "source_stage_code": payload["source_stage_code"],
            "fallback_candidate_count": len(candidates),
            "browser_dispatch": dispatch,
        },
    )
    return dispatch


def _requeue_fastmoss_waiting_jobs_after_browser(
    *,
    store: RuntimeStore,
    candidates: list[dict[str, Any]],
    execution: Any,
) -> list[dict[str, Any]]:
    requeued: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = _after_browser_api_payload(candidate=candidate, execution=execution)
        requeued.append(
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("job_id") or ""),
                payload=payload,
                stage=str(payload.get("stage_code") or ""),
            )
        )
    return requeued


def _fail_fastmoss_waiting_jobs_after_browser_failure(
    *,
    store: RuntimeStore,
    candidates: list[dict[str, Any]],
    execution: Any,
) -> list[dict[str, Any]]:
    error = _browser_execution_error(execution)
    failed: list[dict[str, Any]] = []
    for candidate in candidates:
        failed.append(
            store.mark_waiting_api_worker_job_failed(
                job_id=str(candidate.get("job_id") or ""),
                summary={
                    "handler_status": "failed",
                    "fallback_source_status": "failed",
                    "fallback_source_stage": _job_stage_code(candidate),
                    "browser_execution_id": str(getattr(execution, "execution_id", "") or ""),
                    "browser_error_code": error["error_code"],
                },
                result={
                    "status": "failed",
                    "fallback_required": False,
                    "browser_fallback_resolved": False,
                    "browser_execution_id": str(getattr(execution, "execution_id", "") or ""),
                    "browser_error": error,
                },
                error_text=error["message"],
                error_type=error["error_type"],
                error_code="fastmoss_security_browser_fallback_failed",
                dead_letter_reason="browser_fallback_failed",
            )
        )
    return failed


def _browser_execution_error(execution: Any) -> dict[str, str]:
    result = _execution_result(execution)
    handler_error = coerce_mapping(coerce_mapping(result.get("handler_result")).get("error"))
    return {
        "error_type": _first_non_empty(
            handler_error.get("error_type"),
            getattr(execution, "error_type", ""),
            "browser_failure",
        ),
        "error_code": _first_non_empty(
            handler_error.get("error_code"),
            getattr(execution, "error_code", ""),
            "fastmoss_security_browser_fallback_failed",
        ),
        "message": _first_non_empty(
            handler_error.get("message"),
            getattr(execution, "error_text", ""),
            "FastMoss auth/security browser fallback failed.",
        ),
    }


def _execution_result(execution: Any) -> dict[str, Any]:
    if isinstance(execution, Mapping):
        return coerce_mapping(execution.get("result"))
    return coerce_mapping(getattr(execution, "result", None))


def _after_browser_api_payload(*, candidate: Mapping[str, Any], execution: Any) -> dict[str, Any]:
    payload = dict(candidate.get("payload") or {})
    source_stage = str(payload.get("stage_code") or candidate.get("stage") or "")
    payload.update(
        {
            "stage_code": source_stage,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": "fastmoss_security_browser_resolve",
            "browser_execution_id": str(getattr(execution, "execution_id", "") or ""),
            "browser_execution_status": extract_handler_result_status(execution),
            "fastmoss_security_browser_fallback_attempt": int(
                payload.get("fastmoss_security_browser_fallback_attempt") or 0
            )
            + 1,
            "fallback_reason": "",
        }
    )
    payload.pop("force_fallback", None)
    return compact_dict(payload)


def _fallback_digest(candidates: list[dict[str, Any]]) -> str:
    raw = ",".join(sorted(str(candidate.get("job_id") or "") for candidate in candidates))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _source_stage_from_candidates(candidates: list[dict[str, Any]]) -> str:
    for candidate in candidates:
        stage_code = str((candidate.get("payload") or {}).get("stage_code") or candidate.get("stage") or "")
        if stage_code:
            return stage_code
    return DISCOVER_CREATORS_STAGE_CODE


def _execution_payload(execution: Any) -> dict[str, Any]:
    if isinstance(execution, Mapping):
        return coerce_mapping(execution.get("payload"))
    return coerce_mapping(getattr(execution, "payload", None))


def advance(*, store: Any, request: Any, workflow: Any) -> dict[str, Any]:
    del workflow
    return _advance_stage_fastmoss_security_browser_fallback(store=store, request=request)
