from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "examples" / "openclaw" / "openclaw_deploy_utils.py"
SPEC = importlib.util.spec_from_file_location("openclaw_deploy_utils", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_merge_key_value_file_preserves_unknown_keys_and_updates_managed_values(tmp_path: Path) -> None:
    env_file = tmp_path / "skill.local.env"
    env_file.write_text(
        "# existing config\n"
        "INSTALL_DIR=/old/install\n"
        "MUJITASK_FEISHU_BASE_URL=https://old.example/base/app\n"
        "BROWSER_PROFILE_REF=profile-a\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n",
        encoding="utf-8",
    )

    MODULE.merge_key_value_file(
        env_file,
        {
            "INSTALL_DIR": "/new/install",
            "MUJITASK_FEISHU_BASE_URL": "https://new.example/base/app",
            "MUJITASK_FEISHU_ACCESS_TOKEN": "token-new",
        },
    )

    assert env_file.read_text(encoding="utf-8") == (
        "# existing config\n"
        "INSTALL_DIR=/new/install\n"
        "MUJITASK_FEISHU_BASE_URL=https://new.example/base/app\n"
        "BROWSER_PROFILE_REF=profile-a\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n"
        "MUJITASK_FEISHU_ACCESS_TOKEN=token-new\n"
    )


def test_remove_key_value_file_removes_retired_skill_runtime_keys(tmp_path: Path) -> None:
    env_file = tmp_path / "skill.local.env"
    env_file.write_text(
        "# existing config\n"
        "INSTALL_DIR=/install\n"
        "BROWSER_PROFILE_REF=profile-a\n"
        "EXECUTION_CONTROL_DB_URL=postgresql+psycopg://runtime\n"
        "EXECUTION_CONTROL_ARTIFACT_BUCKET=artifacts\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n",
        encoding="utf-8",
    )

    MODULE.remove_key_value_file(
        env_file,
        [
            "BROWSER_PROFILE_REF",
            "EXECUTION_CONTROL_DB_URL",
            "EXECUTION_CONTROL_ARTIFACT_BUCKET",
        ],
    )

    assert env_file.read_text(encoding="utf-8") == (
        "# existing config\n"
        "INSTALL_DIR=/install\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n"
    )


def test_write_deploy_state_file_adds_update_markers_and_preserves_unknown_keys(tmp_path: Path) -> None:
    deploy_state = tmp_path / "openclaw-deploy.env"
    deploy_state.write_text("CUSTOM_FLAG=keep\n", encoding="utf-8")

    MODULE.write_deploy_state_file(
        deploy_state,
        repo_url="https://github.com/example/repo",
        resolved_ref="v1.2.3",
        repo_archive_url="https://example.com/repo.zip",
        framework_archive_url="https://example.com/framework.zip",
        install_layout_version="1",
        update_supported="1",
    )

    data = MODULE.load_key_value_file(deploy_state)
    assert data["REPO_URL"] == "https://github.com/example/repo"
    assert data["LAST_RESOLVED_REF"] == "v1.2.3"
    assert data["REPO_ARCHIVE_URL"] == "https://example.com/repo.zip"
    assert data["FRAMEWORK_ARCHIVE_URL"] == "https://example.com/framework.zip"
    assert data["INSTALL_LAYOUT_VERSION"] == "1"
    assert data["UPDATE_SUPPORTED"] == "1"
    assert data["CUSTOM_FLAG"] == "keep"
    assert MODULE.deploy_state_supports_update(deploy_state) is True


def test_sync_install_tree_preserves_local_state_and_overwrites_managed_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    (source_dir / "pyproject.toml").write_text("name = 'new'\n", encoding="utf-8")
    (source_dir / "README.md").write_text("new readme\n", encoding="utf-8")
    (source_dir / "config").mkdir()
    (source_dir / "config" / "browser_profiles.json").write_text('{"new": true}\n', encoding="utf-8")
    (source_dir / "config" / "browser_profiles.example.json").write_text('{"example": true}\n', encoding="utf-8")
    (source_dir / "skills").mkdir()
    (source_dir / "skills" / "bundle.txt").write_text("new bundle\n", encoding="utf-8")

    (target_dir / ".venv").mkdir()
    (target_dir / ".venv" / "pyvenv.cfg").write_text("keep venv\n", encoding="utf-8")
    (target_dir / "runtime").mkdir()
    (target_dir / "runtime" / "cli_runs").mkdir()
    (target_dir / "runtime" / "cli_runs" / "keep.json").write_text("keep runtime\n", encoding="utf-8")
    (target_dir / ".env").write_text("KEEP_ENV=1\n", encoding="utf-8")
    (target_dir / "config").mkdir()
    (target_dir / "config" / "browser_profiles.json").write_text('{"custom": true}\n', encoding="utf-8")
    (target_dir / "config" / "stale.txt").write_text("remove me\n", encoding="utf-8")
    (target_dir / "obsolete.txt").write_text("remove me\n", encoding="utf-8")

    MODULE.sync_install_tree(
        source_dir,
        target_dir,
        [".venv", "runtime", ".env", "config/browser_profiles.json"],
    )

    assert (target_dir / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8") == "keep venv\n"
    assert (target_dir / "runtime" / "cli_runs" / "keep.json").read_text(encoding="utf-8") == "keep runtime\n"
    assert (target_dir / ".env").read_text(encoding="utf-8") == "KEEP_ENV=1\n"
    assert (target_dir / "config" / "browser_profiles.json").read_text(encoding="utf-8") == '{"custom": true}\n'
    assert not (target_dir / "config" / "stale.txt").exists()
    assert not (target_dir / "obsolete.txt").exists()
    assert (target_dir / "pyproject.toml").read_text(encoding="utf-8") == "name = 'new'\n"
    assert (target_dir / "README.md").read_text(encoding="utf-8") == "new readme\n"
    assert (target_dir / "config" / "browser_profiles.example.json").read_text(encoding="utf-8") == '{"example": true}\n'
    assert (target_dir / "skills" / "bundle.txt").read_text(encoding="utf-8") == "new bundle\n"


def test_read_framework_dependency_prefers_pyproject_as_single_source(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        "dependencies = [\n"
        '  "automation-framework @ git+https://github.com/example/framework.git@v0.3.6",\n'
        '  "requests>=2.32.0",\n'
        "]\n",
        encoding="utf-8",
    )

    dependency = MODULE.read_framework_dependency(pyproject)

    assert dependency == {
        "dependency": "automation-framework @ git+https://github.com/example/framework.git@v0.3.6",
        "source": "git+https://github.com/example/framework.git@v0.3.6",
        "kind": "git",
        "repo_url": "https://github.com/example/framework.git",
        "ref": "v0.3.6",
    }


def test_read_framework_dependency_accepts_extras(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        "dependencies = [\n"
        '  "automation-framework[captcha] @ git+https://github.com/example/framework.git@v0.3.8",\n'
        "]\n",
        encoding="utf-8",
    )

    dependency = MODULE.read_framework_dependency(pyproject)

    assert dependency == {
        "dependency": "automation-framework[captcha] @ git+https://github.com/example/framework.git@v0.3.8",
        "source": "git+https://github.com/example/framework.git@v0.3.8",
        "kind": "git",
        "repo_url": "https://github.com/example/framework.git",
        "ref": "v0.3.8",
    }


def test_deploy_script_embeds_single_file_payloads() -> None:
    deploy_script = ROOT / "examples" / "openclaw" / "deploy-openclaw.sh"
    text = deploy_script.read_text(encoding="utf-8")

    assert "load_openclaw_common" in text
    assert ": <<'__OPENCLAW_DEPLOY_COMMON__'" in text
    assert ": <<'__OPENCLAW_DEPLOY_UTILS__'" in text
    assert "install_framework_from_pyproject" in text
    assert 'add_parser("read-framework-dependency")' in text


def test_macos_deploy_runs_alembic_before_launchd_restart() -> None:
    deploy_script = ROOT / "scripts" / "deploy" / "macos" / "deploy.sh"
    text = deploy_script.read_text(encoding="utf-8")

    migration_command = 'bash "${install_dir}/scripts/execution_control/run_alembic_upgrade.sh"'
    launchd_command = 'bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"'

    assert migration_command in text
    assert launchd_command in text
    assert text.index(migration_command) < text.index(launchd_command)


def test_macos_deploy_installs_tiktok_and_amazon_skills_into_distinct_workspaces() -> None:
    deploy_script = (ROOT / "scripts" / "deploy" / "macos" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    preflight = (ROOT / "scripts" / "deploy" / "macos" / "preflight.sh").read_text(
        encoding="utf-8"
    )
    template = (ROOT / "scripts" / "deploy" / "macos" / "deploy.local.env.example").read_text(
        encoding="utf-8"
    )

    for token in (
        "MUJITASK_TIKTOK_SKILLS_DIR",
        "MUJITASK_AMAZON_SKILLS_DIR",
        "MUJITASK_TIKTOK_OPENCLAW_AGENT_ID",
        "MUJITASK_AMAZON_OPENCLAW_AGENT_ID",
        "MUJITASK_AMAZON_FEISHU_ACCOUNT_ID",
    ):
        assert token in template
    assert "install_amazon_agent_skill" in deploy_script
    assert 'skills/mujitask-tiktok-feishu-sync' in deploy_script
    assert 'skills/mujitask-amazon-feishu-sync' in deploy_script
    assert 'require_config_value MUJITASK_TIKTOK_SKILLS_DIR' in preflight
    assert 'require_config_value MUJITASK_AMAZON_SKILLS_DIR' in preflight


def test_smoke_check_requires_current_public_tasks() -> None:
    common_script = ROOT / "examples" / "openclaw" / "openclaw_deploy_common.sh"
    text = common_script.read_text(encoding="utf-8")

    for task_name in (
        "refresh_competitor_row_by_url",
        "refresh_current_competitor_table",
        "search_keyword_competitor_products",
        "search_keyword_selection_products",
        "sync_tk_influencer_pool",
        "tiktok_fastmoss_product_ingest",
        "tiktok_influencer_outreach_sync",
    ):
        assert f'"{task_name}"' in text
    assert '"feishu_single_row_update"' not in text
    assert '"fastmoss_keyword_candidate_discovery"' not in text


def test_smoke_check_uses_launchctl_print_for_daemon_labels() -> None:
    common_script = ROOT / "examples" / "openclaw" / "openclaw_deploy_common.sh"
    text = common_script.read_text(encoding="utf-8")

    assert 'launchctl print "gui/${launchd_uid}/${launchd_label}"' in text
    assert "launchctl list | grep -q" not in text
    assert '"com.happyzhao.mujitask.watchdog"' in text


def test_smoke_check_runtime_tables_follow_current_schema() -> None:
    common_script = ROOT / "examples" / "openclaw" / "openclaw_deploy_common.sh"
    text = common_script.read_text(encoding="utf-8")

    for table_name in (
        "task_request",
        "task_execution",
        "api_worker_job",
        "notification_outbox",
        "artifact_object",
        "fastmoss_session_cookie_cache",
        "tk_videos",
        "tk_video_product_relations",
        "tk_video_metric_snapshots",
    ):
        assert f'"{table_name}"' in text
    assert '"entity_registry"' not in text
    assert '"external_binding"' not in text
    assert '"entity_snapshot"' not in text
