# Workflow 实现模式规范

日期: 2026-04-24

状态: 受控代码结构与实现模式契约

## 1. 定位

本文约束“新增 workflow 或新业务流程”时的代码实现模式。它不是迁移 checklist；迁移旧代码见 [real-migration-checklist.md](./real-migration-checklist.md)。

本文目标:

- 让新 workflow 默认按项目结构开发。
- 明确每类文件必须写什么、不写什么。
- 固定常用设计模式，避免把业务逻辑重新堆进 handler、flow、daemon 或 agent script。
- 给代码评审和自动测试提供可检查的结构标准。

相关文档:

- 项目结构: [project-architecture-contract.md](./project-architecture-contract.md)
- 当前系统架构: [system-architecture-design.md](./system-architecture-design.md)
- Workflow 拆分规则: [workflow-design-guidelines.md](./workflow-design-guidelines.md)
- Handler contract: [handler-contract-design.md](./handler-contract-design.md)

## 2. 新 Workflow 固定开发顺序

新增 workflow 必须按下面顺序设计和落文件:

```text
skills
  -> domains/{domain}/tasks
  -> domains/{domain}/workflows
  -> domains/{domain}/jobs
  -> domains/{domain}/mappers
  -> domains/{domain}/policies
  -> domains/{domain}/projections
  -> capabilities/**
  -> control_plane/outbox
  -> tests / fixtures
```

顺序含义:

1. 先定义用户如何触发业务。
2. 再定义顶层 task 和 workflow DAG。
3. 再定义 job contract。
4. 再拆业务定制 mapper / policy / projection。
5. 最后补通用 capability handler。
6. 所有最终通知走 outbox。

禁止先写一个大 flow 再事后拆分。

## 3. Workflow Architecture Manifest

每个 workflow 必须有一份结构清单:

```text
contracts/workflow/{workflow_code}.yaml
```

这份 manifest 是新 workflow 的“架构物料清单”，用于把自然语言设计变成自动检查。它必须声明:

- `workflow_origin`: 只能是 `new_workflow` 或 `migrated_existing`。
- `workflow_code` / `domain` / `task` / `workflow`。
- `agent_artifact`: skill code、部署产物源路径和当前状态。
- `jobs`: 每个 job 的 domain job module、`job_code`、`handler_code`、capability module、handler export。
- `custom_logic`: mapper / policy / projection 的 code、module、export。
- `outbox`: summary/outbox job 和通道 handler。
- `known_architecture_gaps`: 只允许迁移中的旧 workflow 使用，用来显式记录尚未拆出的 mapper / projection / policy。

新增 workflow 的硬约束:

- manifest 中的 `workflow_origin` 必须是 `new_workflow`。
- 不允许填写 `known_architecture_gaps`。
- `domains/{domain}/tasks/{task_code}.py` 必须定义 `TASK_CODE`。
- `domains/{domain}/workflows/{workflow_code}.py` 必须定义 `WORKFLOW_CODE`。
- workflow stage 中出现的 `adapter_code` / `mapper_code` 必须能在 `custom_logic` 中找到真实 module 和 export。
- custom logic export 必须在声明的文件内真实定义，不能只 re-export 其他模块。
- capability handler export 必须在 capability 文件内真实定义，不能从旧路径或 `_implementations` 聚合文件转发。

迁移中的旧 workflow 可以标记为 `migrated_existing`，但所有 gap 必须写入 `known_architecture_gaps`。这不是豁免，而是待拆分清单；迁移完成后必须删除 gap 并转为真实 module/export。

自动检查入口:

- `test_workflow_architecture_manifests` 会检查每个 `domains/*/workflows/{workflow_code}.py` 都有 manifest。
- 对 `new_workflow`，测试会启用严格模式，阻止缺文件、缺导出、job/handler 绑定错误、mapper/projection/policy 未落文件、兼容 facade 回流。
- 对 `migrated_existing`，测试会允许显式 gap，但会阻止未登记的隐性 gap。

## 4. 文件模式

### 4.1 Agent Artifact Pattern

项目目录:

```text
skills/{skill_code}/
```

必须包含:

- `SKILL.md`: 触发条件、参数提取、回执说明。
- `run_*_step.sh`: agent 可调用入口。
- `run_skill_step.py` 或轻量 submit helper。
- `skill.env.example` 或 `skill.local.env.example`。

只允许:

- 读取 agent 本地配置。
- 提取少量参数。
- 调用 task request submit。
- 返回 `request_id`、首条状态和用户可读摘要。

禁止:

