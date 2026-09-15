from contextlib import contextmanager
from itertools import count
from types import SimpleNamespace

import pytest

from automation_business_scaffold.capabilities.browser import fastmoss_security_resolve_handler as handler
from automation_business_scaffold.capabilities.browser.fastmoss_security.request_verification import (
    browser_verification_failure_stage,
    observe_browser_verification,
)
from automation_business_scaffold.contracts.handler.contract import HandlerContext


class Page:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) == callback

    def response(self, path, code, *, method="GET", host="www.fastmoss.com", status=200, accepted=True):
        request = SimpleNamespace(
            url=f"https://{host}{path}", method=method,
            response=lambda: SimpleNamespace(status=status, json=lambda: {
                "code": code, "data": {"ticket": "must-not-leak", "is_ok": accepted}, "ext": {"is_login": 1},
            }),
        )
        self.listeners["requestfinished"](request)


REQUEST = {"path": "/api/author/v3/detail/baseInfo", "params": {"uid": "123"}, "method": "GET"}


def test_observer_requires_matching_object_and_origin_and_removes_listener():
    page = Page()
    with observe_browser_verification(page, REQUEST, base_url="https://www.fastmoss.com") as evidence:
        page.response(REQUEST["path"] + "?uid=123", 200, host="unrelated.example")
        page.response(REQUEST["path"] + "?uid=other", 200)
        assert browser_verification_failure_stage(evidence) == "browser_business"
        page.response(REQUEST["path"] + "?uid=123", 200)
        assert browser_verification_failure_stage(evidence) == ""
        page.response("/api/captcha/config", 200, method="POST")
        assert browser_verification_failure_stage(evidence) == "captcha_confirmation"
        page.response("/api/captcha/verify", 200, method="POST", accepted=False)
        assert browser_verification_failure_stage(evidence) == "captcha_confirmation"
        page.response("/api/captcha/verify", 200, method="POST")
        assert browser_verification_failure_stage(evidence) == "browser_business"
        page.response(REQUEST["path"] + "?uid=123", 200)
        assert browser_verification_failure_stage(evidence) == ""
        assert "must-not-leak" not in str(evidence)
    assert not page.listeners


