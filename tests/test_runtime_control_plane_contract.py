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
    assert required_ref in dev_index
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
        "src/automation_business_scaffold/agent.py",
        "src/automation_business_scaffold/cli.py",
        "src/automation_business_scaffold/executor_daemon.py",
        "src/automation_business_scaffold/api_worker_daemon.py",
        "src/automation_business_scaffold/browser_runloop.py",
        "src/automation_business_scaffold/outbox_dispatcher.py",
        "src/automation_business_scaffold/watchdog_scanner.py",
        "business/flows/runtime_orchestrator.py::submit_task_request",
        "business/flows/runtime_common.py",
        "business/flows/runtime_views.py",
        "business/flows/execution_supervisor.py",
        "business/flows/watchdog_scanner.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "scripts/execution_control/executor.local.env",
        "skills/{skill_code}/skill.local.env",
        ".env",
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
        "src/automation_business_scaffold/agent.py",
        "src/automation_business_scaffold/cli.py",
        "src/automation_business_scaffold/executor_daemon.py",
        "src/automation_business_scaffold/api_worker_daemon.py",
        "src/automation_business_scaffold/browser_runloop.py",
        "src/automation_business_scaffold/outbox_dispatcher.py",
        "src/automation_business_scaffold/watchdog_scanner.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "src/automation_business_scaffold/business/flows/runtime_orchestrator.py",
        "src/automation_business_scaffold/business/flows/runtime_common.py",
        "src/automation_business_scaffold/business/flows/runtime_views.py",
        "src/automation_business_scaffold/business/flows/execution_supervisor.py",
        "src/automation_business_scaffold/business/flows/watchdog_scanner.py",
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


def test_runtime_control_plane_console_scripts_are_declared() -> None:
    pyproject = tomllib.loads(_read(REPO_ROOT / "pyproject.toml"))
    scripts = pyproject["project"]["scripts"]

    expected_scripts = {
        "automation-business-scaffold-agent": "automation_business_scaffold.agent:main",
        "automation-business-scaffold-run": "automation_business_scaffold.cli:main",
        "automation-business-scaffold-executor": "automation_business_scaffold.executor_daemon:main",
        "automation-business-scaffold-api-worker": "automation_business_scaffold.api_worker_daemon:main",
        "automation-business-scaffold-browser-runloop": "automation_business_scaffold.browser_runloop:main",
        "automation-business-scaffold-outbox-dispatcher": "automation_business_scaffold.outbox_dispatcher:main",
        "automation-business-scaffold-watchdog": "automation_business_scaffold.watchdog_scanner:main",
    }

    assert {name: scripts.get(name) for name in expected_scripts} == expected_scripts


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
