from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from automation_business_scaffold.contracts.handler.shared import coerce_mapping
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_browser_executions_active,
    browser_executions_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    extract_handler_result_status,
    is_fallback_required,
    render_job_keys,
)
from automation_business_scaffold.domains.tiktok.mappers.influencer_monitor_source_adapter import (
    INFLUENCER_MONITOR_READ_FIELD_NAMES,
)
from automation_business_scaffold.domains.tiktok.policies.influencer_monitor_candidate_policy import (
    aggregate_creator_candidates,
    normalize_min_video_sales_28d,
)
from automation_business_scaffold.domains.tiktok.workflows import (
    get_workflow_definition,
)


TASK_CODE = "monitor_tk_influencers"
WORKFLOW = get_workflow_definition(TASK_CODE)
WORKFLOW_CODE = WORKFLOW.workflow_code
READ_STAGE_CODE = "read_competitor_products"
DISCOVERY_STAGE_CODE = "discover_product_video_creators"
FALLBACK_STAGE_CODE = "fastmoss_security_browser_fallback"
SYNC_STAGE_CODE = "sync_monitored_influencers"
SUMMARY_STAGE_CODE = "ready_for_summary"
ACTIVE_STATUSES = {"pending", "running", "waiting"}
MAX_FASTMOSS_BROWSER_FALLBACK_ATTEMPTS = 1


def advance_stage(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    stage_code: str,
) -> dict[str, Any]:
    del workflow
    if stage_code == READ_STAGE_CODE:
        return _advance_read(store=store, request=request)
    if stage_code == DISCOVERY_STAGE_CODE:
        return _advance_discovery(store=store, request=request)
    if stage_code == FALLBACK_STAGE_CODE:
        return _advance_fallback(store=store, request=request)
    if stage_code == SYNC_STAGE_CODE:
        return _advance_sync(store=store, request=request)
    if stage_code == SUMMARY_STAGE_CODE:
        return finalize_request(store=store, request=request, workflow=WORKFLOW)
    return {
        "action": "finalize",
        "final_status": "failed",
        "summary": {
            "final_status": "failed",
            "warnings": [f"unsupported_stage:{stage_code}"],
        },
        "result": {
            "message": f"Unsupported monitor_tk_influencers stage {stage_code}."
        },
    }


def release_request_after_child_completion(
    store: Any,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        return []
    if str(request.status or "") in {"finished", "cancelled"}:
        return []
    stage_code = _current_stage(request)
    if stage_code == SUMMARY_STAGE_CODE:
        return []
    if stage_code in {DISCOVERY_STAGE_CODE, SYNC_STAGE_CODE}:
        if _fallback_candidates(
            store=store,
            request_id=request_id,
            source_stage_code=stage_code,
        ):
            stage_code = FALLBACK_STAGE_CODE
        elif _has_active_jobs(
            store=store,
            request_id=request_id,
            stage_code=stage_code,
        ):
            return []
    elif stage_code == FALLBACK_STAGE_CODE:
        executions = browser_executions_for_stage(
            store,
            request_id=request_id,
            stage_code=FALLBACK_STAGE_CODE,
        )
        if any_browser_executions_active(executions):
            return []
    elif stage_code == READ_STAGE_CODE and _has_active_jobs(
        store=store,
        request_id=request_id,
        stage_code=stage_code,
    ):
        return []
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=stage_code,
        progress_stage=stage_code,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return [{"request_id": request_id, "stage_code": stage_code, "released": True}]


def finalize_request(
    *,
    store: Any,
    request: Any,
    workflow: Any,
    force_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .summary import finalize_request as _finalize_request

    return _finalize_request(
        store=store,
        request=request,
        workflow=workflow,
        force_result=force_result,
    )


def _advance_read(*, store: Any, request: Any) -> dict[str, Any]:
    jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if not jobs:
        request_payload = dict(request.payload or {})
        job_def = WORKFLOW.resolve_stage_jobs(READ_STAGE_CODE)[0]
        keys = render_job_keys(
            job_def,
            request_payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=READ_STAGE_CODE,
        )
        payload = {
            "request_id": request.request_id,
            "task_code": TASK_CODE,
            "workflow_code": WORKFLOW_CODE,
            "stage_code": READ_STAGE_CODE,
            "request_payload": request_payload,
            "source_table_ref": request_payload.get("source_table_ref"),
            "target_table_ref": request_payload.get("source_table_ref"),
            "field_names": list(INFLUENCER_MONITOR_READ_FIELD_NAMES),
            "adapter_code": "influencer_monitor_source_adapter",
            "source_record_ids": list(request_payload.get("source_record_ids") or []),
            "snapshot_policy": {"store_raw_rows": False},
            **_feishu_common_payload(request_payload),
        }
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code="feishu_table_read",
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                }
            ],
        )
        return _waiting(
            READ_STAGE_CODE,
            "Dispatched TK competitor product read.",
            {"dispatch_payload": dispatch},
        )
    if _has_active_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
    ):
        return _waiting(READ_STAGE_CODE, "TK competitor product read is still running.")
    if _first_failed_job(jobs):
        return {
            "action": "finalize",
            "final_status": "failed",
            "failed_stage": READ_STAGE_CODE,
            "error_code": "monitor_source_read_failed",
            "title": "TK达人监控读取失败",
        }
    return _advance(DISCOVERY_STAGE_CODE, {"stage_transition": "source_read_terminal"})


