from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from automation_business_scaffold.contracts.handler.shared import coerce_mapping
from automation_business_scaffold.control_plane.runtime_config.settings import (
    SELECTION_KEYWORD_TASK_CODE,
)
from automation_business_scaffold.contracts.workflow import WorkflowDefinition
from automation_business_scaffold.contracts.workflow.execution_helpers import (
    api_jobs_for_stage as _api_jobs_for_stage,
    browser_executions_for_stage as _browser_executions_for_stage,
    build_stage_local_dedupe_key,
    extract_effective_result_payload,
    extract_handler_result_status,
    render_job_keys,
    select_latest_successful_api_job,
    timeout_seconds_for_workflow as _timeout_seconds,
)
from automation_business_scaffold.domains.tiktok.mappers.keyword_search_mapper import (
    keyword_search_parameter_mapper,
)


OPTIONAL_FINAL_STATUS_CODES = ("tiktok_product_browser_fetch",)
TIKTOK_REQUEST_PASSTHROUGH_KEYS = (
    "fallback_reason",
    "force_failure",
    "force_fallback",
    "mock_response",
    "normalized_product_result",
    "raw_request_result",
    "request_result",
    "source_payload",
    "tiktok_request_result",
)
FASTMOSS_PRODUCT_PASSTHROUGH_KEYS = (
    "fastmoss_bundle",
    "fastmoss_result",
    "mock_fastmoss_bundle",
    "product_fact_bundle",
    "required",
)
RUNTIME_DB_PASSTHROUGH_KEYS = (
    "execution_control_db_url",
    "db_url",
)
FASTMOSS_BROWSER_PASSTHROUGH_KEYS = (
    "browser_profile_ref",
    "browser_profile_id",
    "browser_provider_name",
    "browser_workspace_id",
    "browser_headless",
    "browser_force_open",
    "browser_timeout_ms",
    "fastmoss_browser_profile_ref",
    "fastmoss_browser_profile_id",
    "fastmoss_browser_provider_name",
    "fastmoss_browser_workspace_id",
    "fastmoss_browser_timeout_ms",
    "fastmoss_slider_max_attempts",
    "fastmoss_slider_appear_timeout_ms",
    "fastmoss_slider_settle_ms",
    "fastmoss_slider_confirm_ms",
    "mock_fastmoss_security_browser_resolve",
)
FACT_PERSISTENCE_PASSTHROUGH_KEYS = (
    "persistence",
    "require_database_persistence",
    "requires_fact_db",
)
ARTIFACT_PASSTHROUGH_KEYS = (
    "artifact_store",
    "require_object_storage",
    "requires_object_storage",
)


class StageContext:
    request_id: str
    task_code: str
    workflow_code: str
    stage_code: str
    payload: Mapping[str, Any] = field(default_factory=dict)


class StageDecision:
    action: str
    stage_code: str
    next_stage: str = ""
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        if self.action == "waiting":
            return {
                "action": "waiting",
                "current_stage": self.stage_code,
                "message": self.message,
                "details": dict(self.details),
            }
        result: dict[str, Any] = {"action": self.action}
        if self.next_stage:
            result["next_stage"] = self.next_stage
        if self.details:
            result["details"] = dict(self.details)
        return result


class SummaryInputs:
    candidate_contexts: list[dict[str, Any]]
    row_results: list[dict[str, Any]]
    child_records: list[Any]


def workflow_stage_context(*, request: Any, workflow: Any, stage_code: str) -> StageContext:
    return StageContext(
        request_id=str(request.request_id),
        task_code=str(request.task_code),
        workflow_code=str(workflow.workflow_code),
        stage_code=stage_code,
        payload=dict(request.payload or {}),
    )


def _fastmoss_search_settings_from_request_payload(request_payload: Mapping[str, Any]) -> dict[str, Any]:
    settings = dict(request_payload.get("fastmoss") or {}) if isinstance(request_payload.get("fastmoss"), Mapping) else {}
    for source_key, target_key in (
        ("fastmoss_phone", "phone"),
        ("fastmoss_password", "password"),
        ("fastmoss_phone_env", "phone_env"),
        ("fastmoss_password_env", "password_env"),
        ("fastmoss_base_url", "base_url"),
        ("region", "region"),
        ("fastmoss_timeout", "timeout"),
        ("browser_cookies", "browser_cookies"),
        ("execution_control_db_url", "execution_control_db_url"),
        ("db_url", "db_url"),
        ("fastmoss_cookie_cache_namespace", "cookie_cache_namespace"),
        ("fastmoss_cookie_cache_enabled", "cookie_cache_enabled"),
        ("fastmoss_cookie_cache_ttl_seconds", "cookie_cache_ttl_seconds"),
    ):
        value = request_payload.get(source_key)
        if value not in (None, "", [], {}):
            settings.setdefault(target_key, value)
    settings.setdefault("live_fetch", True)
    settings.setdefault("ensure_logged_in", True)
    return {key: value for key, value in settings.items() if value not in (None, "", [], {})}


