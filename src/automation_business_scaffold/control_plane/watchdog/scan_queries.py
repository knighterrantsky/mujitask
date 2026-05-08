from __future__ import annotations

import os
import time
from typing import Any, Iterable, Mapping

from automation_business_scaffold.control_plane.watchdog.models import (
    DEFAULT_LIMIT_PER_RULE,
    RULE_PRECEDENCE,
    RULE_SPECS,
    WatchdogCandidate,
    WatchdogRuleSpec,
)


def coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def resolve_db_url(params: Mapping[str, Any] | None = None) -> str:
    normalized = dict(params or {})
    db_url = str(
        normalized.get("execution_control_db_url")
        or normalized.get("db_url")
        or os.getenv("BUSINESS_EXECUTION_CONTROL_DB_URL")
        or os.getenv("EXECUTION_CONTROL_DB_URL")
        or ""
    ).strip()
    return db_url


def build_watchdog_store(params: Mapping[str, Any] | None = None) -> Any:
    from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore

    return RuntimeStore(db_url=resolve_db_url(params))


def missing_watchdog_helpers(store: Any) -> tuple[str, ...]:
    missing: list[str] = []
    for spec in RULE_SPECS:
        helper = getattr(store, spec.helper_name, None)
        if not callable(helper):
            missing.append(spec.helper_name)
    if not callable(getattr(store, "apply_watchdog_action", None)):
        missing.append("apply_watchdog_action")
    return tuple(missing)


def collect_watchdog_candidates(
    store: Any,
    *,
    now: float | None = None,
    limit_per_rule: int | None = DEFAULT_LIMIT_PER_RULE,
) -> tuple[tuple[WatchdogCandidate, ...], tuple[str, ...]]:
    current_time = coerce_float(now) or time.time()
    missing_helpers = missing_watchdog_helpers(store)
    candidates: list[WatchdogCandidate] = []
    for spec in RULE_SPECS:
        helper = getattr(store, spec.helper_name, None)
        if not callable(helper):
            continue
        rows = helper(now=current_time, limit=limit_per_rule)
        for row in rows or ():
            candidates.append(candidate_from_mapping(spec.rule_code, row, spec))
    return dedupe_candidates(candidates), missing_helpers


def candidate_from_mapping(rule_code: str, payload: Mapping[str, Any], spec: WatchdogRuleSpec) -> WatchdogCandidate:
    metadata = coerce_mapping(payload.get("metadata"))
    nested_payload = coerce_mapping(payload.get("payload"))
    target_table = str(
        payload.get("target_table")
        or payload.get("runtime_table")
        or metadata.get("target_table")
        or ""
    ).strip()
    target_id = str(
        payload.get("target_id")
        or payload.get("job_id")
        or payload.get("execution_id")
        or payload.get("request_id")
        or payload.get("outbox_id")
        or metadata.get("target_id")
        or ""
    ).strip()
    request_id = str(payload.get("request_id") or metadata.get("request_id") or "").strip()
    if target_table == "task_request" and not request_id:
        request_id = target_id
    return WatchdogCandidate(
        rule_code=rule_code,
        target_table=target_table,
        target_id=target_id,
        target_status=str(payload.get("status") or spec.target_status or "").strip(),
        target_kind=str(payload.get("target_kind") or payload.get("job_code") or payload.get("item_code") or "").strip(),
        request_id=request_id,
        parent_request_id=str(payload.get("parent_request_id") or metadata.get("parent_request_id") or "").strip(),
        worker_id=str(payload.get("worker_id") or "").strip(),
        worker_pid=coerce_int(payload.get("worker_pid")),
        run_id=str(payload.get("run_id") or metadata.get("run_id") or "").strip(),
        attempt_count=coerce_int(payload.get("attempt_count")),
        max_attempts=coerce_int(payload.get("max_attempts")),
        retry_count=coerce_int(payload.get("retry_count")),
        max_retries=coerce_int(payload.get("max_retries") or payload.get("max_retry_count")),
        lease_until=coerce_float(payload.get("lease_until")),
        started_at=coerce_float(payload.get("started_at")),
        heartbeat_at=coerce_float(payload.get("heartbeat_at")),
        last_progress_at=coerce_float(payload.get("last_progress_at")),
        max_execution_seconds=coerce_float(payload.get("max_execution_seconds")),
        max_idle_seconds=coerce_float(payload.get("max_idle_seconds")),
        heartbeat_timeout_seconds=coerce_float(payload.get("heartbeat_timeout_seconds")),
        progress_stage=str(payload.get("progress_stage") or "").strip(),
        next_retry_at=coerce_float(payload.get("next_retry_at")),
        reason=str(payload.get("reason") or metadata.get("reason") or "").strip(),
        payload=nested_payload,
        metadata=metadata,
    )


def dedupe_candidates(candidates: Iterable[WatchdogCandidate]) -> tuple[WatchdogCandidate, ...]:
    selected: dict[tuple[str, str], WatchdogCandidate] = {}
    for candidate in candidates:
        if not candidate.target_table or not candidate.target_id:
            continue
        key = candidate.dedupe_key
        existing = selected.get(key)
        if existing is None:
            selected[key] = candidate
            continue
        if RULE_PRECEDENCE.get(candidate.rule_code, 0) > RULE_PRECEDENCE.get(existing.rule_code, 0):
            selected[key] = candidate
    ordered = sorted(
        selected.values(),
        key=lambda item: (-RULE_PRECEDENCE.get(item.rule_code, 0), item.target_table, item.target_id),
    )
    return tuple(ordered)
