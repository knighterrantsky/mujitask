from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONTROL_DOC = REPO_ROOT / "docs" / "arch" / "runtime-control-plane-contract.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_runtime_control_plane_contract_is_indexed() -> None:
    assert RUNTIME_CONTROL_DOC.exists()

    readme = _read(REPO_ROOT / "README.md")
    arch_index = _read(REPO_ROOT / "docs" / "arch" / "README.md")
    dev_index = _read(REPO_ROOT / "docs" / "dev" / "README.md")
    project_structure = _read(REPO_ROOT / "docs" / "arch" / "project-structure-contract.md")
    doc_policy = _read(REPO_ROOT / "docs" / "dev" / "documentation-change-policy.md")

    required_ref = "runtime-control-plane-contract.md"
    assert required_ref in readme
    assert required_ref in arch_index
    assert required_ref in project_structure
    assert required_ref in doc_policy


def test_runtime_control_plane_contract_freezes_core_boundaries() -> None:
    doc = _read(RUNTIME_CONTROL_DOC)

    required_tokens = (
        "状态: 受控架构契约",
        "RPC Agent Service",
        "Task Request Entry",
        "Daemon Entry",
        "Execution Supervisor",
        "Reconciler",
        "Watchdog",
        "Project Configuration",
        "Launchd Deployment",
        "src/automation_business_scaffold/apps/rpc_agent/server.py",
        "src/automation_business_scaffold/apps/cli/main.py",
        "src/automation_business_scaffold/apps/daemons/executor/main.py",
        "src/automation_business_scaffold/apps/daemons/api_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/browser_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/outbox/main.py",
        "src/automation_business_scaffold/apps/daemons/watchdog/main.py",
        "control_plane/task_requests/",
        "control_plane/executor/runner.py",
        "control_plane/reconciler/views.py",
        "control_plane/supervisor/execution_supervisor.py",
        "control_plane/watchdog/scanner.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "scripts/execution_control/executor.local.env",
        "skills/{skill_code}/skill.local.env",
        ".env",
        "过滤 skill env 中的运行资源残留",
        "不得提供 Runtime DB、Fact DB、Object Store 或 browser profile 配置",
        "browser profile 属于项目运行资源",
        "CLI 参数 > 环境变量 > executor.local.env > skill.local.env > .env",
        "automation-business-scaffold-agent",
        "automation-business-scaffold-executor",
        "automation-business-scaffold-api-worker",
        "automation-business-scaffold-browser-runloop",
        "automation-business-scaffold-outbox-dispatcher",
        "automation-business-scaffold-watchdog",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "runtime control plane contract is missing required tokens:\n" + "\n".join(missing)


def test_runtime_control_plane_entrypoint_files_exist() -> None:
    required_files = (
        "src/automation_business_scaffold/apps/rpc_agent/server.py",
        "src/automation_business_scaffold/apps/cli/main.py",
        "src/automation_business_scaffold/apps/daemons/executor/main.py",
        "src/automation_business_scaffold/apps/daemons/api_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/browser_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/outbox/main.py",
        "src/automation_business_scaffold/apps/daemons/watchdog/main.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "src/automation_business_scaffold/control_plane/task_requests/submit.py",
        "src/automation_business_scaffold/control_plane/task_requests/status.py",
        "src/automation_business_scaffold/control_plane/task_requests/result.py",
        "src/automation_business_scaffold/control_plane/executor/runner.py",
        "src/automation_business_scaffold/control_plane/reconciler/views.py",
        "src/automation_business_scaffold/control_plane/supervisor/execution_supervisor.py",
        "src/automation_business_scaffold/control_plane/watchdog/scanner.py",
        "scripts/execution_control/executor.local.env.example",
        "scripts/execution_control/install_launch_agents.sh",
        "scripts/execution_control/run_launchd_agent.sh",
        "skills/mujitask-tiktok-feishu-sync/skill.local.env.example",
        ".env.example",
        "config/deployment/launchd/com.happyzhao.mujitask.executor-daemon.plist.template",
        "config/deployment/launchd/com.happyzhao.mujitask.api-worker.plist.template",
        "config/deployment/launchd/com.happyzhao.mujitask.browser-runloop.plist.template",
        "config/deployment/launchd/com.happyzhao.mujitask.outbox-dispatcher.plist.template",
    )

    missing = [path for path in required_files if not (REPO_ROOT / path).is_file()]
    assert missing == [], "runtime control plane files are missing:\n" + "\n".join(missing)


def test_launchd_wrapper_exposes_homebrew_node_path_to_daemons() -> None:
    wrapper = _read(REPO_ROOT / "scripts" / "execution_control" / "run_launchd_agent.sh")

    assert "export PATH=" in wrapper
    assert "/opt/homebrew/bin" in wrapper
    assert "/usr/local/bin" in wrapper


def test_runtime_control_plane_console_scripts_are_declared() -> None:
    pyproject = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))
    scripts = pyproject["project"]["scripts"]

    expected_scripts = {
        "automation-business-scaffold-agent": "automation_business_scaffold.apps.rpc_agent.server:main",
        "automation-business-scaffold-run": "automation_business_scaffold.apps.cli.main:main",
        "automation-business-scaffold-executor": "automation_business_scaffold.apps.daemons.executor.main:main",
        "automation-business-scaffold-api-worker": "automation_business_scaffold.apps.daemons.api_worker.main:main",
        "automation-business-scaffold-browser-runloop": "automation_business_scaffold.apps.daemons.browser_worker.main:main",
        "automation-business-scaffold-outbox-dispatcher": "automation_business_scaffold.apps.daemons.outbox.main:main",
        "automation-business-scaffold-watchdog": "automation_business_scaffold.apps.daemons.watchdog.main:main",
    }

    assert {name: scripts.get(name) for name in expected_scripts} == expected_scripts


