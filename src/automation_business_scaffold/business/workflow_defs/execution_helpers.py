from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, is_dataclass
from string import Formatter
from typing import Any, Iterable, Mapping, TypeAlias

from .models import FinalStatus, JobDefinition, ResolvedStageJobDefinition, SummaryPolicy

RecordLike: TypeAlias = Mapping[str, Any] | object | None

_FORMATTER = Formatter()
_SUCCESSFUL_HANDLER_STATUSES = frozenset({"success", "partial_success"})
_SUCCESSFUL_RECORD_STATUSES = frozenset({"success"})
_FAILURE_HANDLER_STATUSES = frozenset({"failed", "fallback_required"})
_ACTIVE_RECORD_STATUSES = frozenset({"pending", "running", "retry_wait"})
_TERMINAL_RECORD_STATUSES = frozenset({"success", "failed", "skipped", "cancelled"})
_TERMINAL_HANDLER_STATUSES = frozenset({"success", "skipped", "partial_success", "failed", "fallback_required"})


def merge_stage_contexts(*contexts: RecordLike) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for context in contexts:
        _deep_merge_dict(merged, _as_dict(context))
    return merged


def build_template_context(
    *sources: RecordLike,
    request_id: str = "",
    task_code: str = "",
    workflow_code: str = "",
    stage_code: str = "",
    job_code: str = "",
    item_code: str = "",
) -> dict[str, Any]:
    merged = merge_stage_contexts(*sources)
    if request_id:
        merged["request_id"] = request_id
    if task_code:
        merged["task_code"] = task_code
    if workflow_code:
        merged["workflow_code"] = workflow_code
    if stage_code:
        merged["stage_code"] = stage_code
    if job_code:
        merged["job_code"] = job_code
    if item_code:
        merged["item_code"] = item_code

    flattened: dict[str, Any] = {}
    _flatten_template_values(merged, flattened)
    for key, value in merged.items():
        flattened[key] = _clone_value(value)
    return _derive_template_aliases(flattened)


def render_key_template(
    template: str,
    *sources: RecordLike,
    request_id: str = "",
    task_code: str = "",
    workflow_code: str = "",
    stage_code: str = "",
    job_code: str = "",
    item_code: str = "",
) -> str:
    if not template:
        return ""
    render_context = {
        key: _stringify_template_value(value)
        for key, value in build_template_context(
            *sources,
            request_id=request_id,
            task_code=task_code,
            workflow_code=workflow_code,
            stage_code=stage_code,
            job_code=job_code,
            item_code=item_code,
        ).items()
    }
    return template.format_map(_DefaultTemplateValues(render_context))


def render_job_keys(
    job_def: JobDefinition | ResolvedStageJobDefinition,
    *sources: RecordLike,
    request_id: str = "",
    task_code: str = "",
    workflow_code: str = "",
    stage_code: str = "",
    job_code: str = "",
    item_code: str = "",
) -> dict[str, str]:
    resolved_job_code = job_code or job_def.job_code
    return {
        "business_key": render_key_template(
            job_def.business_key_template,
            *sources,
            request_id=request_id,
            task_code=task_code,
            workflow_code=workflow_code,
            stage_code=stage_code,
            job_code=resolved_job_code,
            item_code=item_code,
        ),
        "dedupe_key": render_key_template(
            job_def.dedupe_key_template,
            *sources,
            request_id=request_id,
            task_code=task_code,
            workflow_code=workflow_code,
            stage_code=stage_code,
            job_code=resolved_job_code,
            item_code=item_code,
        ),
    }


def build_stage_local_dedupe_key(base_key: str, job_code: str, *, stage_scope: str = "") -> str:
    normalized = str(base_key or "").strip()
    suffix = str(job_code or "").strip()
    stage_suffix = str(stage_scope or "").strip()
    parts = [normalized] if normalized else []
    if stage_suffix and not normalized.endswith(f":{stage_suffix}"):
        parts.append(stage_suffix)
    if suffix and not normalized.endswith(f":{suffix}"):
        parts.append(suffix)
    if parts:
        return ":".join(part for part in parts if part)
    return suffix


