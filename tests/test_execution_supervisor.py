from __future__ import annotations

import time
import sys

import pytest
from sqlalchemy.exc import OperationalError

from automation_business_scaffold.control_plane.supervisor.child_runner import ChildRunnerConfig
from automation_business_scaffold.control_plane.supervisor.execution_supervisor import (
    ExecutionSupervisorCallbacks,
    run_supervised_handler,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext, HandlerError, HandlerResult
from automation_business_scaffold.contracts.handler.registry import HandlerInvocationContractError


def _build_context() -> HandlerContext:
    return HandlerContext(
        request_id="req-1",
        job_id="job-1",
        handler_code="fastmoss_product_fetch",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload={"search_query": "desk lamp"},
        workflow_code="search_keyword_competitor_products",
        stage_code="collect_product_data",
        job_code="fastmoss_product_fetch",
        worker_id="pytest-worker",
    )


def test_execution_supervisor_records_progress_and_heartbeats() -> None:
    recorded_progress: list[dict[str, object]] = []
    heartbeats: list[float] = []

    def dispatch(context: HandlerContext) -> HandlerResult:
        progress_callback = context.metadata["progress_callback"]
        progress_callback("request_started", message="request-first path started")
        time.sleep(0.045)
        progress_callback("result_normalized", details={"candidate_count": 3})
        return HandlerResult.success(
            context,
            summary={"transport": "request"},
            result={"candidates": [{"product_id": "p-1"}]},
        )

    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=dispatch,
        heartbeat_interval_seconds=0.01,
        callbacks=ExecutionSupervisorCallbacks(
            heartbeat=lambda: heartbeats.append(time.time()),
            on_progress=lambda event: recorded_progress.append(event.to_dict()),
        ),
    )

    assert outcome.worker_result.status == "success"
    assert outcome.progress_stage == "result_normalized"
    assert len(recorded_progress) == 2
    assert len(heartbeats) >= 1
    assert outcome.storage_summary()["progress_stage"] == "result_normalized"
    assert outcome.to_dict()["failure_disposition"] == "none"


def test_execution_supervisor_classifies_contract_errors_as_terminal() -> None:
    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=lambda context: (_ for _ in ()).throw(HandlerInvocationContractError("invalid handler output")),
        heartbeat_interval_seconds=0.01,
    )

    assert outcome.worker_result.status == "failed"
    assert outcome.error is not None
    assert outcome.error.error_type == "contract"
    assert outcome.error.error_code == "handler_contract_error"
    assert outcome.error.retryable is False
    assert outcome.error.terminal is True
    assert outcome.failure_disposition == "terminal"


def test_execution_supervisor_classifies_db_connection_errors_as_retryable_infra() -> None:
    def dispatch(context: HandlerContext) -> HandlerResult:
        del context
        raise OperationalError("select 1", {}, Exception("FATAL: sorry, too many clients already"))

    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=dispatch,
        heartbeat_interval_seconds=0.01,
    )

    assert outcome.worker_result.status == "failed"
    assert outcome.error is not None
    assert outcome.error.error_type == "infrastructure"
    assert outcome.error.error_code == "runtime_db_connection_error"
    assert outcome.error.retryable is True
    assert outcome.error.terminal is False
    assert outcome.failure_disposition == "retryable"


def test_execution_supervisor_preserves_retryable_handler_errors() -> None:
    def dispatch(context: HandlerContext) -> HandlerResult:
        return HandlerResult.failed(
            context,
            error=HandlerError(
                error_type="transport",
                error_code="fastmoss_rate_limited",
                message="FastMoss rate limit",
                retryable=True,
                details={"retry_after_seconds": 5},
            ),
            summary={"transport": "fastmoss"},
        )

    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=dispatch,
        heartbeat_interval_seconds=0.01,
    )

    assert outcome.worker_result.status == "failed"
    assert outcome.error is not None
    assert outcome.error.error_type == "transport"
    assert outcome.error.error_code == "fastmoss_rate_limited"
    assert outcome.error.retryable is True
    assert outcome.error.terminal is False
    assert outcome.failure_disposition == "retryable"


def _child_success_dispatch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata["progress_callback"]
    progress_callback("request_started", message="child request path started")
    progress_callback("result_normalized", details={"candidate_count": 2})
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"candidates": [{"product_id": "p-1"}, {"product_id": "p-2"}]},
    )


def _child_hanging_dispatch(context: HandlerContext) -> HandlerResult:
    time.sleep(0.3)
    return HandlerResult.success(context, summary={"transport": "request"})


def test_execution_supervisor_can_run_handler_in_child_process() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS forbids fork child runner.")
    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=_child_success_dispatch,
        heartbeat_interval_seconds=0.01,
        child_runner_config=ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=1.0,
            start_method="fork",
            poll_interval_seconds=0.01,
        ),
    )

    assert outcome.worker_result.status == "success"
    assert outcome.execution_mode == "child_process"
    assert outcome.child_runner is not None
    assert outcome.child_runner.status == "returned"
    assert outcome.progress_stage == "result_normalized"
    assert outcome.storage_result()["child_runner"]["status"] == "returned"
    assert outcome.to_dict()["execution_mode"] == "child_process"


def test_execution_supervisor_returns_timeout_outcome_from_child_runner() -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS forbids fork child runner.")
    outcome = run_supervised_handler(
        context=_build_context(),
        dispatch=_child_hanging_dispatch,
        heartbeat_interval_seconds=0.01,
        child_runner_config=ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=0.05,
            start_method="fork",
            poll_interval_seconds=0.01,
            terminate_grace_seconds=0.05,
        ),
    )

    assert outcome.worker_result.status == "failed"
    assert outcome.supervisor_status == "timed_out"
    assert outcome.execution_mode == "child_process"
    assert outcome.child_runner is not None
    assert outcome.child_runner.timed_out is True
    assert outcome.error is not None
    assert outcome.error.error_type == "timeout"
    assert outcome.error.error_code == "child_process_timeout"
    assert outcome.failure_disposition == "retryable"
