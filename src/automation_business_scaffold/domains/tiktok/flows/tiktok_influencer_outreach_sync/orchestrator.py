from __future__ import annotations

import hashlib
import time
from datetime import date
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import (
    coerce_mapping,
    coerce_mapping_list,
)
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    any_browser_executions_active,
    browser_executions_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    extract_handler_result_status,
    is_fallback_required,
    render_job_keys,
)
from automation_business_scaffold.domains.tiktok.mappers.feishu_outreach_source_mapper import (
    OUTREACH_READ_FIELD_NAMES,
    group_outreach_rows_by_product,
)
from automation_business_scaffold.domains.tiktok.projections.outbox_message_projection import (
    build_tiktok_outbox_message_text as build_outbox_message_text,
)
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

TASK_CODE = "tiktok_influencer_outreach_sync"
WORKFLOW = get_workflow_definition(TASK_CODE)
WORKFLOW_CODE = WORKFLOW.workflow_code
READ_STAGE_CODE = "read_outreach_rows"
CHECK_STAGE_CODE = "index_product_videos"
FALLBACK_STAGE_CODE = "fastmoss_security_browser_fallback"
REFRESH_STAGE_CODE = "refresh_creator_video_metrics_and_writeback"
SUMMARY_STAGE_CODE = "ready_for_summary"
ACTIVE_STATUSES = {"pending", "running", "waiting"}
TERMINAL_STATUSES = {"success", "skipped", "partial_success", "failed"}
MAX_FASTMOSS_BROWSER_FALLBACK_ATTEMPTS = 3


def advance_stage(*, store: Any, request: Any, workflow: Any, stage_code: str) -> dict[str, Any]:
    del workflow
    if stage_code == READ_STAGE_CODE:
        return _advance_read(store=store, request=request)
    if stage_code == CHECK_STAGE_CODE:
        return _advance_check(store=store, request=request)
    if stage_code == FALLBACK_STAGE_CODE:
        return _advance_fallback(store=store, request=request)
    if stage_code == REFRESH_STAGE_CODE:
        return _advance_refresh(store=store, request=request)
    if stage_code == SUMMARY_STAGE_CODE:
        return finalize_request(store=store, request=request, workflow=WORKFLOW)
    return {
        "action": "finalize",
        "final_status": "failed",
        "summary": {"final_status": "failed", "warnings": [f"unsupported_stage:{stage_code}"]},
        "result": {"message": f"Unsupported tiktok_influencer_outreach_sync stage {stage_code}."},
    }


