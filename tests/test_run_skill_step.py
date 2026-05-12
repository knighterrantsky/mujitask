from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

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
STRICT_RUNTIME_ENV = {
    "EXECUTION_CONTROL_DB_URL": "postgresql+psycopg://runtime",
    "TK_FACT_DB_URL": "postgresql+psycopg://facts",
    "EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER": "minio",
    "EXECUTION_CONTROL_ARTIFACT_BUCKET": "mujitask-test-artifacts",
    "EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX": "tests/skill-submit",
    "EXECUTION_CONTROL_MINIO_ENDPOINT": "127.0.0.1:9000",
    "EXECUTION_CONTROL_MINIO_ACCESS_KEY": "minioadmin",
    "EXECUTION_CONTROL_MINIO_SECRET_KEY": "miniosecret",
    "EXECUTION_CONTROL_MINIO_REGION": "us-east-1",
    "EXECUTION_CONTROL_MINIO_SECURE": "false",
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


def _load_lightweight_submit_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "lightweight_submit.py"
    )
    spec = importlib.util.spec_from_file_location("mujitask_lightweight_submit", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_resolve_browser_target_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "resolve_browser_target.py"
    )
    spec = importlib.util.spec_from_file_location("mujitask_resolve_browser_target", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_entry_files_do_not_own_runtime_or_browser_config():
    root = Path(__file__).resolve().parents[1]
    skill_example = (
        root
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "skill.local.env.example"
    ).read_text(encoding="utf-8")
    run_skill_step = (
        root
        / "skills"
        / "mujitask-tiktok-feishu-sync"
        / "run_skill_step.py"
    ).read_text(encoding="utf-8")

    forbidden_skill_env_tokens = (
        "BROWSER_PROFILE_REF",
        "BROWSER_PROVIDER_NAME",
        "BROWSER_PROFILE_ID",
        "BROWSER_WORKSPACE_ID",
        "BROWSER_PROFILES_FILE",
        "DEFAULT_PROFILE_REF",
        "EXECUTION_CONTROL_DB_URL",
        "EXECUTION_CONTROL_ARTIFACT_ROOT",
        "EXECUTION_CONTROL_ARTIFACT_BUCKET",
        "EXECUTION_CONTROL_REQUESTED_BY",
    )
    forbidden_run_step_tokens = (
        '"BROWSER_PROFILE_REF"',
        '"BROWSER_PROVIDER_NAME"',
        '"BROWSER_PROFILE_ID"',
        '"BROWSER_WORKSPACE_ID"',
        '"EXECUTION_CONTROL_REQUESTED_BY"',
        '"BUSINESS_EXECUTION_CONTROL_REQUESTED_BY"',
    )

    assert not any(token in skill_example for token in forbidden_skill_env_tokens)
    assert not any(token in run_skill_step for token in forbidden_run_step_tokens)


def test_lightweight_submitter_supports_selection_keyword_search():
    module = _load_lightweight_submit_module()
    root = Path(__file__).resolve().parents[1]

    from automation_business_scaffold.control_plane.executor import runner

    submitter = module._load_submitter(root, "search_keyword_selection_products")

    assert submitter is runner.run_search_keyword_selection_products_request


def test_lightweight_submit_rejects_worker_control_actions(tmp_path, monkeypatch):
    module = _load_lightweight_submit_module()
    result_file = tmp_path / "result.json"

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("worker control action crossed the skill submit boundary")

    monkeypatch.setattr(module, "_load_submitter", fail_if_called)

    with pytest.raises(ValueError, match="control_action=submit"):
        module.main(
            [
                "--install-dir",
                str(tmp_path),
                "--task-name",
                "search_keyword_selection_products",
                "--params-json",
                json.dumps({"control_action": "api_worker_once"}),
                "--result-file",
                str(result_file),
            ]
        )

    assert not result_file.exists()


def test_resolve_browser_target_ignores_skill_local_browser_defaults(tmp_path, monkeypatch):
    module = _load_resolve_browser_target_module()
    skill_env = tmp_path / "skills" / "mujitask-tiktok-feishu-sync" / "skill.local.env"
    skill_env.parent.mkdir(parents=True)
    skill_env.write_text('BROWSER_PROFILE_REF="stale-skill-profile"\n', encoding="utf-8")
    for key in (
        "BROWSER_PROFILES_FILE",
        "DEFAULT_PROFILE_REF",
        "BROWSER_PROFILE_REF",
        "BROWSER_PROVIDER_NAME",
        "BROWSER_PROFILE_ID",
        "BROWSER_WORKSPACE_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="No browser profile_ref provided"):
        module.resolve_browser_target(
            install_dir=tmp_path,
            profile_ref=None,
            fallback_profile_ref=None,
        )


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


def test_append_runtime_params_does_not_include_persistence_config():
    module = _load_run_skill_step_module()

    params = module._append_runtime_params(
        ["control_action=submit"],
        {
            **STRICT_RUNTIME_ENV,
            "EXECUTION_CONTROL_REQUESTED_BY": "legacy-skill",
            "BUSINESS_EXECUTION_CONTROL_REQUESTED_BY": "project-runtime",
        },
    )

    forbidden_prefixes = (
        "requested_by=",
        "execution_control_db_url=",
        "fact_db_url=",
        "execution_control_fact_db_url=",
        "execution_control_artifact_",
        "execution_control_minio_",
        "browser_provider_name=",
        "browser_profile_id=",
        "browser_workspace_id=",
        "requires_fact_db=",
        "requires_object_storage=",
        "require_database_persistence=",
        "require_object_storage=",
    )
    assert not any(item.startswith(forbidden_prefixes) for item in params)


def test_influencer_pool_browser_params_only_pass_profile_ref(monkeypatch, tmp_path):
    module = _load_run_skill_step_module()

    monkeypatch.setattr(
        module,
        "_resolve_browser_target",
        lambda **kwargs: {
            "profile_ref": "roxy-tiktok",
            "provider": "roxy",
            "profile_id": "profile-123",
            "workspace_id": "workspace-456",
        },
    )

    params = module._append_influencer_pool_browser_params(
        params=[],
        skill_env={
            "BROWSER_PROVIDER_NAME": "skill-provider",
            "BROWSER_PROFILE_ID": "skill-profile",
            "BROWSER_WORKSPACE_ID": "skill-workspace",
        },
        python_bin=tmp_path / "python",
        install_dir=tmp_path,
        requested_profile_ref="",
        fallback_profile_ref="",
    )

    assert params == ["profile_ref=roxy-tiktok"]


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

    exit_code = module.main(["refresh-current-competitor-table-submit"])

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
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured.update(kwargs)
        return (0, {"status": "success", "request_id": "req-table-refs"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)

    exit_code = module.main(["refresh-current-competitor-table-submit"])

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

    exit_code = module.main(["refresh-current-competitor-table-submit"])

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
            "competitor-row-by-url-submit",
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


def test_main_product_url_complete_submit_uses_selection_table_without_runtime_config_params(tmp_path, monkeypatch):
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
            **STRICT_RUNTIME_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (0, {"status": "success", "request_id": "req-product-url-123", "request_status": "pending"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", lambda payload: 0)

    exit_code = module.main(
        [
            "product-url-complete-submit",
            "--product-url",
            "https://www.tiktok.com/shop/pdp/123456789",
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "tiktok_fastmoss_product_ingest"
    params = list(captured_calls[0]["params"])
    assert "source_table_ref=feishu://mujitask/tk_selection" in params
    assert "selection_table_ref=feishu://mujitask/tk_selection" in params
    assert f"table_url={FEISHU_TABLE_URLS['tk_selection']}" in params
    forbidden_prefixes = (
        "run_mode=",
        "execution_control_db_url=",
        "fact_db_url=",
        "execution_control_fact_db_url=",
        "execution_control_artifact_",
        "execution_control_minio_",
        "browser_provider_name=",
        "browser_profile_id=",
        "browser_workspace_id=",
        "requires_fact_db=",
        "requires_object_storage=",
    )
    assert not any(item.startswith(forbidden_prefixes) for item in params)


def test_main_selection_table_complete_submit_uses_selection_table_without_product_url(tmp_path, monkeypatch):
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
            **STRICT_RUNTIME_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        return (0, {"status": "success", "request_id": "req-selection-table-123", "request_status": "pending"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", lambda payload: 0)

    exit_code = module.main(["selection-table-complete-submit"])

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "tiktok_fastmoss_product_ingest"
    params = list(captured_calls[0]["params"])
    assert "source_table_ref=feishu://mujitask/tk_selection" in params
    assert "selection_table_ref=feishu://mujitask/tk_selection" in params
    assert f"table_url={FEISHU_TABLE_URLS['tk_selection']}" in params
    assert not any(item.startswith("product_url=") for item in params)
    assert "fallback_allowed=true" in params


def test_keyword_search_submit_params_include_total_sales_without_default_day7(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()

    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    params = module._keyword_search_submit_params(
        python_bin=tmp_path / "python",
        install_dir=tmp_path,
        requested_profile_ref="",
        fallback_profile_ref="roxy-tiktok",
        search_keyword="gel blaster",
        sales_7d_threshold="",
        total_sales_threshold="200",
        skip_fastmoss_login_validation=False,
        ensure_ready=False,
    )

    assert "total_sales_threshold=200" in params
    assert "sales_7d_threshold=200" not in params


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
            "keyword-search-submit",
            "--search-keyword",
            "Easter Basket Stuffers",
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "search_keyword_competitor_products"
    assert "control_action=submit" in captured_calls[0]["params"]
    assert "search_keyword=Easter Basket Stuffers" in captured_calls[0]["params"]
    assert "sales_7d_threshold=200" in captured_calls[0]["params"]
    assert "fastmoss_phone_env=FASTMOSS_PHONE" in captured_calls[0]["params"]
    assert "fastmoss_password_env=FASTMOSS_PASSWORD" in captured_calls[0]["params"]
    assert emitted["request_id"] == "req-keyword-123"
    assert emitted["request_status"] == "pending"


def test_main_keyword_search_total_sales_returns_after_submit(tmp_path, monkeypatch):
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
                "request_id": "req-keyword-total-sales-123",
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
            "keyword-search-submit",
            "--search-keyword",
            "gel blaster",
            "--total-sales-threshold",
            "200",
        ]
    )

    assert exit_code == 0
    params = list(captured_calls[0]["params"])
    assert captured_calls[0]["task_name"] == "search_keyword_competitor_products"
    assert "search_keyword=gel blaster" in params
    assert "total_sales_threshold=200" in params
    assert "sales_7d_threshold=200" not in params
    assert emitted["request_id"] == "req-keyword-total-sales-123"


def test_main_selection_keyword_search_returns_after_submit(tmp_path, monkeypatch):
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
                "request_id": "req-selection-keyword-123",
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
            "selection-keyword-search-submit",
            "--search-keyword",
            "east egg",
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "search_keyword_selection_products"
    params = list(captured_calls[0]["params"])
    assert "control_action=submit" in params
    assert "selection_table_ref=feishu://mujitask/tk_selection" in params
    assert "seed_table_ref=feishu://mujitask/tk_selection" in params
    assert "target_table_ref=feishu://mujitask/tk_selection" in params
    assert f"table_url={FEISHU_TABLE_URLS['tk_selection']}" in params
    assert "search_keyword=east egg" in params
    assert "sales_7d_threshold=500" in params
    assert "product_price_threshold=10.99" in params
    assert "keyword_workflow_mode=selection" in params
    assert not any(item.startswith("total_sales_threshold=") for item in params)
    assert "fastmoss_phone_env=FASTMOSS_PHONE" in params
    assert "fastmoss_password_env=FASTMOSS_PASSWORD" in params
    assert emitted["request_id"] == "req-selection-keyword-123"
    assert emitted["request_status"] == "pending"


def test_main_batch_keyword_competitor_search_submit_fans_out_rows(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
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
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        row_number = len(captured_calls)
        return (0, {"status": "success", "request_id": f"req-batch-{row_number}", "request_status": "pending"})

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    items_json = json.dumps(
        [
            {"search_keyword": "dog toy", "threshold_type": "total_sales", "threshold_value": "300"},
            {"search_keyword": "cat toy"},
        ],
        ensure_ascii=False,
    )

    exit_code = module.main(
        [
            "batch-keyword-search-submit",
            "--target-intent",
            "keyword_competitor_search",
            "--items-json",
            items_json,
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 2
    assert all(call["task_name"] == "search_keyword_competitor_products" for call in captured_calls)
    first_params = list(captured_calls[0]["params"])
    second_params = list(captured_calls[1]["params"])
    assert "search_keyword=dog toy" in first_params
    assert "total_sales_threshold=300" in first_params
    assert "sales_7d_threshold=200" not in first_params
    assert "max_candidates=20" in first_params
    assert "search_keyword=cat toy" in second_params
    assert "sales_7d_threshold=200" in second_params
    assert "max_candidates=20" in second_params
    assert any(item.startswith("idempotency_key=") for item in first_params)
    assert emitted["status"] == "success"
    assert emitted["request_ids"] == ["req-batch-1", "req-batch-2"]
    assert emitted["failed_item_count"] == 0


def test_main_batch_keyword_selection_search_submit_uses_selection_defaults(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
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
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")
    monkeypatch.setattr(
        module,
        "_run_lightweight_submit_capture_payload",
        lambda **kwargs: (captured_calls.append(kwargs) or (0, {"status": "success", "request_id": "req-selection-batch"})),
    )
    monkeypatch.setattr(module, "_emit_final_result", lambda payload: emitted.update(payload) or 0)

    items_json = json.dumps([{"search_keyword": "summer dress"}], ensure_ascii=False)

    exit_code = module.main(
        [
            "batch-keyword-search-submit",
            "--target-intent",
            "keyword_selection_search",
            "--items-json",
            items_json,
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["task_name"] == "search_keyword_selection_products"
    params = list(captured_calls[0]["params"])
    assert "selection_table_ref=feishu://mujitask/tk_selection" in params
    assert "seed_table_ref=feishu://mujitask/tk_selection" in params
    assert "target_table_ref=feishu://mujitask/tk_selection" in params
    assert "search_keyword=summer dress" in params
    assert "sales_7d_threshold=500" in params
    assert "product_price_threshold=10.99" in params
    assert "keyword_workflow_mode=selection" in params
    assert not any(item.startswith("total_sales_threshold=") for item in params)
    assert emitted["request_ids"] == ["req-selection-batch"]


def test_batch_keyword_submit_rejects_unsupported_fields_before_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
        },
    )
    monkeypatch.setattr(
        module,
        "_run_lightweight_submit_capture_payload",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit should not run")),
    )

    items_json = json.dumps([{"search_keyword": "dog toy", "filters": {"region": "US"}}], ensure_ascii=False)

    with pytest.raises(ValueError, match="unsupported fields"):
        module.main(
            [
                "batch-keyword-search-submit",
                "--target-intent",
                "keyword_competitor_search",
                "--items-json",
                items_json,
            ]
        )


def test_batch_keyword_selection_rejects_total_sales_before_submit(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        module,
        "_load_skill_env",
        lambda _path: {
            "INSTALL_DIR": str(install_dir),
            **FEISHU_TABLE_ROUTE_ENV,
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token",
        },
    )
    monkeypatch.setattr(
        module,
        "_run_lightweight_submit_capture_payload",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit should not run")),
    )

    items_json = json.dumps(
        [{"search_keyword": "summer dress", "threshold_type": "total_sales", "threshold_value": "300"}],
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="do not support total_sales"):
        module.main(
            [
                "batch-keyword-search-submit",
                "--target-intent",
                "keyword_selection_search",
                "--items-json",
                items_json,
            ]
        )


def test_main_batch_keyword_search_submit_reports_partial_success(tmp_path, monkeypatch):
    module = _load_run_skill_step_module()
    install_dir = tmp_path / "install"
    python_bin = install_dir / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
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
            "FASTMOSS_PHONE": "18000000000",
            "FASTMOSS_PASSWORD": "secret",
        },
    )
    monkeypatch.setattr(module, "_resolve_profile_ref_for_task", lambda **kwargs: "roxy-tiktok")

    def fake_run_lightweight_submit_capture_payload(**kwargs):
        captured_calls.append(kwargs)
        if len(captured_calls) == 1:
            return (0, {"status": "success", "request_id": "req-batch-ok", "request_status": "pending"})
        return (1, {"status": "failed", "error": "boom"})

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", lambda payload: emitted.update(payload) or 0)

    items_json = json.dumps([{"search_keyword": "dog toy"}, {"search_keyword": "cat toy"}], ensure_ascii=False)

    exit_code = module.main(
        [
            "batch-keyword-search-submit",
            "--target-intent",
            "keyword_competitor_search",
            "--items-json",
            items_json,
        ]
    )

    assert exit_code == 0
    assert len(captured_calls) == 2
    assert emitted["status"] == "partial_success"
    assert emitted["request_ids"] == ["req-batch-ok"]
    assert emitted["failed_item_count"] == 1
    assert emitted["items"][1]["error"] == "boom"


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

    def fake_emit_final_result(payload):
        emitted.update(payload)
        return 0

    monkeypatch.setattr(module, "_run_lightweight_submit_capture_payload", fake_run_lightweight_submit_capture_payload)
    monkeypatch.setattr(module, "_emit_final_result", fake_emit_final_result)

    exit_code = module.main(["influencer-pool-sync-submit"])

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

    exit_code = module.main(["influencer-pool-sync-submit"])

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
