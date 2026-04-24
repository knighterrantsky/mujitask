from __future__ import annotations

import pytest

from automation_business_scaffold.business.flows import runtime_orchestrator
from automation_business_scaffold.business.handlers import (
    HandlerContext,
    HandlerResult,
    build_outbox_handler_registry,
    register_outbox_handler,
)
from automation_business_scaffold.business.tasks.tiktok_fastmoss_product_ingest import (
    TikTokFastMossProductIngestTask,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
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
    runtime_orchestrator.ensure_request_outbox(store=store, request_id=request_id)

    registry = build_outbox_handler_registry()

    register_outbox_handler(registry, "outbox_dispatch", _fake_outbox_dispatch_with_progress)
    monkeypatch.setattr(runtime_orchestrator, "build_outbox_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "OUTBOX_HANDLER_REGISTRY", registry, raising=False)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request_id
    assert payload["channel_code"] == "feishu"
    assert payload["item"]["status"] == "sent"
    assert payload["supervisor"]["worker_type"] == "outbox_dispatcher"
    assert payload["supervisor"]["progress_stage"] == "dispatching"


def test_outbox_dispatcher_can_use_child_process_runner(
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
    runtime_orchestrator.ensure_request_outbox(store=store, request_id=request_id)

    registry = build_outbox_handler_registry()
    register_outbox_handler(registry, "outbox_dispatch", _fake_outbox_dispatch_with_progress)
    monkeypatch.setattr(runtime_orchestrator, "build_outbox_handler_registry", lambda: registry, raising=False)
    monkeypatch.setattr(runtime_orchestrator, "OUTBOX_HANDLER_REGISTRY", registry, raising=False)

    payload = runtime_orchestrator.dispatch_outbox_once(
        _runtime_params(
            runtime_db_url,
            execution_child_runner_mode="child_process",
            execution_child_timeout_seconds=1.0,
        )
    )

    assert payload["request_id"] == request_id
    assert payload["item"]["status"] == "sent"
    assert payload["supervisor"]["execution_mode"] == "child_process"
    assert payload["supervisor"]["progress_stage"] == "dispatching"


def test_runtime_workers_default_to_child_process_with_record_timeout() -> None:
    api_config = runtime_orchestrator._build_child_runner_config(
        {},
        worker_type="api_worker",
        handler_code="fastmoss_product_fetch",
        runtime_timeout_seconds=240.0,
    )
    browser_config = runtime_orchestrator._build_child_runner_config(
        {},
        worker_type="browser_worker",
        handler_code="tiktok_product_browser_fetch",
        runtime_timeout_seconds=600.0,
    )
    outbox_config = runtime_orchestrator._build_child_runner_config(
        {},
        worker_type="outbox_dispatcher",
        handler_code="outbox_dispatch",
    )

    assert api_config is not None
    assert api_config.mode == "child_process"
    assert api_config.timeout_seconds == 240.0
    assert browser_config is not None
    assert browser_config.mode == "child_process"
    assert browser_config.timeout_seconds == 600.0
    assert outbox_config is not None
    assert outbox_config.mode == "child_process"
    assert outbox_config.timeout_seconds == 60.0


def test_runtime_child_process_policy_allows_explicit_inline_override() -> None:
    config = runtime_orchestrator._build_child_runner_config(
        {"execution_child_runner_mode": "inline"},
        worker_type="browser_worker",
        handler_code="tiktok_product_browser_fetch",
        runtime_timeout_seconds=600.0,
    )

    assert config is None


def test_runtime_child_process_policy_allows_explicit_timeout_override() -> None:
    config = runtime_orchestrator._build_child_runner_config(
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
