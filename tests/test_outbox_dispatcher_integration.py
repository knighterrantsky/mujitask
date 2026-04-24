from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

import pytest
import requests

from automation_business_scaffold.business.flows import runtime_orchestrator
from automation_business_scaffold.business.handlers import HandlerContext
from automation_business_scaffold.business.handlers.outbox import (
    implementations as outbox_implementations,
)
from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore


@dataclass
class _FakeOutboxRecord:
    outbox_id: str
    channel_code: str
    payload: dict[str, Any]
    ref_id: str = "req-fake"
    ref_type: str = "task_request"
    event_type: str = "task_request.completed"
    reply_target: str = "reply://pytest"
    status: str = "pending"
    progress_stage: str = "queued"
    retry_count: int = 0
    max_retry_count: int = 3
    next_retry_at: float = 0.0
    worker_id: str = ""
    lease_until: float = 0.0
    heartbeat_at: float = 0.0
    last_error_text: str = ""
    error_type: str = ""
    error_code: str = ""
    dead_letter_reason: str = ""
    sent_at: float = 0.0
    last_progress_at: float = 0.0
    max_execution_seconds: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "channel_code": self.channel_code,
            "event_type": self.event_type,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "reply_target": self.reply_target,
            "payload": dict(self.payload),
            "status": self.status,
            "progress_stage": self.progress_stage,
            "retry_count": self.retry_count,
            "max_retry_count": self.max_retry_count,
            "next_retry_at": self.next_retry_at,
            "worker_id": self.worker_id,
            "lease_until": self.lease_until,
            "heartbeat_at": self.heartbeat_at,
            "last_error_text": self.last_error_text,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "dead_letter_reason": self.dead_letter_reason,
            "sent_at": self.sent_at,
            "last_progress_at": self.last_progress_at,
            "max_execution_seconds": self.max_execution_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class _FakeOutboxStore:
    outbox: _FakeOutboxRecord | None
    progress_updates: list[str] = field(default_factory=list)
    heartbeat_count: int = 0
    mark_sent_count: int = 0
    retryable_values: list[bool] = field(default_factory=list)

    def claim_next_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> _FakeOutboxRecord | None:
        del lease_seconds
        if self.outbox is None:
            return None
        self.outbox.status = "sending"
        self.outbox.progress_stage = "sending"
        self.outbox.worker_id = worker_id
        return self.outbox

    def heartbeat_outbox(self, *, outbox_id: str, lease_seconds: float) -> None:
        del outbox_id, lease_seconds
        self.heartbeat_count += 1

    def update_outbox_progress(
        self,
        *,
        outbox_id: str,
        progress_stage: str,
        lease_seconds: float | None = None,
    ) -> _FakeOutboxRecord:
        del outbox_id, lease_seconds
        if self.outbox is None:
            raise AssertionError("fake outbox is missing")
        self.progress_updates.append(progress_stage)
        self.outbox.progress_stage = progress_stage
        return self.outbox

    def mark_outbox_sent(self, *, outbox_id: str) -> _FakeOutboxRecord:
        del outbox_id
        if self.outbox is None:
            raise AssertionError("fake outbox is missing")
        self.mark_sent_count += 1
        self.outbox.status = "sent"
        self.outbox.progress_stage = "sent"
        self.outbox.worker_id = ""
        self.outbox.last_error_text = ""
        self.outbox.error_type = ""
        self.outbox.error_code = ""
        self.outbox.dead_letter_reason = ""
        return self.outbox

    def mark_outbox_retry_or_failed(
        self,
        *,
        outbox_id: str,
        error_text: str,
        retry_delay_seconds: float = 30.0,
        retryable: bool = True,
        error_type: str = "",
        error_code: str = "",
        dead_letter_reason: str = "",
    ) -> _FakeOutboxRecord:
        del outbox_id, retry_delay_seconds
        if self.outbox is None:
            raise AssertionError("fake outbox is missing")
        self.retryable_values.append(retryable)
        self.outbox.retry_count += 1
        self.outbox.status = (
            "retry_wait"
            if retryable and self.outbox.retry_count < self.outbox.max_retry_count
            else "failed"
        )
        self.outbox.progress_stage = self.outbox.status
        self.outbox.next_retry_at = time.time() if self.outbox.status == "retry_wait" else 0.0
        self.outbox.last_error_text = error_text
        self.outbox.error_type = error_type
        self.outbox.error_code = error_code
        self.outbox.dead_letter_reason = dead_letter_reason or (
            "max_retry_exhausted" if retryable and self.outbox.status == "failed" else ""
        )
        return self.outbox


