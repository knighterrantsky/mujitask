from __future__ import annotations

import multiprocessing
import sys
import time

import pytest

import automation_business_scaffold.control_plane.executor.runner as runtime_orchestrator
import automation_business_scaffold.control_plane.outbox.dispatcher as outbox_dispatcher
from automation_business_scaffold.control_plane.executor.looping import build_child_runner_config
from automation_business_scaffold.contracts.handler.api import (
    build_api_handler_registry,
    register_api_handler,
)
from automation_business_scaffold.contracts.handler.contract import (
    HandlerContext,
    HandlerResult,
)
from automation_business_scaffold.contracts.handler.outbox import (
    build_outbox_handler_registry,
    register_outbox_handler,
)
from automation_business_scaffold.domains.tiktok.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "allow_test_persistence_overrides": True,
        "execution_control_db_url": runtime_db_url,
        "fact_db_url": runtime_db_url,
        "execution_control_artifact_store_provider": "minio",
        "execution_control_artifact_bucket": "pytest-runtime-artifacts",
        "execution_control_minio_endpoint": "127.0.0.1:9000",
        "execution_control_minio_access_key": "minioadmin",
        "execution_control_minio_secret_key": "miniosecret",
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _fake_outbox_dispatch_with_progress(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("dispatching", message="dispatching workflow summary")
    return HandlerResult.success(
        context,
        summary={"delivery": "feishu"},
        result={"event_type": "task_request.completed"},
    )


def _hanging_tiktok_request_fetch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("request_started", message="request path started before timeout")
    time.sleep(0.3)
    return HandlerResult.success(
        context,
        summary={"transport": "request"},
        result={"normalized_product_result": {"product_id": "123"}},
    )


def _successful_fastmoss_product_fetch(context: HandlerContext) -> HandlerResult:
    progress_callback = context.metadata.get("progress_callback")
    if callable(progress_callback):
        progress_callback("fastmoss_product_fetch", message="fastmoss product fetched")
    return HandlerResult.success(
        context,
        summary={"transport": "fastmoss"},
        result={"product_fact_bundle": {"product_id": "123"}},
    )


def _bind_timeout_e2e_api_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_api_handler_registry()
    register_api_handler(registry, "tiktok_product_request_fetch", _hanging_tiktok_request_fetch)
    register_api_handler(registry, "fastmoss_product_fetch", _successful_fastmoss_product_fetch)
    monkeypatch.setattr(runtime_orchestrator, "build_api_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "API_HANDLER_REGISTRY", registry, raising=False)


def test_outbox_dispatcher_runs_through_execution_supervisor(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = TikTokFastMossProductIngestTask()
    submitted = task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            product_url="https://www.tiktok.com/shop/pdp/123",
            product_id="123",
            source_channel_code="feishu",
            reply_target="feishu://reply-target",
        )
    )
    request_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="success",
        current_stage="completed",
        summary={"final_status": "success"},
        result={"normalized_product_result": {"product_id": "123"}},
    )
    outbox_dispatcher.ensure_request_outbox(store=store, request_id=request_id)

    registry = build_outbox_handler_registry()

    register_outbox_handler(registry, "outbox_dispatch", _fake_outbox_dispatch_with_progress)
    monkeypatch.setattr(outbox_dispatcher, "OUTBOX_HANDLER_REGISTRY", registry, raising=False)

    payload = outbox_dispatcher.dispatch_outbox_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["channel_code"] == "feishu"
    assert payload["item"]["status"] == "sent"
    assert payload["supervisor"]["worker_type"] == "outbox_dispatcher"
    assert payload["supervisor"]["progress_stage"] == "dispatching"


