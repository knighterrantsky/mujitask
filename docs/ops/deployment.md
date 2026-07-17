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
- `skills/mujitask-amazon-feishu-sync/`

部署目标必须分离:

- `MUJITASK_TIKTOK_SKILLS_DIR/mujitask-tiktok-feishu-sync`
- `MUJITASK_AMAZON_SKILLS_DIR/mujitask-amazon-feishu-sync`

Amazon 固定使用 `amazon-ops` / `workspace-amazon`；TikTok 使用 `tiktok-ops` / `workspace-tiktok`。两个 workspace 不得交叉安装对方 Skill。二者共用飞书账号 `default`，Amazon 通过新建群聊的 `oc_*` peer binding 精确路由；飞书机器人密钥仍只由 OpenClaw secret 配置管理。

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
  - `skills/mujitask-amazon-feishu-sync/skill.spec.yaml`
  - `skills/mujitask-amazon-feishu-sync/examples.eval.yaml`
  - `skills/mujitask-amazon-feishu-sync/SKILL.md`
  - `skills/mujitask-amazon-feishu-sync/run_task.sh`
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
- Runtime / Amazon Fact migration 连接串都不属于项目运行配置，不能写入 `executor.local.env`、`skill.local.env` 或 launchd plist。
- `deploy.local.env` 可能包含管理员连接串和全部业务密钥，必须由当前部署用户持有、权限为 `0400` 或 `0600`，且不能是符号链接；`preflight.sh` 和 `deploy.sh` 都会在加载前拒绝不满足这些条件的文件。
- Native MinIO launchd plist 包含 root 凭据；部署在写入前检查非符号链接和 owner，写入后固定并复核为当前用户持有的 `0600` 文件。

### 6.1 skill.local.env

至少需要：

- `INSTALL_DIR`
- `MUJITASK_FEISHU_BASE_URL`
- `MUJITASK_FEISHU_TK_*_TABLE_ID`
- `MUJITASK_FEISHU_TK_*_VIEW_ID`
- `MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID`
- `MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`

浏览器 profile 和 migration 身份属于项目运行配置或发布配置；不要写入 `skill.local.env`。
部署会拒绝符号链接或非当前用户持有的既有 `skill.local.env`，并在写入后固定为 `0600`。

飞书业务表路由配置必须使用英文 alias，不在配置 key 或配置值中写中文表名，也不维护第二套完整 URL 配置。系统只使用一个 Base URL 加每张表的 `table_id` / `view_id` 拼出完整 table URL。当前标准 alias 为:

| alias | table_id | view_id |
| --- | --- | --- |
| `TK_SELECTION` | `tblpF46y6SkmVCE5` | `vewhXPD4x1` |
| `TK_COMPETITOR` | `tblpzuTZXHtDq83t` | `vewT6AtfED` |
| `TK_INFLUENCER_POOL` | `tblwLYl59TkfVFLe` | `vewuKd9i6D` |
| `TK_INFLUENCER_OUTREACH` | `tblpK4zCGaaL6h6v` | `vewmMgDNV5` |
| `TK_HOT_VIDEO` | `tblP9S5mRrirutDT` | `vewu7vztKp` |
| `AMAZON_PRODUCTS` | 部署环境显式配置 | 部署环境显式配置 |

达人池同步的来源表固定由 `TK_COMPETITOR` 路由推导，目标表固定由 `TK_INFLUENCER_POOL` 路由推导；配置层不再维护派生出来的完整表 URL。

当前模板见：

- `skills/mujitask-tiktok-feishu-sync/skill.local.env.example`

### 6.2 executor.local.env

