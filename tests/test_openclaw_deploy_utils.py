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
        "TABLE_URL=https://old.example\n"
        "BROWSER_PROFILE_REF=profile-a\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n",
        encoding="utf-8",
    )

    MODULE.merge_key_value_file(
        env_file,
        {
            "INSTALL_DIR": "/new/install",
            "TABLE_URL": "https://new.example",
            "FEISHU_ACCESS_TOKEN": "token-new",
        },
    )

    assert env_file.read_text(encoding="utf-8") == (
        "# existing config\n"
        "INSTALL_DIR=/new/install\n"
        "TABLE_URL=https://new.example\n"
        "BROWSER_PROFILE_REF=profile-a\n"
        "FASTMOSS_PHONE=13800000000\n"
        "UNKNOWN_KEY=keep-me\n"
        "FEISHU_ACCESS_TOKEN=token-new\n"
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


def test_deploy_script_embeds_single_file_payloads() -> None:
    deploy_script = ROOT / "examples" / "openclaw" / "deploy-openclaw.sh"
    text = deploy_script.read_text(encoding="utf-8")

    assert "load_openclaw_common" in text
    assert ": <<'__OPENCLAW_DEPLOY_COMMON__'" in text
    assert ": <<'__OPENCLAW_DEPLOY_UTILS__'" in text
    assert "install_framework_from_pyproject" in text
    assert 'add_parser("read-framework-dependency")' in text
