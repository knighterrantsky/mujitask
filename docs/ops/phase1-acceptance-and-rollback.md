# Phase 1 可交付验收与回退说明

> 状态: Ops 历史文档。本文保留 Phase 1 验收与回退基线；当前系统全貌以 [../arch/README.md](../arch/README.md) 为准。

更新时间：`2026-04-14`

## 1. 本文目的

本文记录 Phase 1 首次可交付时的验收与回退口径。

补充说明：

- 当前仓库已经继续推进并完成了 Phase 2 的对象存储接入与 Phase 3 的实体快照沉淀。
- 因此本文件应视为“Phase 1 历史交付基线”，不是当前系统全貌。
- 当前正式运行形态请以 [deployment.md](./deployment.md) 和 [../arch/README.md](../arch/README.md) 为准。

补充说明：
当前 Phase 1/2 运行时不是“只执行一次 CLI 命令”就结束，而是依赖 4 个常驻守护进程：

- `executor_daemon`
- `api_worker_daemon`
- `browser_runloop`
- `outbox_dispatcher`

如果部署文档里不明确写出这 4 个常驻进程，后续很容易出现“任务能提交，但无人消费”的误判。

Phase 1 历史交付口径包含 3 类要求：

- 代码交付物
- 数据交付物
- 运维交付物

## 2. Phase 1 交付范围

按历史阶段口径，Phase 1 只交付 `feishu_single_row_update` 的执行控制闭环，不扩大到达人链或视频链。

当前交付内容：

- `submit / status / result`
- `execute_next / daemon_once / daemon_loop`
- 同一 `profile_ref` 的排队与租约
- `request_id -> execution_id -> run_id` 可回查
- Postgres 控制面接入
- Alembic 初始迁移
- executor 启动脚本与环境模板
- 保留旧同步直跑作为 fallback

按 Phase 1 历史边界，本阶段仍不包含：

- MinIO 正式上传
- `entity_registry / external_binding / entity_snapshot`
- `notification_outbox`

当前现状说明：

- 以上三项在后续阶段已经补齐
- 因此如果你现在查看当前代码或线上环境，不应再用这一段判断“当前系统是否具备这些能力”

## 3. 交付物清单

以下清单记录的是 Phase 1 当时的最小交付物，不等于当前完整系统交付物。

- 控制面代码
  - `src/automation_business_scaffold/infrastructure/runtime/runtime_store.py`
  - `src/automation_business_scaffold/business/flows/refresh_current_competitor_table_flow.py`
  - `src/automation_business_scaffold/executor_daemon.py`
- 数据库迁移
  - [alembic.ini](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/alembic.ini:1)
  - [env.py](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/alembic/env.py:1)
  - [20260412_0001_phase1_execution_control.py](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/alembic/versions/20260412_0001_phase1_execution_control.py:1)
- 运行脚本
  - [run_executor_daemon.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/run_executor_daemon.sh:1)
  - [run_alembic_upgrade.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/run_alembic_upgrade.sh:1)
  - [executor.local.env.example](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/executor.local.env.example:1)

## 4. 数据库交付标准

Phase 1 正式数据库以 `BUSINESS_EXECUTION_CONTROL_DB_URL` 为准，推荐：

```bash
export BUSINESS_EXECUTION_CONTROL_DB_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/automation_business_scaffold'
```

本地 DB-backed 回归测试使用独立的 `TEST_DATABASE_URL`，推荐：

```bash
export TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/automation_business_scaffold_test'
```

`pytest` fixture 会优先使用 `TEST_DATABASE_URL`，然后才回退到 `BUSINESS_EXECUTION_CONTROL_DB_URL` / `EXECUTION_CONTROL_DB_URL`。开发机应提前创建 `automation_business_scaffold_test`，避免数据库测试因为没有可用 DB URL 被跳过。直接用 `psql` 连接时，连接串要写成 `postgresql://...`，不要带 SQLAlchemy driver 后缀 `+psycopg`。

首次配置测试库：

```bash
createdb automation_business_scaffold_test
```

当前 Alembic 初始迁移会创建 4 张表：

- `task_request`
- `task_execution`
- `resource_lease`
- `artifact_object`

必须配置 `BUSINESS_EXECUTION_CONTROL_DB_URL`。运行时不再提供本地文件数据库兜底，避免 daemon 误连到临时控制面。

## 5. 标准部署流程

### 5.1 安装依赖

推荐在虚拟环境里执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
```

### 5.2 配置环境

复制一份本地环境模板：

```bash
cp scripts/execution_control/executor.local.env.example scripts/execution_control/executor.local.env
```

至少需要确认这些变量：

- `BUSINESS_EXECUTION_CONTROL_DB_URL`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_ROOT`
- `BUSINESS_EXECUTION_CONTROL_ARTIFACT_BUCKET`
- `BUSINESS_EXECUTION_CONTROL_WORKER_ID`

### 5.3 执行数据库迁移

```bash
bash scripts/execution_control/run_alembic_upgrade.sh
```

### 5.4 启动 executor
历史上 Phase 1 只强调过单个 `executor_daemon`。
按当前实现，正式部署时应同时启动 4 个常驻进程：

- 顶层任务推进：`executor_daemon`
- API / 网络 / I/O job 消费：`api_worker_daemon`
- 浏览器叶子任务消费：`browser_runloop`
- 最终通知发送：`outbox_dispatcher`

如果只启动其中一个，系统会出现以下问题：

- 只启动 `executor_daemon`：任务只能推进到 `waiting_children`
- 只启动 `browser_runloop`：没有新的顶层任务被拆解入队
- 只启动 `outbox_dispatcher`：不会有新的业务执行，也不会产生新的汇总通知

