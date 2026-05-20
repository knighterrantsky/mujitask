from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_mapping
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
    ARTIFACT_PASSTHROUGH_KEYS,
    FASTMOSS_BROWSER_PASSTHROUGH_KEYS,
    RUNTIME_DB_PASSTHROUGH_KEYS,
    TIKTOK_REQUEST_PASSTHROUGH_KEYS,
)
from ..context.stage_inputs import (
    _browser_resource_code,
    _compact_mapping,
    _fastmoss_browser_resource_code,
    _fastmoss_search_settings_from_request_payload,
    _first_text,
    _latest_row_job,
    _minimal_seed_context,
    _payload_subset,
    _record_effective_status,
    _runtime_child_context,
)
from ..context.runtime_views import (
    _seed_context_by_candidate_key,
    _seed_contexts,
)
from ..context.decision_models import (
    _is_fallback_required,
    _waiting,
)

if TYPE_CHECKING:
    from automation_business_scaffold.contracts.workflow import WorkflowDefinition
    from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


STAGE_CODE = "browser_fallback"

def advance(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
) -> dict[str, Any]:
    stage_code = "browser_fallback"
    executions = _browser_executions_for_stage(store=store, request_id=request.request_id, stage_code=stage_code)
    fallback_candidates = _browser_fallback_candidates(store=store, request_id=request.request_id)
    if not fallback_candidates and not executions:
        return {
            "action": "advance",
            "next_stage": "refresh_competitor_rows",
            "details": {"fallback_candidate_count": 0},
        }
    dispatch_candidates = _fallback_candidates_needing_dispatch(
        fallback_candidates=fallback_candidates,
        executions=executions,
    )
    if dispatch_candidates:
        dispatches: dict[str, Any] = {}
        for fallback_handler in sorted(
            {str(candidate.get("fallback_handler") or "") for candidate in dispatch_candidates}
        ):
            if not fallback_handler:
                continue
            job_def = workflow.require_job(fallback_handler)
            items: list[dict[str, Any]] = []
            for candidate in dispatch_candidates:
                if str(candidate.get("fallback_handler") or "") != fallback_handler:
                    continue
                payload = _browser_execution_payload(
                    request=request,
                    workflow=workflow,
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
                "dispatch_candidate_count": len(dispatch_candidates),
            },
        )
        return _waiting(
            stage_code=stage_code,
            message="Enqueued browser fallback executions.",
            details={
                "created_count": sum(int(dispatch.get("created_count") or 0) for dispatch in dispatches.values())
            },
        )
    if _any_browser_executions_active(executions):
        return _waiting(stage_code=stage_code, message="Waiting for browser fallback executions to finish.")
    requeued_jobs = _requeue_competitor_rows_after_browser(
        store=store,
        stage_code="refresh_competitor_rows",
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
            stage_code="refresh_competitor_rows",
            message="Requeued competitor row refresh after browser fallback.",
            details={
                "requeued_count": len(requeued_jobs),
            },
        )
    return {
        "action": "advance",
        "next_stage": "refresh_competitor_rows",
        "details": {"execution_count": len(executions), "requeued_row_count": 0},
    }


def _row_has_after_browser_terminal(
    store: RuntimeStore,
    *,
    request_id: str,
    source_record_id: str,
    fallback_handler: str,
) -> bool:
    row_job = _latest_row_job(
        [
            job
            for job in _api_jobs_for_stage(
                store=store,
                request_id=request_id,
                stage_code="refresh_competitor_rows",
            )
            if coerce_mapping(job.get("payload")).get("browser_fallback_resolved")
            and str(coerce_mapping(job.get("payload")).get("browser_fallback_handler") or "") == fallback_handler
        ],
        source_record_id=source_record_id,
        job_code="competitor_row_refresh",
    )
    if row_job is None:
        return False
    if _record_effective_status(row_job) in {"pending", "running"}:
        return False
    return True