def build_projection_record(
    *,
    request_id: str,
    source_record_id: str,
    product_id: str,
    product_url: str,
    refresh_status: str,
    details: Mapping[str, Any],
    candidate_key: str = "",
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {"source_record_id": str(source_record_id or "")}
    if candidate_key:
        record["candidate_key"] = str(candidate_key)
    record.update(
        {
            "product_id": str(product_id or ""),
            "product_url": str(product_url or ""),
            "refresh_status": str(refresh_status or ""),
            "request_id": str(request_id or ""),
            "details": _clone_value(details),
        }
    )
    if extra_fields:
        record.update({str(key): _clone_value(value) for key, value in extra_fields.items()})
    return record


def build_projection_write_payload(
    *,
    stage_code: str,
    request_id: str,
    target_table_ref: str,
    records: Iterable[Mapping[str, Any]],
    mapper_code: str,
    write_mode: str,
    request_payload: Mapping[str, Any] | None = None,
    source_record_id: str = "",
    candidate_key: str = "",
    business_entity_key: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"stage_code": str(stage_code or "")}
    if candidate_key:
        payload["candidate_key"] = str(candidate_key)
    if business_entity_key:
        payload["business_entity_key"] = str(business_entity_key)
    if source_record_id:
        payload["source_record_id"] = str(source_record_id)
    payload.update(
        {
            "target_table_ref": str(target_table_ref or ""),
            "records": [_clone_value(record) for record in records],
            "mapper_code": str(mapper_code or ""),
            "write_mode": str(write_mode or ""),
            "idempotency_context": _build_idempotency_context(
                request_id=request_id,
                candidate_key=candidate_key,
                source_record_id=source_record_id,
            ),
        }
    )
    if request_payload:
        payload["request_payload"] = _clone_value(request_payload)
    return payload


def extract_handler_result(record_or_payload: RecordLike) -> dict[str, Any]:
    payload = _as_dict(record_or_payload)
    direct = payload.get("handler_result")
    if isinstance(direct, Mapping):
        return {str(key): _clone_value(value) for key, value in direct.items()}

    nested_result = payload.get("result")
    if isinstance(nested_result, Mapping):
        nested_direct = nested_result.get("handler_result")
        if isinstance(nested_direct, Mapping):
            return {str(key): _clone_value(value) for key, value in nested_direct.items()}
        if _looks_like_handler_result(nested_result):
            return {str(key): _clone_value(value) for key, value in nested_result.items()}

    if _looks_like_handler_result(payload):
        return payload
    return {}


def extract_handler_result_status(record_or_payload: RecordLike, *, default: str = "") -> str:
    handler_result = extract_handler_result(record_or_payload)
    if handler_result:
        return str(handler_result.get("status") or default)
    payload = _as_dict(record_or_payload)
    return str(payload.get("status") or default)


def extract_effective_result_payload(record_or_payload: RecordLike) -> dict[str, Any]:
    payload = _as_dict(record_or_payload)
    nested_result = payload.get("result")
    if isinstance(nested_result, Mapping):
        handler_result = extract_handler_result(nested_result)
        if handler_result:
            inner = handler_result.get("result")
            if isinstance(inner, Mapping):
                return {str(key): _clone_value(value) for key, value in inner.items()}
        return {str(key): _clone_value(value) for key, value in nested_result.items()}

    handler_result = extract_handler_result(payload)
    inner = handler_result.get("result") if handler_result else None
    if isinstance(inner, Mapping):
        return {str(key): _clone_value(value) for key, value in inner.items()}
    return {}


def extract_effective_summary_payload(record_or_payload: RecordLike) -> dict[str, Any]:
    payload = _as_dict(record_or_payload)
    nested_summary = payload.get("summary")
    if isinstance(nested_summary, Mapping):
        handler_result = extract_handler_result(payload)
        if handler_result:
            inner = handler_result.get("summary")
            if isinstance(inner, Mapping):
                return {str(key): _clone_value(value) for key, value in inner.items()}
        return {str(key): _clone_value(value) for key, value in nested_summary.items()}

    handler_result = extract_handler_result(payload)
    inner = handler_result.get("summary") if handler_result else None
    if isinstance(inner, Mapping):
        return {str(key): _clone_value(value) for key, value in inner.items()}
    return {}


def recover_stage_after_browser_summary_promotion(
    *,
    current_stage: str,
    summary_stage_code: str,
    browser_records: Iterable[RecordLike],
    continuation_started: bool,
    continuation_candidate_ready: bool,
    resume_stage_code: str = "browser_fallback",
) -> str:
    if current_stage != summary_stage_code:
        return ""
    browser_record_list = [_as_dict(record) for record in browser_records if _as_dict(record)]
    if not browser_record_list:
        return ""
    if any(str(record.get("status") or "") in _ACTIVE_RECORD_STATUSES for record in browser_record_list):
        return ""
    if not any(_effective_outcome_status(record) in {"success", "partial_success", "skipped"} for record in browser_record_list):
        return ""
    if continuation_started:
        return ""
    return resume_stage_code if continuation_candidate_ready else ""


def recover_browser_fallback_resume_stage(
    store: Any,
    *,
    request_id: str,
    current_stage: str,
    summary_stage_code: str,
    continuation_stage_codes: Iterable[str],
    continuation_candidate_ready: bool,
    browser_stage_code: str = "browser_fallback",
    resume_stage_code: str = "browser_fallback",
) -> str:
    return recover_stage_after_browser_summary_promotion(
        current_stage=current_stage,
        summary_stage_code=summary_stage_code,
        browser_records=stage_child_records(store, request_id=request_id, stage_code=browser_stage_code),
        continuation_started=any(
            stage_child_records(store, request_id=request_id, stage_code=stage_code)
            for stage_code in continuation_stage_codes
        ),
        continuation_candidate_ready=continuation_candidate_ready,
        resume_stage_code=resume_stage_code,
    )


def is_fallback_required(record_or_payload: RecordLike) -> bool:
    handler_status = extract_handler_result_status(record_or_payload)
    if handler_status == "fallback_required":
        return True
    result_payload = extract_effective_result_payload(record_or_payload)
    return bool(result_payload.get("fallback_required"))


def select_latest_successful_api_job(
    records: Iterable[RecordLike],
    job_code: str,
) -> dict[str, Any] | None:
    return select_latest_successful_record(records, code_field="job_code", code_value=job_code)


def select_latest_successful_api_job_result(
    records: Iterable[RecordLike],
    job_code: str,
) -> dict[str, Any]:
    selected = select_latest_successful_api_job(records, job_code)
    return extract_effective_result_payload(selected)


def select_latest_successful_browser_execution(
    records: Iterable[RecordLike],
    item_code: str,
) -> dict[str, Any] | None:
    return select_latest_successful_record(records, code_field="item_code", code_value=item_code)


def select_latest_successful_browser_execution_result(
    records: Iterable[RecordLike],
    item_code: str,
) -> dict[str, Any]:
    selected = select_latest_successful_browser_execution(records, item_code)
    return extract_effective_result_payload(selected)


def select_latest_successful_record(
    records: Iterable[RecordLike],
    *,
    code_field: str,
    code_value: str,
    accepted_record_statuses: frozenset[str] = _SUCCESSFUL_RECORD_STATUSES,
    accepted_handler_statuses: frozenset[str] = _SUCCESSFUL_HANDLER_STATUSES,
) -> dict[str, Any] | None:
    selected: dict[str, Any] | None = None
    selected_sort_key: tuple[float, float, float, int] | None = None
    for index, record_like in enumerate(records):
        record = _as_dict(record_like)
        if str(record.get(code_field) or "") != code_value:
            continue
        record_status = str(record.get("status") or "")
        handler_status = extract_handler_result_status(record)
        if record_status not in accepted_record_statuses and handler_status not in accepted_handler_statuses:
            continue
        sort_key = _record_sort_key(record, index=index)
        if selected_sort_key is None or sort_key >= selected_sort_key:
            selected = record
            selected_sort_key = sort_key
    return selected


def summarize_child_outcomes(
    records: Iterable[RecordLike],
    *,
    optional_codes: Iterable[str] = (),
) -> dict[str, Any]:
    optional_code_set = {str(code) for code in optional_codes if str(code)}
    status_counts: dict[str, int] = {}
    total_count = 0
    terminal_count = 0
    active_count = 0
    required_failed_count = 0
    optional_failed_count = 0
    fallback_required_count = 0

    for record_like in records:
        record = _as_dict(record_like)
        if not record:
            continue
        total_count += 1
        status = _effective_outcome_status(record)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in _TERMINAL_RECORD_STATUSES or status in _TERMINAL_HANDLER_STATUSES:
            terminal_count += 1
        if status in _ACTIVE_RECORD_STATUSES:
            active_count += 1
        if status == "fallback_required":
            fallback_required_count += 1
        if status in _FAILURE_HANDLER_STATUSES or status == "failed":
            code = _record_code(record)
            if code and code in optional_code_set:
                optional_failed_count += 1
            else:
                required_failed_count += 1

    return {
        "total_count": total_count,
        "terminal_count": terminal_count,
        "active_count": active_count,
        "success_count": status_counts.get("success", 0),
        "partial_success_count": status_counts.get("partial_success", 0),
        "failed_count": status_counts.get("failed", 0),
        "skipped_count": status_counts.get("skipped", 0),
        "cancelled_count": status_counts.get("cancelled", 0),
        "fallback_required_count": fallback_required_count,
        "required_failed_count": required_failed_count,
        "optional_failed_count": optional_failed_count,
        "statuses": status_counts,
    }


def compute_final_status(
    summary_policy: SummaryPolicy,
    *,
    child_records: Iterable[RecordLike] = (),
    optional_codes: Iterable[str] = (),
    explicit_status: str = "",
) -> FinalStatus:
    allowed_statuses = allowed_final_statuses(summary_policy)
    if explicit_status and explicit_status in allowed_statuses:
        return explicit_status  # type: ignore[return-value]

    outcome = summarize_child_outcomes(child_records, optional_codes=optional_codes)
    success_count = int(outcome["success_count"])
    partial_success_count = int(outcome["partial_success_count"])
    failed_count = int(outcome["failed_count"])
    skipped_count = int(outcome["skipped_count"])
    required_failed_count = int(outcome["required_failed_count"])
    optional_failed_count = int(outcome["optional_failed_count"])
    total_count = int(outcome["total_count"])

    if total_count == 0:
        candidate = "failed"
    elif required_failed_count > 0 and success_count + partial_success_count == 0:
        candidate = "failed"
    elif required_failed_count > 0:
        candidate = "partial_success"
    elif failed_count > 0 or optional_failed_count > 0 or partial_success_count > 0:
        candidate = "partial_success"
    elif success_count > 0 or skipped_count == total_count:
        candidate = "success"
    else:
        candidate = "failed"
    return _coerce_final_status(candidate, allowed_statuses)


def api_jobs_for_stage(store: Any, *, request_id: str, stage_code: str) -> list[dict[str, Any]]:
    return [
        job
        for job in store.list_api_worker_jobs_for_request(request_id=request_id)
        if str((job.get("payload") or {}).get("stage_code") or "") == stage_code
    ]


def browser_executions_for_stage(store: Any, *, request_id: str, stage_code: str) -> list[Any]:
    return [
        execution
        for execution in store.list_task_executions(request_id=request_id)
        if str((execution.payload or {}).get("stage_code") or "") == stage_code
    ]


def stage_child_records(store: Any, *, request_id: str, stage_code: str) -> list[Any]:
    return [
        *api_jobs_for_stage(store, request_id=request_id, stage_code=stage_code),
        *browser_executions_for_stage(store, request_id=request_id, stage_code=stage_code),
    ]


def all_child_records(store: Any, *, request_id: str) -> list[Any]:
    return [
        *store.list_api_worker_jobs_for_request(request_id=request_id),
        *store.list_task_executions(request_id=request_id),
    ]


def summarize_stage_children(
    store: Any,
    *,
    request_id: str,
    stage_code: str,
    optional_codes: Iterable[str] = (),
) -> dict[str, Any]:
    outcome = summarize_child_outcomes(
        stage_child_records(store, request_id=request_id, stage_code=stage_code),
        optional_codes=optional_codes,
    )
    return {
        "total_count": int(outcome["total_count"]),
        "terminal_count": int(outcome["terminal_count"]),
        "active_count": int(outcome["active_count"]),
        "statuses": dict(outcome["statuses"]),
    }


def any_api_jobs_active(jobs: Iterable[RecordLike]) -> bool:
    return any(_record_status(job) in _ACTIVE_RECORD_STATUSES for job in jobs)


def any_browser_executions_active(executions: Iterable[RecordLike]) -> bool:
    return any(_record_status(execution) in _ACTIVE_RECORD_STATUSES for execution in executions)


def has_active_records(records: Iterable[RecordLike]) -> bool:
    return any(record_effective_status(record) in _ACTIVE_RECORD_STATUSES for record in records)


def record_effective_status(record_or_payload: RecordLike) -> str:
    record = _as_dict(record_or_payload)
    if record:
        return _effective_outcome_status(record)
    status = str(getattr(record_or_payload, "status", "") or "")
    handler_status = extract_handler_result_status(getattr(record_or_payload, "result", {}) or {})
    return handler_status or status


def timeout_seconds_for_workflow(workflow: Any, target_code: str) -> float:
    for rule in workflow.timeout_policy:
        if rule.target_code == target_code:
            return float(rule.timeout_seconds)
    return 0.0


def update_request_stage_cursor(
    *,
    store: Any,
    request: Any,
    stage_code: str,
    payload: Mapping[str, Any],
) -> None:
    cursor = dict(request.stage_cursor or {})
    stage_results = dict(cursor.get("stage_results") or {})
    stage_results[stage_code] = dict(payload)
    cursor["stage_results"] = stage_results
    store.update_task_request(
        request_id=request.request_id,
        stage_cursor=cursor,
        progress_stage=stage_code,
        last_progress_at=time.time(),
    )


def allowed_final_statuses(summary_policy: SummaryPolicy) -> tuple[str, ...]:
    ordered: list[str] = []
    for rule in summary_policy.rules:
        if rule.final_status not in ordered:
            ordered.append(rule.final_status)
    if not ordered:
        return ("success", "partial_success", "failed")
    return tuple(ordered)


def _as_dict(value: RecordLike) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _clone_value(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return {str(key): _clone_value(item) for key, item in payload.items()}
    if is_dataclass(value):
        payload = asdict(value)
        if isinstance(payload, Mapping):
            return {str(key): _clone_value(item) for key, item in payload.items()}
    return {}


def _deep_merge_dict(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key, value in incoming.items():
        if value is None:
            continue
        if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
            nested_target = {str(child_key): _clone_value(child_value) for child_key, child_value in target[key].items()}
            _deep_merge_dict(nested_target, value)
            target[key] = nested_target
            continue
        if isinstance(value, Mapping):
            target[key] = merge_stage_contexts(value)
            continue
        if isinstance(value, list):
            target[key] = [_clone_value(item) for item in value]
            continue
        if isinstance(value, tuple):
            target[key] = tuple(_clone_value(item) for item in value)
            continue
        target[key] = value


def _build_idempotency_context(*, request_id: str, candidate_key: str, source_record_id: str) -> dict[str, str]:
    context = {"request_id": str(request_id or "")}
    if candidate_key:
        context["candidate_key"] = str(candidate_key)
    if source_record_id:
        context["source_record_id"] = str(source_record_id)
    return context


def _clone_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _clone_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_value(item) for item in value)
    return value


def _flatten_template_values(value: Any, flattened: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key not in flattened:
                flattened[key] = _clone_value(item)
            if isinstance(item, Mapping):
                _flatten_template_values(item, flattened)


def _derive_template_aliases(context: dict[str, Any]) -> dict[str, Any]:
    derived = dict(context)
    if "view_ref_or_default" not in derived:
        derived["view_ref_or_default"] = derived.get("view_ref") or "default"
    if "product_id_or_url" not in derived:
        derived["product_id_or_url"] = (
            derived.get("product_id")
            or derived.get("normalized_product_url")
            or derived.get("product_url")
            or derived.get("url")
            or ""
        )
    if "product_id_or_fastmoss_key" not in derived:
        derived["product_id_or_fastmoss_key"] = (
            derived.get("product_id")
            or derived.get("fastmoss_product_key")
            or derived.get("fastmoss_product_id")
            or derived.get("product_key")
            or ""
        )
    if "product_id_or_group" not in derived:
        derived["product_id_or_group"] = (
            derived.get("product_id")
            or derived.get("group_code")
            or derived.get("group_key")
            or derived.get("business_key")
            or ""
        )
    if "business_entity_key" not in derived:
        derived["business_entity_key"] = (
            derived.get("business_key")
            or derived.get("entity_key")
            or derived.get("creator_id")
            or derived.get("product_id")
            or ""
        )
    if "entity_key" not in derived:
        derived["entity_key"] = (
            derived.get("business_entity_key")
            or derived.get("business_key")
            or derived.get("product_id")
            or derived.get("creator_id")
            or ""
        )
    if "entity_business_keys" not in derived:
        entity_keys = derived.get("entity_keys")
        if isinstance(entity_keys, list):
            derived["entity_business_keys"] = entity_keys
        else:
            fallback_entity_key = derived.get("entity_key") or derived.get("business_key") or ""
            derived["entity_business_keys"] = [fallback_entity_key] if fallback_entity_key else []
    if "asset_source" not in derived:
        derived["asset_source"] = (
            derived.get("asset_source")
            or derived.get("source_type")
            or derived.get("source_url")
            or derived.get("kind")
            or ""
        )
    if "search_digest" not in derived:
        search_query = str(derived.get("search_query") or "")
        filters = derived.get("filters")
        if search_query or filters:
            derived["search_digest"] = _digest_value({"search_query": search_query, "filters": filters})
        else:
            derived["search_digest"] = ""
    if "observation_at" not in derived:
        derived["observation_at"] = (
            derived.get("observation_at")
            or derived.get("snapshot_at")
            or derived.get("collected_at")
            or ""
        )
    return derived


def _stringify_template_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, tuple):
        return _stringify_template_value(list(value))
    if isinstance(value, list):
        if all(not isinstance(item, (Mapping, list, tuple)) for item in value):
            return ",".join(_stringify_template_value(item) for item in value if _stringify_template_value(item))
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _looks_like_handler_result(value: Mapping[str, Any]) -> bool:
    status = value.get("status")
    return isinstance(status, str) and (
        "handler_code" in value or "job_id" in value or "request_id" in value
    )


def _effective_outcome_status(record: Mapping[str, Any]) -> str:
    handler_status = extract_handler_result_status(record)
    if handler_status:
        return handler_status
    return str(record.get("status") or "")


def _record_code(record: Mapping[str, Any]) -> str:
    return (
        str(record.get("job_code") or "")
        or str(record.get("item_code") or "")
        or str(record.get("handler_code") or "")
    )


def _record_status(record_or_payload: RecordLike) -> str:
    record = _as_dict(record_or_payload)
    if record:
        return str(record.get("status") or "")
    return str(getattr(record_or_payload, "status", "") or "")


def _record_sort_key(record: Mapping[str, Any], *, index: int) -> tuple[float, float, float, int]:
    return (
        _coerce_float(record.get("finished_at")),
        _coerce_float(record.get("updated_at")),
        _coerce_float(record.get("created_at")),
        index,
    )


def _coerce_float(value: Any) -> float:
    if value in ("", None):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _digest_value(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _coerce_final_status(candidate: str, allowed_statuses: tuple[str, ...]) -> FinalStatus:
    if candidate in allowed_statuses:
        return candidate  # type: ignore[return-value]
    for fallback in ("partial_success", "failed", "success"):
        if fallback in allowed_statuses:
            return fallback  # type: ignore[return-value]
    return "failed"


class _DefaultTemplateValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


__all__ = [
    "allowed_final_statuses",
    "all_child_records",
    "any_api_jobs_active",
    "any_browser_executions_active",
    "api_jobs_for_stage",
    "browser_executions_for_stage",
    "build_projection_record",
    "build_projection_write_payload",
    "build_stage_local_dedupe_key",
    "build_template_context",
    "compute_final_status",
    "extract_effective_result_payload",
    "extract_effective_summary_payload",
    "extract_handler_result",
    "extract_handler_result_status",
    "has_active_records",
    "is_fallback_required",
    "merge_stage_contexts",
    "record_effective_status",
    "recover_browser_fallback_resume_stage",
    "recover_stage_after_browser_summary_promotion",
    "render_job_keys",
    "render_key_template",
    "select_latest_successful_api_job",
    "select_latest_successful_api_job_result",
    "select_latest_successful_browser_execution",
    "select_latest_successful_browser_execution_result",
    "select_latest_successful_record",
    "stage_child_records",
    "summarize_stage_children",
    "summarize_child_outcomes",
    "timeout_seconds_for_workflow",
    "update_request_stage_cursor",
]
