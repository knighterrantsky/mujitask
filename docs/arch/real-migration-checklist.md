# 真实迁移 Checklist

日期: 2026-04-24

状态: 受控迁移验收契约

## 1. 定位

本文约束“把已有代码迁移到项目结构”的验收口径。它只用于迁移旧实现，不用于新 workflow 从零开发。

相关文档:

- 正式结构: [project-architecture-contract.md](./project-architecture-contract.md)
- 当前系统分层: [system-architecture-design.md](./system-architecture-design.md)
- 新 workflow 开发模式: [workflow-implementation-patterns.md](./workflow-implementation-patterns.md)
- 重构验收基准: [rewrite-acceptance-contract.md](./rewrite-acceptance-contract.md)

## 2. 迁移模式声明

每次迁移任务开始前，必须明确声明迁移模式。

| 模式 | 含义 | 允许交付 | 不允许声称 |
| --- | --- | --- | --- |
| `scaffold` | 为正式结构预留目录、模块和测试入口 | 空模块、TODO、导入计划、结构文档 | 真实迁移完成 |
| `real_migration` | 把实现所有权从旧目录搬到项目目录 | 项目目录真实实现、旧实现只作参考、目标 import 主路径 | 兼容包装算完成 |

当任务描述包含“真实代码迁移”“不要兼容旧逻辑”“旧逻辑只作为参考”“全部文件按真实逻辑拆分”时，必须使用 `real_migration`。

## 3. 通用完成标准

一次 `real_migration` 完成时，必须同时满足:

- 项目目录拥有真实实现。
- runtime import 主路径指向项目目录。
- 旧目录不再承载主实现。
- 旧实现只作为功能验证参考，不作为运行时依赖。
- 行为对照或 fixture 证明迁移前后的业务结果等价。
- 静态结构检查能阻止 facade / shim / re-export 回流。

不算完成:

- 只移动 import 路径，不移动实现代码。
- 新文件只写 `from .implementations import xxx`。
- 新文件只写 `from automation_business_scaffold.business... import xxx`。
- 使用 `sys.modules[__name__] = old_module`。
- 新建 `capabilities/_implementations/api.py`、`domains/_legacy.py` 等大杂烩。
- 为了 monkeypatch 或旧测试保留旧 import 主路径。
- 只跑通旧测试，没有证明实现归属已经迁移。

## 4. 分层迁移 Checklist

### 4.1 Apps

目标:

- `apps/rpc_agent/**` 承接 RPC/HTTP agent service。
- `apps/cli/**` 承接 CLI。
- `apps/daemons/{daemon_code}/main.py` 承接 daemon main。

完成标准:

- console script 或根包入口直接调用 `apps/**`。
- `apps/**` 只解析参数、加载配置、调用 `control_plane/**`。
- `apps/**` 不 import domain mapper / projection / policy。

禁止:

- 根包 `*_daemon.py` 继续承载真实 daemon 逻辑。
- `apps/**` 转调旧根包 daemon 再执行。
- `apps/**` 写业务字段映射或 handler 实现。

### 4.2 Control Plane

目标:

- `control_plane/task_requests/**` 承接 submit/status/result/cancel。
- `control_plane/executor/**` 承接 workflow 推进和 stage/job 调度。
- `control_plane/supervisor/**` 承接 Execution Supervisor 和 child runner。
- `control_plane/reconciler/**` 承接 parent-child 汇总和 runtime views。
- `control_plane/watchdog/**` 承接 watchdog rules 和 scanner。
- `control_plane/outbox/**` 承接 outbox 控制和 dispatcher。
- `control_plane/runtime_config/**` 承接配置加载和 typed settings。

完成标准:

- 目标模块拥有原控制逻辑函数。
- `business/flows/runtime_*` 不再是控制面主实现。
- 控制面只依赖 contracts、stores、domain workflow contract 和 capability registry。

禁止:

- `control_plane/**` re-export `business/flows/**`。
- 在 control plane 中写 Feishu 字段、FastMoss 筛选、TikTok 页面业务策略。
- 为单个业务新增专用 supervisor / reconciler / watchdog。

### 4.3 Domains

目标:

- `domains/{domain}/tasks/**` 承接业务入口。
- `domains/{domain}/workflows/**` 承接 workflow definition。
- `domains/{domain}/jobs/**` 承接 job contract。
- `domains/{domain}/mappers/**` 承接输入源 / 事实源到业务对象的映射。
- `domains/{domain}/projections/**` 承接业务结果到表格 / 消息字段的投影。
- `domains/{domain}/policies/**` 承接筛选、幂等、终态、summary、finalize 规则。
- `domains/{domain}/flows/**` 承接业务组合逻辑。

