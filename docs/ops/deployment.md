# 部署文档

> 状态: Ops 文档。本文是当前部署说明，部署和回退材料不再放在 `docs/business`。

更新时间：`2026-04-24`

本文件描述当前可运行版本的真实部署方式。当前正式运行形态已经不是“部署一个 skill 然后手工在终端里跑脚本”，而是：

- Agent skill 负责提交顶层任务
- `executor_daemon` 负责推进顶层任务
- `api_worker_daemon` 负责网络/API 异步 job
- `browser_runloop` 负责串行消费浏览器叶子任务
- `outbox_dispatcher` 负责发送最终通知
- `Postgres` 负责控制状态
- `MinIO` 负责对象存储

旧的 `examples/openclaw/*` 首装脚本仍可作为历史参考，但不再代表当前最完整的部署形态。

## 1. 当前交付目标

部署完成后，环境至少需要满足：

1. 目标 agent 可以识别并调用 `mujitask-tiktok-feishu-sync` skill。
2. skill 可以同步提交顶层任务，并立即返回 `request_id`。
3. `Postgres` 中可以看到 `task_request / task_execution / api_worker_job / resource_lease / notification_outbox / artifact_object`。
4. `MinIO` 中可以看到运行产物对象。
5. `launchd` 托管的 4 个守护进程可以持续消费任务。
6. 真实飞书任务可以收到最终汇总通知。

## 2. 当前支持范围

当前完整部署口径以 `macOS` 为准。

- `launchd` 托管脚本和模板当前只覆盖 macOS。
- Windows 仍可参考旧版 skill 部署方式，但不属于当前完整运行时的标准交付路径。

## 3. 运行时组件

当前部署后的核心组件如下：

| 组件 | 角色 | 备注 |
| --- | --- | --- |
| Agent skill | 意图识别、参数提取、顶层任务提交 | 不负责任务主编排 |
| `executor_daemon` | 推进顶层 `task_request` | 负责 cleanup / scan / summary / outbox 创建 |
| `api_worker_daemon` | 消费 API worker job | 负责 TikTok requests、FastMoss HTTP、MinIO 上传、事实入库、达人池 product/author/finalizer job |
| `browser_runloop` | 消费浏览器叶子任务 | 按 `resource_code` 串行 |
| `outbox_dispatcher` | 发送最终通知 | 支持重试和恢复 |
| `Postgres` | 控制面数据库 | 任务、执行、租约、outbox、实体快照索引 |
| `MinIO` | 对象存储 | 运行产物、截图、引用文件 |

## 4. 项目内关键文件

部署和运行主要依赖这些文件：

- skill 包：
  - `skills/mujitask-tiktok-feishu-sync/SKILL.md`
  - `skills/mujitask-tiktok-feishu-sync/skill.local.env`
  - `skills/mujitask-tiktok-feishu-sync/run_refresh_current_competitor_table_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_keyword_search_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_skill_step.py`
  - `skills/mujitask-tiktok-feishu-sync/lightweight_submit.py`
- 执行环境：
  - `scripts/execution_control/executor.local.env`
  - `scripts/execution_control/install_launch_agents.sh`
  - `scripts/execution_control/run_launchd_agent.sh`
- `launchd` 模板：
  - `config/deployment/launchd/com.happyzhao.mujitask.executor-daemon.plist.template`
  - `config/deployment/launchd/com.happyzhao.mujitask.api-worker.plist.template`
  - `config/deployment/launchd/com.happyzhao.mujitask.browser-runloop.plist.template`
  - `config/deployment/launchd/com.happyzhao.mujitask.outbox-dispatcher.plist.template`

## 5. 基础依赖

当前标准部署依赖：

- Python `3.11`
- 项目 `.venv`
- 目标 agent，例如 OpenClaw 或 Hermes Agent
- 可用浏览器 profile
  - `roxy`
  - 或 `chrome_cdp`
- `Postgres`
- `MinIO`

## 6. 必备配置

说明：

- 当前 Python 运行时会自动尝试读取：
  1. `scripts/execution_control/executor.local.env`
  2. `skills/mujitask-tiktok-feishu-sync/skill.local.env`
  3. `.env`
- 自动加载不会覆盖已经显式传入的进程环境变量或 CLI 参数。
- Runtime DB / MinIO 的正式默认配置应放在 `executor.local.env`，不要只放在 `skill.local.env`。

