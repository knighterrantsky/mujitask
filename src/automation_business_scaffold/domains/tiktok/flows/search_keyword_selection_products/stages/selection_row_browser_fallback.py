from __future__ import annotations

import hashlib
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import compact_dict, coerce_mapping
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

from ..context.models import (
    FASTMOSS_BROWSER_PASSTHROUGH_KEYS,
    RUNTIME_DB_PASSTHROUGH_KEYS,
)
from ..context.stage_inputs import (
    _fastmoss_browser_resource_code,
    _fastmoss_search_settings_from_request_payload,
    _first_text,
    _payload_subset,
    _record_effective_status,
)
from ..context.runtime_views import (
    _selection_row_has_after_browser_terminal,
)
from ..context.decision_models import (
    _waiting,
)


STAGE_CODE = "selection_row_browser_fallback"


def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "selection_row_browser_fallback"
    executions = _browser_executions_for_stage(
        store=store,
        request_id=request.request_id,
        stage_code=stage_code,
    )
    fallback_candidates = _selection_row_browser_fallback_candidates(
        store=store,
        request_id=request.request_id,
    )
    if not fallback_candidates and not executions:
        return {
            "action": "advance",
            "next_stage": "refresh_selection_rows",
            "details": {"fallback_candidate_count": 0},
        }
    if not executions and fallback_candidates:
        dispatches: dict[str, Any] = {}
        for fallback_handler in sorted(
            {str(candidate.get("fallback_handler") or "") for candidate in fallback_candidates}
        ):
            if not fallback_handler:
                continue
            job_def = workflow.require_job(fallback_handler)
            items: list[dict[str, Any]] = []
            for candidate in fallback_candidates:
                if str(candidate.get("fallback_handler") or "") != fallback_handler:
                    continue
                payload = _selection_row_browser_execution_payload(
                    request_payload=request.payload,
                    stage_code=stage_code,
                    candidate=candidate,
                )
                keys = render_job_keys(
                    job_def,
                    request.payload,
                    candidate,
                    payload,
                    request_id=request.request_id,
                    task_code=request.task_code,
                    workflow_code=workflow.workflow_code,
                    stage_code=stage_code,
                    item_code=job_def.job_code,
                )
                items.append(
                    {
                        "business_key": keys["business_key"]
                        or str(candidate.get("business_entity_key") or ""),
                        "dedupe_key": build_stage_local_dedupe_key(
                            keys["dedupe_key"],
                            job_def.job_code,
                            stage_scope=stage_code,
                        ),
                        "resource_code": _row_browser_resource_code(
                            fallback_handler=fallback_handler,
                            payload=payload,
                            candidate=candidate,
                        ),
                        "payload": payload,
                        "max_execution_seconds": _timeout_seconds(workflow, job_def.job_code),
                    }
                )
            if not items:
                continue
            dispatches[fallback_handler] = store.enqueue_task_executions(
                request_id=request.request_id,
                item_code=job_def.job_code,
                workflow_code=workflow.workflow_code,
                items=items,
            )
        _update_request_cursor(
            store=store,
            request=request,
            stage_code=stage_code,
            payload={
                "browser_dispatches": dispatches,
                "fallback_candidate_count": len(fallback_candidates),
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued selection row browser fallback executions.",
            details={
                "created_count": sum(int(dispatch.get("created_count") or 0) for dispatch in dispatches.values()),
                "fallback_candidate_count": len(fallback_candidates),
            },
        )
    if _any_browser_executions_active(executions):
        return _waiting(
            stage_code=stage_code,
            message="Waiting for selection row browser fallback executions to finish.",
        )
    requeued_jobs = _requeue_selection_rows_after_browser(
        store=store,
        stage_code="refresh_selection_rows",
        fallback_candidates=fallback_candidates,
        executions=executions,
    )
    _update_request_cursor(
        store=store,
        request=request,
        stage_code=stage_code,
        payload={
            "execution_count": len(executions),
            "requeued_row_count": len(requeued_jobs),
            "status": "success" if requeued_jobs else "failed",
        },
    )
    if requeued_jobs:
        return _waiting(
            stage_code="refresh_selection_rows",
            message="Requeued selection row refresh after browser fallback.",
            details={
                "requeued_row_count": len(requeued_jobs),
            },
        )
    return {
        "action": "advance",
        "next_stage": "refresh_selection_rows",
        "details": {"execution_count": len(executions), "requeued_row_count": 0},
    }


def _selection_row_browser_fallback_candidates(
    store: RuntimeStore,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for job in _api_jobs_for_stage(
        store=store,
        request_id=request_id,
        stage_code="refresh_selection_rows",
    ):
        if str(job.get("job_code") or "") != "selection_row_refresh":
            continue
        if not _is_fallback_required(job):
            continue
        row_payload = dict(job.get("payload") or {})
        result_payload = extract_effective_result_payload(job)
        fallback_handler = _first_text(result_payload.get("fallback_handler"))
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = coerce_mapping(result_payload.get("browser_fallback_payload"))
        if not browser_payload:
            next_action = coerce_mapping(result_payload.get("next_action"))
            browser_payload = coerce_mapping(next_action.get("payload"))
        source_record_id = _first_text(
            result_payload.get("source_record_id"),
            row_payload.get("source_record_id"),
        )
        if _selection_row_has_after_browser_terminal(
            store=store,
            request_id=request_id,
            source_record_id=source_record_id,
        ):
            continue
        business_entity_key = _first_text(
            result_payload.get("business_entity_key"),
            row_payload.get("business_key"),
            job.get("business_key"),
            source_record_id,
        )
        browser_payload = {
            **browser_payload,
            "source_record_id": source_record_id,
        }
        candidates.append(
            {
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_text(result_payload.get("fallback_reason")),
                "source_record_id": source_record_id,
                "business_entity_key": business_entity_key,
                "candidate_key": business_entity_key,
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": row_payload,
                "row_result": result_payload,
                "browser_fallback_payload": compact_dict(browser_payload),
                "product_identity": coerce_mapping(
                    result_payload.get("product_identity")
                )
                or coerce_mapping(row_payload.get("product_identity")),
                "normalized_product_url": _first_text(
                    browser_payload.get("normalized_product_url"),
                    row_payload.get("normalized_product_url"),
                    coerce_mapping(row_payload.get("product_identity")).get("normalized_product_url"),
                ),
                "normalized_product_result": coerce_mapping(
                    result_payload.get("normalized_product_result")
                ),
            }
        )
    return candidates


def _selection_row_browser_execution_payload(
    *,
    request_payload: Mapping[str, Any],
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = {
        **_payload_subset(request_payload, FASTMOSS_BROWSER_PASSTHROUGH_KEYS + RUNTIME_DB_PASSTHROUGH_KEYS),
        **coerce_mapping(candidate.get("browser_fallback_payload")),
        "stage_code": stage_code,
        "source_record_id": str(candidate.get("source_record_id") or ""),
        "business_entity_key": str(candidate.get("business_entity_key") or ""),
        "candidate_key": str(candidate.get("candidate_key") or ""),
        "fallback_handler": fallback_handler,
    }
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault("search_query", str(candidate.get("business_entity_key") or ""))
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        payload.setdefault("search_request", coerce_mapping(payload.get("search_request")))
        payload.setdefault("verification_request", coerce_mapping(payload.get("verification_request")))
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request_payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
    return compact_dict(payload)


def _selection_row_after_browser_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(coerce_mapping(candidate.get("row_payload")))
    browser_payload = coerce_mapping(candidate.get("browser_execution_payload"))
    payload.update(
        {
            "stage_code": stage_code,
            "browser_fallback_resolved": True,
            "browser_fallback_handler": fallback_handler,
            "browser_execution_id": str(candidate.get("browser_execution_id") or ""),
            "browser_execution_status": str(candidate.get("browser_execution_status") or ""),
            "browser_fallback_failed": str(candidate.get("browser_execution_status") or "") not in {"success", "partial_success"},
            "force_fallback": False,
            "fallback_reason": "",
        }
    )
    if fallback_handler == "tiktok_product_browser_fetch":
        payload["normalized_product_result"] = coerce_mapping(
            browser_payload.get("normalized_product_result")
        )
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized_product = coerce_mapping(candidate.get("normalized_product_result"))
        if normalized_product:
            payload["normalized_product_result"] = normalized_product
    return compact_dict(payload)


def _requeue_selection_rows_after_browser(
    *,
    store: RuntimeStore,
    stage_code: str,
    fallback_candidates: list[dict[str, Any]],
    executions: list[Any],
) -> list[dict[str, Any]]:
    requeued: list[dict[str, Any]] = []
    terminal_by_key: dict[str, Any] = {}
    for execution in executions:
        if str(getattr(execution, "status", "") or "") not in {"finished", "cancelled"}:
            continue
        payload = dict(execution.payload or {})
        fallback_handler = str(execution.item_code or payload.get("fallback_handler") or "")
        source_record_id = _first_text(payload.get("source_record_id"))
        terminal_by_key[
            _row_fallback_key(source_record_id=source_record_id, fallback_handler=fallback_handler)
        ] = execution

    for candidate in fallback_candidates:
        execution = terminal_by_key.get(str(candidate.get("fallback_key") or ""))
        if execution is None:
            continue
        requeued.append(
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("row_job_id") or ""),
                payload=_selection_row_after_browser_payload(
                    stage_code=stage_code,
                    candidate={
                        **dict(candidate),
                        "browser_execution_id": str(execution.execution_id),
                        "browser_execution_payload": extract_effective_result_payload(execution),
                        "browser_execution_status": extract_handler_result_status(execution),
                    },
                ),
                stage=stage_code,
            )
        )
    return requeued


def _row_fallback_key(*, source_record_id: str, fallback_handler: str) -> str:
    from ..policies.fallback import row_fallback_key

    return row_fallback_key(source_record_id=source_record_id, fallback_handler=fallback_handler)


def _search_digest_for_row_fallback(candidate: Mapping[str, Any]) -> str:
    value = _first_text(
        candidate.get("source_record_id"),
        candidate.get("business_entity_key"),
        candidate.get("row_job_id"),
    )
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _row_browser_resource_code(
    *,
    fallback_handler: str,
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> str:
    if fallback_handler == "fastmoss_security_browser_resolve":
        return _fastmoss_browser_resource_code(payload)
    return _browser_resource_code(candidate)


def _is_fallback_required(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    if extract_handler_result_status(job) == "fallback_required":
        return True
    payload = extract_effective_result_payload(job)
    return bool(payload.get("fallback_required"))


def _browser_resource_code(candidate: Mapping[str, Any]) -> str:
    business_key = str(candidate.get("business_entity_key") or candidate.get("candidate_key") or "")
    return f"tiktok_product:{business_key}" if business_key else ""