def test_outbox_dispatcher_can_use_child_process_runner(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS forbids fork child runner; child_process remains explicit experimental mode.")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Runtime child-process registry monkeypatch e2e requires fork start method.")
    task = TikTokFastMossProductIngestTask()
    submitted = task.run_runtime_request(
        _runtime_params(
            runtime_db_url,
            control_action="submit",
            product_url="https://www.tiktok.com/shop/pdp/123",
            product_id="123",
            source_channel_code="feishu",
            reply_target="feishu://reply-target",
        )
    )
    request_id = str(submitted["request_id"])
    store = RuntimeStore(db_url=runtime_db_url)
    store.update_task_request(
        request_id=request_id,
        status="success",
        current_stage="completed",
        summary={"final_status": "success"},
        result={"normalized_product_result": {"product_id": "123"}},
    )
    outbox_dispatcher.ensure_request_outbox(store=store, request_id=request_id)

    registry = build_outbox_handler_registry()
    register_outbox_handler(registry, "outbox_dispatch", _fake_outbox_dispatch_with_progress)
    monkeypatch.setattr(outbox_dispatcher, "OUTBOX_HANDLER_REGISTRY", registry, raising=False)

    payload = outbox_dispatcher.dispatch_outbox_once(
        _runtime_params(
            runtime_db_url,
            execution_child_runner_mode="child_process",
            execution_child_start_method="fork",
            execution_child_timeout_seconds=1.0,
        )
    )

    assert payload["request_id"] == request_id
    assert payload["item"]["status"] == "sent"
    assert payload["supervisor"]["execution_mode"] == "child_process"
    assert payload["supervisor"]["progress_stage"] == "dispatching"


def test_runtime_workers_default_to_inline_supervision() -> None:
    api_config = build_child_runner_config(
        {},
        worker_type="api_worker",
        handler_code="fastmoss_product_fetch",
        runtime_timeout_seconds=240.0,
    )
    browser_config = build_child_runner_config(
        {},
        worker_type="browser_worker",
        handler_code="tiktok_product_browser_fetch",
        runtime_timeout_seconds=600.0,
    )
    outbox_config = build_child_runner_config(
        {},
        worker_type="outbox_dispatcher",
        handler_code="outbox_dispatch",
    )

    assert api_config is None
    assert browser_config is None
    assert outbox_config is None


def test_runtime_child_process_policy_allows_explicit_inline_override() -> None:
    config = build_child_runner_config(
        {"execution_child_runner_mode": "inline"},
        worker_type="browser_worker",
        handler_code="tiktok_product_browser_fetch",
        runtime_timeout_seconds=600.0,
    )

    assert config is None


def test_runtime_child_process_policy_allows_explicit_timeout_override() -> None:
    config = build_child_runner_config(
        {
            "execution_child_runner_mode": "child_process",
            "execution_child_timeout_seconds": 1.5,
        },
        worker_type="api_worker",
        handler_code="fastmoss_product_fetch",
        runtime_timeout_seconds=240.0,
    )

    assert config is not None
    assert config.mode == "child_process"
    assert config.timeout_seconds == 1.5


