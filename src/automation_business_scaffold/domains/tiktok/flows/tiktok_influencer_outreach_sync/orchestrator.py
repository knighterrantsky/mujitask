from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_mapping_list
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
from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

TASK_CODE = "tiktok_influencer_outreach_sync"
WORKFLOW = get_workflow_definition(TASK_CODE)
WORKFLOW_CODE = WORKFLOW.workflow_code
READ_STAGE_CODE = "read_outreach_rows"
CHECK_STAGE_CODE = "check_product_videos"
FALLBACK_STAGE_CODE = "fastmoss_security_browser_fallback"
WRITEBACK_STAGE_CODE = "writeback_outreach_rows"
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
    if stage_code == WRITEBACK_STAGE_CODE:
        return _advance_writeback(store=store, request=request)
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
    if current_stage not in {READ_STAGE_CODE, CHECK_STAGE_CODE, FALLBACK_STAGE_CODE, WRITEBACK_STAGE_CODE}:
        return []
    if current_stage == CHECK_STAGE_CODE:
        if _has_pending_or_running_jobs(store=store, request_id=request_id, stage_code=current_stage):
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


def finalize_request(*, store: Any, request: Any, workflow: Any, force_result: dict[str, Any] | None = None) -> dict[str, Any]:
    del workflow
    summary = force_result or _build_summary(store=store, request=request)
    final_status = str(summary.get("final_status") or "success")
    return {
        "action": "finalize",
        "final_status": final_status,
        "summary": summary,
        "result": {"summary": summary, "title": "达人建联检查完成"},
    }


def _advance_read(*, store: Any, request: Any) -> dict[str, Any]:
    if not _stage_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read"):
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
                        "target_table_ref": request_payload.get("target_table_ref") or request_payload.get("source_table_ref"),
                        "field_names": list(OUTREACH_READ_FIELD_NAMES),
                        "adapter_code": "outreach_source_adapter",
                        "source_record_ids": list(request_payload.get("source_record_ids") or []),
                        **_feishu_common_payload(request_payload),
                    },
                }
            ],
        )
        return _waiting(READ_STAGE_CODE, "Executor dispatched outreach table read.", {"dispatch_payload": enqueue_result})
    if _has_active_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE):
        return _waiting(READ_STAGE_CODE, "Outreach table read is still running.")
    return _advance(CHECK_STAGE_CODE, {"stage_transition": "outreach_rows_read"})


def _advance_check(*, store: Any, request: Any) -> dict[str, Any]:
    existing = _stage_browser_executions(store=store, request_id=request.request_id, stage_code=CHECK_STAGE_CODE, item_code="product_video_outreach_check")
    if not existing:
        request_payload = dict(request.payload or {})
        trigger_date = str(request_payload.get("trigger_date") or date.today().isoformat())
        source_rows = _read_source_rows(store=store, request_id=request.request_id)
        product_groups = group_outreach_rows_by_product(source_rows, trigger_date=trigger_date)
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
                    "resource_code": _fastmoss_browser_resource_code(request_payload),
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
            enqueue_result = store.enqueue_task_executions(
                request_id=request.request_id,
                item_code="product_video_outreach_check",
                workflow_code=WORKFLOW_CODE,
                items=jobs,
            )
        if jobs:
            return _waiting(CHECK_STAGE_CODE, "Executor dispatched browser product video outreach checks.", {"dispatch_payload": enqueue_result})
        return _advance(WRITEBACK_STAGE_CODE, {"candidate_count": 0})
    if any_browser_executions_active(existing):
        return _waiting(CHECK_STAGE_CODE, "Browser product video outreach checks are still running.")
    return _advance(WRITEBACK_STAGE_CODE, {"stage_transition": "product_video_checks_terminal"})


