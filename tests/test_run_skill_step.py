from __future__ import annotations

import importlib.util
import json
from pathlib import Path

FEISHU_BASE_URL = "https://example.feishu.cn/base/app"
FEISHU_TABLE_ROUTE_ENV = {
    "MUJITASK_FEISHU_BASE_URL": FEISHU_BASE_URL,
    "MUJITASK_FEISHU_TK_SELECTION_TABLE_ID": "tblSelection",
    "MUJITASK_FEISHU_TK_SELECTION_VIEW_ID": "vewSelection",
    "MUJITASK_FEISHU_TK_COMPETITOR_TABLE_ID": "tblCompetitor",
    "MUJITASK_FEISHU_TK_COMPETITOR_VIEW_ID": "vewCompetitor",
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_TABLE_ID": "tblInfluencer",
    "MUJITASK_FEISHU_TK_INFLUENCER_POOL_VIEW_ID": "vewInfluencer",
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_TABLE_ID": "tblOutreach",
    "MUJITASK_FEISHU_TK_INFLUENCER_OUTREACH_VIEW_ID": "vewOutreach",
    "MUJITASK_FEISHU_TK_HOT_VIDEO_TABLE_ID": "tblVideo",
    "MUJITASK_FEISHU_TK_HOT_VIDEO_VIEW_ID": "vewVideo",
}
FEISHU_TABLE_URLS = {
    "tk_selection": f"{FEISHU_BASE_URL}?table=tblSelection&view=vewSelection",
    "tk_competitor": f"{FEISHU_BASE_URL}?table=tblCompetitor&view=vewCompetitor",
    "tk_influencer_pool": f"{FEISHU_BASE_URL}?table=tblInfluencer&view=vewInfluencer",
    "tk_influencer_outreach": f"{FEISHU_BASE_URL}?table=tblOutreach&view=vewOutreach",
    "tk_hot_video": f"{FEISHU_BASE_URL}?table=tblVideo&view=vewVideo",
}