至少需要：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_FACT_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`
- `MUJITASK_FEISHU_ACCESS_TOKEN`
- `BROWSER_PROFILE_REF`
- `AMAZON_US_BROWSER_PROFILE_REF`
- `FASTMOSS_PHONE`
- `FASTMOSS_PASSWORD`
- `NOTIFICATION_CHANNEL_CODE`

说明：

- 当前推荐使用 `BUSINESS_*` 前缀。
- 代码也兼容 `EXECUTION_CONTROL_*`，但部署文档统一按 `BUSINESS_*` 说明。
- skill、daemon、CLI 和 pytest 会自动加载这份文件；Runtime / Amazon Fact Alembic 不使用这里的 worker URL 作为 External migration 凭据。
- macOS 部署会清除历史遗留的 `TK_FACT_DB_URL` 和所有 migration key，确保共享 Fact DB 连接统一使用受限的 Fact runtime 账号。
- `AMAZON_US_BROWSER_PROFILE_REF` 只写入 `executor.local.env`；Amazon 表 route 同时写入已安装的 `skill.local.env` 和 `executor.local.env`，确保调用入口与 API worker 都能解析 `AMAZON_PRODUCTS`。浏览器 profile 不进入 skill 配置或 plist。
- `executor.local.env` 包含 Runtime / Fact worker 连接串和业务密钥。部署在写入前拒绝符号链接和非当前用户持有的既有文件，写入后固定为 `0600` 并再次核对 owner；不要把它复制到共享目录。

### 6.3 migration.local.env

macOS 部署生成 `${MUJITASK_INSTALL_DIR}/runtime/deployment/migration.local.env`，权限固定为
`0600`。它只在发布进程中短时提供：

- `BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE`

该文件位于被 Git 忽略的 `runtime/` 下，不由 `run_launchd_agent.sh` 加载，也不会渲染到
任何 plist。只有 Fact migration runner 在显式传入
`BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE` 时读取它，并拒绝权限宽于 `0600` 的文件；
它不读取 `executor.local.env`，也不接受进程环境中残留的 Fact migration URL 作为回退。
部署通过 stdin 或仅对子进程可见的受控环境传递数据库连接，不把连接串或密码放进
Python / `psql` 参数。发布成功、失败或被中断时，EXIT cleanup 都会删除该临时文件。
External Runtime migration URL 不写入该文件；部署只在调用 Runtime Alembic runner 时通过
`BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL` 子进程环境短时传递，runner 加载 worker
配置后再显式覆盖 Alembic URL。

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
| `mujitask_runtime_user` | executor / daemon / dispatcher / watchdog | 当前 Native 兼容路径仍负责既有 Runtime/TikTok schema bootstrap |
| `mujitask_fact_runtime` | API worker 共用 Fact 身份 | 契约治理的 `tk_*` / `amazon_*` 表 DML、Fact version 表只读；不含 Runtime 表权限或 DDL |
| `mujitask_migration_user` | 发布 Amazon Fact migration | `CREATE / ALTER / DROP / CREATE INDEX`，不进入 worker 配置 |
| `mujitask_readonly_user` | 排障、报表、只读分析 | `SELECT` |

本次 Amazon 交付只收敛 Fact worker 边界：Native 部署创建独立的无高权 Fact login，
撤销它的 schema `CREATE`，仅按机器维护的 TikTok/Amazon Fact 表白名单授予
`SELECT/INSERT/UPDATE/DELETE`，只读 `fact_alembic_version`，并拒绝其对 Runtime 或其他表
保留任何权限。现有 Runtime migration 图尚未
完全覆盖 Runtime/TikTok schema，因此 Native 安装仍保留既有 bootstrap；不能把这一路径描述为
Runtime 权限模型已经治理完成。

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
现场实施必须显式设定三个目录：

- `MUJITASK_INSTALL_DIR`：项目安装路径，例如 `$HOME/apps/mujitask`
- `MUJITASK_TIKTOK_SKILLS_DIR`：TikTok agent 的 skills 根目录，例如 `$HOME/.openclaw/workspace-tiktok/skills`
- `MUJITASK_AMAZON_SKILLS_DIR`：Amazon agent 的 skills 根目录，例如 `$HOME/.openclaw/workspace-amazon/skills`

部署脚本把两个 skill bundle 分别安装到显式目录，并拒绝两个目录相同。OpenClaw agent 与飞书账号的机器绑定以 `contracts/agents/business-agent-bindings.yaml` 为准；创建飞书应用、保存 App Secret 和给机器人授权仍属于现场 secret 配置，不由仓库写入。

本机 Postgres Runtime 连接默认使用 `mujitask`，Fact 连接使用显式配置的
`MUJITASK_FACT_RUNTIME_ROLE`。Native 部署会创建独立的 Fact login，拒绝把 Runtime DB owner
复用为 Fact role，并只为它补齐既有 `tk_*` DML 和本次 migration 授予的 `amazon_*` DML /
`fact_alembic_version` SELECT。部署还会用实际连接核对 Fact worker URL 与 Fact migration
URL 的当前数据库、数据库 OID、Postgres 启动时间和版本，确保二者落到同一运行实例、同一库。
`MUJITASK_POSTGRES_SOCKET_DIR=/tmp` 只用于部署进程以本机管理员
身份执行 Fact migration，不进入 daemon 连接串。External 模式必须显式提供 Runtime worker URL、
Runtime migration URL、Fact worker URL、Fact migration URL 和 Fact runtime role；部署分别核对
两组 worker/migration URL 指向同一运行实例和数据库。对象存储默认前缀仍为
`MUJITASK_ARTIFACT_OBJECT_PREFIX=mujitask/local`。

首次部署或更新已有部署：

```bash
cp scripts/deploy/macos/deploy.local.env.example scripts/deploy/macos/deploy.local.env
# 填写 deploy.local.env 中的项目安装路径、skills 安装路径、飞书、FastMoss、浏览器和通知配置
chmod 600 scripts/deploy/macos/deploy.local.env
bash scripts/deploy/macos/preflight.sh
bash scripts/deploy/macos/deploy.sh
```

关键文件：

- `scripts/deploy/macos/preflight.sh`：检查 macOS、Homebrew、launchd、端口、Node.js/npm、必填配置、Chrome 提示。
- `scripts/deploy/macos/deploy.sh`：同步项目到安装路径、安装 Python 与 Node.js 运行依赖、安装并启动本机 Postgres/MinIO、写入隔离的运行/迁移配置、依次执行 Runtime 和 Fact Alembic migration、校验 Fact 权限、安装 skill bundle、安装 launchd 并执行 smoke check。
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
- `MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID`
- `MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID`
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
- `BUSINESS_EXECUTION_CONTROL_FACT_DB_URL`
- `AMAZON_US_BROWSER_PROFILE_REF`
- `TEST_DATABASE_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_STORE_PROVIDER`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ENDPOINT`
- `BUSINESS_EXECUTION_CONTROL_MINIO_ACCESS_KEY`
- `BUSINESS_EXECUTION_CONTROL_MINIO_SECRET_KEY`

