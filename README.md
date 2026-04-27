# Mujitask

Mujitask 是当前 TikTok / FastMoss / 飞书自动化业务项目。

它的核心职责是：

- 从 OpenClaw / Skill / CLI 接收业务任务。
- 通过 Runtime DB 编排长流程任务。
- 使用 `executor_daemon`、`api_worker_daemon`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 执行业务和运行时恢复。
- 采集 TikTok / FastMoss 数据，写回飞书业务表，并沉淀事实数据库和运行产物。

当前项目依赖 `automation-framework`，但不在本仓库维护 framework 的接口说明和 contract 文档。framework 的公开接口、运行时契约和升级说明应直接从 `automation-framework` 包或 framework 仓库读取；本仓库 README 只说明 Mujitask 这个业务项目如何部署、运行和维护。

## 1. 先读什么

项目入口：

1. [docs/README.md](./docs/README.md): 全部文档地图。
2. [docs/business/README.md](./docs/business/README.md): 客户需求、业务规则、飞书表口径、验收口径。
3. [docs/arch/README.md](./docs/arch/README.md): 当前系统架构、workflow、Runtime DB、Fact DB、Storage。
4. [docs/ops/README.md](./docs/ops/README.md): 部署、验收、回退和 runbook 归口。
5. [docs/reference/README.md](./docs/reference/README.md): FastMoss / TikTok 等外部接口和研究材料归口。

重要边界：

- 根 `README.md` 是项目入口，不承载详细需求和架构设计。
- `docs/README.md` 是文档总入口。
- `docs/business` 只放需求和业务规则。
- `docs/arch` 是当前架构事实来源。
- 项目工程组织和新增 workflow 拆分方式见 [docs/arch/project-architecture-contract.md](./docs/arch/project-architecture-contract.md)。
- Runtime 控制面归属见 [docs/arch/runtime-control-plane-contract.md](./docs/arch/runtime-control-plane-contract.md)，包括 RPC/CLI/daemon/config/watchdog/supervisor/reconciler/outbox。
- Runtime DB schema、Fact DB schema、workflow contract、handler contract 是受控契约，不能随普通业务代码自由破坏。
- framework contract 不再从本仓库文档读取。

## 2. 当前正式业务入口

当前正式业务入口是顶层 Task，而不是单个 leaf task。

| task_code | 作用 | 执行形态 |
| --- | --- | --- |
| `refresh_current_competitor_table` | 定时刷新当前飞书竞品表 | submit 入队，executor 编排，browser/API worker 执行 |
| `search_keyword_competitor_products` | 根据关键词新增竞品到飞书竞品表 | submit 入队，executor 编排，browser/API worker 执行 |
| `sync_tk_influencer_pool` | 从竞品表扩展达人池 | submit 入队，api worker 消费 product/author/finalizer job |
| `tiktok_fastmoss_product_ingest` | 单商品 TikTok + FastMoss 事实采集 | submit 入队，api worker 采集、上传媒体、写事实库 |

内部 / debug leaf task 仍可直接运行，但不作为客户正式入口：

- `tiktok_product_link_cleanup`
- `feishu_pending_rows_scan`
- `tiktok_feishu_single_sync`
- `fastmoss_login_check`

说明：竞品表刷新和关键词竞品入库的正式重构路径统一通过 `feishu_table_read` / `feishu_table_write` / `fastmoss_product_search` 和商品事实采集 handler 组合完成；关键词只是 `fastmoss_product_search` 的一种输入 filter。

## 3. 推荐部署方式

当前正式部署路径收敛为：

```text
macOS + launchd + Homebrew Postgres + MinIO + Mujitask skill bundle
```

部署前先复制并填写本地部署配置：

```bash
cp scripts/deploy/macos/deploy.local.env.example scripts/deploy/macos/deploy.local.env
```

必须确认这些关键配置：

| 配置 | 说明 |
| --- | --- |
| `MUJITASK_INSTALL_DIR` | 项目安装目录，默认示例为 `$HOME/apps/mujitask` |
| `MUJITASK_SKILLS_DIR` | 部署 agent 读取 skills 的目录 |
| `MUJITASK_TABLE_URL` | 飞书 Base / Table URL |
| `MUJITASK_FEISHU_ACCESS_TOKEN` | 飞书访问 token |
| `MUJITASK_FASTMOSS_PHONE` / `MUJITASK_FASTMOSS_PASSWORD` | FastMoss 登录账号 |
| `MUJITASK_BROWSER_PROFILE_REF` | 浏览器 profile 引用，默认 `roxy-tiktok` |