### 6.1 skill.local.env

至少需要：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

当前模板见：

- `skills/mujitask-tiktok-feishu-sync/skill.local.env.example`

### 6.2 executor.local.env

至少需要：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`
- `FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`
- `NOTIFICATION_CHANNEL_CODE`

说明：

- 当前推荐使用 `BUSINESS_*` 前缀。
- 代码也兼容 `EXECUTION_CONTROL_*`，但部署文档统一按 `BUSINESS_*` 说明。
- 如果在项目仓库内运行 skill、daemon、CLI、pytest 或 Alembic，运行时会自动加载这份文件；不需要每次手工导出。

## 7. 数据库与对象存储

### 7.1 Postgres

当前正式控制库使用 `Postgres`。

真实运行至少涉及这些 Runtime 表：

- `task_request`
- `task_execution`
- `api_worker_job`
- `resource_lease`
- `notification_outbox`
- `artifact_object`
- `fastmoss_session_cookie_cache`

历史达人同步专用 job 表属于待迁移实现细节，不作为新部署和新 workflow 设计的目标 Runtime 表。

事实库表以 [../arch/fact-db-schema-design.md](../arch/fact-db-schema-design.md) 为准。

生产数据库账号建议拆分：

| 账号 | 用途 | 权限 |
| --- | --- | --- |
| `mujitask_runtime_user` | daemon / worker / dispatcher / watchdog | Runtime/Fact 表读写，不含 DDL |
| `mujitask_migration_user` | 发布 migration | `CREATE / ALTER / DROP / CREATE INDEX` |
| `mujitask_readonly_user` | 排障、报表、只读分析 | `SELECT` |

### 7.2 MinIO

当前正式对象存储使用 `MinIO`。

至少会写入：

- `run.json`
- `steps.json`
- `signals.json`
- `stdout.log`
- `state.json`
- 页面截图
- 商品图片
- FastMoss 截图

## 8. 标准部署步骤

### 8.0 macOS 一键部署

当前标准交付先收窄为 `macOS + launchd + Homebrew 本机 Postgres/MinIO`。
运行前需要 Homebrew 已安装；`deploy.sh` 会安装缺失的 `postgresql@17` / `minio` formula，`preflight.sh` 会提前检查端口和必填配置。
现场实施必须显式设定两个目录：

- `MUJITASK_INSTALL_DIR`：项目安装路径，例如 `$HOME/apps/mujitask`
- `MUJITASK_SKILLS_DIR`：目标 agent 读取 skills 的根目录，例如 OpenClaw 的 `$HOME/.openclaw/workspace/skills`，或 Hermes Agent 在现场约定的 skills 目录

部署脚本只负责把 skill bundle 安装到 `MUJITASK_SKILLS_DIR/mujitask-tiktok-feishu-sync`，不再推断任何 agent workspace。

首次部署：

```bash
cp scripts/deploy/macos/deploy.local.env.example scripts/deploy/macos/deploy.local.env
# 填写 deploy.local.env 中的项目安装路径、skills 安装路径、飞书、FastMoss、浏览器和通知配置
bash scripts/deploy/macos/preflight.sh
bash scripts/deploy/macos/deploy.sh
```

关键文件：

- `scripts/deploy/macos/preflight.sh`：检查 macOS、Homebrew、launchd、端口、必填配置、Chrome 提示。
- `scripts/deploy/macos/deploy.sh`：同步项目到安装路径、安装项目依赖、安装并启动本机 Postgres/MinIO、写入运行配置、安装 skill bundle、安装 launchd 并执行 smoke check。
- `scripts/deploy/macos/deploy.local.env.example`：一键部署配置模板。

这条路径用于“用户机器依赖不确定”的默认交付；如果目标环境使用已有 Postgres/MinIO，可以在 `deploy.local.env` 中改为 `MUJITASK_RUNTIME_MODE=external` 并提供对应连接配置。

### 8.1 安装项目

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
```

### 8.2 准备 skill 配置

在目标 agent 的 skills 根目录中生成或更新：

- `skill.local.env`

至少确认：

