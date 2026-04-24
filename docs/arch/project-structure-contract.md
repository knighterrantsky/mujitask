# 项目结构与命名契约

日期: 2026-04-24

状态: 受控架构契约

## 1. 定位

本文定义 Mujitask 代码的工程结构、命名和定位规则。后续新增 agent skill bundle、workflow、job、handler、飞书 adapter/projection mapper 或业务 flow 时，必须遵守本文；如果确实需要改变结构，应同步修改本文、相关架构文档和结构契约测试。

本文解决三个问题:

- 开发者能从系统架构快速定位代码。
- 文件命名能直接表达 runtime contract。
- Agent / skill 入口产物和 runtime 编排代码有清晰边界。
- 防止 handler、job、workflow、mapper、flow 再次混在同一个模糊层里。

相关事实来源:

- 系统执行链路: [current-system-architecture-design.md](./current-system-architecture-design.md)
- Workflow 设计规则: [workflow-design-guidelines.md](./workflow-design-guidelines.md)
- Handler 契约: [handler-contract-design.md](./handler-contract-design.md)
- Runtime 控制面契约: [runtime-control-plane-contract.md](./runtime-control-plane-contract.md)
- 文档修改治理: [../dev/documentation-change-policy.md](../dev/documentation-change-policy.md)

## 2. 快速定位路径

从一个 agent skill 定位正式业务入口时，按下面路径查:

```text
skills/{skill_code}/SKILL.md
  -> skills/{skill_code}/run_*_step.sh
  -> skills/{skill_code}/run_skill_step.py 或 lightweight_submit.py
  -> business/tasks/{task_code}.py
  -> business/workflow_defs/{workflow_code}.py
```

从一个正式业务入口定位代码时，按下面路径查:

```text
task_code
  -> business/tasks/{task_code}.py
  -> business/workflow_defs/{workflow_code}.py
  -> business/jobs/{job_code}.py
  -> business/handlers/{worker_lane}/{handler_code}.py
  -> business/feishu/source_adapters.py 或 projection_mappers.py
  -> business/flows/** 业务实现细节
  -> infrastructure/** 外部系统客户端和存储实现
```

排查某条 Runtime job 时，按下面路径查:

```text
api_worker_job.job_code / task_execution.item_code / notification_outbox.event_type
  -> business/jobs/{job_code}.py
  -> JOB_DEFINITION.handler_code
  -> business/handlers/{worker_lane}/{handler_code}.py
  -> HandlerResult.summary / HandlerResult.result
  -> workflow_defs 中消费该 result 的 stage/reconciler
```

排查 RPC / daemon / watchdog / supervisor / reconciler / 项目配置时，按下面路径查:

```text
console script 或 agent/CLI 请求
  -> src/automation_business_scaffold/agent.py 或 cli.py
  -> src/automation_business_scaffold/*_daemon.py / browser_runloop.py / outbox_dispatcher.py / watchdog_scanner.py
  -> business/flows/runtime_orchestrator.py
  -> business/flows/runtime_common.py
  -> business/flows/execution_supervisor.py
  -> business/flows/runtime_views.py
  -> business/flows/watchdog_scanner.py
  -> src/automation_business_scaffold/project_env.py
  -> src/automation_business_scaffold/config.py
```

飞书读写业务差异按下面路径查:

```text
feishu_table_read
  -> business/handlers/api/feishu_table_read.py
  -> business/feishu/source_adapters.py
  -> adapter_code

feishu_table_write
  -> business/handlers/api/feishu_table_write.py
  -> business/feishu/projection_mappers.py
  -> mapper_code
```

## 3. Agent Artifact 边界

`agent` 在本项目中优先理解为“部署到 OpenClaw / Hermes / 用户 agent workspace 的入口产物”，而不是 Runtime DB 中的执行层。

当前仓库内的 agent artifact 源是:

```text
skills/{skill_code}/
  SKILL.md
  run_*_step.sh
  run_skill_step.py
  lightweight_submit.py
  skill.local.env.example
```

部署时，脚本负责:

1. 把 `skills/{skill_code}` 复制到目标 agent 读取的 skills 根目录，例如 `MUJITASK_SKILLS_DIR/{skill_code}`。
2. 在目标 skill 目录生成或保留 `skill.local.env`。
3. 在项目安装目录生成 runtime 配置，例如 `scripts/execution_control/executor.local.env`。
4. 安装/刷新 daemon、worker、outbox 等后台运行进程。

边界约束:

- Agent skill 只负责意图识别、少量参数提取、提交顶层 `task_request`、返回 `request_id`。
- Agent skill 不负责 workflow 主编排、worker retry、浏览器 runloop、最终 outbox 分发。
- 多个业务可以拥有多个 `skills/{skill_code}` bundle；bundle 名称应表达业务入口，不表达内部 handler/job。
- `skill.local.env` 是 agent skill 的部署配置，不是 Runtime DB / Fact DB / Object Store contract 的事实来源。
- Runtime 配置优先归口到项目安装目录下的 `executor.local.env` 或等价部署配置。