def _keyword_seed_import_search_request(
    request_payload: Mapping[str, Any],
    *,
    latest_import_job: Mapping[str, Any] | None,
    retry_after_fastmoss_browser: bool,
) -> dict[str, Any]:
    previous_payload = coerce_mapping((latest_import_job or {}).get("payload"))
    previous_search_request = coerce_mapping(previous_payload.get("search_request"))
    search_request = dict(previous_search_request) if retry_after_fastmoss_browser and previous_search_request else keyword_search_parameter_mapper(request_payload)
    for key in RUNTIME_DB_PASSTHROUGH_KEYS:
        if request_payload.get(key) not in (None, ""):
            search_request[key] = request_payload.get(key)
    if retry_after_fastmoss_browser:
        search_request["fastmoss_security_browser_fallback_attempt"] = 1
    return search_request


def _keyword_seed_import_retry_after_fastmoss_browser_exists(jobs: list[dict[str, Any]]) -> bool:
    return any(
        int(coerce_mapping(job.get("payload")).get("fastmoss_security_browser_fallback_attempt") or 0) > 0
        for job in jobs
    )


def _fastmoss_security_browser_fallback_cursor(*, store: RuntimeStore, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    return dict(stage_results.get("fastmoss_security_browser_fallback") or {})


def _fastmoss_security_browser_fallback_attempted(*, store: RuntimeStore, request_id: str) -> bool:
    if _fastmoss_security_browser_fallback_cursor(store=store, request_id=request_id):
        return True
    return bool(
        _browser_executions_for_stage(
            store=store,
            request_id=request_id,
            stage_code="fastmoss_security_browser_fallback",
        )
    )


def _fastmoss_security_fallback_payload_from_job(import_job: Mapping[str, Any]) -> dict[str, Any]:
    job_payload = coerce_mapping(import_job.get("payload"))
    result_payload = extract_effective_result_payload(import_job)
    search_request = coerce_mapping(job_payload.get("search_request")) or coerce_mapping(result_payload.get("search_request"))
    security_context = coerce_mapping(result_payload.get("security_context"))
    return {
        "search_query": _first_text(
            search_request.get("search_query"),
            search_request.get("keyword"),
            job_payload.get("search_query"),
        ),
        "search_digest": _first_text(job_payload.get("search_digest"), search_request.get("search_digest")),
        "search_request": search_request,
        "security_context": security_context,
        "fallback_source_job_id": _first_text(result_payload.get("fallback_source_job_id"), import_job.get("job_id")),
    }


def _finalize_fastmoss_security_required(
    import_job: Mapping[str, Any],
    *,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    result_payload = extract_effective_result_payload(import_job)
    return {
        "action": "finalize",
        "final_status": "failed",
        "details": {
            "error_code": "fastmoss_security_verification_required",
            "fallback_required": True,
            "fallback_reason": "fastmoss_search_security_verification",
            "security_context": dict(result_payload.get("security_context") or {}),
            **dict(details),
        },
    }


def _fastmoss_browser_resource_code(payload: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("fastmoss_browser_profile_ref"),
        payload.get("browser_profile_ref"),
        payload.get("profile_ref"),
        "fastmoss:browser",
    )


def _payload_subset(payload: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}


def _candidate_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_candidates = keyword_import.get("candidate_contexts")
    if isinstance(import_candidates, list):
        return [dict(item) for item in import_candidates if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    candidates = import_payload.get("normalized_candidates")
    if isinstance(candidates, list):
        return [dict(item) for item in candidates if isinstance(item, Mapping)]

    processed = dict(stage_results.get("process_product_candidates") or {})
    legacy_candidates = processed.get("candidate_contexts")
    if isinstance(legacy_candidates, list):
        return [dict(item) for item in legacy_candidates if isinstance(item, Mapping)]

    search_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="search_product_candidates")
    search_job = select_latest_successful_api_job(search_jobs, "fastmoss_product_search")
    search_payload = extract_effective_result_payload(search_job)
    return _normalize_search_candidates(
        search_payload.get("candidates"),
        search_query=str(request.payload.get("search_query") or ""),
        output_conditions=dict(request.payload.get("output_conditions") or {}),
        max_candidates=int(request.payload.get("max_candidates") or 0),
    )


def _keyword_seed_import_payload(store: RuntimeStore, *, request_id: str) -> dict[str, Any]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    return {**import_payload, **keyword_import}


def _seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    request = store.load_task_request(request_id=request_id)
    stage_results = dict((request.stage_cursor or {}).get("stage_results") or {})
    keyword_import = dict(stage_results.get("keyword_seed_import") or {})
    import_seeds = keyword_import.get("seed_contexts")
    if isinstance(import_seeds, list):
        return [dict(item) for item in import_seeds if isinstance(item, Mapping)]

    import_jobs = _api_jobs_for_stage(store=store, request_id=request_id, stage_code="keyword_seed_import")
    import_payload = extract_effective_result_payload(_latest_job(import_jobs, job_code="keyword_seed_import"))
    seeds = import_payload.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]

    inserted = dict(stage_results.get("insert_seed_rows") or {})
    seeds = inserted.get("seed_contexts")
    if isinstance(seeds, list):
        return [dict(item) for item in seeds if isinstance(item, Mapping)]
    return _build_seed_contexts(
        candidates=_candidate_contexts(store=store, request_id=request_id),
        jobs=_api_jobs_for_stage(store=store, request_id=request_id, stage_code="insert_seed_rows"),
    )


def _successful_seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in _seed_contexts(store=store, request_id=request_id)
        if str(item.get("seed_status") or "") == "success"
    ]