def _advance_fallback(*, store: Any, request: Any) -> dict[str, Any]:
    candidates = _fallback_candidates(store=store, request_id=request.request_id)
    executions = browser_executions_for_stage(store, request_id=request.request_id, stage_code=FALLBACK_STAGE_CODE)
    if not candidates:
        if any_browser_executions_active(executions):
            return _waiting(FALLBACK_STAGE_CODE, "Waiting for FastMoss security browser fallback to finish.")
        return _advance(CHECK_STAGE_CODE, {"fallback_candidate_count": 0})
    candidates = candidates[:1]
    fallback_digest = _fallback_digest(candidates)
    relevant_executions = [execution for execution in executions if _execution_payload(execution).get("fallback_digest") == fallback_digest]
    if not relevant_executions:
        dispatch = _dispatch_fallback(store=store, request=request, candidates=candidates, fallback_digest=fallback_digest)
        return _waiting(FALLBACK_STAGE_CODE, "Enqueued FastMoss security browser fallback.", {"dispatch_payload": dispatch, "fallback_candidate_count": len(candidates)})
    if any_browser_executions_active(relevant_executions):
        return _waiting(FALLBACK_STAGE_CODE, "Waiting for FastMoss security browser fallback to finish.")
    execution = relevant_executions[-1]
    if extract_handler_result_status(execution) in {"success", "partial_success"}:
        requeued = []
        for candidate in candidates:
            requeued.append(
                store.requeue_waiting_api_worker_job(
                    job_id=str(candidate.get("job_id") or ""),
                    payload=_after_browser_payload(candidate=candidate, execution=execution),
                    stage=CHECK_STAGE_CODE,
                )
            )
        return _waiting(CHECK_STAGE_CODE, "Requeued product video checks after FastMoss browser fallback.", {"requeued_count": len(requeued)})
    return _advance(CHECK_STAGE_CODE, {"fallback_status": "failed", "browser_execution_status": extract_handler_result_status(execution)})


def _advance_writeback(*, store: Any, request: Any) -> dict[str, Any]:
    existing = _stage_jobs(store=store, request_id=request.request_id, stage_code=WRITEBACK_STAGE_CODE, job_code="feishu_table_write")
    if not existing:
        request_payload = dict(request.payload or {})
        writeback_groups = _build_writeback_groups(store=store, request_id=request.request_id)
        if not _writeback_enabled(request_payload):
            planned_count = sum(len(group.get("records") or []) for group in writeback_groups)
            return _advance(
                SUMMARY_STAGE_CODE,
                {"writeback_suppressed": True, "planned_writeback_count": planned_count, "reason": "writeback_not_approved"},
            )
        jobs = []
        target_table_ref = request_payload.get("target_table_ref") or request_payload.get("source_table_ref")
        for group in writeback_groups:
            records = list(group.get("records") or [])
            product_id = str(group.get("product_id") or "").strip()
            if not records:
                continue
            jobs.append(
                {
                    "business_key": f"{target_table_ref}:product:{product_id}",
                    "dedupe_key": f"{request.request_id}:feishu_table_write:{target_table_ref}:product:{product_id}",
                    "payload": {
                        "request_id": request.request_id,
                        "task_code": TASK_CODE,
                        "workflow_code": WORKFLOW_CODE,
                        "stage_code": WRITEBACK_STAGE_CODE,
                        "request_payload": request_payload,
                        "target_table_ref": target_table_ref,
                        "mapper_code": "outreach_result_projection_mapper",
                        "write_mode": "update",
                        "product_id": product_id,
                        "records": records,
                        "trigger_date": str(request_payload.get("trigger_date") or date.today().isoformat()),
                        **_feishu_common_payload(request_payload),
                    },
                }
            )
        enqueue_result = {"created_count": 0, "updated_count": 0, "skipped_count": 0}
        if jobs:
            enqueue_result = store.enqueue_api_worker_jobs(
                request_id=request.request_id,
                task_code=TASK_CODE,
                job_code="feishu_table_write",
                jobs=jobs,
            )
            return _waiting(WRITEBACK_STAGE_CODE, "Executor dispatched outreach writebacks.", {"dispatch_payload": enqueue_result})
        return _advance(SUMMARY_STAGE_CODE, {"writeback_record_count": 0})
    if _has_active_jobs(store=store, request_id=request.request_id, stage_code=WRITEBACK_STAGE_CODE):
        return _waiting(WRITEBACK_STAGE_CODE, "Outreach writebacks are still running.")
    return _advance(SUMMARY_STAGE_CODE, {"stage_transition": "writeback_terminal"})


