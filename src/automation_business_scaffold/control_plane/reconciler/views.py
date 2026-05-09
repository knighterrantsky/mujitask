from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal, Mapping

from automation_business_scaffold.infrastructure.runtime.runtime_records import (
    RuntimeTaskExecutionRecord,
    RuntimeTaskRequestRecord,
)

ChildKind = Literal["api_worker_job", "task_execution"]
ChildRecord = Mapping[str, Any] | RuntimeTaskExecutionRecord
RequestRecord = Mapping[str, Any] | RuntimeTaskRequestRecord

ACTIVE_CHILD_STATUSES = frozenset({"pending", "running"})
TERMINAL_CHILD_STATUSES = frozenset({"finished", "cancelled", "success", "failed", "skipped", "partial_success"})
SUCCESS_CHILD_STATUSES = frozenset({"success", "skipped", "partial_success"})
FAILED_CHILD_STATUSES = frozenset({"failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class RequestChildView:
    child_kind: ChildKind
    runtime_table: str
    child_id: str
    request_id: str
    child_code: str
    status: str
    task_code: str = ""
    workflow_code: str = ""
    stage_code: str = ""
    business_key: str = ""
    dedupe_key: str = ""
    resource_code: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    worker_id: str = ""
    run_id: str = ""
    available_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    heartbeat_at: float = 0.0
    error_text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_CHILD_STATUSES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_CHILD_STATUSES

    @property
    def sort_timestamp(self) -> float:
        return max(
            self.finished_at,
            self.updated_at,
            self.heartbeat_at,
            self.started_at,
            self.available_at,
            self.created_at,
            0.0,
        )

    @property
    def has_fallback_signal(self) -> bool:
        return _has_fallback_signal(self.summary) or _has_fallback_signal(self.result)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RequestChildSummary:
    total_count: int
    counts: dict[str, int]
    active_count: int
    terminal_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    cancelled_count: int
    partial_success_count: int
    fallback_required_count: int
    api_worker_job_count: int
    task_execution_count: int
    latest_updated_at: float = 0.0
    latest_terminal_at: float = 0.0

    def request_counter_fields(self) -> dict[str, int]:
        return {
            "child_total_count": self.total_count,
            "child_terminal_count": self.terminal_count,
            "child_success_count": self.success_count,
            "child_failed_count": self.failed_count,
            "child_skipped_count": self.skipped_count,
            "child_active_count": self.active_count,
            "child_cancelled_count": self.cancelled_count,
            "child_partial_success_count": self.partial_success_count,
            "child_fallback_required_count": self.fallback_required_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total_count,
            "counts": dict(self.counts),
            "active_count": self.active_count,
            "terminal_count": self.terminal_count,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "cancelled_count": self.cancelled_count,
            "partial_success_count": self.partial_success_count,
            "fallback_required_count": self.fallback_required_count,
            "api_worker_job_count": self.api_worker_job_count,
            "task_execution_count": self.task_execution_count,
            "latest_updated_at": self.latest_updated_at,
            "latest_terminal_at": self.latest_terminal_at,
            "request_counts": self.request_counter_fields(),
        }


def build_request_child_views(
    *,
    api_worker_jobs: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    task_executions: list[ChildRecord] | tuple[ChildRecord, ...] = (),
) -> tuple[RequestChildView, ...]:
    views: list[RequestChildView] = []
    for job in api_worker_jobs:
        views.append(_build_api_worker_job_view(job))
    for execution in task_executions:
        views.append(_build_task_execution_view(execution))
    return tuple(sorted(views, key=_child_sort_key, reverse=True))


def summarize_child_status_counts(
    child_views: list[RequestChildView] | tuple[RequestChildView, ...],
) -> RequestChildSummary:
    counts = Counter(view.status for view in child_views if view.status)
    latest_updated_at = max((view.sort_timestamp for view in child_views), default=0.0)
    latest_terminal_at = max((view.sort_timestamp for view in child_views if view.is_terminal), default=0.0)
    ordered_counts = {status: counts[status] for status in sorted(counts)}
    return RequestChildSummary(
        total_count=len(child_views),
        counts=ordered_counts,
        active_count=sum(1 for view in child_views if view.is_active),
        terminal_count=sum(1 for view in child_views if view.is_terminal),
        success_count=sum(1 for view in child_views if view.status in SUCCESS_CHILD_STATUSES),
        failed_count=sum(1 for view in child_views if view.status in FAILED_CHILD_STATUSES),
        skipped_count=counts.get("skipped", 0),
        cancelled_count=counts.get("cancelled", 0),
        partial_success_count=counts.get("partial_success", 0),
        fallback_required_count=sum(1 for view in child_views if view.has_fallback_signal),
        api_worker_job_count=sum(1 for view in child_views if view.child_kind == "api_worker_job"),
        task_execution_count=sum(1 for view in child_views if view.child_kind == "task_execution"),
        latest_updated_at=latest_updated_at,
        latest_terminal_at=latest_terminal_at,
    )


def aggregate_request_child_counts(
    *,
    api_worker_jobs: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    task_executions: list[ChildRecord] | tuple[ChildRecord, ...] = (),
) -> dict[str, int]:
    summary = summarize_child_status_counts(
        build_request_child_views(
            api_worker_jobs=api_worker_jobs,
            task_executions=task_executions,
        )
    )
    return summary.request_counter_fields()


def filter_request_child_views(
    child_views: list[RequestChildView] | tuple[RequestChildView, ...],
    *,
    child_kind: str = "",
    child_code: str = "",
    status: str = "",
    request_id: str = "",
    business_key: str = "",
    terminal_only: bool = False,
    fallback_required_only: bool = False,
) -> tuple[RequestChildView, ...]:
    normalized_kind = str(child_kind or "").strip()
    normalized_code = str(child_code or "").strip()
    normalized_status = str(status or "").strip()
    normalized_request_id = str(request_id or "").strip()
    normalized_business_key = str(business_key or "").strip()
    return tuple(
        view
        for view in child_views
        if (not normalized_kind or view.child_kind == normalized_kind)
        and (not normalized_code or view.child_code == normalized_code)
        and (not normalized_status or view.status == normalized_status)
        and (not normalized_request_id or view.request_id == normalized_request_id)
        and (not normalized_business_key or view.business_key == normalized_business_key)
        and (not terminal_only or view.is_terminal)
        and (not fallback_required_only or view.has_fallback_signal)
    )


def latest_request_child_view(
    child_views: list[RequestChildView] | tuple[RequestChildView, ...],
    *,
    child_kind: str = "",
    child_code: str = "",
    status: str = "",
    request_id: str = "",
    business_key: str = "",
    terminal_only: bool = False,
    fallback_required_only: bool = False,
) -> RequestChildView | None:
    filtered = filter_request_child_views(
        child_views,
        child_kind=child_kind,
        child_code=child_code,
        status=status,
        request_id=request_id,
        business_key=business_key,
        terminal_only=terminal_only,
        fallback_required_only=fallback_required_only,
    )
    return filtered[0] if filtered else None


def latest_terminal_child_views(
    child_views: list[RequestChildView] | tuple[RequestChildView, ...],
    *,
    child_kind: str = "",
    child_code: str = "",
    request_id: str = "",
    business_key: str = "",
    limit: int = 10,
) -> tuple[RequestChildView, ...]:
    normalized_limit = max(int(limit or 0), 0)
    filtered = filter_request_child_views(
        child_views,
        child_kind=child_kind,
        child_code=child_code,
        request_id=request_id,
        business_key=business_key,
        terminal_only=True,
    )
    if normalized_limit == 0:
        return ()
    return filtered[:normalized_limit]


def build_worker_payload_fragment(
    *,
    child_view: RequestChildView | None = None,
    child_summary: RequestChildSummary | None = None,
) -> dict[str, Any]:
    fragment: dict[str, Any] = {
        "child_summary": child_summary.to_dict() if child_summary is not None else {},
        "worker_item": {},
    }
    if child_view is None:
        return fragment
    fragment["worker_item"] = {
        "request_id": child_view.request_id,
        "child_id": child_view.child_id,
        "child_kind": child_view.child_kind,
        "runtime_table": child_view.runtime_table,
        "child_code": child_view.child_code,
        "status": child_view.status,
        "stage_code": child_view.stage_code,
        "business_key": child_view.business_key,
        "attempt_count": child_view.attempt_count,
        "max_attempts": child_view.max_attempts,
        "worker_id": child_view.worker_id,
        "run_id": child_view.run_id,
        "error": child_view.error_text,
    }
    return fragment


def build_executor_payload_fragment(
    *,
    request: RequestRecord | None = None,
    child_summary: RequestChildSummary | None = None,
    latest_child: RequestChildView | None = None,
    latest_terminal_child: RequestChildView | None = None,
) -> dict[str, Any]:
    fragment: dict[str, Any] = {
        "request": build_request_view_fragment(request),
        "child_summary": child_summary.to_dict() if child_summary is not None else {},
        "latest_child": latest_child.to_dict() if latest_child is not None else {},
        "latest_terminal_child": latest_terminal_child.to_dict() if latest_terminal_child is not None else {},
    }
    if child_summary is not None:
        fragment.update(child_summary.request_counter_fields())
    return fragment


def build_request_view_fragment(request: RequestRecord | None) -> dict[str, Any]:
    request_dict = _record_to_dict(request)
    if not request_dict:
        return {}
    return {
        "request_id": _coerce_str(request_dict.get("request_id")),
        "task_code": _coerce_str(request_dict.get("task_code")),
        "request_status": _coerce_str(request_dict.get("result_status") or request_dict.get("status")),
        "status": _coerce_str(request_dict.get("status")),
        "result_status": _coerce_str(request_dict.get("result_status")),
        "current_stage": _coerce_str(request_dict.get("current_stage")),
        "worker_id": _coerce_str(request_dict.get("worker_id")),
        "error": _coerce_str(request_dict.get("error_text")),
        "child_total_count": _coerce_int(request_dict.get("child_total_count")),
        "child_terminal_count": _coerce_int(request_dict.get("child_terminal_count")),
        "child_success_count": _coerce_int(request_dict.get("child_success_count")),
        "child_failed_count": _coerce_int(request_dict.get("child_failed_count")),
        "child_skipped_count": _coerce_int(request_dict.get("child_skipped_count")),
        "started_at": _coerce_float(request_dict.get("started_at")),
        "finished_at": _coerce_float(request_dict.get("finished_at")),
        "updated_at": _coerce_float(request_dict.get("updated_at")),
    }


def _build_api_worker_job_view(job: Mapping[str, Any]) -> RequestChildView:
    return RequestChildView(
        child_kind="api_worker_job",
        runtime_table="api_worker_job",
        child_id=_coerce_str(job.get("job_id")),
        request_id=_coerce_str(job.get("request_id")),
        task_code=_coerce_str(job.get("task_code")),
        child_code=_coerce_str(job.get("job_code")),
        status=_coerce_str(job.get("result_status") or job.get("status")),
        stage_code=_coerce_str(job.get("stage")),
        business_key=_coerce_str(job.get("business_key")),
        dedupe_key=_coerce_str(job.get("dedupe_key")),
        attempt_count=_coerce_int(job.get("attempt_count")),
        max_attempts=_coerce_int(job.get("max_attempts")),
        worker_id=_coerce_str(job.get("worker_id")),
        run_id=_coerce_str(job.get("run_id")),
        available_at=_coerce_float(job.get("available_at")),
        created_at=_coerce_float(job.get("created_at")),
        updated_at=_coerce_float(job.get("updated_at")),
        started_at=_coerce_float(job.get("started_at")),
        finished_at=_coerce_float(job.get("finished_at")),
        heartbeat_at=_coerce_float(job.get("heartbeat_at")),
        error_text=_coerce_str(job.get("error_text")),
        payload=_coerce_dict(job.get("payload")),
        summary=_coerce_dict(job.get("summary")),
        result=_coerce_dict(job.get("result")),
    )


def _build_task_execution_view(execution: ChildRecord) -> RequestChildView:
    execution_dict = _record_to_dict(execution)
    payload = _coerce_dict(execution_dict.get("payload"))
    return RequestChildView(
        child_kind="task_execution",
        runtime_table="task_execution",
        child_id=_coerce_str(execution_dict.get("execution_id")),
        request_id=_coerce_str(execution_dict.get("request_id")),
        task_code=_coerce_str(execution_dict.get("task_code") or payload.get("task_code")),
        workflow_code=_coerce_str(execution_dict.get("workflow_code")),
        child_code=_coerce_str(execution_dict.get("item_code")),
        status=_coerce_str(execution_dict.get("result_status") or execution_dict.get("status")),
        stage_code=_coerce_str(execution_dict.get("stage_code") or payload.get("stage_code")),
        business_key=_coerce_str(execution_dict.get("business_key")),
        dedupe_key=_coerce_str(execution_dict.get("dedupe_key")),
        resource_code=_coerce_str(execution_dict.get("resource_code")),
        attempt_count=_coerce_int(execution_dict.get("attempt_count")),
        max_attempts=_coerce_int(execution_dict.get("max_attempts")),
        worker_id=_coerce_str(execution_dict.get("worker_id")),
        run_id=_coerce_str(execution_dict.get("run_id")),
        available_at=_coerce_float(execution_dict.get("available_at")),
        created_at=_coerce_float(execution_dict.get("created_at")),
        updated_at=_coerce_float(execution_dict.get("updated_at")),
        started_at=_coerce_float(execution_dict.get("started_at")),
        finished_at=_coerce_float(execution_dict.get("finished_at")),
        heartbeat_at=_coerce_float(execution_dict.get("heartbeat_at")),
        error_text=_coerce_str(execution_dict.get("error_text")),
        payload=payload,
        summary=_coerce_dict(execution_dict.get("summary")),
        result=_coerce_dict(execution_dict.get("result")),
    )


def _child_sort_key(view: RequestChildView) -> tuple[float, float, str]:
    return (view.sort_timestamp, view.created_at, view.child_id)


def _has_fallback_signal(payload: Mapping[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("fallback_required") is True:
        return True
    status = str(payload.get("status") or "").strip()
    if status == "fallback_required":
        return True
    next_action = payload.get("next_action")
    if isinstance(next_action, Mapping):
        next_action_type = str(next_action.get("type") or "").strip()
        if "fallback" in next_action_type:
            return True
    error = payload.get("error")
    if isinstance(error, Mapping) and str(error.get("fallback_reason") or "").strip():
        return True
    return False


def _record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, Mapping):
        return dict(record)
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    if is_dataclass(record):
        payload = asdict(record)
        if isinstance(payload, dict):
            return payload
    raise TypeError(f"Unsupported runtime view record type: {type(record)!r}")


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _coerce_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


__all__ = [
    "ACTIVE_CHILD_STATUSES",
    "FAILED_CHILD_STATUSES",
    "SUCCESS_CHILD_STATUSES",
    "TERMINAL_CHILD_STATUSES",
    "RequestChildSummary",
    "RequestChildView",
    "aggregate_request_child_counts",
    "build_executor_payload_fragment",
    "build_request_child_views",
    "build_request_view_fragment",
    "build_worker_payload_fragment",
    "filter_request_child_views",
    "latest_request_child_view",
    "latest_terminal_child_views",
    "summarize_child_status_counts",
]