def _advance_discovery(*, store: Any, request: Any) -> dict[str, Any]:
    jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVERY_STAGE_CODE,
        job_code="product_video_creator_discovery",
    )
    if not jobs:
        request_payload = dict(request.payload or {})
        threshold = normalize_min_video_sales_28d(
            request_payload.get("min_video_sales_28d")
        )
        job_def = WORKFLOW.resolve_stage_jobs(DISCOVERY_STAGE_CODE)[0]
        dispatch_jobs = []
        for source_row in _read_source_rows(
            store=store,
            request_id=request.request_id,
        ):
            product_id = str(source_row.get("product_id") or "").strip()
            if not product_id:
                continue
            payload = {
                "request_id": request.request_id,
                "task_code": TASK_CODE,
                "workflow_code": WORKFLOW_CODE,
                "stage_code": DISCOVERY_STAGE_CODE,
                "request_payload": request_payload,
                "product_id": product_id,
                "min_video_sales_28d": threshold,
                "source_record_ids": list(
                    source_row.get("source_record_ids") or []
                ),
                "source_product_images": list(
                    source_row.get("source_product_images") or []
                ),
                "holidays": list(source_row.get("holidays") or []),
                **_fastmoss_common_payload(request_payload),
            }
            keys = render_job_keys(
                job_def,
                payload,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=DISCOVERY_STAGE_CODE,
            )
            dispatch_jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                }
            )
        if not dispatch_jobs:
            return _advance(SYNC_STAGE_CODE, {"product_job_count": 0})
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code="product_video_creator_discovery",
            jobs=dispatch_jobs,
        )
        return _waiting(
            DISCOVERY_STAGE_CODE,
            "Dispatched product-video creator discovery jobs.",
            {"dispatch_payload": dispatch},
        )
    if _fallback_candidates(
        store=store,
        request_id=request.request_id,
        source_stage_code=DISCOVERY_STAGE_CODE,
    ):
        return _advance(
            FALLBACK_STAGE_CODE,
            {"fallback_source_stage": DISCOVERY_STAGE_CODE},
        )
    if _has_active_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=DISCOVERY_STAGE_CODE,
    ):
        return _waiting(
            DISCOVERY_STAGE_CODE,
            "Product-video creator discovery jobs are still running.",
        )
    return _advance(SYNC_STAGE_CODE, {"stage_transition": "product_jobs_terminal"})


