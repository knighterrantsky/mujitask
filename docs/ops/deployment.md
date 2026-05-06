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
5. `launchd` 托管的 5 个守护进程可以持续消费任务并执行运行时恢复。
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

## 3.1 Agent Skill Bundle 边界

Agent skill bundle 是部署给 OpenClaw、Hermes 或其他目标 agent workspace 的入口产物，不是 runtime worker。

当前仓库内源目录:

- `skills/mujitask-tiktok-feishu-sync/`

部署目标:

- `MUJITASK_SKILLS_DIR/mujitask-tiktok-feishu-sync`

部署脚本负责:

1. 复制仓库内 skill bundle 到部署 agent skills 目录。
2. 生成或保留部署目录下的 `skill.local.env`。
3. 生成项目安装目录下的 `scripts/execution_control/executor.local.env`。
4. 安装并启动 executor、api worker、browser runloop、outbox dispatcher。

后续多个业务可以拥有多个 skill bundle；每个 bundle 仍只负责意图识别、参数提取、顶层 task 提交和首条 `request_id` 回执。

## 4. 项目内关键文件

部署和运行主要依赖这些文件：

- skill 包：
  - `skills/mujitask-tiktok-feishu-sync/skill.spec.yaml`
  - `skills/mujitask-tiktok-feishu-sync/examples.eval.yaml`
  - `skills/mujitask-tiktok-feishu-sync/SKILL.md`
  - `skills/mujitask-tiktok-feishu-sync/skill.local.env`
  - `skills/mujitask-tiktok-feishu-sync/run_selection_table_complete_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_refresh_current_competitor_table_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_competitor_row_by_url_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_product_url_complete_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_keyword_search_step.sh`
  - `skills/mujitask-tiktok-feishu-sync/run_influencer_pool_sync_step.sh`
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
  - `config/deployment/launchd/com.happyzhao.mujitask.watchdog.plist.template`

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
- Runtime DB / Fact DB / MinIO / 浏览器 profile 的正式默认配置应放在项目运行配置中，不能放在 `skill.local.env`。

### 6.1 skill.local.env

至少需要：

- `INSTALL_DIR`
- `MUJITASK_FEISHU_BASE_URL`
- `MUJITASK_FEISHU_TK_*_TABLE_ID`
- `MUJITASK_FEISHU_TK_*_VIEW_ID`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

浏览器 profile 默认值属于项目运行配置；不要写入 `skill.local.env`。

飞书业务表路由配置必须使用英文 alias，不在配置 key 或配置值中写中文表名，也不维护第二套完整 URL 配置。系统只使用一个 Base URL 加每张表的 `table_id` / `view_id` 拼出完整 table URL。当前标准 alias 为:

| alias | table_id | view_id |
| --- | --- | --- |
| `TK_SELECTION` | `tblpF46y6SkmVCE5` | `vewhXPD4x1` |
| `TK_COMPETITOR` | `tblpzuTZXHtDq83t` | `vewT6AtfED` |
| `TK_INFLUENCER_POOL` | `tblwLYl59TkfVFLe` | `vewuKd9i6D` |
| `TK_INFLUENCER_OUTREACH` | `tblpK4zCGaaL6h6v` | `vewmMgDNV5` |
| `TK_HOT_VIDEO` | `tblP9S5mRrirutDT` | `vewu7vztKp` |

达人池同步的来源表固定由 `TK_COMPETITOR` 路由推导，目标表固定由 `TK_INFLUENCER_POOL` 路由推导；配置层不再维护派生出来的完整表 URL。

当前模板见：

- `skills/mujitask-tiktok-feishu-sync/skill.local.env.example`

### 6.2 executor.local.env

