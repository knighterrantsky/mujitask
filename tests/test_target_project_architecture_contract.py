from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_ARCH_DOC = REPO_ROOT / "docs" / "arch" / "target-project-architecture-contract.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_project_architecture_contract() -> None:
    assert TARGET_ARCH_DOC.exists()

    doc = _read(TARGET_ARCH_DOC)
    readme = _read(REPO_ROOT / "README.md")
    arch_index = _read(REPO_ROOT / "docs" / "arch" / "README.md")
    dev_index = _read(REPO_ROOT / "docs" / "dev" / "README.md")
    doc_policy = _read(REPO_ROOT / "docs" / "dev" / "documentation-change-policy.md")

    required_refs = (
        "target-project-architecture-contract.md",
        "目标项目架构契约",
    )
    assert required_refs[0] in readme
    assert required_refs[0] in arch_index
    assert required_refs[0] in dev_index
    assert required_refs[0] in doc_policy
    assert required_refs[1] in arch_index

    required_tokens = (
        "状态: 受控目标架构契约",
        "不以当前工程结构为合理性依据",
        "apps/",
        "control_plane/",
        "domains/",
        "capabilities/",
        "infrastructure/",
        "contracts/",
        "configs/",
        "agent_artifacts/",
        "scripts/",
        "运行控制面 / 业务编排层 / 集成能力层 / 基础设施层 / 部署产物层",
        "`apps/**`",
        "`control_plane/**`",
        "`domains/{business_domain}/**`",
        "`capabilities/**`",
        "`infrastructure/**`",
        "`configs/**`",
        "`agent_artifacts/**`",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "target architecture contract is missing tokens:\n" + "\n".join(missing)


def test_workflow_development_contract() -> None:
    doc = _read(TARGET_ARCH_DOC)

    required_tokens = (
        "## 6. Workflow 开发契约",
        "1. Agent 配置",
        "2. Task Request 入口",
        "3. Workflow 编排",
        "4. Job Contract",
        "5. 输入数据源",
        "6. 事实数据源",
        "7. 数据库与文件存储",
        "8. 输出通道",
        "9. 运行控制",
        "agent_artifacts/skills/{skill_code}",
        "domains/{domain}/tasks/{task_code}",
        "domains/{domain}/workflows/{workflow_code}",
        "domains/{domain}/jobs/{job_code}",
        "domains/{domain}/mappers",
        "domains/{domain}/projections",
        "domains/{domain}/policies",
        "capabilities/input_sources/{source}",
        "capabilities/fact_sources/{source}",
        "capabilities/persistence",
        "capabilities/channels/{channel}",
        "control_plane/task_requests",
        "outbox",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "workflow development contract is missing tokens:\n" + "\n".join(missing)


def test_control_plane_boundary() -> None:
    doc = _read(TARGET_ARCH_DOC)

    required_tokens = (
        "RPC Agent Service",
        "Daemon",
        "Task Request",
        "Executor",
        "Execution Supervisor",
        "Reconciler",
        "Watchdog",
        "Outbox 调度",
        "Project Configuration",
        "禁止在 `apps/**` 中导入 domain mapper、projection、policy",
        "禁止在 `control_plane/**` 中写 Feishu 表字段",
        "禁止为单个业务新增专用 daemon、专用 Watchdog、专用 Reconciler 或专用 Execution Supervisor",
    )
    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "control plane boundary contract is missing tokens:\n" + "\n".join(missing)

    app_entrypoints = (
        "src/automation_business_scaffold/agent.py",
        "src/automation_business_scaffold/cli.py",
        "src/automation_business_scaffold/executor_daemon.py",
        "src/automation_business_scaffold/api_worker_daemon.py",
        "src/automation_business_scaffold/browser_runloop.py",
        "src/automation_business_scaffold/outbox_dispatcher.py",
        "src/automation_business_scaffold/watchdog_scanner.py",
    )
    banned_import_fragments = (
        ".mappers",
        ".projections",
        ".policies",
        "projection_mapper",
        "source_adapter",
        "feishu_common",
    )

    violations: list[str] = []
    for relative_path in app_entrypoints:
        path = REPO_ROOT / relative_path
        for module in _imported_modules(path):
            if any(fragment in module for fragment in banned_import_fragments):
                violations.append(f"{relative_path}: {module}")

    assert violations == [], "app/control-plane entrypoints import business customization modules:\n" + "\n".join(
        violations
    )


def test_capability_boundary() -> None:
    doc = _read(TARGET_ARCH_DOC)

    required_tokens = (
        "## 5. 外部系统分类",
        "输入数据源",
        "飞书表格",
        "钉钉表格",
        "事实数据源",
        "TikTok",
        "FastMoss",
        "AWS",
        "数据库",
        "Runtime DB",
        "Fact DB",
        "文件存储",
        "MinIO",
        "S3",
        "消息通道",
        "飞书",
        "钉钉",
        "Discord",
        "capabilities/input_sources/{source}/",
        "capabilities/fact_sources/{source}/",
        "capabilities/persistence/database/",
        "capabilities/persistence/object_storage/",
        "capabilities/channels/{channel}/",
        "禁止在 capability handler 中写业务域专属筛选、字段投影或终态判定",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "capability boundary contract is missing tokens:\n" + "\n".join(missing)


def test_agent_artifact_boundary() -> None:
    doc = _read(TARGET_ARCH_DOC)

    required_tokens = (
        "agent_artifacts/skills/{skill_code}/",
        "OpenClaw / Hermes / 用户 agent workspace",
        "只提交 `task_request`",
        "不消费 runtime job",
        "Agent script / skills 是部署产物源",
        "部署时复制到用户 agent workspace 并生成配置文件",
        "禁止让 agent skill 直接消费 `api_worker_job`、`task_execution` 或 `notification_outbox`",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "agent artifact boundary contract is missing tokens:\n" + "\n".join(missing)