- 直接消费 `api_worker_job` / `task_execution` / `notification_outbox`。
- 串联内部 leaf step。
- 写 workflow 主编排。
- 调用 Feishu / TikTok / FastMoss / Discord API。

### 4.2 Task Entry Pattern

项目目录:

```text
domains/{domain}/tasks/{task_code}.py
```

职责:

- 定义顶层业务入口。
- 校验用户可见参数。
- 生成 task request payload。
- 暴露 task metadata。

必须包含:

- `TASK_CODE`
- task class 或 task factory
- payload validation
- submit/status/result 入口绑定

禁止:

- claim job。
- 执行外部 API。
- 直接写 Runtime DB 表。
- 处理 stage DAG。
- 写 mapper/projection。

### 4.3 Workflow Definition Pattern

项目目录:

```text
domains/{domain}/workflows/{workflow_code}.py
```

职责:

- 定义 workflow_code。
- 定义 stage DAG。
- 定义每个 stage 绑定哪些 job。
- 定义 summary/result/outbox 触发点。
- 定义 stage 级 timeout/watchdog/retry 策略引用。

必须包含:

- `WORKFLOW_CODE`
- workflow definition builder
- stage list / DAG
- job binding
- summary policy reference
- outbox policy reference

禁止:

- 调用 handler。
- 调用 external client。
- 写 Feishu 字段。
- 写 FastMoss / TikTok 采集逻辑。
- 把 stage 顺序写进 code 名称，例如 `stage1`、`v2`。

### 4.4 Job Contract Pattern

项目目录:

```text
domains/{domain}/jobs/{job_code}.py
```

职责:

- 定义可执行单元。
- 绑定 capability handler。
- 定义 payload schema / result schema。
- 定义 idempotency key / dedupe key。
- 声明 mapper / projection / policy code。

必须包含:

- `JOB_CODE`
- `HANDLER_CODE`
- `JOB_DEFINITION`
- payload rendering rule
- result consumption rule

禁止:

- 直接实现 API transport。
- 直接写外部系统。
- 承载业务大 flow。
- 把 mapper/projection/policy 注册成 handler code。

### 4.5 Mapper Pattern

项目目录:

```text
domains/{domain}/mappers/{source}_{business_object}_mapper.py
```

职责:

- 把输入源行或事实源结果转换成业务对象。
- 解释业务字段含义。
- 标准化业务 key。
- 输出 domain object / candidate / seed / writeback context。

允许:

- 引用业务字段名。
- 做字段兼容和默认值。
- 做业务对象的纯函数转换。

禁止:

- 调用 Feishu / TikTok / FastMoss API。
- 写 Runtime DB / Fact DB / Object Store。
- claim job。
- 发通知。

### 4.6 Policy Pattern

项目目录:

```text
domains/{domain}/policies/{policy_code}.py
```

职责:

- 选择、过滤、排序。
- retry / timeout / idempotency 业务语义。
- 事实合并、可信度、冲突处理。
- finalizer / summary / terminal status 判定。

允许:

- 引用 domain object。
- 读取 handler result envelope。
- 输出 policy decision。

禁止:

- 直接调用 external client。
- 直接发送 outbox。
- 写 handler transport 细节。

### 4.7 Projection Pattern

项目目录:

```text
domains/{domain}/projections/{target}_{view}_projection.py
```

职责:

- 把业务对象或 workflow result 转为外部可见字段。
- 生成 Feishu / Dingding 表格字段。
- 生成 Discord / Feishu / Dingding 消息内容。
- 生成报表或验收投影。

允许:

- 引用外部视图字段名。
- 处理字段缺失、格式化、枚举展示。
- 输出 projection payload。

禁止:

- 直接调用 channel API。
- 写入数据库。
- 处理 retry / lease / heartbeat。

### 4.8 Capability Handler Pattern

项目目录:

```text
capabilities/{category}/{system}/{capability}_handler.py
```

职责:

- 实现可复用外部能力。
- 处理 transport、鉴权、分页、限速、错误分类。
- 返回标准 HandlerResult。

必须包含:

- `HANDLER_CODE`
- `CONTRACT`
- handler function
- payload parsing
- external client / store 调用
- retryable / fatal error 分类
- summary/result envelope

禁止:

- 写 domain 专属字段筛选。
- 写 domain projection。
- 推进 parent task。
- 直接构造 workflow summary。
- re-export 旧 handler。
- 使用 `_implementations` 聚合大文件。

### 4.9 Outbox Channel Pattern

项目目录:

```text
capabilities/channels/{channel}/{message_type}_handler.py
control_plane/outbox/**
```