def _advance_sync(*, store: Any, request: Any) -> dict[str, Any]:
    jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=SYNC_STAGE_CODE,
        job_code="influencer_monitor_sync",
    )
    if not jobs:
        request_payload = dict(request.payload or {})
        job_def = WORKFLOW.resolve_stage_jobs(SYNC_STAGE_CODE)[0]
        dispatch_jobs = []
        for candidate in aggregate_creator_candidates(
            _product_discovery_candidates(
                store=store,
                request_id=request.request_id,
            )
        ):
            creator_id = str(candidate.get("creator_id") or "").strip()
            if not creator_id:
                continue
            payload = {
                "request_id": request.request_id,
                "task_code": TASK_CODE,
                "workflow_code": WORKFLOW_CODE,
                "stage_code": SYNC_STAGE_CODE,
                "request_payload": request_payload,
                "target_table_ref": request_payload.get("target_table_ref"),
                "creator_id": creator_id,
                "creator_identity": {
                    "creator_id": creator_id,
                    "uid": str(candidate.get("uid") or creator_id),
                    "unique_id": str(candidate.get("unique_id") or ""),
                },
                "creator_run_max_sales_28d": candidate[
                    "creator_run_max_sales_28d"
                ],
                "product_hits": list(candidate.get("product_hits") or []),
                "source_product_images": list(
                    candidate.get("source_product_images") or []
                ),
                "holidays": list(candidate.get("holidays") or []),
                **_fastmoss_common_payload(request_payload),
                **_feishu_common_payload(request_payload),
            }
            keys = render_job_keys(
                job_def,
                payload,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=SYNC_STAGE_CODE,
            )
            dispatch_jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": payload,
                }
            )
        if not dispatch_jobs:
            return _advance(SUMMARY_STAGE_CODE, {"creator_job_count": 0})
        dispatch = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code="influencer_monitor_sync",
            jobs=dispatch_jobs,
        )
        return _waiting(
            SYNC_STAGE_CODE,
            "Dispatched monitored creator sync jobs.",
            {"dispatch_payload": dispatch},
        )
    if _fallback_candidates(
        store=store,
        request_id=request.request_id,
        source_stage_code=SYNC_STAGE_CODE,
    ):
        return _advance(FALLBACK_STAGE_CODE, {"fallback_source_stage": SYNC_STAGE_CODE})
    if _has_active_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=SYNC_STAGE_CODE,
    ):
        return _waiting(SYNC_STAGE_CODE, "Monitored creator sync jobs are still running.")
    return _advance(SUMMARY_STAGE_CODE, {"stage_transition": "creator_jobs_terminal"})


def _advance_fallback(*, store: Any, request: Any) -> dict[str, Any]:
    candidates = _fallback_candidates(store=store, request_id=request.request_id)
    executions = browser_executions_for_stage(
        store,
        request_id=request.request_id,
        stage_code=FALLBACK_STAGE_CODE,
    )
    if not candidates:
        if any_browser_executions_active(executions):
            return _waiting(FALLBACK_STAGE_CODE, "FastMoss browser recovery is running.")
        return _advance(_stage_after_fallback(store=store, request_id=request.request_id))
    digest = _fallback_digest(candidates)
    relevant = [
        execution
        for execution in executions
        if _execution_payload(execution).get("fallback_digest") == digest
    ]
    if not relevant:
        dispatch = _dispatch_fallback(
            store=store,
            request=request,
            candidates=candidates,
            fallback_digest=digest,
        )
        return _waiting(
            FALLBACK_STAGE_CODE,
            "Dispatched FastMoss browser recovery.",
            {"dispatch_payload": dispatch},
        )
    if any_browser_executions_active(relevant):
        return _waiting(FALLBACK_STAGE_CODE, "FastMoss browser recovery is running.")
    execution = relevant[-1]
    source_stage = _stage_after_fallback(store=store, request_id=request.request_id)
    if extract_handler_result_status(execution) in {"success", "partial_success"}:
        for candidate in candidates:
            candidate_stage = str(
                coerce_mapping(candidate.get("payload")).get("stage_code")
                or source_stage
            )
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("job_id") or ""),
                payload=_after_browser_payload(
                    candidate=candidate,
                    execution=execution,
                ),
                stage=candidate_stage,
            )
        return _waiting(source_stage, "Requeued FastMoss jobs after browser recovery.")
    for candidate in candidates:
        store.mark_waiting_api_worker_job_failed(
            job_id=str(candidate.get("job_id") or ""),
            summary={
                "handler_status": "failed",
                "fallback_source_status": "failed",
            },
            result={
                "status": "failed",
                "fallback_required": False,
                "browser_fallback_resolved": False,
            },
            error_text="FastMoss auth/security browser recovery failed.",
            error_type="browser_failure",
            error_code="fastmoss_security_browser_fallback_failed",
            dead_letter_reason="browser_fallback_failed",
        )
    return _advance(source_stage, {"failed_waiting_job_count": len(candidates)})