def _selection_row_source_record_id(seed: Mapping[str, Any]) -> str:
    return _first_text(seed.get("source_record_id"), seed.get("product_id"), seed.get("candidate_key"))


def _all_selection_row_refresh_jobs(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    return [
        *_api_jobs_for_stage(store=store, request_id=request_id, stage_code="refresh_selection_rows"),
        *_api_jobs_for_stage(
            store=store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        ),
    ]


def _selection_row_has_final_status(
    store: RuntimeStore,
    *,
    request_id: str,
    source_record_id: str,
) -> bool:
    row_job = _latest_row_job(
        _all_selection_row_refresh_jobs(store=store, request_id=request_id),
        source_record_id=source_record_id,
        job_code="selection_row_refresh",
    )
    return _record_effective_status(row_job) in {"success", "partial_success", "failed", "skipped"}


def _selection_row_has_successful_resume(
    store: RuntimeStore,
    *,
    request_id: str,
    source_record_id: str,
) -> bool:
    row_job = _latest_row_job(
        _api_jobs_for_stage(
            store=store,
            request_id=request_id,
            stage_code="resume_selection_rows_after_browser_fallback",
        ),
        source_record_id=source_record_id,
        job_code="selection_row_refresh",
    )
    return _record_effective_status(row_job) in {"success", "partial_success", "skipped"}


def _pending_selection_seed_contexts(store: RuntimeStore, *, request_id: str) -> list[dict[str, Any]]:
    return [
        seed
        for seed in _successful_seed_contexts(store=store, request_id=request_id)
        if not _selection_row_has_final_status(
            store=store,
            request_id=request_id,
            source_record_id=_selection_row_source_record_id(seed),
        )
    ]


def _dispatch_next_selection_row_refresh_job(
    *,
    store: RuntimeStore,
    request: Any,
    workflow: WorkflowDefinition,
    seed_contexts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not seed_contexts:
        return {"created_count": 0, "updated_count": 0, "skipped_count": 0}
    row_job_def = workflow.require_job("selection_row_refresh")
    source_table_ref = str(
        request.payload.get("selection_table_ref")
        or request.payload.get("seed_table_ref")
        or request.payload.get("target_table_ref")
        or request.payload.get("table_url")
        or ""
    )
    seed = dict(seed_contexts[0])
    product_identity = dict(seed.get("product_identity") or {})
    source_record_id = _selection_row_source_record_id(seed)
    row_payload = {
        **_payload_subset(
            request.payload,
            TIKTOK_REQUEST_PASSTHROUGH_KEYS
            + FASTMOSS_PRODUCT_PASSTHROUGH_KEYS
            + FACT_PERSISTENCE_PASSTHROUGH_KEYS
            + ARTIFACT_PASSTHROUGH_KEYS
            + ("table_refs", "access_token", "access_token_env", "validate_schema"),
        ),
        "request_payload": dict(request.payload or {}),
        "stage_code": "refresh_selection_rows",
        "source_record_id": source_record_id,
        "source_record_id_or_product_id": _first_text(source_record_id, seed.get("product_id")),
        "business_key": seed.get("business_entity_key") or seed.get("candidate_key") or "",
        "product_identity": product_identity,
        "normalized_product_url": seed.get("normalized_product_url")
        or product_identity.get("normalized_product_url")
        or "",
        "source_table_ref": source_table_ref,
        "target_table_ref": source_table_ref,
        "source_context": dict(seed.get("source_context") or {}),
        "fallback_allowed": bool(request.payload.get("fallback_allowed", True)),
        "writeback_enabled": bool(request.payload.get("writeback_enabled", True)),
    }
    row_payload["requires_fact_db"] = True
    row_payload["requires_object_storage"] = True
    row_payload["require_database_persistence"] = True
    row_payload["require_object_storage"] = True
    row_keys = render_job_keys(
        row_job_def,
        request.payload,
        seed,
        row_payload,
        request_id=request.request_id,
        task_code=request.task_code,
        workflow_code=workflow.workflow_code,
        stage_code="refresh_selection_rows",
        job_code=row_job_def.job_code,
    )
    return store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code=request.task_code,
        job_code=row_job_def.job_code,
        jobs=[
            {
                "business_key": row_keys["business_key"],
                "dedupe_key": build_stage_local_dedupe_key(row_keys["dedupe_key"], row_job_def.job_code),
                "payload": row_payload,
                "max_execution_seconds": _timeout_seconds(workflow, row_job_def.job_code),
            }
        ],
    )


def _seed_context_by_candidate_key(store: RuntimeStore, *, request_id: str) -> dict[str, dict[str, Any]]:
    return {str(item.get("candidate_key") or ""): item for item in _seed_contexts(store=store, request_id=request_id)}


def _build_seed_contexts(*, candidates: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    job_by_candidate = {
        str((job.get("payload") or {}).get("candidate_key") or ""): job
        for job in jobs
        if str((job.get("payload") or {}).get("candidate_key") or "")
    }
    seed_contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = candidate["candidate_key"]
        job = job_by_candidate.get(candidate_key)
        result_payload = extract_effective_result_payload(job)
        target_record_ids = result_payload.get("target_record_ids") if isinstance(result_payload.get("target_record_ids"), list) else []
        source_record_id = str(target_record_ids[0] if target_record_ids else candidate_key)
        seed_contexts.append(
            {
                **candidate,
                "source_record_id": source_record_id,
                "seed_status": _record_effective_status(job),
                "seed_result": result_payload,
                "target_record_ids": [str(item) for item in target_record_ids],
            }
        )
    return seed_contexts


def _normalize_search_candidates(
    raw_candidates: Any,
    *,
    search_query: str,
    output_conditions: Mapping[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    from .policies.candidate_filter import normalize_search_candidates

    return normalize_search_candidates(
        raw_candidates,
        search_query=search_query,
        output_conditions=output_conditions,
        max_candidates=max_candidates,
    )


def _latest_job(jobs: list[dict[str, Any]], *, job_code: str) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        selected = job
    return selected


def _latest_row_job(
    jobs: list[dict[str, Any]],
    *,
    source_record_id: str,
    job_code: str,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    for job in jobs:
        if str(job.get("job_code") or "") != job_code:
            continue
        payload = dict(job.get("payload") or {})
        if str(payload.get("source_record_id") or "") != source_record_id:
            continue
        selected = job
    return selected


def _record_effective_status(record: Any) -> str:
    if record is None:
        return ""
    if isinstance(record, Mapping):
        status = str(record.get("status") or "")
        handler_status = extract_handler_result_status(record)
        return handler_status or status
    status = str(getattr(record, "status", "") or "")
    handler_status = extract_handler_result_status(record)
    return handler_status or status


def _waiting(*, stage_code: str, message: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "action": "waiting",
        "current_stage": stage_code,
        "message": message,
        "details": dict(details or {}),
    }


def _require_keyword_workflow() -> WorkflowDefinition:
    from automation_business_scaffold.domains.tiktok.workflows import get_workflow_definition

    return get_workflow_definition(SELECTION_KEYWORD_TASK_CODE)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