至少需要：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `TK_FACT_DB_URL` 或 `BUSINESS_EXECUTION_CONTROL_FACT_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
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

长期运行必须按 [runtime-db-connection-stability.md](./runtime-db-connection-stability.md) 配置连接保护。核心要求:

- runtime / worker 进程使用受限账号，不允许无限占用连接。
- 本机 Postgres 应设置空闲连接超时，避免 pgAdmin 或异常进程长期占用连接。
- watchdog / preflight 应检查连接数健康，接近上限时阻止继续提交大量任务。
- 事实库写入路径应复用 RuntimeStore，或显式使用有界连接池 / 无池化连接策略。

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
运行前需要 Homebrew 已安装；`deploy.sh` 会安装缺失的 `postgresql@17` / `minio` / `node` formula，`preflight.sh` 会提前检查端口、Node.js/npm 和必填配置。
现场实施必须显式设定两个目录：

- `MUJITASK_INSTALL_DIR`：项目安装路径，例如 `$HOME/apps/mujitask`
- `MUJITASK_SKILLS_DIR`：目标 agent 读取 skills 的根目录，例如 OpenClaw 的 `$HOME/.openclaw/workspace/skills`，或 Hermes Agent 在现场约定的 skills 目录

部署脚本只负责把 skill bundle 安装到 `MUJITASK_SKILLS_DIR/mujitask-tiktok-feishu-sync`，不再推断任何 agent workspace。

本机 Postgres 运行时默认通过 TCP 连接，配置为 `mujitask/mujitask@127.0.0.1:5432/automation_business_scaffold`。部署脚本会在 `native` 模式下创建或更新这个 runtime 账号，并把生成的 URL 写入 `scripts/execution_control/executor.local.env`。`MUJITASK_POSTGRES_SOCKET_DIR=/tmp` 只用于部署脚本以本机管理员身份 bootstrap Homebrew Postgres，不进入 daemon 的运行时连接串；切换云 Postgres 时改为 `MUJITASK_RUNTIME_MODE=external` 并填写 `MUJITASK_DB_URL` 即可。对象存储默认前缀为 `MUJITASK_ARTIFACT_OBJECT_PREFIX=mujitask/local`，用于给 MinIO bucket 内的运行产物分目录，例如 `mujitask/local/runs/...`。

首次部署：

```bash
cp scripts/deploy/macos/deploy.local.env.example scripts/deploy/macos/deploy.local.env
# 填写 deploy.local.env 中的项目安装路径、skills 安装路径、飞书、FastMoss、浏览器和通知配置
bash scripts/deploy/macos/preflight.sh
bash scripts/deploy/macos/deploy.sh
```

关键文件：

- `scripts/deploy/macos/preflight.sh`：检查 macOS、Homebrew、launchd、端口、Node.js/npm、必填配置、Chrome 提示。
- `scripts/deploy/macos/deploy.sh`：同步项目到安装路径、安装 Python 与 Node.js 运行依赖、安装并启动本机 Postgres/MinIO、写入运行配置、安装 skill bundle、安装 launchd 并执行 smoke check。
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
- `MUJITASK_FEISHU_BASE_URL`
- `MUJITASK_FEISHU_TK_*_TABLE_ID`
- `MUJITASK_FEISHU_TK_*_VIEW_ID`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
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
- `TK_FACT_DB_URL`
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
- `~/Library/LaunchAgents/com.happyzhao.mujitask.api-worker.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.browser-runloop.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.outbox-dispatcher.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.watchdog.plist`

查看状态：

```bash
launchctl list | grep 'com.happyzhao.mujitask'
```

## 9. Skill 默认入口

当前默认对外入口：

- 竞品采集：

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
   - `skill.spec.yaml`
   - `examples.eval.yaml`
   - `SKILL.md`
   - `skill.local.env`
   - `run_selection_table_complete_step.sh`
   - `run_refresh_current_competitor_table_step.sh`
   - `run_competitor_row_by_url_step.sh`
   - `run_product_url_complete_step.sh`
   - `run_keyword_search_step.sh`
   - `run_influencer_pool_sync_step.sh`
   - `run_skill_step.py`
   - `lightweight_submit.py`
4. `launchctl list` 中 5 个守护进程处于运行状态。
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
createdb -O mujitask automation_business_scaffold_test

BUSINESS_EXECUTION_CONTROL_DB_URL=postgresql+psycopg://mujitask:mujitask@127.0.0.1:5432/automation_business_scaffold
TK_FACT_DB_URL=postgresql+psycopg://mujitask:mujitask@127.0.0.1:5432/automation_business_scaffold
TEST_DATABASE_URL=postgresql+psycopg://mujitask:mujitask@127.0.0.1:5432/automation_business_scaffold_test
```

fixture 优先使用 `TEST_DATABASE_URL`。没有这两个 DB URL 时，数据库相关测试会跳过；直接用 `psql` 连接排障时请使用 `postgresql://...` 格式。

## 11. 日志与排障路径

主要看这些位置：

- 守护进程日志：
  - `runtime/daemons`
- 运行产物：
  - `runtime/execution_control/object_store`
- CLI 运行记录：
  - `runtime/cli_runs`
- OpenClaw gateway：
  - `~/.openclaw/logs/gateway.log`

## 12. 当前不再推荐的旧路径

旧 leaf step / 人工排障 wrapper 已从 skill bundle 移除，不应再作为 OpenClaw 入口使用。

旧的 `examples/openclaw/*` 脚本也应视为历史部署资产，不应替代当前 `Postgres + MinIO + launchd + top-level task skill` 的正式部署方式。
