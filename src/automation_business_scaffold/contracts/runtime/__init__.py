"""Runtime control contract facade."""

from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionProgressEvent,
    ExecutionSupervisorCallbacks,
    ExecutionSupervisorError,
    ExecutionSupervisorOutcome,
    FailureDisposition,
    run_supervised_handler,
)
from automation_business_scaffold.business.flows.runtime_common import (
    FORMAL_TASK_CODES,
    RuntimeExecutionSettings,
    build_runtime_settings,
    normalize_control_action,
)
from automation_business_scaffold.control_plane.reconciler.views import (
    ACTIVE_CHILD_STATUSES,
    FAILED_CHILD_STATUSES,
    SUCCESS_CHILD_STATUSES,
    TERMINAL_CHILD_STATUSES,
    ChildKind,
    RequestChildSummary,
    RequestChildView,
)

__all__ = [
    "ACTIVE_CHILD_STATUSES",
    "FAILED_CHILD_STATUSES",
    "FORMAL_TASK_CODES",
    "SUCCESS_CHILD_STATUSES",
    "TERMINAL_CHILD_STATUSES",
    "ChildKind",
    "ExecutionProgressEvent",
    "ExecutionSupervisorCallbacks",
    "ExecutionSupervisorError",
    "ExecutionSupervisorOutcome",
    "FailureDisposition",
    "RequestChildSummary",
    "RequestChildView",
    "RuntimeExecutionSettings",
    "build_runtime_settings",
    "normalize_control_action",
    "run_supervised_handler",
]