def release_request_after_child_completion(store: Any, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    if request.task_code != TASK_CODE:
        return []
    if str(request.status or "") in {"finished", "cancelled"}:
        return []
    current_stage = _current_stage(request)
    if current_stage not in {
        READ_STAGE_CODE,
        CHECK_STAGE_CODE,
        FALLBACK_STAGE_CODE,
        REFRESH_STAGE_CODE,
    }:
        return []
    if current_stage in {CHECK_STAGE_CODE, REFRESH_STAGE_CODE}:
        if _fallback_candidates(
            store=store, request_id=request_id, source_stage_code=current_stage
        ):
            current_stage = FALLBACK_STAGE_CODE
        elif _has_pending_or_running_jobs(
            store=store, request_id=request_id, stage_code=current_stage
        ):
            return []
    elif current_stage == FALLBACK_STAGE_CODE:
        executions = browser_executions_for_stage(
            store, request_id=request_id, stage_code=FALLBACK_STAGE_CODE
        )
        if any_browser_executions_active(executions):
            return []
    elif _has_active_jobs(store=store, request_id=request_id, stage_code=current_stage):
        return []
    store.update_task_request(
        request_id=request_id,
        status="pending",
        current_stage=current_stage,
        progress_stage=current_stage,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
    )
    return [{"request_id": request_id, "stage_code": current_stage, "released": True}]


def finalize_request(
    *, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None
) -> dict[str, Any]:
    del workflow
    summary = force_result or _build_summary(store=store, request=request)
    final_status = str(summary.get("final_status") or "success")
    result = {"summary": summary, "title": "达人建联检查完成"}
    updated = store.update_task_request(
        request_id=request.request_id,
        status=final_status,
        current_stage=SUMMARY_STAGE_CODE,
        progress_stage=SUMMARY_STAGE_CODE,
        summary=summary,
        result=result,
        worker_id="",
        lease_until=0.0,
        heartbeat_at=0.0,
        error_text="",
        error_type="",
        error_code="",
        dead_letter_reason="",
        finished_at=time.time(),
    )
    outbox = store.create_notification_outbox(
        channel_code=str(getattr(request, "source_channel_code", "") or "noop"),
        event_type="task_request.completed",
        ref_id=request.request_id,
        reply_target=str(getattr(request, "reply_target", "") or ""),
        payload={
            "request_id": request.request_id,
            "task_code": request.task_code,
            "workflow_code": WORKFLOW_CODE,
            "summary_payload": summary,
            "result": result,
            "message_text": build_outbox_message_text(
                request_id=request.request_id,
                task_code=request.task_code,
                summary=summary,
                result=result,
                message_format=str(
                    (getattr(request, "payload", {}) or {}).get("outbox_message_format") or ""
                ),
                message_template=str(
                    (getattr(request, "payload", {}) or {}).get("outbox_message_template") or ""
                ),
            ),
        },
        dedupe_key=f"task_request.completed:{request.request_id}",
    )
    return {
        "action": "finalized",
        "request_id": request.request_id,
        "request_status": updated.result_status or updated.status,
        "status": updated.status,
        "result_status": updated.result_status,
        "current_stage": updated.current_stage,
        "summary": updated.summary,
        "result": updated.result,
        "task_request": updated.to_dict(),
        "outbox": [outbox.to_dict()],
    }


def _advance_read(*, store: Any, request: Any) -> dict[str, Any]:
    read_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=READ_STAGE_CODE,
        job_code="feishu_table_read",
    )
    if not read_jobs:
        request_payload = dict(request.payload or {})
        resolved_job = WORKFLOW.resolve_stage_jobs(READ_STAGE_CODE)[0]
        keys = render_job_keys(
            resolved_job,
            request_payload,
            request_id=request.request_id,
            task_code=TASK_CODE,
            workflow_code=WORKFLOW_CODE,
            stage_code=READ_STAGE_CODE,
        )
        enqueue_result = store.enqueue_api_worker_jobs(
            request_id=request.request_id,
            task_code=TASK_CODE,
            job_code="feishu_table_read",
            jobs=[
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": READ_STAGE_CODE,
                        "request_payload": request_payload,
                        "source_table_ref": request_payload.get("source_table_ref"),
                        "target_table_ref": request_payload.get("target_table_ref")
                        or request_payload.get("source_table_ref"),
                        "field_names": list(OUTREACH_READ_FIELD_NAMES),
                        "adapter_code": "outreach_source_adapter",
                        "source_record_ids": list(request_payload.get("source_record_ids") or []),
                        **_feishu_common_payload(request_payload),
                    },
                }
            ],
        )
        return _waiting(
            READ_STAGE_CODE,
            "Executor dispatched outreach table read.",
            {"dispatch_payload": enqueue_result},
        )
    if _has_active_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE):
        return _waiting(READ_STAGE_CODE, "Outreach table read is still running.")
    failed_job = _first_failed_job(read_jobs)
    if failed_job:
        return {
            "action": "finalize",
            "final_status": "failed",
            "title": "达人建联读取失败",
            "read_status": "failed",
            "failed_stage": READ_STAGE_CODE,
            "failed_job_code": "feishu_table_read",
            "failed_job_id": str(failed_job.get("job_id") or ""),
            "error_code": str(failed_job.get("error_code") or ""),
            "error": str(failed_job.get("error_text") or ""),
        }
    return _advance(CHECK_STAGE_CODE, {"stage_transition": "outreach_rows_read"})


