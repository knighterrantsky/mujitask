from __future__ import annotations

import ast
import importlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ARCH_DOC = REPO_ROOT / "docs" / "arch" / "project-architecture-contract.md"
PACKAGE_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
DOMAIN_ROOT = REPO_ROOT / "src" / "automation_business_scaffold" / "domains" / "tiktok"


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
    assert PROJECT_ARCH_DOC.exists()

    doc = _read(PROJECT_ARCH_DOC)
    readme = _read(REPO_ROOT / "README.md")
    arch_index = _read(REPO_ROOT / "docs" / "arch" / "README.md")
    dev_index = _read(REPO_ROOT / "docs" / "dev" / "README.md")
    doc_policy = _read(REPO_ROOT / "docs" / "dev" / "documentation-change-policy.md")

    required_refs = (
        "project-architecture-contract.md",
        "项目架构契约",
    )
    assert required_refs[0] in readme
    assert required_refs[0] in arch_index
    assert required_refs[0] in dev_index
    assert required_refs[0] in doc_policy
    assert required_refs[1] in arch_index

    required_tokens = (
        "状态: 受控项目架构契约",
        "当前正式项目工程组织方式",
        "apps/",
        "control_plane/",
        "domains/",
        "capabilities/",
        "infrastructure/",
        "contracts/",
        "config/",
        "skills/",
        "scripts/",
        "运行控制面 / 业务编排层 / 集成能力层 / 基础设施层 / 部署产物层",
        "`apps/**`",
        "`control_plane/**`",
        "`domains/{business_domain}/**`",
        "`capabilities/**`",
        "`infrastructure/**`",
        "`config/**`",
        "`skills/**`",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "project architecture contract is missing tokens:\n" + "\n".join(missing)


def test_workflow_development_contract() -> None:
    doc = _read(PROJECT_ARCH_DOC)

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
        "skills/{skill_code}",
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
    doc = _read(PROJECT_ARCH_DOC)

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
        "src/automation_business_scaffold/apps/rpc_agent/server.py",
        "src/automation_business_scaffold/apps/cli/main.py",
        "src/automation_business_scaffold/apps/daemons/executor/main.py",
        "src/automation_business_scaffold/apps/daemons/api_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/browser_worker/main.py",
        "src/automation_business_scaffold/apps/daemons/outbox/main.py",
        "src/automation_business_scaffold/apps/daemons/watchdog/main.py",
        "src/automation_business_scaffold/apps/daemons/reconciler/main.py",
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
    doc = _read(PROJECT_ARCH_DOC)

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
        "外部 API 节流",
        "FastMoss、Feishu、TikTok request、Webhook 的 request pacing",
        "`infrastructure/rate_limit/` + provider client",
        "默认区间为 `0.5s` 到 `1.0s`",
        "默认值必须能通过 runtime config / env 覆盖",
        "`MUJITASK_API_REQUEST_MIN_DELAY_SECONDS`",
        "`MUJITASK_API_REQUEST_MAX_DELAY_SECONDS`",
        "capabilities/input_sources/{source}/",
        "capabilities/fact_sources/{source}/",
        "capabilities/persistence/database/",
        "capabilities/persistence/object_storage/",
        "capabilities/channels/{channel}/",
        "禁止在 capability handler 中写业务域专属筛选、字段投影或终态判定",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "capability boundary contract is missing tokens:\n" + "\n".join(missing)


def test_real_migration_contract() -> None:
    doc = _read(PROJECT_ARCH_DOC)

    required_tokens = (
        "## 9. 真实迁移验收口径",
        "`scaffold`",
        "`real_migration`",
        "facade、shim、re-export、`sys.modules` alias",
        "capabilities/_implementations/*.py",
        "不能只 `from .implementations import xxx_handler`",
        "domains/{domain}/**",
        "不能只 re-export `business/**`",
        "旧代码只作为功能验证参考",
        "跑通旧测试",
        "不是 `real_migration` 完成标准",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "real migration contract is missing tokens:\n" + "\n".join(missing)


def test_project_capability_real_implementation_files_exist() -> None:
    required_files = (
        "capabilities/input_sources/feishu/table_read_handler.py",
        "capabilities/fact_sources/tiktok/product_request_fetch_handler.py",
        "capabilities/fact_sources/fastmoss/product_search_handler.py",
        "capabilities/fact_sources/fastmoss/product_fetch_handler.py",
        "capabilities/fact_sources/fastmoss/creator_fetch_handler.py",
        "capabilities/fact_sources/fastmoss/shop_fetch_handler.py",
        "capabilities/fact_sources/fastmoss/video_fetch_handler.py",
        "capabilities/persistence/database/fact_bundle_upsert_handler.py",
        "capabilities/channels/feishu/table_write_handler.py",
        "capabilities/channels/outbox/message_dispatch_handler.py",
        "capabilities/browser/tiktok_product_fetch_handler.py",
        "capabilities/media/asset_sync_handler.py",
    )

    missing = [path for path in required_files if not (PACKAGE_ROOT / path).is_file()]
    assert missing == [], "project capability implementation files are missing:\n" + "\n".join(missing)


def test_project_capability_files_must_own_real_implementations() -> None:
    capability_files = (
        PACKAGE_ROOT / "capabilities/input_sources/feishu/table_read_handler.py",
        PACKAGE_ROOT / "capabilities/fact_sources/tiktok/product_request_fetch_handler.py",
        PACKAGE_ROOT / "capabilities/fact_sources/fastmoss/product_search_handler.py",
        PACKAGE_ROOT / "capabilities/fact_sources/fastmoss/product_fetch_handler.py",
        PACKAGE_ROOT / "capabilities/fact_sources/fastmoss/creator_fetch_handler.py",
        PACKAGE_ROOT / "capabilities/persistence/database/fact_bundle_upsert_handler.py",
        PACKAGE_ROOT / "capabilities/channels/feishu/table_write_handler.py",
        PACKAGE_ROOT / "capabilities/channels/outbox/message_dispatch_handler.py",
        PACKAGE_ROOT / "capabilities/browser/tiktok_product_fetch_handler.py",
        PACKAGE_ROOT / "capabilities/media/asset_sync_handler.py",
    )
    forbidden_fragments = (
        "facade",
        "from .implementations import",
        "from ..implementations import",
        "from automation_business_scaffold.capabilities._implementations",
        "from automation_business_scaffold.business.handlers",
        "sys.modules[__name__]",
    )

    violations: list[str] = []
    for path in capability_files:
        source = _read(path)
        found = [fragment for fragment in forbidden_fragments if fragment in source]
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)}: forbidden {', '.join(found)}")
        if "def " not in source and "class " not in source:
            violations.append(f"{path.relative_to(REPO_ROOT)}: no real implementation function/class")

    implementation_aggregator = PACKAGE_ROOT / "capabilities" / "_implementations"
    if implementation_aggregator.exists():
        violations.append("src/automation_business_scaffold/capabilities/_implementations must not exist after real_migration")

    assert violations == [], "project capability files must own real implementations:\n" + "\n".join(violations)