单进程手工启动只适合本地排障，不适合正式部署。

单个进程手工运行示例：

`executor_daemon`：

```bash
bash scripts/execution_control/run_executor_daemon.sh
```

空闲退出模式：

```bash
bash scripts/execution_control/run_executor_daemon.sh --stop-when-idle --max-idle-cycles 2
```

只消费一个请求：

```bash
bash scripts/execution_control/run_executor_daemon.sh --once
```

### 5.5 推荐部署方式：launchd

在 macOS 真机环境，推荐用 `launchd` 托管这 4 个守护进程，而不是手工在终端里常驻。

项目内已提供：

- 模板目录：[config/deployment/launchd](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/config/deployment/launchd)
- 启动包装脚本：[run_launchd_agent.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/run_launchd_agent.sh:1)
- 安装脚本：[install_launch_agents.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/install_launch_agents.sh:1)

本地开发机完整测试建议直接使用项目配置的 Postgres：

```bash
scripts/execution_control/run_local_postgres_tests.sh
```

该脚本会读取 `scripts/execution_control/executor.local.env`。如果设置了 `TEST_DATABASE_URL`，测试 fixture 会在测试库中创建临时 schema；否则才回退到运行库 URL。

标准安装命令：

```bash
bash scripts/execution_control/install_launch_agents.sh
```

安装后会在当前用户目录生成：

- `~/Library/LaunchAgents/com.happyzhao.mujitask.executor-daemon.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.browser-runloop.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.outbox-dispatcher.plist`

查看状态：

```bash
launchctl list | grep 'com.happyzhao.mujitask'
```

查看单个服务详情：

```bash
launchctl print gui/$(id -u)/com.happyzhao.mujitask.executor-daemon
```

### 5.6 守护进程日志路径

守护进程日志统一落在：

- [runtime/phase1_daemons](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/runtime/phase1_daemons)

如果是 `launchd` 托管，重点看这些文件：

- `executor_daemon.launchd.stdout.log`
- `executor_daemon.launchd.stderr.log`
- `api_worker_daemon.launchd.stdout.log`
- `api_worker_daemon.launchd.stderr.log`
- `browser_runloop.launchd.stdout.log`
- `browser_runloop.launchd.stderr.log`
- `outbox_dispatcher.launchd.stdout.log`
- `outbox_dispatcher.launchd.stderr.log`

## 6. 标准操作流

当前标准操作流已经从“单行任务入队”升级为“顶层 workflow 入队”：

1. Skill 提交顶层 `task_request`
2. `executor_daemon` 拆解确定性步骤并写入浏览器叶子任务
3. `browser_runloop` 消费浏览器叶子任务
4. `executor_daemon` 汇总父任务并写入 outbox
5. `outbox_dispatcher` 发送最终通知

Skill 侧示例：

```bash
python3 skills/mujitask-tiktok-feishu-sync/run_skill_step.py \
  refresh-current-competitor-table \
  --profile-ref main
```

CLI 直连模式也可以分开执行：

```bash
automation-business-scaffold-run run \
  --task refresh_current_competitor_table \
  --param control_action=submit \
  --param profile_ref=main
```

```bash
automation-business-scaffold-executor --stop-when-idle --max-idle-cycles 1
```

```bash
automation-business-scaffold-run run \
  --task refresh_current_competitor_table \
  --param control_action=result \
  --param request_id=<submit 返回的 request_id>
```

## 7. 验收清单

### 7.1 契约验收

- `submit` 返回 `request_id`
- `status/result` 能按 `request_id` 查询
- 保留原 `feishu_single_row_update` 任务名
- 不传 `control_action` 时仍可同步直跑

### 7.2 行为验收

- 同一 `profile_ref` 下两个请求会排队
- executor 异常退出后，租约超时后可回收
- 顶层 `task_request` 异常中断后，租约超时后会回收到 `pending` 或 `ready_for_summary`
- `notification_outbox` 发送中断后，超时后会回收到 `retry_wait`
- `request_id -> execution_id -> run_id` 可在数据库回查
- daemon 执行完成后可查询到 `artifact_object`
- 4 个守护进程由 `launchd` 托管后，进程退出会被自动拉起

### 7.3 业务验收

- `feishu_single_row_update` 的最终写入结果与旧链路一致
- 失败时可从 `run.json / steps.json / signals.json / stdout.log / state.json` 排障
- OpenClaw 最终仍能拿到稳定的 `__OPENCLAW_RESULT__`

## 8. 回退方案

如果 Phase 1 上线后需要快速回退，按下面顺序做：

1. 停掉 4 个守护进程
2. 停止给 skill/CLI 传 `control_action` 与 `execution_control_*` 参数
3. 清空或移除 `BUSINESS_EXECUTION_CONTROL_DB_URL`
4. 恢复到原同步直跑方式

如果当前环境使用 `launchd` 托管，停服务建议执行：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.happyzhao.mujitask.executor-daemon.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.happyzhao.mujitask.browser-runloop.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.happyzhao.mujitask.outbox-dispatcher.plist
```

回退后的行为特点：

- 业务仍通过原 `feishu_single_row_update` 入口执行
- 不依赖 Postgres daemon
- 不依赖受控排队
- 本地 runtime 与原排障路径保持不变

## 9. 当前阶段结论

按“每个阶段必须是可交付完整流程”的标准，当前 Phase 1 的完成定义应该是：

- 已有正式数据库入口
- 已有迁移脚本
- 已有 executor 启动脚本
- 已有标准操作流
- 已有验收与回退文档

满足这几个条件后，才建议继续进入 Phase 2 的 MinIO 正式接入。