执行预检和部署：

```bash
bash scripts/deploy/macos/preflight.sh
bash scripts/deploy/macos/deploy.sh
```

部署脚本会完成：

- 创建或更新 `.venv`。
- 安装当前项目依赖和 `automation-framework`。
- 安装并启动本机 Postgres 和 MinIO。
- 生成 `scripts/execution_control/executor.local.env`。
- 安装 `skills/mujitask-tiktok-feishu-sync` 到部署 skills 目录。
- 安装并刷新 5 个 launchd 守护进程。
- 执行 smoke check。

## 4. 本地开发运行

创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m playwright install chromium
```

复制基础配置：

```bash
cp .env.example .env
cp config/browser_profiles.example.json config/browser_profiles.json
```

准备 Runtime / 外部系统连接配置：

```bash
cp scripts/execution_control/executor.local.env.example scripts/execution_control/executor.local.env
cp skills/mujitask-tiktok-feishu-sync/skill.local.env.example \
  skills/mujitask-tiktok-feishu-sync/skill.local.env
```

说明：

- `scripts/execution_control/executor.local.env` 是 Runtime DB、MinIO、worker lease/heartbeat 的主配置入口。
- `skills/mujitask-tiktok-feishu-sync/skill.local.env` 是 skill 固定输入配置入口。
- `.env` 是浏览器、agent、通用本地默认配置入口。
- Python 运行时、CLI、daemon、Alembic 和 pytest 会自动尝试读取这三份文件，不需要每次手工 `source`。

启动本地 agent API：

```bash
uvicorn automation_business_scaffold.apps.rpc_agent.server:app --app-dir src --host 127.0.0.1 --port 8110
```

查看已注册任务：

```bash
curl http://127.0.0.1:8110/tasks
```

也可以直接使用 CLI 查看任务：

```bash
automation-business-scaffold-run list-tasks
```

## 5. 常驻进程

生产和本地 launchd 部署会运行 5 个常驻角色：

| 进程 | console script | 作用 |
| --- | --- | --- |
| executor | `automation-business-scaffold-executor` | 顶层 workflow 编排，拆 job，汇总结果，写 outbox |
| API worker | `automation-business-scaffold-api-worker` | 处理 API/HTTP/飞书/FastMoss/事实入库/媒体上传 job |
| Browser worker | `automation-business-scaffold-browser-runloop` | 串行消费需要浏览器 profile/CDP 的任务 |
| Outbox dispatcher | `automation-business-scaffold-outbox-dispatcher` | 发送最终通知，处理通知重试 |
| Watchdog | `automation-business-scaffold-watchdog` | 扫描 lease 过期、stale progress、timeout、stuck parent 和 outbox timeout |

launchd 安装脚本：

```bash
bash scripts/execution_control/install_launch_agents.sh
```

本地也可以直接运行单进程排障，例如：

```bash
automation-business-scaffold-executor --once
automation-business-scaffold-api-worker --once
automation-business-scaffold-browser-runloop --once
automation-business-scaffold-outbox-dispatcher --once
automation-business-scaffold-watchdog --once
```

## 6. 提交和查询任务

提交达人池同步：

```bash
automation-business-scaffold-run run \
  --task sync_tk_influencer_pool \
  --params-json '{
    "control_action": "submit",
    "table_url": "https://my.feishu.cn/base/appXXX?table=tblSource",
    "target_table_url": "https://my.feishu.cn/base/appXXX?table=tblTarget",
    "access_token_env": "FEISHU_ACCESS_TOKEN",
    "fastmoss_phone_env": "FASTMOSS_PHONE",
    "fastmoss_password_env": "FASTMOSS_PASSWORD"
  }'