def _load_run_skill_step_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "run_skill_step.py"
    )
    spec = importlib.util.spec_from_file_location("mujitask_run_skill_step", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _param_value(params: list[str], key: str):
    prefix = f"{key}="
    for item in params:
        if item.startswith(prefix):
            return item.split("=", 1)[1]
    raise AssertionError(f"missing param {key}")


def test_append_runtime_params_uses_explicit_openclaw_delivery_context():
    module = _load_run_skill_step_module()

    params = module._append_runtime_params(
        ["control_action=submit"],
        {
            "NOTIFICATION_CHANNEL_CODE": "",
            "OPENCLAW_DELIVERY_CHANNEL": "feishu",
            "OPENCLAW_DELIVERY_TO": "user:ou_test_user",
            "OPENCLAW_DELIVERY_ACCOUNT_ID": "default",
            "OPENCLAW_DELIVERY_SESSION_ID": "session-123",
        },
    )

    assert "notification_channel_code=openclaw_message" in params
    assert "source_session_id=session-123" in params
    reply_target_entries = [item for item in params if item.startswith("reply_target=")]
    assert len(reply_target_entries) == 1
    reply_target = json.loads(reply_target_entries[0].split("=", 1)[1])
    assert reply_target == {
        "channel": "feishu",
        "to": "user:ou_test_user",
        "accountId": "default",
        "sessionId": "session-123",
    }


def test_append_runtime_params_falls_back_to_openclaw_session_store(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    for key in (
        "OPENCLAW_DELIVERY_CONTEXT_JSON",
        "OPENCLAW_DELIVERY_CHANNEL",
        "OPENCLAW_DELIVERY_TO",
        "OPENCLAW_DELIVERY_ACCOUNT_ID",
        "OPENCLAW_DELIVERY_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    sessions_dir = tmp_path / "agents" / "tiktok-ops" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                "agent:tiktok-ops:feishu:direct:ou_latest": {
                    "sessionId": "session-latest",
                    "updatedAt": 200,
                    "deliveryContext": {
                        "channel": "feishu",
                        "to": "user:ou_latest",
                        "accountId": "default",
                    },
                },
                "agent:tiktok-ops:feishu:direct:ou_old": {
                    "sessionId": "session-old",
                    "updatedAt": 100,
                    "deliveryContext": {
                        "channel": "feishu",
                        "to": "user:ou_old",
                        "accountId": "default",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    params = module._append_runtime_params(
        ["control_action=submit"],
        {
            "NOTIFICATION_CHANNEL_CODE": "",
            "OPENCLAW_AGENT_ID": "tiktok-ops",
            "OPENCLAW_STATE_DIR": str(tmp_path),
        },
    )

    assert "notification_channel_code=openclaw_message" in params
    assert "source_session_id=session-latest" in params
    reply_target_entries = [item for item in params if item.startswith("reply_target=")]
    assert len(reply_target_entries) == 1
    reply_target = json.loads(reply_target_entries[0].split("=", 1)[1])
    assert reply_target["channel"] == "feishu"
    assert reply_target["to"] == "user:ou_latest"
    assert reply_target["accountId"] == "default"
    assert reply_target["sessionId"] == "session-latest"


def test_run_cli_task_capture_payload_writes_result_file_via_extra_env(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()

    def fake_monitor_process(**kwargs):
        return None

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.returncode = 0

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(module, "_monitor_process", fake_monitor_process)
    monkeypatch.setattr(
        module,
        "_build_result_json",
        lambda **kwargs: json.dumps(
            {
                "status": "success",
                "task_name": "refresh_current_competitor_table",
                "request_id": "req-123",
                "summary": {"total": 1, "counts": {"queued": 1}},
                "summary_text": "queued=1, total=1",
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)

    cli_bin = tmp_path / "automation-business-scaffold-run"
    python_bin = tmp_path / "python"
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    status, payload = module._run_cli_task_capture_payload(
        install_dir=tmp_path,
        python_bin=python_bin,
        cli_bin=cli_bin,
        task_name="refresh_current_competitor_table",
        run_mode="canary",
        params=["control_action=submit"],
        stdout_prefix="test-step",
        extra_env={},
    )

    assert status == 0
    assert payload["request_id"] == "req-123"
    assert payload["summary"]["counts"] == {"queued": 1}


def test_refresh_competitor_submit_params_include_fastmoss_env_markers(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()

    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    params = module._refresh_competitor_submit_params(
        python_bin=tmp_path / "python",
        install_dir=tmp_path,
        requested_profile_ref="",
        fallback_profile_ref="roxy-tiktok",
        ensure_ready=False,
    )

    assert params == [
        "profile_ref=roxy-tiktok",
        "verify_fastmoss_login=false",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
        "fastmoss_window_days=90",
    ]


def test_product_url_complete_submit_params_enable_browser_fallback(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()

    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    params = module._product_url_complete_submit_params(
        python_bin=tmp_path / "python",
        install_dir=tmp_path,
        requested_profile_ref="",
        fallback_profile_ref="roxy-tiktok",
        ensure_ready=False,
    )

    assert params == [
        "profile_ref=roxy-tiktok",
        "verify_fastmoss_login=false",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
        "fastmoss_window_days=90",
        "fallback_allowed=true",
    ]


def test_keyword_search_submit_params_include_keyword_and_fastmoss_env_markers(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()

    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    params = module._keyword_search_submit_params(
        python_bin=tmp_path / "python",
        install_dir=tmp_path,
        requested_profile_ref="",
        fallback_profile_ref="roxy-tiktok",
        search_keyword="Easter Basket Stuffers",
        sales_7d_threshold="200",
        skip_fastmoss_login_validation=False,
        ensure_ready=False,
    )

    assert params == [
        "profile_ref=roxy-tiktok",
        "search_keyword=Easter Basket Stuffers",
        "sales_7d_threshold=200",
        "fastmoss_phone_env=FASTMOSS_PHONE",
        "fastmoss_password_env=FASTMOSS_PASSWORD",
    ]


def test_main_refresh_submit_passes_fastmoss_env_markers(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured.update(kwargs)
        return (
            0,
            {
                "status": "success",
                "control_action": "submit",
                "request_id": "req-refresh-submit-123",
                "request_status": "pending",
                "summary": {"total": 1, "counts": {"queued": 1}},
            },
        )

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)

    exit_code = module.main(["refresh-current-competitor-table-submit", "--run-mode", "canary"])

    assert exit_code == 0
    params = list(captured["params"])
    assert f"table_url={FEISHU_TABLE_URLS['tk_competitor']}" in params
    assert "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN" in params
    assert "url_field_name=产品链接" in params
    assert "control_action=submit" in params
    assert "profile_ref=roxy-tiktok" in params
    assert "verify_fastmoss_login=false" in params
    assert "fastmoss_phone_env=FASTMOSS_PHONE" in params
    assert "fastmoss_password_env=FASTMOSS_PASSWORD" in params
    assert captured["accepted_message"] == "Refresh task accepted for asynchronous execution."


def test_main_refresh_submit_resolves_competitor_table_from_english_route_config(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured.update(kwargs)
        return (0, {"status": "success", "request_id": "req-table-refs"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)

    exit_code = module.main(["refresh-current-competitor-table-submit", "--run-mode", "canary"])

    assert exit_code == 0
    params = list(captured["params"])
    assert f"table_url={FEISHU_TABLE_URLS['tk_competitor']}" in params
    assert "source_table_ref=feishu://mujitask/tk_competitor" in params
    table_refs = json.loads(_param_value(params, "table_refs"))
    assert table_refs["tk_competitor"] == FEISHU_TABLE_URLS["tk_competitor"]
    assert table_refs["feishu://mujitask/tk_competitor"] == FEISHU_TABLE_URLS["tk_competitor"]
    assert table_refs["tk_influencer_pool"] == FEISHU_TABLE_URLS["tk_influencer_pool"]
    assert all("TK" not in key or key.startswith("tk_") for key in table_refs)


def test_main_refresh_current_competitor_table_returns_after_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured_calls: list[dict[str, object]] = []
    emitted: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (
            0,
            {
                "status": "success",
                "control_action": "submit",
                "request_id": "req-async-123",
                "request_status": "pending",
                "summary": {"total": 1, "counts": {"queued": 1}},
            },
        )

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    exit_code = module.main(["refresh-current-competitor-table", "--run-mode", "canary"])

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "refresh_current_competitor_table"
    assert "control_action=submit" in captured_calls[0]["params"]
    assert emitted["request_id"] == "req-async-123"
    assert emitted["request_status"] == "pending"


def test_main_competitor_row_by_url_returns_after_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured_calls: list[dict[str, object]] = []
    emitted: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (
            0,
            {
                "status": "success",
                "control_action": "submit",
                "request_id": "req-competitor-url-123",
                "request_status": "pending",
                "summary": {"total": 1, "counts": {"queued": 1}},
            },
        )

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    exit_code = module.main(
        [
            "competitor-row-by-url",
            "--run-mode",
            "canary",
            "--product-url",
            "https://www.tiktok.com/shop/pdp/123456789",
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "refresh_competitor_row_by_url"
    params = list(captured_calls[0]["params"])
    assert "source_table_ref=feishu://mujitask/tk_competitor" in params
    assert f"table_url={FEISHU_TABLE_URLS['tk_competitor']}" in params
    assert "product_url=https://www.tiktok.com/shop/pdp/123456789" in params
    assert "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN" in params
    assert "fallback_allowed=true" in params
    assert emitted["request_id"] == "req-competitor-url-123"
    assert emitted["request_status"] == "pending"


def test_main_keyword_search_returns_after_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured_calls: list[dict[str, object]] = []
    emitted: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (
            0,
            {
                "status": "success",
                "control_action": "submit",
                "request_id": "req-keyword-123",
                "request_status": "pending",
                "summary": {"total": 1, "counts": {"queued": 1}},
            },
        )

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    exit_code = module.main(
        [
            "keyword-search",
            "--run-mode",
            "canary",
            "--search-keyword",
            "Easter Basket Stuffers",
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "search_keyword_competitor_products"
    assert "control_action=submit" in captured_calls[0]["params"]
    assert "search_keyword=Easter Basket Stuffers" in captured_calls[0]["params"]
    assert emitted["request_id"] == "req-keyword-123"
    assert emitted["request_status"] == "pending"


def test_main_influencer_pool_sync_returns_after_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured_calls: list[dict[str, object]] = []
    emitted: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
            "INFLUENCER_POOL_FASTMOSS_PHONE_ENV": "FASTMOSS_PHONE",
            "INFLUENCER_POOL_FASTMOSS_PASSWORD_ENV": "FASTMOSS_PASSWORD",
        },
    )

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (
            0,
            {
                "status": "success",
                "control_action": "submit",
                "request_id": "req-influencer-123",
                "request_status": "pending",
                "summary": {"total": 1, "counts": {"queued": 1}},
            },
        )

    def fake_run_cli_task_capture_payload(**kwargs):
        raise AssertionError("influencer-pool-sync must submit asynchronously instead of direct CLI execution")

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_run_cli_task_capture_payload", fake_run_cli_task_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    exit_code = module.main(["influencer-pool-sync", "--run-mode", "canary"])

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "sync_tk_influencer_pool"
    params = list(captured_calls[0]["params"])
    assert "control_action=submit" in params
    assert f"table_url={FEISHU_TABLE_URLS['tk_competitor']}" in params
    assert f"target_table_url={FEISHU_TABLE_URLS['tk_influencer_pool']}" in params
    assert "access_token_env=MUJITASK_FEISHU_ACCESS_TOKEN" in params
    assert "fastmoss_phone_env=FASTMOSS_PHONE" in params
    assert "fastmoss_password_env=FASTMOSS_PASSWORD" in params
    assert emitted["request_id"] == "req-influencer-123"
    assert emitted["request_status"] == "pending"


def test_main_influencer_pool_sync_uses_english_route_config_for_source_and_target(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    cli_bin = install_dir / ".venv" / "bin" / "automation-business-scaffold-run"
    python_bin = install_dir / ".venv" / "bin" / "python"
    cli_bin.parent.mkdir(parents=True, exist_ok=True)
    cli_bin.write_text("", encoding="utf-8")
    python_bin.write_text("", encoding="utf-8")

    captured_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "BROWSER_PROFILE_REF": "roxy-default",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
            "INFLUENCER_POOL_FASTMOSS_PHONE_ENV": "FASTMOSS_PHONE",
            "INFLUENCER_POOL_FASTMOSS_PASSWORD_ENV": "FASTMOSS_PASSWORD",
        },
    )

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (0, {"status": "success", "request_id": "req-influencer-table-refs"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)

    exit_code = module.main(["influencer-pool-sync", "--run-mode", "canary"])

    assert exit_code == 0
    params = list(captured_calls[0]["params"])
    assert f"table_url={FEISHU_TABLE_URLS['tk_competitor']}" in params
    assert f"target_table_url={FEISHU_TABLE_URLS['tk_influencer_pool']}" in params
    assert "source_table_ref=feishu://mujitask/tk_competitor" in params
    assert "target_table_ref=feishu://mujitask/tk_influencer_pool" in params
    table_refs = json.loads(_param_value(params, "table_refs"))
    assert table_refs["tk_competitor"] == FEISHU_TABLE_URLS["tk_competitor"]
    assert table_refs["tk_influencer_pool"] == FEISHU_TABLE_URLS["tk_influencer_pool"]
    assert all("TK" not in key or key.startswith("tk_") for key in table_refs)