def _fallback_candidates(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in _stage_jobs(store=store, request_id=request_id, stage_code=CHECK_STAGE_CODE, job_code="product_video_outreach_check"):
        payload = job.get("payload") or {}
        if str(job.get("status") or "") != "waiting":
            continue
        if int(payload.get("fastmoss_security_browser_fallback_attempt") or 0) >= MAX_FASTMOSS_BROWSER_FALLBACK_ATTEMPTS:
            continue
        if is_fallback_required(job):
            candidates.append(job)
    return candidates


def _dispatch_fallback(*, store: Any, request: Any, candidates: list[dict[str, Any]], fallback_digest: str) -> dict[str, Any]:
    job_def = WORKFLOW.require_job("fastmoss_security_browser_resolve")
    source_job = candidates[0]
    fallback_payload = _fallback_payload_from_job(source_job)
    payload = {
        **_fastmoss_common_payload(dict(request.payload or {})),
        "stage_code": FALLBACK_STAGE_CODE,
        "fallback_digest": fallback_digest,
        "source_stage_code": CHECK_STAGE_CODE,
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
                "dedupe_key": build_stage_local_dedupe_key(keys["dedupe_key"], job_def.job_code, stage_scope=FALLBACK_STAGE_CODE),
                "resource_code": _fastmoss_browser_resource_code(payload),
                "payload": payload,
            }
        ],
    )