def _runtime_params(runtime_db_url: str, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "execution_control_db_url": runtime_db_url,
        "execution_control_stop_when_idle": True,
        "execution_control_max_iterations": 1,
        "execution_retry_delay_seconds": 0.1,
        "requested_by": "pytest",
    }
    params.update(overrides)
    return params


def _bind_fake_store(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeOutboxStore,
) -> None:
    monkeypatch.setattr(
        runtime_orchestrator,
        "create_runtime_store",
        lambda settings: store,
    )


def _create_outbox(
    store: RuntimeStore,
    *,
    channel_code: str,
    payload: dict[str, Any],
    dedupe_key: str,
    ref_id: str = "req-outbox",
) -> str:
    outbox = store.create_notification_outbox(
        channel_code=channel_code,
        event_type="task_request.completed",
        ref_id=ref_id,
        reply_target="reply://pytest",
        payload=payload,
        dedupe_key=dedupe_key,
    )
    return outbox.outbox_id


def _handler_context(
    *,
    payload: dict[str, Any],
    channel_code: str,
    progress_events: list[dict[str, Any]],
) -> HandlerContext:
    def _progress_callback(
        progress_stage: str,
        *,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        progress_events.append(
            {"progress_stage": progress_stage, "message": message, "details": details or {}}
        )

    return HandlerContext(
        request_id="req-handler",
        job_id="outbox-handler",
        handler_code="outbox_dispatch",
        worker_type="outbox_dispatcher",
        runtime_table="notification_outbox",
        payload=payload,
        job_code="outbox_dispatch",
        metadata={
            "channel_code": channel_code,
            "reply_target": "reply://pytest",
            "progress_callback": _progress_callback,
        },
    )


def _expire_outbox_claim(store: RuntimeStore, *, outbox_id: str) -> None:
    expired_at = time.time() - 5.0
    with store._engine.begin() as connection:  # noqa: SLF001
        connection.execute(
            store._text(  # noqa: SLF001
                """
                UPDATE notification_outbox
                SET lease_until = :lease_until,
                    heartbeat_at = :heartbeat_at
                WHERE outbox_id = :outbox_id
                """
            ),
            {
                "outbox_id": outbox_id,
                "lease_until": expired_at,
                "heartbeat_at": expired_at,
            },
        )


def test_outbox_handler_renders_message_text_and_reports_progress_without_db() -> None:
    progress_events: list[dict[str, Any]] = []
    context = _handler_context(
        payload={"message_text": "hello console", "dry_run": True},
        channel_code="console",
        progress_events=progress_events,
    )

    result = outbox_implementations.outbox_dispatch_handler(context)

    assert result.status == "success"
    assert result.result["message"] == "hello console"
    assert result.result["delivery_state"] == "simulated"
    assert [event["progress_stage"] for event in progress_events] == [
        "dispatching",
        "dispatch_simulated",
    ]


def test_outbox_handler_classifies_webhook_http_4xx_as_terminal_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_events: list[dict[str, Any]] = []

    class _Response:
        status_code = 400

        def raise_for_status(self) -> None:
            raise requests.HTTPError("400 Client Error", response=self)

    def _fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        return _Response()

    monkeypatch.setattr(outbox_implementations.requests, "post", _fake_post)
    context = _handler_context(
        payload={
            "message_text": "bad webhook",
            "webhook_url": "https://example.test/hook",
            "dry_run": False,
        },
        channel_code="webhook",
        progress_events=progress_events,
    )

    result = outbox_implementations.outbox_dispatch_handler(context)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.retryable is False
    assert result.error.details["status_code"] == 400
    assert progress_events[-1]["progress_stage"] == "dispatch_terminal_failure"


def test_dispatch_outbox_once_supervises_console_success_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeOutboxStore(
        outbox=_FakeOutboxRecord(
            outbox_id="outbox-console",
            channel_code="console",
            payload={"message_text": "hello supervised console", "dry_run": True},
        )
    )
    _bind_fake_store(monkeypatch, store)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params("postgresql://unused"))

    assert payload["outbox_id"] == "outbox-console"
    assert payload["item"]["status"] == "sent"
    assert payload["worker_result"]["status"] == "success"
    assert payload["worker_result"]["result"]["delivery_state"] == "simulated"
    assert payload["supervisor"]["worker_type"] == "outbox_dispatcher"
    assert payload["supervisor"]["progress_stage"] == "dispatch_simulated"
    assert store.progress_updates == ["dispatching", "dispatch_simulated"]
    assert store.mark_sent_count == 1


