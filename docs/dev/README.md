# Dev 文档索引

更新时间: 2026-04-24

本目录用于承载开发、调试、集成、本地验证和代码维护相关文档。

## 事实来源边界

`docs/dev` 是开发工作流、调试方法、集成说明和代码维护约定的事实来源。

它不作为客户需求、系统架构、部署运维或外部接口研究的事实来源:

- 客户需求和业务验收口径见 [../business/README.md](../business/README.md)。
- 系统架构、workflow、Runtime DB、Fact DB 和 Storage 设计见 [../arch/README.md](../arch/README.md)。
- 后续重构和新增业务的项目工程组织方式见 [../arch/project-architecture-contract.md](../arch/project-architecture-contract.md)。
- 工程结构、文件命名和代码定位契约见 [../arch/project-structure-contract.md](../arch/project-structure-contract.md)。
- RPC/CLI/daemon/config/watchdog/supervisor/reconciler 的运行控制面契约见 [../arch/runtime-control-plane-contract.md](../arch/runtime-control-plane-contract.md)。
- 部署、验收、回退和 runbook 见 [../ops/README.md](../ops/README.md)。
- 外部接口、页面研究和字段样例见 [../reference/README.md](../reference/README.md)。

代码实现仍以仓库当前源码为准。本文档域用于帮助开发者理解和维护实现，不替代源码本身。

## 文档

| 文档 | 说明 |
| --- | --- |
| [documentation-change-policy.md](./documentation-change-policy.md) | 代码实现时哪些文档可同步修改、哪些需要确认、哪些不应修改 |
| [openclaw-skills.md](./openclaw-skills.md) | OpenClaw skill 的开发集成边界、入口脚本和调试口径 |
| [project-configuration.md](./project-configuration.md) | 项目级 `.env` / `executor.local.env` / `skill.local.env` 的自动加载规则和优先级 |
| [rewrite-state.yaml](./rewrite-state.yaml) | 当前重构阶段、canonical owner、legacy reference 和必须保持绿色的检查 |
| [rewrite-development-plan.md](./rewrite-development-plan.md) | 本轮重构的依赖关系、开发顺序、subagent 并行拆分和 worktree 计划 |
| [worktree-parallel-development-handoff.md](./worktree-parallel-development-handoff.md) | 当前 checkpoint 之后的 worktree 并行开发分工、边界、建议测试和开工提示词 |

## 维护规则

1. 新增本地开发、测试、调试、代码生成、脚手架、skill 集成和维护流程文档时，优先放在本目录。
2. 如果文档描述的是系统架构、Runtime 状态机或数据模型设计，应放在 `docs/arch`。
3. 如果文档描述的是客户需求、业务字段含义或验收口径，应放在 `docs/business`。
4. 如果文档描述的是生产部署、回滚、巡检或故障处理，应放在 `docs/ops`。