def _advance_check(*, store: Any, request: Any) -> dict[str, Any]:
    existing = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=CHECK_STAGE_CODE,
        job_code="product_video_outreach_check",
    )
    if not existing:
        request_payload = dict(request.payload or {})
        trigger_date = str(request_payload.get("trigger_date") or date.today().isoformat())
        source_rows = _read_source_rows(store=store, request_id=request.request_id)
        product_groups = group_outreach_rows_by_product(
            source_rows, trigger_date=trigger_date, request_payload=request_payload
        )
        resolved_job = WORKFLOW.resolve_stage_jobs(CHECK_STAGE_CODE)[0]
        jobs = []
        for group in product_groups:
            keys = render_job_keys(
                resolved_job,
                group,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=CHECK_STAGE_CODE,
            )
            jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": CHECK_STAGE_CODE,
                        "request_payload": request_payload,
                        **group,
                        **_fastmoss_common_payload(request_payload),
                    },
                }
            )
        enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0}
        if jobs:
            enqueue_result = store.enqueue_api_worker_jobs(
                request_id=request.request_id,
                task_code=TASK_CODE,
                job_code="product_video_outreach_check",
                jobs=jobs,
            )
        if jobs:
            return _waiting(
                CHECK_STAGE_CODE,
                "Executor dispatched API product video outreach checks.",
                {"dispatch_payload": enqueue_result},
            )
        return _advance(REFRESH_STAGE_CODE, {"candidate_count": 0})
    if _fallback_candidates(
        store=store, request_id=request.request_id, source_stage_code=CHECK_STAGE_CODE
    ):
        return _advance(
            FALLBACK_STAGE_CODE,
            {"stage_transition": "product_video_check_requires_browser_fallback"},
        )
    if _has_pending_or_running_jobs(
        store=store, request_id=request.request_id, stage_code=CHECK_STAGE_CODE
    ):
        return _waiting(CHECK_STAGE_CODE, "API product video outreach checks are still running.")
    return _advance(REFRESH_STAGE_CODE, {"stage_transition": "product_video_indexes_terminal"})


def _advance_fallback(*, store: Any, request: Any) -> dict[str, Any]:
    candidates = _fallback_candidates(store=store, request_id=request.request_id)
    executions = browser_executions_for_stage(
        store, request_id=request.request_id, stage_code=FALLBACK_STAGE_CODE
    )
    if not candidates:
        if any_browser_executions_active(executions):
            return _waiting(
                FALLBACK_STAGE_CODE, "Waiting for FastMoss security browser fallback to finish."
            )
        return _advance(
            _stage_after_fallback(store=store, request_id=request.request_id),
            {"fallback_candidate_count": 0},
        )
    fallback_digest = _fallback_digest(candidates)
    relevant_executions = [
        execution
        for execution in executions
        if _execution_payload(execution).get("fallback_digest") == fallback_digest
    ]
    if not relevant_executions:
        dispatch = _dispatch_fallback(
            store=store, request=request, candidates=candidates, fallback_digest=fallback_digest
        )
        return _waiting(
            FALLBACK_STAGE_CODE,
            "Enqueued FastMoss security browser fallback.",
            {"dispatch_payload": dispatch, "fallback_candidate_count": len(candidates)},
        )
    if any_browser_executions_active(relevant_executions):
        return _waiting(
            FALLBACK_STAGE_CODE, "Waiting for FastMoss security browser fallback to finish."
        )
    execution = relevant_executions[-1]
    if extract_handler_result_status(execution) in {"success", "partial_success"}:
        requeued = []
        for candidate in candidates:
            source_stage = str(
                (candidate.get("payload") or {}).get("stage_code") or CHECK_STAGE_CODE
            )
            requeued.append(
                store.requeue_waiting_api_worker_job(
                    job_id=str(candidate.get("job_id") or ""),
                    payload=_after_browser_payload(candidate=candidate, execution=execution),
                    stage=source_stage,
                )
            )
        return _waiting(
            _stage_after_fallback(store=store, request_id=request.request_id),
            "Requeued FastMoss jobs after browser fallback.",
            {"requeued_count": len(requeued)},
        )
    error = _browser_execution_error(execution)
    failed_jobs = _fail_waiting_jobs_after_browser_failure(
        store=store, candidates=candidates, execution=execution
    )
    return {
        "action": "finalize",
        "final_status": "failed",
        "title": "达人建联 FastMoss 浏览器恢复失败",
        "failed_stage": FALLBACK_STAGE_CODE,
        "fallback_status": "failed",
        "error_code": "fastmoss_security_browser_fallback_failed",
        "error_type": error["error_type"],
        "error": error["message"],
        "browser_execution_id": _execution_attr(execution, "execution_id"),
        "browser_execution_status": extract_handler_result_status(execution),
        "failed_waiting_job_count": len(failed_jobs),
    }