def test_dispatch_outbox_once_schedules_retryable_failure_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_post(url: str, *, json: dict[str, Any], timeout: float) -> None:
        raise requests.Timeout("webhook timed out")

    monkeypatch.setattr(outbox_implementations.requests, "post", _fake_post)
    store = _FakeOutboxStore(
        outbox=_FakeOutboxRecord(
            outbox_id="outbox-retry",
            channel_code="webhook",
            payload={
                "message_text": "retryable webhook",
                "webhook_url": "https://example.test/hook",
                "dry_run": False,
            },
        )
    )
    _bind_fake_store(monkeypatch, store)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params("postgresql://unused"))

    assert payload["outbox_id"] == "outbox-retry"
    assert payload["item"]["status"] == "retry_wait"
    assert payload["retry_scheduled_count"] == 1
    assert payload["failed_count"] == 0
    assert payload["supervisor"]["failure_disposition"] == "retryable"
    assert payload["supervisor"]["progress_stage"] == "dispatch_retryable_failure"
    assert store.retryable_values == [True]


def test_dispatch_outbox_once_marks_terminal_failure_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeOutboxStore(
        outbox=_FakeOutboxRecord(
            outbox_id="outbox-terminal",
            channel_code="webhook",
            payload={"message_text": "missing webhook url", "dry_run": False},
        )
    )
    _bind_fake_store(monkeypatch, store)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params("postgresql://unused"))

    assert payload["outbox_id"] == "outbox-terminal"
    assert payload["item"]["status"] == "failed"
    assert payload["retry_scheduled_count"] == 0
    assert payload["failed_count"] == 1
    assert payload["error_code"] == "outbox_webhook_missing_url"
    assert payload["supervisor"]["failure_disposition"] == "terminal"
    assert store.retryable_values == [False]