## 4. 目录职责契约

| 路径 | 职责 | 不应放入 |
| --- | --- | --- |
| `skills/{skill_code}/` | 仓库内 agent skill bundle 源；部署时复制到目标 agent workspace/skills 目录 | workflow 主编排、worker 执行逻辑、数据库 schema |
| `scripts/deploy/` | 安装项目、复制 skill bundle、生成部署配置、安装守护进程 | 业务字段映射、handler 实现 |
| `src/automation_business_scaffold/agent.py` | RPC Agent Service 门面，暴露 platform/framework 兼容 task registry 和提交入口 | daemon loop、业务字段 mapper |
| `src/automation_business_scaffold/cli.py` | 本地/manual task submit/status/result/control action 入口 | worker 具体执行、浏览器操作 |
| `src/automation_business_scaffold/*_daemon.py`、`browser_runloop.py`、`outbox_dispatcher.py`、`watchdog_scanner.py` | 常驻进程或 `--once` 进程门面，负责参数解析和调用 flow | stage/job 业务实现、外部 API 字段映射 |
| `src/automation_business_scaffold/project_env.py`、`config.py` | 项目配置加载和 typed defaults | handler 业务规则、部署脚本动作 |
| `scripts/execution_control/` | 运行控制配置示例、launchd 安装脚本和 daemon 启动包装 | 业务字段映射、Runtime schema 迁移 |
| `config/deployment/launchd/` | macOS launchd plist 模板 | Python 业务实现 |
| `business/tasks/` | 顶层 task 和兼容内部 task 入口，负责 submit/status/cancel 等入口参数适配 | worker 具体执行逻辑、外部 API 细节 |
| `business/workflow_defs/` | `WorkflowDefinition`、stage、job binding、summary/idempotency/timeout/watchdog policy | handler 实现、飞书字段映射函数 |
| `business/workflows/` | 旧 WorkflowSpec 或兼容声明层；新增主路径优先使用 `workflow_defs` | runtime worker handler 实现 |
| `business/jobs/` | Runtime job 定义门面；按 `job_code` 命名并暴露 `JOB_DEFINITION` | 直接调用外部 API、写业务字段映射 |
| `business/handlers/` | handler contract、registry，以及按 `handler_code` 命名的 worker 入口模块 | workflow 编排、table-specific mapper/policy |
| `business/handlers/api/` | API/HTTP/IO/FastMoss/Feishu/Fact/Object Store 类 handler 入口 | browser profile 操作、outbox 消息分发 |
| `business/handlers/browser/` | Browser/CDP/Profile 类 handler 入口 | API worker 逻辑、Feishu 写入 |
| `business/handlers/outbox/` | notification outbox 分发 handler 入口 | workflow summary 生成逻辑 |
| `business/feishu/` | 飞书 source adapter、projection mapper、表级读写业务差异 | handler registry key、Runtime job 编排 |
| `business/flows/` | 业务实现细节、runtime stage 推进、Execution Supervisor、Reconciler、Watchdog、旧链路兼容 flow | 稳定 handler/job contract 事实来源、部署配置源 |
| `infrastructure/` | 外部系统客户端、存储、Runtime Store、Fact Store、浏览器桥接等基础设施 | task/workflow/handler 业务语义 |
| `models/` | 跨层使用的数据模型 | 外部 API 调用流程 |
| `validators/` | 输入和业务数据校验 | runtime 编排 |
| `acceptance/` | 验收比较、runtime projection、测试投影工具 | runtime 主路径依赖 |

## 5. 命名契约

所有 runtime 稳定 code 使用 `snake_case`，表达语义，不表达版本或执行顺序。

| code | 文件位置 | 必须导出 |
| --- | --- | --- |
| `skill_code` | `skills/{skill_code}/` | `SKILL.md`、入口脚本、`skill.local.env.example` |
| `task_code` | `business/tasks/{task_code}.py` | task class 或 task entry |
| `workflow_code` | `business/workflow_defs/{workflow_code}.py` | build definition 函数 |
| `job_code` | `business/jobs/{job_code}.py` | `JOB_CODE`、`HANDLER_CODE`、`JOB_DEFINITION` |
| `handler_code` | `business/handlers/{worker_lane}/{handler_code}.py` | `HANDLER_CODE`、`CONTRACT`、handler callable 或占位 contract |
| `adapter_code` | `business/feishu/source_adapters.py` | adapter registry 中的同名函数或明确映射 |
| `mapper_code` | `business/feishu/projection_mappers.py` | mapper registry 中的同名函数或明确映射 |
| `daemon_code` | 根包 `*_daemon.py` 或 `{lane}_runloop.py` | console script、`main()`、`--once` 或 daemon loop 参数 |
| `control_plane_code` | `business/flows/runtime_*.py`、`execution_supervisor.py`、`watchdog_scanner.py` | runtime control function，不作为 handler registry key |

