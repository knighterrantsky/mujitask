# Machine Contracts

更新时间: 2026-04-25

本目录存放字段、状态和 workflow 的机器可读契约。Markdown 文档解释背景、边界和取舍；本目录定义“到底是什么”，方便后续做索引、生成和 CI 校验。

## 目录边界

| 目录 | 定位 |
| --- | --- |
| [codex](./codex) | Codex app 根目录短 Prompt 的任务路由和最小上下文选择 |
| [fields](./fields) | 飞书业务表字段角色、更新策略、来源和写回目标 |
| [facts](./facts) | 跨 workflow 的事实采集、Fact DB 与对象存储边界 |
| [harness](./harness) | code roadmap、completion claim gate 和任务完成声明契约 |
| [skill_contract.md](./skill_contract.md) / [skill_spec.schema.json](./skill_spec.schema.json) | agent skill spec、生成产物和 CI 校验契约 |
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
- Amazon 商品详情字段与行状态分别以 `fields/feishu-amazon-products.yaml` 和 `states/amazon-product-collection-status.yaml` 为准；首期四阶段流程以 `workflow/refresh_amazon_product_row_by_asin.yaml` 为准。
- 改 Codex 根目录任务分类、默认上下文或禁止读取范围时，必须同步更新 `contracts/codex/**` 和对应测试。
- 改 feature 完成判定、done gate 或 root prompt roadmap 时，必须同步更新 `contracts/harness/**`、`scripts/harness/**` 和对应测试。
- 改 agent skill 入口、意图路由、输入抽取、输出回执或失败处理时，必须同步更新 `skills/{skill_code}/skill.spec.yaml`，重新生成 `SKILL.md`，并通过 `tools/validate_skill.py`。
- `docs/reference/**` 和 `docs/business/requirements-backlog.md` 不能被 workflow contract 标为正式需求来源。