def test_browser_runloop_launchd_uses_child_process_supervision() -> None:
    browser_plist = _read(
        REPO_ROOT / "config" / "deployment" / "launchd" / "com.happyzhao.mujitask.browser-runloop.plist.template"
    )
    api_worker_plist = _read(
        REPO_ROOT / "config" / "deployment" / "launchd" / "com.happyzhao.mujitask.api-worker.plist.template"
    )

    assert "automation_business_scaffold.apps.daemons.browser_worker.main" in browser_plist
    assert "<string>--supervisor-mode</string>" in browser_plist
    assert "<string>child_process</string>" in browser_plist
    assert "<string>inline</string>" not in browser_plist
    assert "automation_business_scaffold.apps.daemons.api_worker.main" in api_worker_plist
    assert "<string>inline</string>" in api_worker_plist


def test_project_configuration_contract_matches_loader_files() -> None:
    project_env = _read(REPO_ROOT / "src" / "automation_business_scaffold" / "project_env.py")
    config = _read(REPO_ROOT / "src" / "automation_business_scaffold" / "config.py")

    required_loader_tokens = (
        "scripts/execution_control/executor.local.env",
        "skills/mujitask-tiktok-feishu-sync/skill.local.env",
        ".env",
        "override: bool = False",
    )
    missing_loader_tokens = [token for token in required_loader_tokens if token not in project_env]
    assert missing_loader_tokens == [], "project env loader is missing tokens:\n" + "\n".join(missing_loader_tokens)

    required_config_tokens = (
        "BusinessDefaults",
        "ExecutionControlDefaults",
        "get_execution_control_defaults",
        "BUSINESS_EXECUTION_CONTROL_",
        "EXECUTION_CONTROL_",
    )
    missing_config_tokens = [token for token in required_config_tokens if token not in config]
    assert missing_config_tokens == [], "typed config is missing tokens:\n" + "\n".join(missing_config_tokens)
