# Phase 1 可交付验收与回退说明

更新时间：`2026-04-12`

## 1. 本文目的

本文把当前 Phase 1 从“代码已开发”收敛成“可以交付”的完整流程。

补充说明：
当前 Phase 1/2 运行时不是“只执行一次 CLI 命令”就结束，而是依赖 3 个常驻守护进程：

- `phase1_executor_daemon`
- `browser_runloop`
- `outbox_dispatcher`

如果部署文档里不明确写出这 3 个常驻进程，后续很容易出现“任务能提交，但无人消费”的误判。

交付口径对应 [13-系统升级开发目标与推进计划.md](./13-%E7%B3%BB%E7%BB%9F%E5%8D%87%E7%BA%A7%E5%BC%80%E5%8F%91%E7%9B%AE%E6%A0%87%E4%B8%8E%E6%8E%A8%E8%BF%9B%E8%AE%A1%E5%88%92.md) 中的 3 类要求：

- 代码交付物
- 数据交付物
- 运维交付物

## 2. Phase 1 交付范围

本阶段只交付 `feishu_single_row_update` 的执行控制闭环，不扩大到达人链或视频链。

当前交付内容：

- `submit / status / result`
- `execute_next / daemon_once / daemon_loop`
- 同一 `profile_ref` 的排队与租约
- `request_id -> execution_id -> run_id` 可回查
- Postgres 控制面接入
- Alembic 初始迁移
- executor 启动脚本与环境模板
- 保留旧同步直跑作为 fallback

本阶段仍不包含：

- MinIO 正式上传
- `entity_registry / external_binding / entity_snapshot`
- `notification_outbox`

## 3. 交付物清单

本次 Phase 1 可交付包由下面几部分组成：

- 控制面代码
  - [execution_control_flow.py](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/src/automation_business_scaffold/flows/execution_control_flow.py:1)
  - [sqlalchemy_execution_control_store.py](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/src/automation_business_scaffold/flows/sqlalchemy_execution_control_store.py:1)
  - [executor_daemon.py](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/src/automation_business_scaffold/executor_daemon.py:1)
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

当前 Alembic 初始迁移会创建 4 张表：

- `task_request`
- `task_execution`
- `resource_lease`
- `artifact_object`

如果没有配置 `BUSINESS_EXECUTION_CONTROL_DB_URL`，系统仍会回退到：

```text
runtime/execution_control/control_plane.sqlite3
```

这条 SQLite 路径保留给本地开发和应急回退，不作为正式交付数据库。

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
按当前实现，正式部署时应同时启动 3 个常驻进程：

- 顶层任务推进：`phase1_executor_daemon`
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

在 macOS 真机环境，推荐用 `launchd` 托管这 3 个守护进程，而不是手工在终端里常驻。

项目内已提供：

- 模板目录：[config/deployment/launchd](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/config/deployment/launchd)
- 启动包装脚本：[run_launchd_agent.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/run_launchd_agent.sh:1)
- 安装脚本：[install_launch_agents.sh](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/scripts/execution_control/install_launch_agents.sh:1)

标准安装命令：

```bash
bash scripts/execution_control/install_launch_agents.sh
```

安装后会在当前用户目录生成：

- `~/Library/LaunchAgents/com.happyzhao.mujitask.phase1-executor.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.browser-runloop.plist`
- `~/Library/LaunchAgents/com.happyzhao.mujitask.outbox-dispatcher.plist`

查看状态：

```bash
launchctl list | grep 'com.happyzhao.mujitask'
```

查看单个服务详情：

```bash
launchctl print gui/$(id -u)/com.happyzhao.mujitask.phase1-executor
```

### 5.6 守护进程日志路径

守护进程日志统一落在：

- [runtime/phase1_daemons](/Users/happyzhao/Work/mujitask-wt-system-architecture-upgrade/runtime/phase1_daemons)

如果是 `launchd` 托管，重点看这些文件：

- `phase1_executor.launchd.stdout.log`
- `phase1_executor.launchd.stderr.log`
- `browser_runloop.launchd.stdout.log`
- `browser_runloop.launchd.stderr.log`
- `outbox_dispatcher.launchd.stdout.log`
- `outbox_dispatcher.launchd.stderr.log`

## 6. 标准操作流

推荐 Phase 1 的标准操作流固定为：

1. `submit`
2. `daemon_loop`
3. `result`

Skill 侧可以直接用组合命令：

```bash
python3 skills/mujitask-tiktok-feishu-sync/run_skill_step.py \
  single-row-update-submit-then-daemon-loop \
  --record-id recXXXX \
  --profile-ref main
```

CLI 直连模式也可以分开执行：

```bash
automation-business-scaffold-run run \
  --task feishu_single_row_update \
  --param control_action=submit \
  --param record_id=recXXXX \
  --param profile_ref=main
```

```bash
automation-business-scaffold-executor --stop-when-idle --max-idle-cycles 1
```

```bash
automation-business-scaffold-run run \
  --task feishu_single_row_update \
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
- 3 个守护进程由 `launchd` 托管后，进程退出会被自动拉起

### 7.3 业务验收

- `feishu_single_row_update` 的最终写入结果与旧链路一致
- 失败时可从 `run.json / steps.json / signals.json / stdout.log / state.json` 排障
- OpenClaw 最终仍能拿到稳定的 `__OPENCLAW_RESULT__`

## 8. 回退方案

如果 Phase 1 上线后需要快速回退，按下面顺序做：

1. 停掉 3 个守护进程
2. 停止给 skill/CLI 传 `control_action` 与 `execution_control_*` 参数
3. 清空或移除 `BUSINESS_EXECUTION_CONTROL_DB_URL`
4. 恢复到原同步直跑方式

如果当前环境使用 `launchd` 托管，停服务建议执行：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.happyzhao.mujitask.phase1-executor.plist
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