def test_project_contract_modules_exist() -> None:
    required_modules = (
        "automation_business_scaffold.contracts.runtime",
        "automation_business_scaffold.contracts.workflow",
        "automation_business_scaffold.contracts.handler",
        "automation_business_scaffold.contracts.config",
        "automation_business_scaffold.contracts.outbox",
    )

    for module_name in required_modules:
        module = importlib.import_module(module_name)
        assert module.__all__


def test_agent_artifact_boundary() -> None:
    doc = _read(PROJECT_ARCH_DOC)

    required_tokens = (
        "skills/{skill_code}/",
        "OpenClaw / Hermes / 用户 agent workspace",
        "只提交 `task_request`",
        "不消费 runtime job",
        "Agent script / skills 是部署产物源",
        "部署时复制到用户 agent workspace 并生成配置文件",
        "禁止让 agent skill 直接消费 `api_worker_job`、`task_execution` 或 `notification_outbox`",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "agent artifact boundary contract is missing tokens:\n" + "\n".join(missing)


def test_tiktok_domain_structure() -> None:
    required_dirs = (
        "tasks",
        "workflows",
        "jobs",
        "flows",
        "mappers",
        "projections",
        "policies",
    )
    missing_dirs = [path for path in required_dirs if not (DOMAIN_ROOT / path).is_dir()]
    assert missing_dirs == [], "tiktok domain dirs are missing:\n" + "\n".join(missing_dirs)

    required_files = (
        "tasks/refresh_competitor_row_by_url.py",
        "tasks/refresh_current_competitor_table.py",
        "tasks/search_keyword_competitor_products.py",
        "tasks/sync_tk_influencer_pool.py",
        "tasks/tiktok_fastmoss_product_ingest.py",
        "workflows/refresh_competitor_row_by_url.py",
        "workflows/refresh_current_competitor_table.py",
        "workflows/search_keyword_competitor_products.py",
        "workflows/sync_tk_influencer_pool.py",
        "workflows/tiktok_fastmoss_product_ingest.py",
        "jobs/feishu_table_read.py",
        "jobs/feishu_table_write.py",
        "jobs/tiktok_product_request_fetch.py",
        "jobs/fastmoss_product_fetch.py",
        "mappers/feishu_competitor_row_mapper.py",
        "projections/feishu_competitor_projection.py",
        "policies/workflow_policies.py",
    )
    missing_files = [path for path in required_files if not (DOMAIN_ROOT / path).is_file()]
    assert missing_files == [], "tiktok domain files are missing:\n" + "\n".join(
        missing_files
    )


def test_tiktok_domain_files_must_not_reexport_business_modules() -> None:
    domain_files = [
        path
        for path in DOMAIN_ROOT.rglob("*.py")
        if path.name != "__init__.py"
    ]
    forbidden_fragments = (
        "from automation_business_scaffold.business import",
        "from automation_business_scaffold.business.",
        "import automation_business_scaffold.business.",
        "facade",
        "re-export",
        "reexport",
    )

    violations: list[str] = []
    for path in domain_files:
        source = _read(path)
        found = [fragment for fragment in forbidden_fragments if fragment in source]
        if found:
            violations.append(f"{path.relative_to(REPO_ROOT)}: forbidden {', '.join(found)}")
        if "def " not in source and "class " not in source and "JOB_DEFINITION" not in source:
            violations.append(f"{path.relative_to(REPO_ROOT)}: no domain implementation content")

    assert violations == [], "domain files must own real business implementation:\n" + "\n".join(
        violations
    )