def _writeback_enabled(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("writeback_enabled") or payload.get("allow_feishu_writeback") or "").strip().lower() in {"1", "true", "yes", "on"}


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
    resume_page = _fallback_error_page(candidate)
    partial_rows = _fallback_partial_rows(candidate)
    payload.update(
        {
            "stage_code": CHECK_STAGE_CODE,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": "fastmoss_security_browser_resolve",
            "browser_execution_id": str(getattr(execution, "execution_id", "") or ""),
            "browser_execution_status": extract_handler_result_status(execution),
            "fastmoss_security_browser_fallback_attempt": int(payload.get("fastmoss_security_browser_fallback_attempt") or 0) + 1,
            "fallback_reason": "",
        }
    )
    if resume_page:
        payload["fastmoss_video_start_page"] = resume_page
    carried_rows = _merge_video_rows(coerce_mapping_list(payload.get("fastmoss_video_carried_rows")), partial_rows)
    if carried_rows:
        payload["fastmoss_video_carried_rows"] = carried_rows
    payload.pop("force_fallback", None)
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _fallback_partial_rows(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = extract_effective_result_payload(candidate)
    rows = result.get("partial_video_rows")
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _merge_video_rows(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    params = result.get("verification_request", {}).get("params") if isinstance(result.get("verification_request"), Mapping) else {}
    if not isinstance(params, Mapping):
        params = result.get("security_context", {}).get("params") if isinstance(result.get("security_context"), Mapping) else {}
    try:
        return int(str((params or {}).get("page") or "").strip())
    except ValueError:
        return 0


def _fallback_digest(candidates: list[dict[str, Any]]) -> str:
    parts = []
    for candidate in candidates:
        payload = candidate.get("payload") or {}
        parts.append(f"{candidate.get('job_id') or ''}:{payload.get('fastmoss_security_browser_fallback_attempt') or 0}")
    raw = ",".join(sorted(parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _execution_payload(execution: Any) -> dict[str, Any]:
    if isinstance(execution, Mapping):
        payload = execution.get("payload")
    else:
        payload = getattr(execution, "payload", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_source_rows(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    for job in reversed(_stage_jobs(store=store, request_id=request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read")):
        result = extract_effective_result_payload(job)
        rows = result.get("source_rows") if isinstance(result, dict) else []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _build_writeback_groups(*, store: Any, request_id: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for execution in _stage_browser_executions(store=store, request_id=request_id, stage_code=CHECK_STAGE_CODE, item_code="product_video_outreach_check"):
        result = extract_effective_result_payload(execution)
        if not isinstance(result, dict) or result.get("fetch_status") != "success":
            continue
        records = [row for row in list(result.get("matched_rows") or []) + list(result.get("unmatched_rows") or []) if isinstance(row, dict)]
        if records:
            groups.append({"product_id": str(result.get("product_id") or "").strip(), "records": records})
    return groups


def _build_summary(*, store: Any, request: Any) -> dict[str, Any]:
    read_result = {}
    for job in reversed(_stage_jobs(store=store, request_id=request.request_id, stage_code=READ_STAGE_CODE, job_code="feishu_table_read")):
        result = extract_effective_result_payload(job)
        if isinstance(result, dict):
            read_result = result
            break
    check_jobs = _stage_browser_executions(store=store, request_id=request.request_id, stage_code=CHECK_STAGE_CODE, item_code="product_video_outreach_check")
    write_jobs = _stage_jobs(store=store, request_id=request.request_id, stage_code=WRITEBACK_STAGE_CODE, job_code="feishu_table_write")
    matched = 0
    unmatched = 0
    product_success = 0
    product_failed = 0
    for job in check_jobs:
        result = extract_effective_result_payload(job)
        if isinstance(result, dict) and result.get("fetch_status") == "success":
            product_success += 1
            matched += len(result.get("matched_rows") or [])
            unmatched += len(result.get("unmatched_rows") or [])
        elif str(job.get("result_status") or job.get("status") or "") == "failed":
            product_failed += 1
    write_success = sum(int((job.get("summary") or {}).get("written_count") or 0) for job in write_jobs if isinstance(job.get("summary"), dict))
    write_failed = sum(int((job.get("summary") or {}).get("failed_count") or 0) for job in write_jobs if isinstance(job.get("summary"), dict))
    final_status = "failed" if product_success == 0 and product_failed > 0 else "partial_success" if product_failed or write_failed else "success"
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
        "matched_row_count": matched,
        "unmatched_checked_row_count": unmatched,
        "feishu_write_success_count": write_success,
        "feishu_write_failed_count": write_failed,
    }


def _stage_jobs(*, store: Any, request_id: str, stage_code: str, job_code: str | None = None) -> list[dict[str, Any]]:
    list_jobs = getattr(store, "list_api_worker_jobs_for_request")
    try:
        jobs = list_jobs(request_id=request_id, job_code=job_code) if job_code else list_jobs(request_id=request_id)
    except TypeError:
        jobs = list_jobs(request_id=request_id)
    return [dict(job) for job in jobs if str((job.get("payload") or {}).get("stage_code") or "") == stage_code]


def _stage_browser_executions(*, store: Any, request_id: str, stage_code: str, item_code: str | None = None) -> list[Any]:
    executions = browser_executions_for_stage(store, request_id=request_id, stage_code=stage_code)
    if item_code:
        return [execution for execution in executions if str(getattr(execution, "item_code", "") or _execution_payload(execution).get("item_code") or "") == item_code]
    return list(executions)


def _has_active_jobs(*, store: Any, request_id: str, stage_code: str) -> bool:
    return any(str(job.get("status") or "") in ACTIVE_STATUSES for job in _stage_jobs(store=store, request_id=request_id, stage_code=stage_code))


def _has_pending_or_running_jobs(*, store: Any, request_id: str, stage_code: str) -> bool:
    return any(str(job.get("status") or "") in {"pending", "running"} for job in _stage_jobs(store=store, request_id=request_id, stage_code=stage_code))


def _current_stage(request: Any) -> str:
    return str(getattr(request, "current_stage", "") or getattr(request, "progress_stage", "") or READ_STAGE_CODE)


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
    return {key: value for key, value in request_payload.items() if str(key).startswith(("fastmoss", "mock_fastmoss")) or key in {"browser_cookies"}}


def _waiting(stage_code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "waiting", "current_stage": stage_code, "message": message, "details": details or {}}


def _advance(next_stage: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": "advance", "next_stage": next_stage, "details": details or {}}
