from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok"
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
        "domains/{domain}/tasks/{task_code}.py",
        "domains/{domain}/workflows/{workflow_code}.py",
        "domains/{domain}/jobs/{job_code}.py",
        "domains/{domain}/mappers/{mapper_module}.py",
        "domains/{domain}/flows/** 业务实现细节",
        "capabilities/{capability_role}/{system}/{handler_code}_handler.py",
        "control_plane/executor/runner.py",
        "control_plane/supervisor/execution_supervisor.py",
        "control_plane/reconciler/views.py",
        "control_plane/watchdog/scanner.py",
        "src/automation_business_scaffold/project_env.py",
        "src/automation_business_scaffold/config.py",
        "skills/{skill_code}/SKILL.md",
        "MUJITASK_SKILLS_DIR/{skill_code}",
        "skill.local.env",
        "executor.local.env",
        "domains/{domain}/tasks/",
        "domains/{domain}/workflows/",
        "domains/{domain}/jobs/",
        "domains/{domain}/mappers/",
        "domains/{domain}/projections/",
        "domains/{domain}/policies/",
        "domains/{domain}/flows/",
        "capabilities/input_sources/",
        "capabilities/fact_sources/",
        "capabilities/channels/",
        "control_plane/",
        "business/**/achieve/",
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


def test_current_domain_core_structure_directories_exist() -> None:
    required_dirs = (
        "tasks",
        "workflows",
        "jobs",
        "mappers",
        "projections",
        "policies",
        "flows",
    )

    missing = [path for path in required_dirs if not (DOMAIN_ROOT / path).is_dir()]
    assert missing == [], "domain core structure directories are missing:\n" + "\n".join(missing)


def test_agent_skill_bundle_source_exists() -> None:
    skill_code = "mujitask-tiktok-feishu-sync"
    skill_root = SKILLS_ROOT / skill_code
    required_files = (
        "SKILL.md",
        "skill.local.env.example",
        "run_refresh_current_competitor_table_step.sh",
        "run_competitor_row_by_url_step.sh",
        "run_product_url_complete_step.sh",
        "run_keyword_search_step.sh",
        "run_influencer_pool_sync_step.sh",
        "run_skill_step.py",
        "lightweight_submit.py",
    )

    missing = [path for path in required_files if not (skill_root / path).is_file()]
    assert missing == [], f"{skill_code} skill bundle is missing files:\n" + "\n".join(missing)


def test_domain_structure_entrypoint_files_exist() -> None:
    required_files = (
        "tasks/refresh_current_competitor_table.py",
        "tasks/search_keyword_competitor_products.py",
        "tasks/sync_tk_influencer_pool.py",
        "tasks/tiktok_fastmoss_product_ingest.py",
        "workflows/refresh_current_competitor_table.py",
        "workflows/search_keyword_competitor_products.py",
        "workflows/sync_tk_influencer_pool.py",
        "workflows/tiktok_fastmoss_product_ingest.py",
        "jobs/competitor_row_refresh.py",
        "jobs/feishu_table_read.py",
        "jobs/feishu_table_write.py",
        "mappers/registry.py",
        "projections/registry.py",
    )

    missing = [path for path in required_files if not (DOMAIN_ROOT / path).is_file()]
    assert missing == [], "domain structure entrypoint files are missing:\n" + "\n".join(missing)