职责:

- domain projection 生成消息内容。
- workflow/executor 写入 `notification_outbox`。
- outbox dispatcher claim outbox。
- channel handler 发送消息。

禁止:

- workflow 直接调用 Discord / Dingding / Feishu 通知 API。
- channel handler 修改业务 task 终态。
- outbox 失败反向改成功业务状态。

## 5. 依赖方向

允许依赖方向:

```text
apps -> control_plane
control_plane -> contracts / infrastructure stores / domain workflow contract / capability registry
domains -> contracts / capabilities contract / domain internal modules
capabilities -> contracts / infrastructure clients / infrastructure stores
infrastructure -> external systems / low-level libraries
```

禁止依赖方向:

```text
apps -> domains.mappers / domains.projections / domains.policies
control_plane -> domain business fields
capabilities -> domains
infrastructure -> domains / workflows / tasks / jobs
domains -> infrastructure.clients
skills -> runtime tables / worker queues
```

## 6. 命名规则

稳定 code 使用 `snake_case`，表达语义，不表达版本或执行顺序。

必须稳定:

- `skill_code`
- `task_code`
- `workflow_code`
- `stage_code`
- `job_code`
- `handler_code`
- `mapper_code`
- `projection_code`
- `policy_code`
- `channel_code`

禁止:

- `v1`、`v2`、`new`、`legacy`、`stage1`、`stage2`。
- `orchestrate_*` 作为 handler / job。
- `run_*_workflow` 作为 handler / job。
- `*_mapper`、`*_projection`、`*_policy` 作为 handler_code。

## 7. 新 Workflow 文件清单

新增 workflow PR 至少应包含:

```text
contracts/workflow/{workflow_code}.yaml
domains/{domain}/tasks/{task_code}.py
domains/{domain}/workflows/{workflow_code}.py
domains/{domain}/jobs/{job_code}.py
domains/{domain}/mappers/{source}_{object}_mapper.py
domains/{domain}/policies/{policy_code}.py
domains/{domain}/projections/{target}_{view}_projection.py
capabilities/{category}/{system}/{capability}_handler.py
tests/test_{workflow_code}_workflow_contract.py
tests/test_{workflow_code}_mappers.py
tests/test_{workflow_code}_projections.py
tests/test_{workflow_code}_policies.py
tests/test_{capability_code}_handler.py
```

如果 workflow 复用已有 mapper/projection/policy/capability，应在 workflow contract 测试中明确断言复用项。

## 8. 测试模式

每个新 workflow 至少覆盖:

| 测试 | 目的 |
| --- | --- |
| workflow architecture manifest test | manifest 到真实文件、函数导出、job/capability/custom logic 绑定 |
| workflow contract test | workflow_code、stage、job binding、summary/outbox contract |
| job contract test | job_code、handler_code、payload schema、idempotency key |
| mapper test | 输入源/事实源 fixture 到 domain object |
| policy test | selection/filter/idempotency/finalize 决策 |
| projection test | domain object 到 Feishu/Dingding/Discord 字段 |
| capability handler test | payload 到 HandlerResult，包含 retry/fatal error |
| runtime trace fixture | submit 后 Runtime DB 状态、jobs、outbox 投影 |

测试导入必须使用目标路径，例如:

```python
from automation_business_scaffold.domains.tiktok.workflows.refresh_current_competitor_table import ...
from automation_business_scaffold.capabilities.input_sources.feishu.table_read_handler import ...
```

禁止为了测试方便导入旧 legacy 主路径。

## 9. Code Review Checklist

评审新增 workflow 时逐项检查:

- [ ] 是否按 `agent -> task -> workflow -> job -> mapper/policy/projection -> capability -> outbox` 顺序拆分。
- [ ] 是否新增或更新了 `contracts/workflow/{workflow_code}.yaml`。
- [ ] 如果是新增 workflow，`workflow_origin` 是否为 `new_workflow` 且没有 `known_architecture_gaps`。
- [ ] 是否所有新文件都在项目目录。
- [ ] 是否没有 facade / shim / re-export。
- [ ] 是否没有 `_implementations` 聚合大文件。
- [ ] 是否 capability handler 拥有真实实现。
- [ ] 是否 domain mapper/projection/policy 不调用 external client。
- [ ] 是否 workflow 不直接调用 handler 或 external API。
- [ ] 是否所有最终通知走 outbox。
- [ ] 是否 Runtime DB / Fact DB / Object Store 落点清晰。
- [ ] 是否测试导入目标路径。
