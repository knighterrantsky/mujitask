# Machine Contracts

更新时间: 2026-04-25

本目录存放字段、状态和 workflow 的机器可读契约。Markdown 文档解释背景、边界和取舍；本目录定义“到底是什么”，方便后续做索引、生成和 CI 校验。

## 目录边界

| 目录 | 定位 |
| --- | --- |
| [fields](./fields) | 飞书业务表字段角色、更新策略、来源和写回目标 |
| [states](./states) | 业务状态字段的枚举、终态和重置规则 |
| [workflow](./workflow) | workflow 的业务入口、字段契约、阶段和不变量索引 |

## 事实来源关系

- 客户需求和验收口径仍以 `docs/business/**` 为准。
- 架构边界和运行设计仍以 `docs/arch/**` 为准。
- 本目录承接字段、状态和 workflow 结构中适合机器校验的部分。
- 代码包内实现侧 workflow manifest 位于 `src/automation_business_scaffold/contracts/workflow/**`；本目录的 workflow 契约作为仓库级阅读和校验入口，必须链接到对应实现 manifest。

## 维护规则

- 改字段含义、pending 判断、状态枚举或系统覆盖策略时，必须同步更新 `contracts/fields/**` 或 `contracts/states/**`。
- 改 workflow stage、job、handler、adapter、projection 或关键不变量时，必须同步更新 `contracts/workflow/**`。
- `docs/reference/**` 和 `docs/business/requirements-backlog.md` 不能被 workflow contract 标为正式需求来源。