禁止:

- 在 `task_code`、`workflow_code`、`stage_code`、`job_code`、`handler_code` 中使用 `v1`、`v2`、`legacy`、`new`、`stage1` 这类版本/顺序后缀。
- 把 `orchestrate_*`、`run_*_workflow`、`run_sync_*`、`*_orchestrator` 写成 handler/job 文件名或 registry key。
- 把 `*_adapter`、`*_mapper`、`*_policy`、`*_renderer` 直接注册为 runtime handler。
- 新增业务专用 handler 来绕开已有 capability handler + adapter/mapper/policy 的组合，除非先更新 handler 准入清单。

## 6. 新增代码流程

新增 agent skill bundle:

1. 在 `skills/{skill_code}/` 新增 `SKILL.md`、入口脚本和 `skill.local.env.example`。
2. `SKILL.md` 只描述 agent 触发条件、参数提取、提交入口和首条回执契约。
3. 入口脚本只提交顶层 task，不串联 runtime 内部 leaf steps。
4. 如果需要部署安装，更新 `scripts/deploy/**` 或部署文档中的复制目标和配置生成规则。
5. 如果新增业务入口，同步新增或复用 `business/tasks/{task_code}.py` 和 `business/workflow_defs/{workflow_code}.py`。

新增正式 workflow:

1. 在 `business/tasks/{task_code}.py` 增加或确认入口。
2. 在 `business/workflow_defs/{workflow_code}.py` 定义 stage、job binding、summary policy。
3. 复用 `business/jobs/{job_code}.py` 中已有 job；没有则先新增 job contract。
4. 复用 `business/handlers/{worker_lane}/{handler_code}.py` 中已有 handler；没有则先更新 `handler-contract-design.md` 准入清单。
5. 如果是飞书表差异，优先新增/扩展 `business/feishu/source_adapters.py` 或 `business/feishu/projection_mappers.py`。
6. 复杂业务实现放入 `business/flows/**`，但 flow 不能成为 runtime registry key。
7. 同步测试: workflow contract、handler registry、job module、Feishu adapter/mapper registry。

新增 handler:

1. 先更新 [handler-contract-design.md](./handler-contract-design.md) 的准入清单和 payload/result/error/retry/timeout/idempotency/side effects。
2. 更新 `business/handlers/allowlist.py`。
3. 新增 `business/handlers/{worker_lane}/{handler_code}.py`。
4. 如需正式 runtime job，新增 `business/jobs/{job_code}.py`。
5. 更新 registry 绑定和契约测试。

新增飞书表级逻辑:

1. 读取候选筛选放入 source adapter。
2. 写回字段构造放入 projection mapper。
3. handler 只负责 Feishu transport、schema 校验、batch write、错误分类和幂等边界。
4. 如果表级逻辑需要独立 retry/timeout/artifact/外部副作用，先按 business handler candidate 评审，不能直接塞进 handler registry。

新增 Runtime 控制面入口:

1. 先更新 [runtime-control-plane-contract.md](./runtime-control-plane-contract.md) 的组件归属和命名规则。
2. 根包只新增薄门面文件，例如 `*_daemon.py`、`*_runloop.py` 或 service entry。
3. 运行控制逻辑放入 `business/flows/runtime_*.py`、`execution_supervisor.py`、`runtime_views.py`、`watchdog_scanner.py` 或明确的新 control-plane flow。
4. 新增 console script 时同步 `pyproject.toml`、部署模板、README/ops 文档和结构契约测试。
5. 项目配置新增项必须同步 `project_env.py` / `config.py`、`*.env.example`、`docs/dev/project-configuration.md` 和测试。

## 7. 测试护栏

结构契约由以下测试守住:

| 测试 | 守护内容 |
| --- | --- |
| `tests/test_project_structure_contract.py` | 本文存在、目录职责关键词存在、agent skill bundle 边界、禁止命名规则存在 |
| `tests/test_runtime_control_plane_contract.py` | Runtime 控制面契约、RPC/daemon/config/watchdog/supervisor/reconciler 入口、console script 和配置优先级 |
| `tests/test_handler_registry_contract.py` | handler allowlist、同名 handler module、禁止 handler 名称 |
| `tests/test_workflow_defs_contract.py` | workflow_defs 和 jobs 同名模块、核心 contract 字段 |
| `tests/test_feishu_common_handlers.py` | Feishu source adapter / projection mapper registry |

任何新增 skill/workflow/job/handler/adapter/mapper 时，如果这些测试需要修改，必须同步解释为什么结构契约发生了变化。

## 8. 最终规则

一句话规则:

> Skill/Agent 定部署入口产物，Task 定业务入口，Workflow 定阶段，Job 定可执行单元，Handler 定 worker 能力，Adapter/Mapper 定业务字段差异，Flow 承载实现细节；文件名必须跟稳定 code 对齐，registry 只能引用已准入 contract，结构变化必须同步文档和测试。