这样 daemon、CLI 和 pytest 都从同一个项目运行配置入口读取。不要向该文件加入
`BUSINESS_EXECUTION_CONTROL_MIGRATION_DB_URL`、
`BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL`、
`BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL` 或
`BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE`，也不要加入对应的 `MUJITASK_*_MIGRATION_DB_URL`。

### 8.4 初始化或升级数据库

如果当前环境还没有对应 schema，本地开发可以使用 runtime store bootstrap 或 migration 脚本初始化。

生产环境不要让 daemon / worker 在正常消费任务时自动建表、改表或删表。生产发布应先使用 migration 账号执行 Alembic migration 或等价迁移脚本，然后让运行进程使用 runtime 账号启动并校验 schema version。

首次部署和更新已有部署都必须在启动或重启 `launchd` 之前执行 Runtime migration 与独立
Amazon Fact migration。External 手工执行时先准备 Runtime migration URL 和权限为 `0600` 的
私有 Fact migration env，然后运行：

```bash
BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL="$RUNTIME_MIGRATION_DB_URL" \
  bash scripts/execution_control/run_alembic_upgrade.sh
BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE=/secure/path/migration.local.env \
  bash scripts/execution_control/run_fact_alembic_upgrade.sh
```

推荐更新顺序：

1. 更新项目代码和 Python 依赖。
2. 确认 `executor.local.env` 只包含 Runtime / Fact worker URL，migration URL 不在 executor、skill 或 plist 中。
3. 确认私有 migration env 只包含 Fact migration URL 和显式 Fact runtime role，且权限为 `0600`。
4. External 模式先核对 Runtime worker/migration URL 的数据库身份，再用独立 migration URL 执行
   Runtime migration；Native 模式保持既有 Runtime migration 与随后 Runtime/TikTok 兼容 bootstrap。
5. 执行独立 `fact_alembic_version` 图的 Amazon Fact migration。
6. Native 模式按显式 Fact 表白名单补齐 Fact DML；External 模式由数据库管理员预先创建
   TikTok Fact 表并授权。两种模式都检查 Fact runtime 账号对权威 TikTok 表全集和 9 张
   Amazon 表具备 DML、对 `fact_alembic_version` 只有只读权限。
7. 读取 `fact_alembic_version.version_num`，确认它与应用要求的 Fact revision 完全一致；仅凭表存在不能通过发布门禁。
8. 使用 Fact worker URL 只读核验角色身份、无高权属性，并扫描所有非系统 schema 的 table、
   partitioned table、view、materialized view、foreign table 与 sequence；除默认 Fact schema 白名单外
   不允许任何有效权限或 ownership。
9. 删除临时 migration env，再安装或重启 `launchd` 守护进程。

当前 macOS 一键部署脚本会在安装 launchd 之前自动执行上述顺序，并在数据库身份、任一迁移、
实际 revision 或权限核验失败时停止。Native 模式继续通过 `install_launch_agents.sh` 执行既有
Runtime/TikTok 兼容 bootstrap；External 模式使用不含 schema bootstrap 的 launchd 安装路径，
不会用 external Runtime worker URL 执行建表。Runtime migration 图补齐之前，Native 兼容路径
不能移除，也不能宣称 Runtime worker 已经完全取消 DDL 权限。

如果跳过 migration，已有环境可能停留在旧 `alembic_version`，新 worker 会在写入或查询新字段时失败，例如 Fact DB 缺少 `tk_videos.creator_uid` / `tk_videos.creator_unique_id`。

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