def test_outbox_dispatcher_claims_and_supervises_noop_from_request_finalize(
    runtime_db_url: str,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(
        project_code="automation-business-scaffold",
        task_code="tiktok_fastmoss_product_ingest",
        payload={"product_id": "123"},
        requested_by="pytest",
        source_channel_code="noop",
        reply_target="reply://pytest",
    )
    store.update_task_request(
        request_id=request.request_id,
        status="success",
        current_stage="completed",
        summary={"final_status": "success"},
        result={"normalized_product_result": {"product_id": "123"}},
    )
    runtime_orchestrator.ensure_request_outbox(store=store, request_id=request.request_id)
    outbox = store.list_request_outbox(request_id=request.request_id)[0]

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    assert payload["request_id"] == request.request_id
    assert payload["outbox_id"] == outbox.outbox_id
    assert payload["item"]["status"] == "sent"
    assert payload["worker_result"]["status"] == "success"
    assert payload["worker_result"]["result"]["delivery_state"] == "skipped"
    assert payload["worker_result"]["result"]["message"] == outbox.payload["message_text"]
    assert payload["supervisor"]["worker_type"] == "outbox_dispatcher"
    assert payload["supervisor"]["progress_stage"] == "dispatch_skipped"


def test_outbox_dispatcher_marks_webhook_success_sent(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    posted_payloads: list[dict[str, Any]] = []

    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    def _fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        posted_payloads.append({"url": url, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(outbox_implementations.requests, "post", _fake_post)
    outbox_id = _create_outbox(
        store,
        channel_code="webhook",
        payload={
            "message_text": "hello webhook",
            "webhook_url": "https://example.test/hook",
            "dry_run": False,
        },
        dedupe_key="outbox:webhook-success",
    )

    payload = runtime_orchestrator.dispatch_outbox_once(
        _runtime_params(runtime_db_url, execution_child_runner_mode="inline")
    )

    assert payload["outbox_id"] == outbox_id
    assert payload["item"]["status"] == "sent"
    assert payload["worker_result"]["result"]["status_code"] == 204
    assert payload["supervisor"]["progress_stage"] == "dispatch_sent"
    assert posted_payloads[0]["json"]["message"] == "hello webhook"


def test_outbox_dispatcher_schedules_retry_for_retryable_webhook_failure(
    runtime_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)

    def _fake_post(url: str, *, json: dict[str, Any], timeout: float) -> None:
        raise requests.Timeout("webhook timed out")

    monkeypatch.setattr(outbox_implementations.requests, "post", _fake_post)
    outbox_id = _create_outbox(
        store,
        channel_code="webhook",
        payload={
            "message_text": "retry me",
            "webhook_url": "https://example.test/hook",
            "dry_run": False,
        },
        dedupe_key="outbox:webhook-retry",
    )

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    item = payload["item"]
    assert payload["outbox_id"] == outbox_id
    assert item["status"] == "retry_wait"
    assert item["retry_count"] == 1
    assert item["next_retry_at"] > 0
    assert payload["failed_count"] == 0
    assert payload["retry_scheduled_count"] == 1
    assert payload["supervisor"]["failure_disposition"] == "retryable"
    assert payload["supervisor"]["progress_stage"] == "dispatch_retryable_failure"


def test_outbox_dispatcher_terminal_failure_does_not_retry(runtime_db_url: str) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    outbox_id = _create_outbox(
        store,
        channel_code="webhook",
        payload={"message_text": "missing url", "dry_run": False},
        dedupe_key="outbox:webhook-terminal",
    )

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    item = payload["item"]
    assert payload["outbox_id"] == outbox_id
    assert item["status"] == "failed"
    assert item["retry_count"] == 1
    assert item["next_retry_at"] == 0.0
    assert item["dead_letter_reason"] == "supervisor_failed"
    assert payload["failed_count"] == 1
    assert payload["retry_scheduled_count"] == 0
    assert payload["supervisor"]["failure_disposition"] == "terminal"
    assert payload["error_code"] == "outbox_webhook_missing_url"


def test_outbox_dispatcher_reclaims_expired_sending_lease_before_dispatch(
    runtime_db_url: str,
) -> None:
    store = RuntimeStore(db_url=runtime_db_url)
    outbox_id = _create_outbox(
        store,
        channel_code="console",
        payload={"message_text": "lease reclaim", "dry_run": True},
        dedupe_key="outbox:lease-reclaim",
    )
    claimed = store.claim_next_outbox(worker_id="dispatcher-a", lease_seconds=30.0)
    assert claimed is not None
    assert claimed.outbox_id == outbox_id
    _expire_outbox_claim(store, outbox_id=outbox_id)

    payload = runtime_orchestrator.dispatch_outbox_once(_runtime_params(runtime_db_url))

    item = payload["item"]
    assert payload["outbox_id"] == outbox_id
    assert item["status"] == "sent"
    assert item["retry_count"] == 1
    assert item["last_error_text"] == ""
    assert payload["worker_result"]["result"]["delivery_state"] == "simulated"
    assert payload["supervisor"]["progress_stage"] == "dispatch_simulated"