def _browser_fallback_candidates(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seed_by_source_record = {
        str(seed.get("source_record_id") or ""): seed
        for seed in _seed_contexts(store=store, request_id=request_id)
        if str(seed.get("source_record_id") or "")
    }
    seed_by_candidate_key = _seed_context_by_candidate_key(store=store, request_id=request_id)
    for job in _api_jobs_for_stage(store=store, request_id=request_id, stage_code="refresh_competitor_rows"):
        if str(job.get("job_code") or "") != "competitor_row_refresh":
            continue
        if not _is_fallback_required(job):
            continue
        payload = dict(job.get("payload") or {})
        result = extract_effective_result_payload(job)
        handler_result = coerce_mapping(coerce_mapping(job.get("result")).get("handler_result"))
        next_action = coerce_mapping(handler_result.get("next_action"))
        next_action_payload = coerce_mapping(next_action.get("payload"))
        fallback_handler = _first_text(
            result.get("fallback_handler"),
            "tiktok_product_browser_fetch" if str(next_action.get("type") or "") == "browser_fallback" else "",
        )
        if fallback_handler not in {"tiktok_product_browser_fetch", "fastmoss_security_browser_resolve"}:
            continue
        browser_payload = coerce_mapping(result.get("browser_fallback_payload")) or next_action_payload
        source_record_id = _first_text(
            result.get("source_record_id"),
            browser_payload.get("source_record_id"),
            payload.get("source_record_id"),
        )
        if _row_has_after_browser_terminal(
            store=store,
            request_id=request_id,
            source_record_id=source_record_id,
            fallback_handler=fallback_handler,
        ):
            continue
        candidate_key = _first_text(
            result.get("candidate_key"),
            browser_payload.get("candidate_key"),
            payload.get("candidate_key"),
            payload.get("business_key"),
        )
        seed = dict(seed_by_source_record.get(source_record_id) or seed_by_candidate_key.get(candidate_key) or {})
        if not seed:
            seed = _minimal_seed_context(payload)
        business_entity_key = _first_text(
            result.get("business_entity_key"),
            browser_payload.get("business_entity_key"),
            payload.get("business_entity_key"),
            payload.get("business_key"),
            seed.get("business_entity_key"),
            candidate_key,
            source_record_id,
        )
        browser_payload = {
            **browser_payload,
            "candidate_key": _first_text(seed.get("candidate_key"), candidate_key, business_entity_key),
            "source_record_id": source_record_id,
            "business_entity_key": business_entity_key,
        }
        candidate = dict(seed)
        candidate.update(
            {
                "candidate_key": _first_text(seed.get("candidate_key"), candidate_key, business_entity_key),
                "fallback_key": _row_fallback_key(
                    source_record_id=source_record_id,
                    business_entity_key=business_entity_key,
                    fallback_handler=fallback_handler,
                ),
                "fallback_handler": fallback_handler,
                "fallback_reason": _first_text(result.get("fallback_reason")),
                "row_job_id": str(job.get("job_id") or ""),
                "row_payload": payload,
                "row_result": result,
                "source_record_id": source_record_id,
                "business_entity_key": business_entity_key,
                "browser_fallback_payload": _compact_mapping(browser_payload),
                "normalized_product_result": (
                    dict(result.get("normalized_product_result"))
                    if isinstance(result.get("normalized_product_result"), Mapping)
                    else {}
                ),
            }
        )
        candidates.append(candidate)
    return candidates

def _browser_execution_payload(
    *,
    request: Any,
    workflow: WorkflowDefinition,
    stage_code: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    fallback_payload = (
        dict(candidate.get("browser_fallback_payload"))
        if isinstance(candidate.get("browser_fallback_payload"), Mapping)
        else {}
    )
    payload = {
        **_runtime_child_context(request=request, workflow=workflow, stage_code=stage_code),
        **_payload_subset(
            request.payload,
            TIKTOK_REQUEST_PASSTHROUGH_KEYS
            + FASTMOSS_BROWSER_PASSTHROUGH_KEYS
            + RUNTIME_DB_PASSTHROUGH_KEYS
            + ARTIFACT_PASSTHROUGH_KEYS,
        ),
        **fallback_payload,
        "stage_code": stage_code,
        "candidate_key": str(candidate.get("candidate_key") or fallback_payload.get("candidate_key") or ""),
        "source_record_id": str(candidate.get("source_record_id") or fallback_payload.get("source_record_id") or ""),
        "business_entity_key": str(
            candidate.get("business_entity_key") or fallback_payload.get("business_entity_key") or ""
        ),
        "fallback_handler": fallback_handler,
    }
    if fallback_handler == "fastmoss_security_browser_resolve":
        payload.setdefault(
            "search_query",
            _first_text(candidate.get("search_query"), request.payload.get("search_query")),
        )
        payload.setdefault("search_digest", _search_digest_for_row_fallback(candidate))
        if not isinstance(payload.get("search_request"), Mapping):
            payload["search_request"] = {}
        if not isinstance(payload.get("verification_request"), Mapping):
            payload["verification_request"] = {}
        fastmoss_settings = _fastmoss_search_settings_from_request_payload(request.payload)
        if fastmoss_settings:
            payload["fastmoss"] = fastmoss_settings
        payload["fastmoss_browser_require_config_login"] = True
        payload["fastmoss_clear_browser_session_before_login"] = True
    return _compact_mapping(payload)

def _after_browser_row_payload(*, stage_code: str, candidate: Mapping[str, Any]) -> dict[str, Any]:
    fallback_handler = str(candidate.get("fallback_handler") or "")
    payload = dict(candidate.get("row_payload") or {}) if isinstance(candidate.get("row_payload"), Mapping) else {}
    browser_payload = (
        dict(candidate.get("browser_execution_payload"))
        if isinstance(candidate.get("browser_execution_payload"), Mapping)
        else {}
    )
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
        normalized = browser_payload.get("normalized_product_result")
        if isinstance(normalized, Mapping):
            payload["normalized_product_result"] = dict(normalized)
    elif fallback_handler == "fastmoss_security_browser_resolve":
        payload["fastmoss_security_browser_fallback_attempt"] = 1
        normalized = candidate.get("normalized_product_result")
        if isinstance(normalized, Mapping) and normalized:
            payload["normalized_product_result"] = dict(normalized)
    return _compact_mapping(payload)


def _browser_execution_fallback_key(execution: Any) -> str:
    payload = dict(getattr(execution, "payload", None) or {})
    fallback_handler = str(getattr(execution, "item_code", "") or payload.get("fallback_handler") or "")
    source_record_id = _first_text(payload.get("source_record_id"))
    business_entity_key = _first_text(payload.get("business_entity_key"), payload.get("candidate_key"))
    if not fallback_handler or not _first_text(source_record_id, business_entity_key):
        return ""
    return _row_fallback_key(
        source_record_id=source_record_id,
        business_entity_key=business_entity_key,
        fallback_handler=fallback_handler,
    )


def _fallback_candidates_needing_dispatch(
    *,
    fallback_candidates: list[dict[str, Any]],
    executions: list[Any],
) -> list[dict[str, Any]]:
    existing_keys = {
        key
        for execution in executions
        if (key := _browser_execution_fallback_key(execution))
    }
    return [
        candidate
        for candidate in fallback_candidates
        if str(candidate.get("fallback_key") or "") not in existing_keys
    ]


def _requeue_competitor_rows_after_browser(
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
        fallback_key = _browser_execution_fallback_key(execution)
        if fallback_key:
            terminal_by_key[fallback_key] = execution

    for candidate in fallback_candidates:
        execution = terminal_by_key.get(str(candidate.get("fallback_key") or ""))
        if execution is None:
            continue
        requeued.append(
            store.requeue_waiting_api_worker_job(
                job_id=str(candidate.get("row_job_id") or ""),
                payload=_after_browser_row_payload(
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

def _row_fallback_key(*, source_record_id: str, business_entity_key: str, fallback_handler: str) -> str:
    row_key = _first_text(source_record_id, business_entity_key)
    return f"{fallback_handler}:{row_key}"

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