def _advance_refresh(*, store: Any, request: Any) -> dict[str, Any]:
    existing = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=REFRESH_STAGE_CODE,
        job_code="outreach_creator_video_metric_refresh",
    )
    if not existing:
        request_payload = dict(request.payload or {})
        trigger_date = str(request_payload.get("trigger_date") or date.today().isoformat())
        source_rows = _read_source_rows(store=store, request_id=request.request_id)
        successful_products = _successful_index_product_ids(
            store=store, request_id=request.request_id
        )
        resolved_job = WORKFLOW.resolve_stage_jobs(REFRESH_STAGE_CODE)[0]
        jobs = []
        target_table_ref = request_payload.get("target_table_ref") or request_payload.get(
            "source_table_ref"
        )
        for row in source_rows:
            product_id = str(row.get("product_id") or "").strip()
            creator_unique_id = str(row.get("creator_unique_id") or "").strip()
            source_record_id = str(row.get("source_record_id") or "").strip()
            if not product_id or not creator_unique_id or not source_record_id:
                continue
            if product_id not in successful_products:
                continue
            job_payload = {
                "request_id": request.request_id,
                "task_code": TASK_CODE,
                "workflow_code": WORKFLOW_CODE,
                "stage_code": REFRESH_STAGE_CODE,
                "request_payload": request_payload,
                "target_table_ref": target_table_ref,
                "trigger_date": trigger_date,
                **row,
                **_fastmoss_common_payload(request_payload),
                **_feishu_common_payload(request_payload),
            }
            keys = render_job_keys(
                resolved_job,
                job_payload,
                request_id=request.request_id,
                task_code=TASK_CODE,
                workflow_code=WORKFLOW_CODE,
                stage_code=REFRESH_STAGE_CODE,
            )
            jobs.append(
                {
                    "business_key": keys["business_key"],
                    "dedupe_key": keys["dedupe_key"],
                    "payload": job_payload,
                }
            )
        enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0}
        if jobs:
            enqueue_result = store.enqueue_api_worker_jobs(
                request_id=request.request_id,
                task_code=TASK_CODE,
                job_code="outreach_creator_video_metric_refresh",
                jobs=jobs,
            )
            return _waiting(
                REFRESH_STAGE_CODE,
                "Executor dispatched outreach creator video metric refresh jobs.",
                {"dispatch_payload": enqueue_result},
            )
        return _advance(SUMMARY_STAGE_CODE, {"creator_refresh_job_count": 0})
    if _fallback_candidates(
        store=store, request_id=request.request_id, source_stage_code=REFRESH_STAGE_CODE
    ):
        return _advance(
            FALLBACK_STAGE_CODE,
            {"stage_transition": "creator_video_metric_refresh_requires_browser_fallback"},
        )
    if _has_pending_or_running_jobs(
        store=store, request_id=request.request_id, stage_code=REFRESH_STAGE_CODE
    ):
        return _waiting(
            REFRESH_STAGE_CODE, "Outreach creator video metric refresh jobs are still running."
        )
    return _advance(
        SUMMARY_STAGE_CODE, {"stage_transition": "creator_video_metric_refresh_terminal"}
    )


def _fallback_candidates(
    *, store: Any, request_id: str, source_stage_code: str = ""
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for stage_code, job_code in (
        (CHECK_STAGE_CODE, "product_video_outreach_check"),
        (REFRESH_STAGE_CODE, "outreach_creator_video_metric_refresh"),
    ):
        if source_stage_code and source_stage_code != stage_code:
            continue
        for job in _stage_jobs(
            store=store, request_id=request_id, stage_code=stage_code, job_code=job_code
        ):
            payload = job.get("payload") or {}
            if str(job.get("status") or "") != "waiting":
                continue
            if (
                int(payload.get("fastmoss_security_browser_fallback_attempt") or 0)
                >= MAX_FASTMOSS_BROWSER_FALLBACK_ATTEMPTS
            ):
                continue
            if is_fallback_required(job):
                candidates.append(job)
    return candidates


def _dispatch_fallback(
    *, store: Any, request: Any, candidates: list[dict[str, Any]], fallback_digest: str
) -> dict[str, Any]:
    job_def = WORKFLOW.require_job("fastmoss_security_browser_resolve")
    source_job = candidates[0]
    fallback_payload = _fallback_payload_from_job(source_job)
    source_stage = str((source_job.get("payload") or {}).get("stage_code") or CHECK_STAGE_CODE)
    payload = {
        **_fastmoss_common_payload(dict(request.payload or {})),
        "stage_code": FALLBACK_STAGE_CODE,
        "fallback_digest": fallback_digest,
        "source_stage_code": source_stage,
        "source_job_ids": [str(job.get("job_id") or "") for job in candidates],
        "search_query": str(fallback_payload.get("search_query") or fallback_digest),
        "search_digest": fallback_digest,
        "search_request": dict(fallback_payload.get("search_request") or {}),
        "security_context": dict(fallback_payload.get("security_context") or {}),
        "verification_request": dict(fallback_payload.get("verification_request") or {}),
        "request_payload": dict(request.payload or {}),
    }
    keys = render_job_keys(
        job_def,
        dict(request.payload or {}),
        fallback_payload,
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
                "business_key": keys["business_key"] or f"fastmoss-security:{fallback_digest}",
                "dedupe_key": build_stage_local_dedupe_key(
                    keys["dedupe_key"], job_def.job_code, stage_scope=FALLBACK_STAGE_CODE
                ),
                "resource_code": _fastmoss_browser_resource_code(payload),
                "payload": payload,
            }
        ],
    )