```

提交单商品事实采集：

```bash
automation-business-scaffold-run run \
  --task tiktok_fastmoss_product_ingest \
  --params-json '{
    "control_action": "submit",
    "product_url": "https://www.tiktok.com/shop/pdp/1732183068040729370",
    "fastmoss_phone_env": "FASTMOSS_PHONE",
    "fastmoss_password_env": "FASTMOSS_PASSWORD",
    "execution_control_artifact_store_provider": "minio"
  }'
```

查询状态时传入 `request_id`：

```bash
automation-business-scaffold-run run \
  --task sync_tk_influencer_pool \
  --params-json '{
    "control_action": "status",
    "request_id": "replace-with-request-id"
  }'
```

说明：

- `submit` 只负责提交顶层任务。
- 后续推进由 executor 和 worker 完成。
- 最终通知由 outbox dispatcher 完成。
- 不传 `control_action` 的同步直跑模式只用于本地 debug。

## 7. 目录边界

| 路径 | 说明 |
| --- | --- |
| `src/automation_business_scaffold/domains/tiktok/` | 当前 TikTok 业务域实现 owner；包含 task、workflow、job、mapper、projection、policy、flow |
| `src/automation_business_scaffold/capabilities/` | 通用 handler 能力；承载 Feishu、TikTok、FastMoss、media、Fact DB、outbox 等外部能力入口 |
| `src/automation_business_scaffold/control_plane/` | Runtime 控制面；承载 executor、supervisor、reconciler、watchdog、outbox 和 runtime config |
| `src/automation_business_scaffold/contracts/` | 代码包内 handler/runtime/workflow contract model 与实现侧 manifest |
| `src/automation_business_scaffold/business/` | legacy / achieve reference；允许读取理解旧行为，普通新实现不得落回该目录 |
| `src/automation_business_scaffold/infrastructure/` | 飞书、FastMoss、Runtime Store、Fact Store、Artifact Store 等基础设施 |
| `src/automation_business_scaffold/models/` | 运行时和业务模型 |
| `src/automation_business_scaffold/validators/` | 业务参数校验 |
| `contracts/` | 根级字段、状态、workflow、Codex task routing 机器契约 |
| `skills/mujitask-tiktok-feishu-sync/` | 仓库内 agent skill bundle 源；部署时复制到部署 agent workspace/skills 目录并生成配置 |
| `scripts/deploy/macos/` | macOS 一键部署 |
| `scripts/execution_control/` | Runtime DB、daemon、launchd、测试辅助脚本 |
| `docs/` | 项目文档地图 |

治理边界：

- `.platform/` 是平台管理规则，普通业务开发不直接修改。
- `AGENTS.md` 是仓库级协作规则，只有明确的仓库治理变更才修改。
- `docs/dev/rewrite-state.yaml` 定义当前重构阶段和 canonical owner；根目录短 Prompt 的上下文路由见 `contracts/codex/task-routing.yaml`。
- framework 的接口和 contract 以 `automation-framework` 自身文档为准，本仓库不再复制或维护这部分说明。

## 8. 配置边界

业务运行配置主要分三类：

| 配置来源 | 说明 |
| --- | --- |
| `.env` | 本地 agent / browser profile / 通用调试配置 |
| `scripts/deploy/macos/deploy.local.env` | macOS 部署输入配置 |
| `scripts/execution_control/executor.local.env` | Runtime DB、MinIO、lease、heartbeat、worker 等执行控制主配置 |
| `skills/mujitask-tiktok-feishu-sync/skill.local.env` | skill 固定输入配置和兼容 `EXECUTION_CONTROL_*` 参数 |

当前代码自动加载的优先级是：

1. CLI 参数
2. 当前 shell / launchd / CI 显式环境变量
3. `scripts/execution_control/executor.local.env`
4. `skills/mujitask-tiktok-feishu-sync/skill.local.env`
5. `.env`

常见执行控制环境变量：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_OBJECT_PREFIX`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_LEASE_SECONDS`
- `BUSINESS_EXECUTION_CONTROL_HEARTBEAT_INTERVAL_SECONDS`

详细 storage 规则见 [docs/arch/storage-architecture-design.md](./docs/arch/storage-architecture-design.md)。
本地配置规则详见 [docs/dev/project-configuration.md](./docs/dev/project-configuration.md)。

### 8.1 数据库和 Contract 安全边界

生产运行进程不能拥有修改数据库结构的权限。

推荐拆分：

| 账号 | 用途 | 权限 |
| --- | --- | --- |
| `mujitask_runtime_user` | executor / worker / dispatcher / watchdog 正常运行 | `SELECT / INSERT / UPDATE / DELETE` |
| `mujitask_migration_user` | 发布 schema migration | `CREATE / ALTER / DROP / CREATE INDEX` |
| `mujitask_readonly_user` | 排障、报表、只读分析 | `SELECT` |

运行规则：

- Runtime DB schema 和 Fact DB schema 变更必须走 migration。
- 生产 daemon / worker 启动时只做 schema version 校验，版本不匹配应 fail fast。
- 不允许生产任务消费路径自动 `CREATE TABLE`、`ALTER TABLE` 或 `DROP TABLE`。
- workflow / handler payload/result/error contract 需要保持兼容；破坏性变更要通过 `contract_revision`、adapter、migration 或清理旧 job 处理，不能把 `v1` / `v2` 写进稳定 code 名称。

详细规则见 [docs/arch/project-architecture-contract.md](./docs/arch/project-architecture-contract.md)、[docs/arch/project-structure-contract.md](./docs/arch/project-structure-contract.md)、[docs/dev/documentation-change-policy.md](./docs/dev/documentation-change-policy.md)、[docs/arch/workflow-design-guidelines.md](./docs/arch/workflow-design-guidelines.md)、[docs/arch/runtime-db-schema-design.md](./docs/arch/runtime-db-schema-design.md)、[docs/arch/fact-db-schema-design.md](./docs/arch/fact-db-schema-design.md) 和 [docs/arch/handler-contract-design.md](./docs/arch/handler-contract-design.md)。

## 9. 验证

本地 Postgres 相关测试：

```bash
scripts/execution_control/run_local_postgres_tests.sh
```

该脚本会读取 `scripts/execution_control/executor.local.env`。本地开发建议同时配置：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`：运行时数据库，例如 `postgresql+psycopg://mujitask:mujitask@127.0.0.1:5432/automation_business_scaffold`
- `TEST_DATABASE_URL`：测试专用数据库，例如 `postgresql+psycopg://mujitask:mujitask@127.0.0.1:5432/automation_business_scaffold_test`