@pytest.mark.parametrize("captcha_code,browser_code,http_ok,expected_stage", [
    (None, None, True, "captcha_confirmation"),
    ("MSG_SAFE_0001", None, True, "captcha_confirmation"),
    (200, "MSG_SAFE_0001", True, "browser_business"),
    (200, 200, False, "http_replay"),
    (200, 200, None, "http_replay"),
    (200, 200, True, ""),
    (200, None, True, ""),
])
def test_chain_does_not_repeat_captcha_or_claim_success_from_disappearing_slider(
    monkeypatch, captcha_code, browser_code, http_ok, expected_stage,
):
    page = Page()
    opened, replayed, cached, probed = [], [], [], []

    @contextmanager
    def open_page(**kwargs):
        opened.append(True)
        yield SimpleNamespace(page=page, raw_page=page)

    def goto(*args, **kwargs):
        page.response(REQUEST["path"] + "?uid=123", "MSG_SAFE_0001")
        page.response("/api/captcha/config", 200, method="POST")

    def slider(*args, **kwargs):
        assert kwargs["max_attempts"] <= 3
        if captcha_code is not None:
            page.response("/api/captcha/verify", captcha_code, method="POST")
        if browser_code is not None:
            page.response(REQUEST["path"] + "?uid=123", browser_code)
        return {"attempted": True, "resolved": True, "reason": "slider_cleared"}

    def replay(*args, **kwargs):
        replayed.append(True)
        if http_ok is None:
            raise handler.FastMossHTTPError("secret-server-message", status_code=503)
        return {"verified": http_ok, "response_code": "200" if http_ok else "MSG_SAFE_0001"}

    def probe(*args, **kwargs):
        probed.append(True)
        assert kwargs["timeout_ms"] == 5000
        if browser_code is None:
            page.response(REQUEST["path"] + "?uid=123", 200)

    def cache(**kwargs):
        cached.append(True)
        return {"cookie_count": 1, "has_fd_tk": True}

    monkeypatch.setattr(handler, "open_automation_page", open_page)
    monkeypatch.setattr(handler, "_page_goto", goto)
    monkeypatch.setattr(handler, "_safe_wait_for_timeout", lambda *a, **k: None)
    monkeypatch.setattr(handler, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    ticks = count(0, 0.5)
    monkeypatch.setattr(handler, "_read_fastmoss_slider_state", lambda *a: {"visible": True})
    monkeypatch.setattr(handler, "_capture_fastmoss_browser_diagnostic_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(handler, "_export_fastmoss_browser_cookies", lambda *a, **k: [
        {"name": "fd_tk", "value": "test-cookie", "domain": ".fastmoss.com"},
    ])
    monkeypatch.setattr(handler, "_try_resolve_fastmoss_slider_security_check", slider)
    monkeypatch.setattr(handler, "_verify_original_request_with_cookies_result", replay)
    monkeypatch.setattr(handler, "request_original_api_in_browser", probe)
    monkeypatch.setattr(handler, "_save_browser_cookies_to_cache", cache)
    context = HandlerContext(
        request_id="test", job_id="test", handler_code=handler.HANDLER_CODE,
        worker_type="browser_worker", runtime_table="task_execution", payload={
            "verification_request": REQUEST, "verification_chain_revision": 1,
            "fastmoss_browser_max_attempts": 99, "fastmoss_slider_max_attempts": 99,
        },
    )
    result = handler.fastmoss_security_browser_resolve_handler(context)
    assert result.status == ("failed" if expected_stage else "success")
    chain = result.result["verification_chain"]
    assert chain["failure_stage"] == expected_stage
    assert len(opened) == 1
    assert len(replayed) == int(expected_stage in ("", "http_replay"))
    assert len(probed) == int(captcha_code == 200 and browser_code != 200)
    assert len(cached) == int(not expected_stage)
    assert "test-cookie" not in str(result.to_dict())
    assert "secret-server-message" not in str(result.to_dict())
    assert not page.listeners


def test_monitor_recovery_has_bounded_runtime_and_chain_revision():
    from test_runtime_monitor_tk_influencers import RecoveryStore
    from automation_business_scaffold.control_plane.executor.looping import build_child_runner_config

    store = RecoveryStore()
    store.advance()
    execution = store.executions[0]
    assert execution.payload["verification_chain_revision"] == 1
    assert execution.max_execution_seconds == 420
    config = build_child_runner_config(
        {}, worker_type="browser_worker", handler_code=handler.HANDLER_CODE,
        runtime_timeout_seconds=execution.max_execution_seconds,
    )
    assert config.timeout_seconds == 180


def test_browser_business_success_stops_sliding_even_when_popup_remains(monkeypatch):
    from automation_business_scaffold.capabilities.browser.fastmoss_security import slider_challenge as slider

    attempted = []
    monkeypatch.setattr(slider, "_resolve_fastmoss_slider_selector_payload", lambda *a, **k: {})
    monkeypatch.setattr(slider, "_build_slider_captcha_provider", lambda *a, **k: object())
    monkeypatch.setattr(slider, "_resolve_one_fastmoss_mixed_slider_attempt", lambda *a, **k: attempted.append(True) or {})
    monkeypatch.setattr(slider, "_persist_fastmoss_slider_artifacts_payload", lambda *a, **k: [])
    monkeypatch.setattr(slider, "_framework_slider_attempts_from_audit", lambda *a, **k: [])
    monkeypatch.setattr(slider, "_wait_for_fastmoss_slider_post_drag_state", lambda *a, **k: {"visible": True})
    result = slider._resolve_fastmoss_slider_with_framework_captcha(
        object(), page=object(), initial_state={}, search_url="https://www.fastmoss.com",
        max_attempts=3, settle_ms=5000, confirm_ms=2000, audit_dir="unused",
        provider_config={}, resolver_config={}, selectors={}, business_verified=lambda: bool(attempted),
    )
    assert len(attempted) == 1
    assert result["reason"] == "browser_business_verified"