def _fail_waiting_jobs_after_browser_failure(
    *, store: Any, candidates: list[dict[str, Any]], execution: Any
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
                    "fallback_source_stage": str(
                        (candidate.get("payload") or {}).get("stage_code") or ""
                    ),
                    "browser_execution_id": _execution_attr(execution, "execution_id"),
                    "browser_error_code": error["error_code"],
                },
                result={
                    "status": "failed",
                    "fallback_required": False,
                    "browser_fallback_resolved": False,
                    "browser_execution_id": _execution_attr(execution, "execution_id"),
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
            _execution_attr(execution, "error_type"),
            "browser_failure",
        ),
        "error_code": _first_non_empty(
            handler_error.get("error_code"),
            _execution_attr(execution, "error_code"),
            "fastmoss_security_browser_fallback_failed",
        ),
        "message": _first_non_empty(
            handler_error.get("message"),
            _execution_attr(execution, "error_text"),
            "FastMoss auth/security browser fallback failed.",
        ),
    }


def _execution_result(execution: Any) -> dict[str, Any]:
    if isinstance(execution, Mapping):
        return coerce_mapping(execution.get("result"))
    return coerce_mapping(getattr(execution, "result", None))


def _execution_attr(execution: Any, key: str) -> str:
    if isinstance(execution, Mapping):
        return str(execution.get(key) or "")
    return str(getattr(execution, key, "") or "")