def test_child_timeout_retries_then_failed_child_releases_parent_for_executor_convergence(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("macOS runtime defaults to inline and forbids fork child runner.")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Runtime child-process registry monkeypatch e2e requires fork start method.")

    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="tiktok_fastmoss_product_ingest",
        payload={
            "product_url": "https://www.tiktok.com/shop/pdp/123",
            "product_id": "123",
            "fallback_allowed": True,
        },
        requested_by="pytest",
        source_channel_code="noop",
        reply_target="reply://timeout-e2e",
    )
    store.update_task_request(
        request_id=request.request_id,
        status="waiting_children",
        current_stage="collect_product_data",
    )
    common_payload = {
        "request_id": request.request_id,
        "task_code": "tiktok_fastmoss_product_ingest",
        "workflow_code": "tiktok_fastmoss_product_ingest",
        "stage_code": "collect_product_data",
        "product_id": "123",
        "normalized_product_url": "https://www.tiktok.com/shop/pdp/123",
        "product_identity": {
            "product_id": "123",
            "product_url": "https://www.tiktok.com/shop/pdp/123",
            "normalized_product_url": "https://www.tiktok.com/shop/pdp/123",
            "business_key": "123",
        },
    }
    enqueue_tiktok = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="tiktok_product_request_fetch",
        jobs=[
            {
                "business_key": "123",
                "dedupe_key": f"{request.request_id}:timeout:tiktok_request",
                "max_attempts": 2,
                "max_execution_seconds": 0.05,
                "payload": dict(common_payload),
            }
        ],
    )
    enqueue_fastmoss = store.enqueue_api_worker_jobs(
        request_id=request.request_id,
        task_code="tiktok_fastmoss_product_ingest",
        job_code="fastmoss_product_fetch",
        jobs=[
            {
                "business_key": "123",
                "dedupe_key": f"{request.request_id}:timeout:fastmoss_product",
                "max_attempts": 1,
                "max_execution_seconds": 1.0,
                "payload": dict(common_payload),
            }
        ],
    )
    tiktok_job_id = str(enqueue_tiktok["created_records"][0]["job_id"])
    fastmoss_job_id = str(enqueue_fastmoss["created_records"][0]["job_id"])
    _bind_timeout_e2e_api_handlers(monkeypatch)
    worker_params = _runtime_params(
        runtime_db_url,
        execution_child_runner_mode="child_process",
        execution_child_start_method="fork",
        execution_child_poll_interval_seconds=0.005,
        execution_child_terminate_grace_seconds=0.02,
        execution_retry_delay_seconds=0.1,
    )

    first_timeout = runtime_orchestrator.execute_api_worker_once(worker_params)

    first_tiktok = store.load_api_worker_job(job_id=tiktok_job_id)
    parent_after_retry = store.load_task_request(request_id=request.request_id)
    assert first_timeout["api_worker_job"]["job_id"] == tiktok_job_id
    assert first_timeout["api_worker_job"]["status"] == "retry_wait"
    assert first_timeout["failed_count"] == 0
    assert first_timeout["parent_updates"] == []
    assert first_timeout["supervisor"]["execution_mode"] == "child_process"
    assert first_timeout["supervisor"]["supervisor_status"] == "timed_out"
    assert first_timeout["supervisor"]["error"]["error_code"] == "child_process_timeout"
    assert first_tiktok["attempt_count"] == 1
    assert first_tiktok["error_type"] == "timeout"
    assert first_tiktok["error_code"] == "child_process_timeout"
    assert parent_after_retry.status == "waiting_children"
    assert parent_after_retry.current_stage == "collect_product_data"

    fastmoss_success = runtime_orchestrator.execute_api_worker_once(worker_params)

    assert fastmoss_success["api_worker_job"]["job_id"] == fastmoss_job_id
    assert fastmoss_success["api_worker_job"]["status"] == "success"
    assert store.load_task_request(request_id=request.request_id).status == "waiting_children"

    time.sleep(0.12)
    final_timeout = runtime_orchestrator.execute_api_worker_once(worker_params)

    failed_tiktok = store.load_api_worker_job(job_id=tiktok_job_id)
    released_parent = store.load_task_request(request_id=request.request_id)
    assert final_timeout["api_worker_job"]["job_id"] == tiktok_job_id
    assert final_timeout["api_worker_job"]["status"] == "failed"
    assert final_timeout["failed_count"] == 1
    assert final_timeout["parent_updates"] == [
        {
            "request_id": request.request_id,
            "stage_code": "collect_product_data",
            "released": True,
            "next_executor_status": "pending",
        }
    ]
    assert failed_tiktok["attempt_count"] == 2
    assert failed_tiktok["dead_letter_reason"] == "max_attempts_exhausted"
    assert released_parent.status == "pending"
    assert released_parent.current_stage == "collect_product_data"
    assert released_parent.child_total_count == 2
    assert released_parent.child_terminal_count == 2
    assert released_parent.child_success_count == 1
    assert released_parent.child_failed_count == 1

    converged = runtime_orchestrator.execute_executor_once(_runtime_params(runtime_db_url))

    assert converged["request_id"] == request.request_id
    assert converged["request_status"] == "failed"
    assert converged["current_stage"] == "completed"
    assert converged["final_status"] == "failed"
    assert converged["result"]["message"] == "TikTok request-first collection failed."
    assert converged["outbox"], "failed request should still create a completion outbox"
