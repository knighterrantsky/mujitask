# Dev 文档索引

更新时间: 2026-04-24

本目录用于承载开发规范、实现模式、调试、集成、本地验证和代码维护相关文档。

## 事实来源边界

`docs/dev` 是开发实践指南、代码实现模式和开发工作流的事实来源。它回答"怎么开发、怎么跑、怎么测"。

系统架构契约（项目结构、模块归属等受控契约）见 [../arch/README.md](../arch/README.md)。

它不作为客户需求、系统架构、部署运维或外部接口研究的事实来源:

- 客户需求和业务验收口径见 [../business/README.md](../business/README.md)。
- 系统架构、workflow、Runtime DB、Fact DB 和 Storage 设计见 [../arch/README.md](../arch/README.md)。
- 项目结构、命名和模块归属契约见 [../arch/project-structure-contract.md](../arch/project-structure-contract.md)。
- 测试策略和验证流程见 [../test/README.md](../test/README.md)。
- 部署、验收、回退和 runbook 见 [../ops/README.md](../ops/README.md)。
- 外部接口、页面研究和字段样例见 [../reference/README.md](../reference/README.md)。

代码实现仍以仓库当前源码为准。本文档域用于帮助开发者理解和维护实现，不替代源码本身。

## 文档

### 开发实践指南

| 文档 | 说明 |
| --- | --- |
| [code-style.md](./code-style.md) | 代码风格、命名规则、分层边界和提交前检查 |
| [git-workflow.md](./git-workflow.md) | 日常开发分支命名、提交规范和合并流程 |
| [local-development.md](./local-development.md) | 本机环境搭建、本地启动、测试运行和常见问题排障 |
| [module-guide.md](./module-guide.md) | 模块阅读指南：从功能需求快速定位对应代码 |
| [dependencies.md](./dependencies.md) | Python 依赖、外部运行依赖和升级规则 |

### 实现模式与规范

| 文档 | 说明 |
| --- | --- |
| [workflow-implementation-patterns.md](./workflow-implementation-patterns.md) | 新增 workflow 的代码结构、设计模式、依赖方向和测试模式 |
| [workflow-design-guidelines.md](./workflow-design-guidelines.md) | 新增 workflow 的拆分规范、stage/job 颗粒度约束 |

### 开发工具与配置

| 文档 | 说明 |
| --- | --- |
| [project-configuration.md](./project-configuration.md) | 项目级 `.env` / `executor.local.env` / `skill.local.env` 的自动加载规则和优先级 |
| [openclaw-skills.md](./openclaw-skills.md) | OpenClaw skill 的开发集成边界、入口脚本和调试口径 |
| [documentation-change-policy.md](./documentation-change-policy.md) | 代码实现时哪些文档可同步修改、哪些需要确认、哪些不应修改 |

## 维护规则

1. 新增本地开发、测试、调试、代码生成、脚手架、skill 集成和维护流程文档时，优先放在本目录。
2. 如果文档描述的是系统架构、Runtime 状态机或数据模型设计，应放在 `docs/arch`。
3. 如果文档描述的是客户需求、业务字段含义或验收口径，应放在 `docs/business`。
4. 如果文档描述的是生产部署、回滚、巡检或故障处理，应放在 `docs/ops`。
