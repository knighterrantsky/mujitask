# Public Timeline

这份文档面向业务仓库与业务侧 Codex，用来同步 platform 当前的公开演进节奏。

说明：

- 这不是 release note。
- 这不是内部任务拆解。
- 这里只记录会影响业务决策的公开节奏。

当前基线：

- current framework version: `0.2.1`
- current reference commit: `55e8223a92f562f4053006c55e66fe5491c9be61`

## Current

当前已经可对业务开放的能力：

- 显式 task registry + `create_app(...)`
- `BaseWorkflowTask` + `WorkflowSpec`
- step / signal / artifact 持久化与查询
- `run_mode` 对 side effect 的基础约束
- recorder MVP
- review-only `workflow_draft` 生成

业务建议：

- 现在就可以基于 step runtime 开发新的自动化业务流程
- 现在就可以把 `trace -> workflow_draft -> WorkflowSpec` 当作最小闭环

## Next

下一阶段优先对外补齐的能力：

- 更稳定的 business-facing contract 文档包
- scaffold 的受保护区与升级清单
- 更清晰的 migration 文档与版本差异说明
- business-side 更完整的模板与示例任务

业务建议：

- 新业务优先以 scaffold 为基线开工
- 升级 framework 前，先比对 contract pack 与 migration guide

## Later

后续规划中的能力：

- runtime 直接加载 `workflow.yaml`
- replay / rerun 支撑能力
- 更完整的 approval gate
- 更自动化的 scaffold 升级流程

业务建议：

- 这些方向可以纳入长期架构预期
- 但当前版本不要把它们写进业务实现前提

## Not Committed

当前没有对业务承诺交付时间的方向：

- LLM repair / ReAct loop
- 完整可视化 workflow 编辑器
- 自动把业务仓库升级到最新 scaffold 的统一工具

这些方向只有在进入公开 contract 后，才会进入 `public-capability-status` 与 `public-migration-guide`。