pytest fixture 会优先使用 `TEST_DATABASE_URL`，并在该数据库内为每次测试创建临时 schema，避免数据库回归测试因为没有 DB URL 被跳过，也避免测试污染运行库。直接用 `psql` 排障时需要把 SQLAlchemy URL 的 `postgresql+psycopg://` 改成 `postgresql://`。

首次配置时先创建测试库：

```bash
createdb -O mujitask automation_business_scaffold_test
```

轻量测试：

```bash
uv run --extra dev pytest
```

不要裸跑 `uv run pytest`。如果当前 `.venv` 没有安装 dev 依赖，`uv` 可能会从 `PATH` 找到 Homebrew 等全局 `pytest`，导致测试进程没有项目 dev extra，进而出现看起来像业务模块导入失败的错误。`pyproject.toml` 同时声明了默认 `dev` dependency group 用作兜底，但仓库文档和脚本仍统一使用 `uv run --extra dev pytest`。

部署后建议至少验证：

- `automation-business-scaffold-run list-tasks` 能列出正式 task。
- 5 个 launchd 守护进程能启动。
- Postgres Runtime 表可连接。
- MinIO bucket 可写入 artifact。
- `refresh_current_competitor_table` 可以 submit 并返回 `request_id`。
- executor / worker 能推进任务状态。
- outbox dispatcher 能处理最终通知。

## 10. Framework 依赖说明

`automation-framework` 由 `pyproject.toml` 管理：

```toml
automation-framework @ git+https://github.com/knighterrantsky/automation-framework.git@v0.3.6
```

升级 framework 时：

1. 在 framework 包或 framework 仓库中查看对应版本的公开接口、contract 和迁移说明。
2. 更新 `pyproject.toml` 中的 framework 版本。
3. 安装依赖并运行测试。
4. 只在确有需要时同步调整 `.platform/` 或 framework 接入代码。

本仓库不再把 framework contract 作为项目 README 的阅读入口；Mujitask README 只维护业务项目自身的入口、部署、运行和文档边界。