完成标准:

- domain 文件内有真实 `def` / `class` / `JOB_DEFINITION` / workflow definition。
- domain 不 re-export `business/**`。
- domain 不直接调用 infrastructure client。
- domain 通过 job contract 绑定 capability handler。

禁止:

- `domains/{domain}/tasks/*.py` 只导入 `business/tasks/*.py`。
- `domains/{domain}/workflows/*.py` 只导入 `business/workflow_defs/*.py`。
- `domains/{domain}/mappers/*.py` 只导入 `business/feishu/*.py`。
- `domains/{domain}/flows/*.py` 只导入 `business/flows/runtime_*.py`。

### 4.4 Capabilities

目标:

- `capabilities/input_sources/{source}/**` 承接输入源 handler。
- `capabilities/fact_sources/{source}/**` 承接事实源 handler。
- `capabilities/persistence/{store}/**` 承接数据库 / 对象存储 handler。
- `capabilities/channels/{channel}/**` 承接出站通道 handler。
- `capabilities/browser/**` 承接浏览器 / CDP / profile handler。
- `capabilities/media/**` 承接媒体和 artifact handler。

完成标准:

- 每个 capability handler 文件拥有真实 handler 函数和主要 helper。
- 旧 `business/handlers/**/implementations.py` 不再承载主实现。
- handler registry 从 capability 文件导入真实 handler。
- capability 不写 domain 专属 mapper / projection / policy。

禁止:

- `capabilities/_implementations/api.py` 或同类聚合大文件。
- capability 文件只 `from .implementations import xxx_handler`。
- capability 文件只 `from automation_business_scaffold.business.handlers... import xxx_handler`。
- 使用 `sys.modules` alias 替换模块。

### 4.5 Contracts

目标:

- `contracts/runtime/**` 承接 runtime envelope、状态、错误、lease、retry contract。
- `contracts/workflow/**` 承接 workflow/stage/job definition contract。
- `contracts/handler/**` 承接 handler payload/result/error contract。
- `contracts/config/**` 承接配置 schema。
- `contracts/outbox/**` 承接 outbox envelope 和 channel contract。

完成标准:

- contracts 不 import domain 实现。
- contracts 可以被 apps/control_plane/domains/capabilities 共同引用。
- 破坏性 contract 变更必须说明 migration / adapter / 兼容窗口。

## 5. 静态验收规则

迁移完成后，必须通过静态检查。

必须禁止的文本或 AST 模式:

- `from .implementations import`
- `from ..implementations import`
- `sys.modules[__name__]`
- `capabilities/_implementations`
- `from automation_business_scaffold.business.handlers`
- `from automation_business_scaffold.business.flows`
- `from automation_business_scaffold.business.tasks`
- `from automation_business_scaffold.business.workflow_defs`
- `facade`
- `shim`
- `re-export`
- `reexport`

允许例外:

- `docs/**` 中描述历史问题。
- `tests/**` 中明确的迁移违规测试 fixture。
- `business/**` 自身在清理前可以作为旧路径，但不得被目标主路径 import。

## 6. 行为验收规则

真实迁移不能只看测试是否绿，还要看行为是否等价。

每个迁移单元至少保留一种行为对照:

- mapper/projection: 输入 fixture -> 输出 fixture。
- capability handler: handler payload -> HandlerResult fixture。
- workflow: task payload -> runtime trace / fact projection / feishu projection / outbox projection。
- control plane: state transition fixture。

旧代码可以作为阅读参考和 fixture 生成来源，但新 runtime 不能 import 旧代码。

## 7. 提交前 Checklist

提交前逐项确认:

- [ ] 本次任务声明了 `scaffold` 或 `real_migration`。
- [ ] 如果是 `real_migration`，项目目录拥有真实实现。
- [ ] 没有新增 facade / shim / re-export / `sys.modules` alias。
- [ ] 没有新增 `_implementations` 聚合大文件。
- [ ] runtime registry / console script / task registry 指向项目目录。
- [ ] 旧路径未作为 runtime 主路径。
- [ ] 新测试使用目标路径 import。
- [ ] 旧实现仅作为功能参考或 fixture 来源。
- [ ] 文档和契约测试同步更新。

