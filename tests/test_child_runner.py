from __future__ import annotations

import time

from automation_business_scaffold.business.flows.child_runner import ChildRunner, ChildRunnerConfig
from automation_business_scaffold.business.handlers.contract import HandlerContext, HandlerResult


def _build_context() -> HandlerContext:
    return HandlerContext(
        request_id="req-child-1",
        job_id="job-child-1",
        handler_code="tiktok_product_request_fetch",
        worker_type="api_worker",
        runtime_table="api_worker_job",
        payload={"product_url": "https://www.tiktok.com/shop/pdp/123"},
        workflow_code="tiktok_fastmoss_product_ingest",
        stage_code="collect_product_data",
        job_code="tiktok_product_request_fetch",
        worker_id="pytest-child-runner",
    )


def _success_dispatch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("request_started", message="request path started")
        progress_callback("request_completed", details={"product_id": "123"})
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"product_id": "123", "source": "tiktok"},
    )


def _hanging_dispatch(context: HandlerContext) -> HandlerResult:
    time.sleep(0.3)
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"product_id": "123"},
    )


def test_child_runner_returns_handler_result_envelope() -> None:
    progress_events: list[str] = []
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_success_dispatch,
        on_progress=lambda event: progress_events.append(event.progress_stage),
    )

    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "returned"
    assert envelope.execution_mode == "child_process"
    assert envelope.timed_out is False
    assert result.status == "success"
    assert result.result["product_id"] == "123"
    assert progress_events == ["request_started", "request_completed"]
    assert envelope.to_dict()["details"]["start_method"]


def test_child_runner_returns_structured_timeout_error() -> None:
    runner = ChildRunner(
        ChildRunnerConfig(
            mode="child_process",
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            terminate_grace_seconds=0.05,
        )
    )

    envelope = runner.run(
        context=_build_context(),
        dispatch=_hanging_dispatch,
    )
    result = envelope.to_handler_result(_build_context())

    assert envelope.status == "timed_out"
    assert envelope.timed_out is True
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "timeout"
    assert result.error.error_code == "child_process_timeout"
    assert result.summary["child_runner_status"] == "timed_out"
