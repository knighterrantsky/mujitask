from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "business"
SKILLS_ROOT = REPO_ROOT / "skills"
PROJECT_STRUCTURE_DOC = REPO_ROOT / "docs" / "arch" / "project-structure-contract.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_project_structure_contract_is_indexed() -> None:
    assert PROJECT_STRUCTURE_DOC.exists()

    readme = _read(REPO_ROOT / "README.md")
    arch_index = _read(REPO_ROOT / "docs" / "arch" / "README.md")
    dev_index = _read(REPO_ROOT / "docs" / "dev" / "README.md")
    doc_policy = _read(REPO_ROOT / "docs" / "dev" / "documentation-change-policy.md")

    required_refs = (
        "docs/arch/project-structure-contract.md",
        "project-structure-contract.md",
        "runtime-control-plane-contract.md",
    )
    assert required_refs[0] in readme
    assert required_refs[1] in arch_index
    assert required_refs[1] in dev_index
    assert required_refs[1] in doc_policy
    assert required_refs[2] in arch_index
    assert required_refs[2] in doc_policy


def test_project_structure_contract_freezes_core_boundaries() -> None:
    doc = _read(PROJECT_STRUCTURE_DOC)

    required_tokens = (
        "状态: 受控架构契约",
        "## 2. 快速定位路径",
        "## 3. Agent Artifact 边界",
        "## 4. 目录职责契约",
        "## 5. 命名契约",
        "## 6. 新增代码流程",
        "## 7. 测试护栏",
        "Runtime 控制面契约",
        "business/flows/runtime_orchestrator.py",
        "business/flows/execution_supervisor.py",
        "business/flows/runtime_views.py",
        "business/flows/watchdog_scanner.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "skills/{skill_code}/SKILL.md",
        "MUJITASK_SKILLS_DIR/{skill_code}",
        "skill.local.env",
        "executor.local.env",
        "business/tasks/{task_code}.py",
        "business/workflow_defs/{workflow_code}.py",
        "business/jobs/{job_code}.py",
        "business/handlers/{worker_lane}/{handler_code}.py",
        "business/feishu/source_adapters.py",
        "business/feishu/projection_mappers.py",
        "`SKILL.md`、入口脚本、`skill.local.env.example`",
        "`JOB_CODE`、`HANDLER_CODE`、`JOB_DEFINITION`",
        "`HANDLER_CODE`、`CONTRACT`",
        "`daemon_code`",
        "`control_plane_code`",
        "禁止",
        "v1",
        "orchestrate_*",
        "*_adapter",
        "*_mapper",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "project structure contract is missing required tokens:\n" + "\n".join(missing)


def test_business_core_structure_directories_exist() -> None:
    required_dirs = (
        "tasks",
        "workflow_defs",
        "workflows",
        "jobs",
        "handlers",
        "handlers/api",
        "handlers/browser",
        "handlers/outbox",
        "feishu",
        "flows",
    )

    missing = [path for path in required_dirs if not (BUSINESS_ROOT / path).is_dir()]
    assert missing == [], "business core structure directories are missing:\n" + "\n".join(missing)


def test_agent_skill_bundle_source_exists() -> None:
    skill_code = "mujitask-tiktok-feishu-sync"
    skill_root = SKILLS_ROOT / skill_code
    required_files = (
        "SKILL.md",
        "skill.local.env.example",
        "run_refresh_current_competitor_table_step.sh",
        "run_keyword_search_step.sh",
        "run_skill_step.py",
        "lightweight_submit.py",
    )

    missing = [path for path in required_files if not (skill_root / path).is_file()]
    assert missing == [], f"{skill_code} skill bundle is missing files:\n" + "\n".join(missing)


def test_business_structure_entrypoint_files_exist() -> None:
    required_files = (
        "workflow_defs/registry.py",
        "jobs/catalog.py",
        "handlers/allowlist.py",
        "handlers/registry.py",
        "handlers/contract.py",
        "handlers/api/registry.py",
        "handlers/browser/registry.py",
        "handlers/outbox/registry.py",
        "feishu/source_adapters.py",
        "feishu/projection_mappers.py",
    )

    missing = [path for path in required_files if not (BUSINESS_ROOT / path).is_file()]
    assert missing == [], "business structure entrypoint files are missing:\n" + "\n".join(missing)
