from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_ARCH_DOC = REPO_ROOT / "docs" / "arch" / "system-architecture-design.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_system_architecture_uses_layered_structure() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "状态: 系统架构设计基准",
        "Agent Artifact / Entry Layer",
        "Runtime Control Plane",
        "Domain Orchestration Layer",
        "Capability Layer",
        "Infrastructure Layer",
        "Deployment / Configuration Layer",
        "skills",
        "control_plane",
        "domains",
        "capabilities",
        "infrastructure",
        "config",
        "project-architecture-contract.md",
        "project-structure-contract.md",
        "runtime-control-plane-contract.md",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "system architecture is missing layered tokens:\n" + "\n".join(
        missing
    )


def test_system_architecture_maps_project_files_to_project_layers() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "## 3. 项目落位到分层的映射",
        "skills/mujitask-tiktok-feishu-sync/",
        "`src/automation_business_scaffold/apps/rpc_agent/`",
        "`src/automation_business_scaffold/apps/cli/`",
        "`src/automation_business_scaffold/control_plane/`",
        "`src/automation_business_scaffold/apps/daemons/`",
        "`src/automation_business_scaffold/domains/tiktok/tasks/`",
        "`workflows/`",
        "`jobs/`",
        "`flows/`",
        "`mappers/`",
        "`projections/`",
        "`policies/`",
        "`src/automation_business_scaffold/capabilities/input_sources/`",
        "`fact_sources/`",
        "`persistence/`",
        "`channels/`",
        "`browser/`",
        "`media/`",
        "`infrastructure/feishu/`",
        "`infrastructure/fastmoss/`",
        "`infrastructure/runtime/`",
        "`infrastructure/facts/`",
        "`infrastructure/artifacts/`",
        "`scripts/execution_control/`",
        "`config/deployment/`",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "system architecture mapping is missing tokens:\n" + "\n".join(
        missing
    )


def test_system_architecture_defines_business_entry_split() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "## 4. 业务进入系统后的标准拆分",
        "### 4.1 Agent 配置",
        "### 4.2 Task Request 与入口协议",
        "### 4.3 消息通道和 Outbox 配置",
        "### 4.4 输入数据源、事实数据源、存储拆分",
        "### 4.5 数据映射和定制逻辑",
        "飞书表",
        "钉钉表格",
        "TikTok",
        "FastMoss",
        "AWS",
        "Runtime DB",
        "Fact DB",
        "MinIO",
        "S3",
        "Discord",
        "domains/{domain}/mappers",
        "domains/{domain}/projections",
        "domains/{domain}/policies",
        "capabilities/channels/{channel}",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "business entry split is missing tokens:\n" + "\n".join(missing)


def test_system_architecture_freezes_runtime_control_components() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "## 5. Runtime Control Plane",
        "RPC Agent Service",
        "Task Request Entry",
        "Executor",
        "API Worker Daemon",
        "Browser Runloop",
        "Execution Supervisor",
        "Reconciler",
        "Watchdog",
        "Outbox Dispatcher",
        "Runtime Config",
        "apps/rpc_agent/server.py",
        "control_plane/executor/runner.py",
        "control_plane/supervisor/execution_supervisor.py",
        "不为单个业务新增专用 daemon",
        "不在 control plane 写 Feishu 表字段",
        "不让 workflow 绕过 outbox",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "runtime control plane section is missing tokens:\n" + "\n".join(
        missing
    )


def test_system_architecture_classifies_capabilities_and_storage() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "## 7. Capability Layer",
        "Input Sources / Feishu",
        "Input Sources / Dingding Sheet",
        "Fact Sources / TikTok",
        "Fact Sources / FastMoss",
        "Fact Sources / AWS",
        "Persistence / Database",
        "Persistence / Object Storage / Media",
        "Channels / Feishu",
        "Channels / Dingding / Discord",
        "Browser / CDP / Profile",
        "Runtime DB",
        "Fact DB",
        "Object Store",
        "Feishu / Dingding 表格",
        "Discord / Feishu / Dingding 消息",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "capability/storage classification is missing tokens:\n" + "\n".join(
        missing
    )


def test_system_architecture_defines_configurable_external_api_pacing() -> None:
    doc = _read(SYSTEM_ARCH_DOC)

    required_tokens = (
        "外部 API 节流配置",
        "`infrastructure/rate_limit/` 是外部 API 请求节流的唯一实现 owner",
        "默认区间为 `0.5s` 到 `1.0s`",
        "默认区间必须可配置",
        "job payload 显式覆盖 > provider 专用环境变量 > 全局环境变量 > 系统默认值",
        "`MUJITASK_API_REQUEST_MIN_DELAY_SECONDS`",
        "`MUJITASK_API_REQUEST_MAX_DELAY_SECONDS`",
        "`MUJITASK_FASTMOSS_API_REQUEST_MIN_DELAY_SECONDS`",
        "`MUJITASK_FEISHU_API_REQUEST_MIN_DELAY_SECONDS`",
        "`MUJITASK_TIKTOK_API_REQUEST_MIN_DELAY_SECONDS`",
        "当前正式 workflow 覆盖范围包括 `refresh_current_competitor_table`、`search_keyword_competitor_products`、`sync_tk_influencer_pool`、`tiktok_fastmoss_product_ingest`",
        "media 远程素材下载",
        "飞书附件远程图片下载",
        "允许不走 request pacing 的路径只限于非外部 API 的本地 IO",
        "Playwright/CDP 浏览器交互等待",
        "retry backoff 是错误恢复策略，不替代正常请求 pacing",
        "provider/resource key",
        "delay_seconds",
        "request_started_at",
        "request_finished_at",
    )

    missing = [token for token in required_tokens if token not in doc]
    assert missing == [], "external API pacing design is missing tokens:\n" + "\n".join(missing)