def _writeback_enabled(payload: Mapping[str, Any]) -> bool:
    return str(
        payload.get("writeback_enabled") or payload.get("allow_feishu_writeback") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    for key in ("fastmoss_browser_profile_ref", "browser_profile_ref", "profile_ref"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "fastmoss:browser"


def _fallback_payload_from_job(job: Mapping[str, Any]) -> dict[str, Any]:
    result = extract_effective_result_payload(job)
    return {
        "search_query": str(result.get("operation") or job.get("business_key") or ""),
        "search_request": dict(result.get("request_payload") or {}),
        "security_context": dict(result.get("security_context") or {}),
        "verification_request": dict(result.get("verification_request") or {}),
    }


def _after_browser_payload(*, candidate: Mapping[str, Any], execution: Any) -> dict[str, Any]:
    payload = dict(candidate.get("payload") or {})
    source_stage = str(payload.get("stage_code") or CHECK_STAGE_CODE)
    resume_page = _fallback_error_page(candidate)
    partial_rows = _fallback_partial_rows(candidate)
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
    if resume_page:
        payload["fastmoss_video_start_page"] = resume_page
    carried_rows = _merge_video_rows(
        coerce_mapping_list(payload.get("fastmoss_video_carried_rows")), partial_rows
    )
    if carried_rows:
        payload["fastmoss_video_carried_rows"] = carried_rows
    payload.pop("force_fallback", None)
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _fallback_partial_rows(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = extract_effective_result_payload(candidate)
    rows = result.get("partial_video_rows")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _merge_video_rows(
    existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in [*existing_rows, *new_rows]:
        key = (
            str(row.get("product_id") or row.get("goods_id") or ""),
            str(row.get("video_id") or row.get("id") or ""),
            str(row.get("unique_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(row))
    return merged


def _fallback_error_page(candidate: Mapping[str, Any]) -> int:
    result = extract_effective_result_payload(candidate)
    params = (
        result.get("verification_request", {}).get("params")
        if isinstance(result.get("verification_request"), Mapping)
        else {}
    )
    if not isinstance(params, Mapping):
        params = (
            result.get("security_context", {}).get("params")
            if isinstance(result.get("security_context"), Mapping)
            else {}
        )
    try:
        return int(str((params or {}).get("page") or "").strip())
    except ValueError:
        return 0


def _fallback_digest(candidates: list[dict[str, Any]]) -> str:
    parts = []
    for candidate in candidates:
        payload = candidate.get("payload") or {}
        parts.append(
            f"{candidate.get('job_id') or ''}:{payload.get('fastmoss_security_browser_fallback_attempt') or 0}"
        )
    raw = ",".join(sorted(parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _execution_payload(execution: Any) -> dict[str, Any]:
    if isinstance(execution, Mapping):
        payload = execution.get("payload")
    else:
        payload = getattr(execution, "payload", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_source_rows(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    for job in reversed(
        _stage_jobs(
            store=store,
            request_id=request_id,
            stage_code=READ_STAGE_CODE,
            job_code="feishu_table_read",
        )
    ):
        result = extract_effective_result_payload(job)
        rows = result.get("source_rows") if isinstance(result, dict) else []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _successful_index_product_ids(*, store: Any, request_id: str) -> set[str]:
    product_ids: set[str] = set()
    for job in _stage_jobs(
        store=store,
        request_id=request_id,
        stage_code=CHECK_STAGE_CODE,
        job_code="product_video_outreach_check",
    ):
        result = extract_effective_result_payload(job)
        if isinstance(result, dict) and result.get("fetch_status") == "success":
            product_id = str(result.get("product_id") or "").strip()
            if product_id:
                product_ids.add(product_id)
    return product_ids


def _stage_after_fallback(*, store: Any, request_id: str) -> str:
    if _has_pending_or_running_jobs(
        store=store, request_id=request_id, stage_code=CHECK_STAGE_CODE
    ):
        return CHECK_STAGE_CODE
    if _stage_jobs(
        store=store,
        request_id=request_id,
        stage_code=REFRESH_STAGE_CODE,
        job_code="outreach_creator_video_metric_refresh",
    ):
        return REFRESH_STAGE_CODE
    return CHECK_STAGE_CODE


def _build_summary(*, store: Any, request: Any) -> dict[str, Any]:
    read_result = {}
    for job in reversed(
        _stage_jobs(
            store=store,
            request_id=request.request_id,
            stage_code=READ_STAGE_CODE,
            job_code="feishu_table_read",
        )
    ):
        result = extract_effective_result_payload(job)
        if isinstance(result, dict):
            read_result = result
            break
    check_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=CHECK_STAGE_CODE,
        job_code="product_video_outreach_check",
    )
    refresh_jobs = _stage_jobs(
        store=store,
        request_id=request.request_id,
        stage_code=REFRESH_STAGE_CODE,
        job_code="outreach_creator_video_metric_refresh",
    )
    indexed_video_count = 0
    new_video_count = 0
    updated_video_count = 0
    product_success = 0
    product_failed = 0
    for job in check_jobs:
        result = extract_effective_result_payload(job)
        if isinstance(result, dict) and result.get("fetch_status") == "success":
            product_success += 1
            indexed_video_count += int(result.get("indexed_video_count") or 0)
            new_video_count += int(result.get("new_video_count") or 0)
            updated_video_count += int(result.get("updated_video_count") or 0)
        elif extract_handler_result_status(job) in {"failed", "fallback_required"} or str(
            job.get("result_status") or job.get("status") or ""
        ) in {"failed", "waiting"}:
            product_failed += 1
    refresh_success = 0
    refresh_skipped = 0
    refresh_failed = 0
    feishu_written = 0
    feishu_failed = 0
    video_count_total = 0
    play_count_total = 0
    no_video_checked = 0
    index_missing_skipped = 0
    overview_failed = 0
    video_count_changed = 0
    play_count_changed = 0
    highest_video_changed = 0
    for job in refresh_jobs:
        result = extract_effective_result_payload(job)
        status = extract_handler_result_status(job)
        if isinstance(result, dict) and result.get("refresh_status") == "success":
            refresh_success += 1
            video_count_total += int(result.get("video_count") or 0)
            play_count_total += int(result.get("total_play_count") or 0)
            written_fields = set(result.get("written_fields") or [])
            if int(result.get("video_count") or 0) == 0 and "检查时间" in written_fields:
                no_video_checked += 1
            if "视频数量" in written_fields:
                video_count_changed += 1
            if "播放量" in written_fields:
                play_count_changed += 1
            if "视频链接" in written_fields:
                highest_video_changed += 1
            if result.get("feishu_written"):
                feishu_written += 1
        elif status == "skipped" or (
            isinstance(result, dict) and result.get("refresh_status") == "skipped"
        ):
            refresh_skipped += 1
            if (
                isinstance(result, dict)
                and result.get("skip_reason") == "existing_link_missing_from_index"
            ):
                index_missing_skipped += 1
        elif status in {"failed", "fallback_required"} or str(
            job.get("result_status") or job.get("status") or ""
        ) in {"failed", "waiting"}:
            refresh_failed += 1
            if isinstance(result, dict) and result.get("error_stage") == "video_overview":
                overview_failed += 1
            if isinstance(result, dict) and result.get("feishu_write"):
                feishu_failed += 1
    final_status = (
        "failed"
        if product_success == 0 and refresh_success == 0 and (product_failed or refresh_failed)
        else "partial_success"
        if product_failed or refresh_failed
        else "success"
    )
    adapter_summary = read_result.get("adapter_summary") if isinstance(read_result, dict) else {}
    return {
        "final_status": final_status,
        "title": "达人建联检查完成",
        "total_rows_read": int((adapter_summary or {}).get("input_row_count") or 0),
        "candidate_row_count": int((adapter_summary or {}).get("source_row_count") or 0),
        "skipped_rows": int((adapter_summary or {}).get("skipped_count") or 0),
        "skip_reasons": dict((adapter_summary or {}).get("skip_reasons") or {}),
        "product_count": len(check_jobs),
        "product_fetch_success_count": product_success,
        "product_fetch_failed_count": product_failed,
        "indexed_video_count": indexed_video_count,
        "new_video_count": new_video_count,
        "updated_video_count": updated_video_count,
        "creator_refresh_success_count": refresh_success,
        "creator_refresh_skipped_count": refresh_skipped,
        "creator_refresh_failed_count": refresh_failed,
        "no_video_checked_count": no_video_checked,
        "index_missing_skipped_count": index_missing_skipped,
        "overview_failed_count": overview_failed,
        "feishu_write_success_count": feishu_written,
        "feishu_write_failed_count": feishu_failed,
        "video_count_change_count": video_count_changed,
        "play_count_change_count": play_count_changed,
        "highest_video_change_count": highest_video_changed,
        "aggregated_video_count": video_count_total,
        "aggregated_play_count": play_count_total,
    }


def _stage_jobs(
    *, store: Any, request_id: str, stage_code: str, job_code: str | None = None
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
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def _first_failed_job(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    for job in jobs:
        if (
            extract_handler_result_status(job) == "failed"
            or str(job.get("result_status") or job.get("status") or "") == "failed"
        ):
            return job
    return {}


def _has_active_jobs(*, store: Any, request_id: str, stage_code: str) -> bool:
    return any(
        str(job.get("status") or "") in ACTIVE_STATUSES
        for job in _stage_jobs(store=store, request_id=request_id, stage_code=stage_code)
    )


def _has_pending_or_running_jobs(*, store: Any, request_id: str, stage_code: str) -> bool:
    return any(
        str(job.get("status") or "") in {"pending", "running"}
        for job in _stage_jobs(store=store, request_id=request_id, stage_code=stage_code)
    )


def _current_stage(request: Any) -> str:
    return str(
        getattr(request, "current_stage", "")
        or getattr(request, "progress_stage", "")
        or READ_STAGE_CODE
    )


def _feishu_common_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_base_id",
        "feishu_table_id",
        "feishu_view_id",
        "feishu_user_access_token",
        "validate_schema",
        "snapshot_policy",
    )
    return {key: request_payload[key] for key in keys if key in request_payload}


def _fastmoss_common_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in request_payload.items()
        if str(key).startswith(("fastmoss", "mock_fastmoss")) or key in {"browser_cookies"}
    }


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _waiting(
    stage_code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": details or {},
    }


def _advance(next_stage: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "advance", "next_stage": next_stage, "details": details or {}}