- `INSTALL_DIR`
- `TABLE_URL`
- `FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

### 8.3 准备 executor 配置

复制环境模板：

```bash
cp scripts/execution_control/executor.local.env.example scripts/execution_control/executor.local.env
```

然后填写数据库、MinIO、通知和浏览器相关变量。

推荐把 Runtime 相关变量只维护在这份文件：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `TEST_DATABASE_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`

这样 daemon、CLI、Alembic 和 pytest 都会从同一个项目配置入口读取，不需要每次运行前再手工 `export`。

### 8.4 初始化数据库

如果当前环境还没有对应 schema，本地开发可以使用 runtime store bootstrap 或 migration 脚本初始化。

生产环境不要让 daemon / worker 在正常消费任务时自动建表、改表或删表。生产发布应先使用 migration 账号执行 Alembic migration 或等价迁移脚本，然后让运行进程使用 runtime 账号启动并校验 schema version。

当前本地部署脚本在安装 `launchd` 前会主动触发一次 schema 初始化；这个行为只代表本地部署便利路径，不应等同于生产运行进程拥有 DDL 权限。

### 8.5 安装 launchd 守护进程

```bash
bash scripts/execution_control/install_launch_agents.sh
```

安装后会生成：

- `~/Library/LaunchAgents/com.happyzhao.mujitask.executor-daemon.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.browser-runloop.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.outbox-dispatcher.plist`

查看状态：

```bash
launchctl list | grep 'com.happyzhao.mujitask'
```

## 9. Skill 默认入口

当前默认对外入口：

- 竞品表刷新：

```bash
bash skills/mujitask-tiktok-feishu-sync/run_refresh_current_competitor_table_step.sh
```

- 关键词搜索：

```bash
bash skills/mujitask-tiktok-feishu-sync/run_keyword_search_step.sh \
  --search-keyword "<keyword>" \
  --sales-7d-threshold <number>
```

- 达人池同步：

```bash
bash skills/mujitask-tiktok-feishu-sync/run_influencer_pool_sync_step.sh
```

说明：

- 这些默认入口都是“同步提交、异步执行”。
- 首条回执只负责返回 `request_id`。
- 最终结果由后台 `outbox_dispatcher` 发送到飞书。

## 10. 当前标准 Smoke Check

部署后最少检查这些项：

1. `.venv/bin/automation-business-scaffold-run list-tasks` 可以执行。
2. 任务列表中能看到：
   - `refresh_current_competitor_table`
   - `search_keyword_competitor_products`
3. 目标 agent skills 根目录中 skill 目录存在且包含：
   - `SKILL.md`
   - `skill.local.env`
   - `run_refresh_current_competitor_table_step.sh`
   - `run_keyword_search_step.sh`
   - `run_skill_step.py`
   - `lightweight_submit.py`
4. `launchctl list` 中 4 个守护进程处于运行状态。
5. `Postgres` 中可以查询到 `task_request / task_execution / api_worker_job / notification_outbox` 等核心表。
6. `MinIO` bucket 可访问，并能看到 smoke object 或实际运行对象。

### 10.1 本地 Postgres 测试

开发机上推荐用项目本地执行配置跑完整测试，避免数据库相关测试因为没有 DB URL 而被跳过：

```bash
scripts/execution_control/run_local_postgres_tests.sh
```

该脚本会读取 `scripts/execution_control/executor.local.env`，pytest fixture 会在当前 Postgres 内为每次测试创建独立 schema，测试结束后自动清理。

本地开发机建议给测试单独建库，并在 `executor.local.env` 中配置：

```bash
createdb automation_business_scaffold_test

BUSINESS_EXECUTION_CONTROL_DB_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/automation_business_scaffold
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/automation_business_scaffold_test
```

fixture 优先使用 `TEST_DATABASE_URL`。没有这两个 DB URL 时，数据库相关测试会跳过；直接用 `psql` 连接排障时请使用 `postgresql://...` 格式。

## 11. 日志与排障路径

主要看这些位置：

- 守护进程日志：
  - `runtime/phase1_daemons`
- 运行产物：
  - `runtime/execution_control/object_store`
- CLI 运行记录：
  - `runtime/cli_runs`
- OpenClaw gateway：
  - `~/.openclaw/logs/gateway.log`

## 12. 当前不再推荐的旧路径

下面这些路径仍可作为人工排障工具，但不再代表默认业务主流程：

- `run_cleanup_step.sh`
- `run_pending_rows_step.sh`
- `run_fastmoss_login_check_step.sh`

旧的 `examples/openclaw/*` 脚本也应视为历史部署资产，不应替代当前 `Postgres + MinIO + launchd + top-level task skill` 的正式部署方式。
