# Handler Contract 摘要

日期: 2026-04-25

状态: 受控架构契约摘要

本文是 [handler-contract-design.md](./handler-contract-design.md) 的短入口。只有修改 handler payload/result/error 细节、冻结样例或兼容策略时，才继续读取详细契约。

## 核心边界

1. Handler 是 Runtime job 的代码入口，必须使用稳定 `handler_code`，不在名称中追加 `v1`、`v2`、`stage1` 这类版本或顺序后缀。
2. Handler result/error 必须使用统一 envelope，调用方不能依赖未声明的临时字段。
3. 兼容变更优先新增可选字段；破坏性变更必须通过 `contract_revision`、adapter、migration 或新语义 handler 表达。
4. `feishu_table_read` 只负责稳定读取、分页、schema 校验、错误分类和通用 result envelope。
5. `feishu_table_write` 只负责通用写入、字段存在性校验、错误分类和写入结果 envelope。
6. 表级业务语义必须放在 source adapter 或 projection mapper 中，例如 `competitor_table_source_adapter`、`competitor_table_projection_mapper`、`influencer_pool_projection_mapper`。
7. Adapter / mapper 不是 registry handler，不允许把它们注册成独立 `handler_code`。

## 详细契约

- Result/error envelope、P0 冻结样例、Feishu handler 边界和 handler registry 禁区见 [handler-contract-design.md](./handler-contract-design.md)。