def _fallback_candidates(
    *,
    store: Any,
    request_id: str,
    source_stage_code: str = "",
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for stage_code, job_code in (
        (DISCOVERY_STAGE_CODE, "product_video_creator_discovery"),
        (SYNC_STAGE_CODE, "influencer_monitor_sync"),
    ):
        if source_stage_code and source_stage_code != stage_code:
            continue
        for job in _stage_jobs(
            store=store,
            request_id=request_id,
            stage_code=stage_code,
            job_code=job_code,
        ):
            payload = coerce_mapping(job.get("payload"))
            if str(job.get("status") or "") != "waiting":
                continue
            if (
                int(payload.get("fastmoss_security_browser_fallback_attempt") or 0)
                >= MAX_FASTMOSS_BROWSER_FALLBACK_ATTEMPTS
            ):
                continue
            if is_fallback_required(job):
                result.append(job)
    return result


def _dispatch_fallback(
    *,
    store: Any,
    request: Any,
    candidates: list[dict[str, Any]],
    fallback_digest: str,
) -> dict[str, Any]:
    job_def = WORKFLOW.require_job("fastmoss_security_browser_resolve")
    source_job = candidates[0]
    effective_result = extract_effective_result_payload(source_job)
    source_stage = str(
        coerce_mapping(source_job.get("payload")).get("stage_code")
        or DISCOVERY_STAGE_CODE
    )
    payload = {
        **_fastmoss_common_payload(dict(request.payload or {})),
        "stage_code": FALLBACK_STAGE_CODE,
        "fallback_digest": fallback_digest,
        "source_stage_code": source_stage,
        "source_job_ids": [str(item.get("job_id") or "") for item in candidates],
        "search_query": str(
            effective_result.get("operation")
            or source_job.get("business_key")
            or fallback_digest
        ),
        "search_digest": fallback_digest,
        "search_request": coerce_mapping(effective_result.get("request_payload")),
        "security_context": coerce_mapping(effective_result.get("security_context")),
        "verification_request": coerce_mapping(
            effective_result.get("verification_request")
        ),
        "request_payload": dict(request.payload or {}),
    }
    keys = render_job_keys(
        job_def,
        dict(request.payload or {}),
        payload,
        request_id=request.request_id,
        task_code=TASK_CODE,
        workflow_code=WORKFLOW_CODE,
        stage_code=FALLBACK_STAGE_CODE,
        item_code=job_def.job_code,
    )
    return store.enqueue_task_executions(
        request_id=request.request_id,
        item_code=job_def.job_code,
        workflow_code=WORKFLOW_CODE,
        items=[
            {
                "business_key": keys["business_key"]
                or f"fastmoss-security:{fallback_digest}",
                "dedupe_key": build_stage_local_dedupe_key(
                    keys["dedupe_key"],
                    job_def.job_code,
                    stage_scope=FALLBACK_STAGE_CODE,
                ),
                "resource_code": "fastmoss:browser",
                "payload": payload,
            }
        ],
    )


def _after_browser_payload(
    *,
    candidate: Mapping[str, Any],
    execution: Any,
) -> dict[str, Any]:
    payload = dict(coerce_mapping(candidate.get("payload")))
    payload.update(
        {
            "browser_fallback_resolved": True,
            "browser_fallback_handler": "fastmoss_security_browser_resolve",
            "browser_execution_id": _execution_attr(execution, "execution_id"),
            "browser_execution_status": extract_handler_result_status(execution),
            "fastmoss_security_browser_fallback_attempt": int(
                payload.get("fastmoss_security_browser_fallback_attempt") or 0
            )
            + 1,
        }
    )
    payload.pop("fallback_reason", None)
    return payload


def _read_source_rows(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    jobs = _stage_jobs(
        store=store,
        request_id=request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    for job in reversed(jobs):
        rows = extract_effective_result_payload(job).get("source_rows")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _product_discovery_candidates(
    *,
    store: Any,
    request_id: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for job in _stage_jobs(
        store=store,
        request_id=request_id,
        stage_code=DISCOVERY_STAGE_CODE,
        job_code="product_video_creator_discovery",
    ):
        payload = coerce_mapping(job.get("payload"))
        effective = extract_effective_result_payload(job)
        for candidate in effective.get("candidates") or []:
            if not isinstance(candidate, Mapping):
                continue
            result.append(
                {
                    **dict(candidate),
                    "source_record_ids": list(
                        payload.get("source_record_ids") or []
                    ),
                    "source_product_images": list(
                        payload.get("source_product_images") or []
                    ),
                    "holidays": list(payload.get("holidays") or []),
                }
            )
    return result


def _stage_after_fallback(*, store: Any, request_id: str) -> str:
    if _stage_jobs(
        store=store,
        request_id=request_id,
        stage_code=SYNC_STAGE_CODE,
        job_code="influencer_monitor_sync",
    ):
        return SYNC_STAGE_CODE
    return DISCOVERY_STAGE_CODE


def _stage_jobs(
    *,
    store: Any,
    request_id: str,
    stage_code: str,
    job_code: str | None = None,
) -> list[dict[str, Any]]:
    list_jobs = getattr(store, "list_api_worker_jobs_for_request")
    try:
        jobs = (
            list_jobs(request_id=request_id, job_code=job_code)
            if job_code
            else list_jobs(request_id=request_id)
        )
    except TypeError:
        jobs = list_jobs(request_id=request_id)
    return [
        dict(job)
        for job in jobs
        if str(coerce_mapping(job.get("payload")).get("stage_code") or "")
        == stage_code
    ]


def _first_failed_job(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    for job in jobs:
        if extract_handler_result_status(job) == "failed" or str(
            job.get("result_status") or job.get("status") or ""
        ) == "failed":
            return job
    return {}


def _has_active_jobs(*, store: Any, request_id: str, stage_code: str) -> bool:
    return any(
        str(job.get("status") or "") in ACTIVE_STATUSES
        for job in _stage_jobs(
            store=store,
            request_id=request_id,
            stage_code=stage_code,
        )
    )


def _current_stage(request: Any) -> str:
    return str(
        getattr(request, "current_stage", "")
        or getattr(request, "progress_stage", "")
        or READ_STAGE_CODE
    )


def _feishu_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_user_access_token",
        "table_refs",
        "validate_schema",
    )
    return {key: request_payload[key] for key in keys if key in request_payload}


def _fastmoss_common_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request_payload.items()
        if str(key).startswith(("fastmoss", "mock_fastmoss"))
        or key in {"browser_cookies"}
    }


def _fallback_digest(candidates: list[dict[str, Any]]) -> str:
    raw = ",".join(
        sorted(
            f"{candidate.get('job_id') or ''}:"
            f"{coerce_mapping(candidate.get('payload')).get('fastmoss_security_browser_fallback_attempt') or 0}"
            for candidate in candidates
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _execution_payload(execution: Any) -> dict[str, Any]:
    value = (
        execution.get("payload")
        if isinstance(execution, Mapping)
        else getattr(execution, "payload", None)
    )
    return coerce_mapping(value)


def _execution_attr(execution: Any, key: str) -> str:
    if isinstance(execution, Mapping):
        return str(execution.get(key) or "")
    return str(getattr(execution, key, "") or "")


def _waiting(
    stage_code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": details or {},
    }


def _advance(
    next_stage: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": "advance",
        "next_stage": next_stage,
        "details": details or {},
    }


__all__ = [
    "DISCOVERY_STAGE_CODE",
    "FALLBACK_STAGE_CODE",
    "READ_STAGE_CODE",
    "SUMMARY_STAGE_CODE",
    "SYNC_STAGE_CODE",
    "TASK_CODE",
    "advance_stage",
    "finalize_request",
    "release_request_after_child_completion",
]