@pytest.mark.parametrize("success_between", [False, True])
def test_three_persisted_source_failures_stop_batch_and_success_resets_streak(success_between):
    from test_runtime_monitor_tk_influencers import RecoveryStore

    store = RecoveryStore()
    for index in range(3):
        job = store.job(str(index), status="finished")
        job.update(finished_at=index + 1, error_code="fastmoss_security_browser_fallback_failed")
        store.jobs.append(job)
    if success_between:
        store.jobs[-2]["error_code"] = ""
        store.jobs[-2]["result"] = {"status": "success"}
    result = store.advance()
    if success_between:
        assert result["action"] == "waiting"
        assert len(store.executions) == 1
    else:
        assert result["action"] == "finalize"
        assert result["error_code"] == "fastmoss_recovery_exhausted"
        assert not store.executions


def test_batch_stop_drains_running_child_without_claiming_pending_child(runtime_db_url):
    from automation_business_scaffold.infrastructure.runtime.runtime_store import RuntimeStore
    from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.summary import finalize_request
    from automation_business_scaffold.domains.tiktok.flows.monitor_tk_influencers.orchestrator import TASK_CODE

    store = RuntimeStore(db_url=runtime_db_url)
    request = store.submit_task_request(project_code="test", task_code=TASK_CODE, payload={}, requested_by="pytest")
    store.update_task_request(request_id=request.request_id, status="waiting")
    store.enqueue_api_worker_jobs(
        request_id=request.request_id, task_code=TASK_CODE, job_code="influencer_monitor_sync",
        jobs=[{"business_key": key, "dedupe_key": key, "payload": {}} for key in ("a", "b")],
    )
    claim = store.claim_next_api_worker_job(worker_id="pytest", lease_seconds=30, request_id=request.request_id)
    result = finalize_request(store=store, request=request, workflow=None, force_result={
        "error_code": "fastmoss_recovery_exhausted", "consecutive_recovery_failures": 3,
    })
    assert result["status"] == "cancelling"
    assert store.claim_next_api_worker_job(worker_id="pytest", lease_seconds=30, request_id=request.request_id) is None
    store.mark_api_worker_job_success(job_id=claim["job_id"], run_id=claim["run_id"], summary={}, result={}, stage="done")
    store.reconcile_cancelling_request(request_id=request.request_id)
    stopped = store.load_task_request(request_id=request.request_id)
    assert stopped.status == "cancelled"
    assert stopped.error_code == "fastmoss_recovery_exhausted"
    assert stopped.child_total_count == stopped.child_terminal_count == 2


def test_browser_probe_is_same_origin_bounded_and_uses_both_signatures():
    from automation_business_scaffold.capabilities.browser.fastmoss_security.request_verification import (
        request_original_api_in_browser,
    )

    calls = []
    page = SimpleNamespace(evaluate=lambda script, value: calls.append((script, value)))
    request_original_api_in_browser(page, REQUEST, base_url="https://www.fastmoss.com", timeout_ms=5000)
    script, payload = calls[0]
    assert payload["url"].startswith("https://www.fastmoss.com" + REQUEST["path"] + "?")
    assert payload["timeout_ms"] == 5000
    assert payload["headers"]["fm-sign"]
    assert '__SIG__.gen' in script and '"fm-sig"' in script
    assert 'credentials: "include"' in script
    assert 'AbortController' in script
    with pytest.raises(ValueError):
        request_original_api_in_browser(page, {**REQUEST, "path": "https://other.example/api/read"},
                                        base_url="https://www.fastmoss.com", timeout_ms=5000)
    with pytest.raises(ValueError):
        request_original_api_in_browser(page, {**REQUEST, "method": "POST"},
                                        base_url="https://www.fastmoss.com", timeout_ms=5000)
    assert len(calls) == 1
