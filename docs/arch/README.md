# Arch 文档索引

日期: 2026-04-24

本目录维护当前系统架构和核心业务 workflow 的设计文档。

## 目录边界

`docs/arch` 是当前系统架构、workflow、Runtime DB、Fact DB、Storage 的 active truth source。历史 `docs/business` 中的架构升级方案、状态流转、数据库结构和当前系统架构说明，只作为迁移来源或历史参考，不再作为当前架构事实来源。

## 事实来源边界

`docs/arch` 是当前系统设计事实来源。它回答系统如何分层、workflow 如何拆分、Runtime/Fact/Storage 如何设计。

其中 Runtime DB schema、Fact DB schema、handler contract、workflow contract、入口/输出 contract 属于受控契约。它们可以随实现同步更新，但不能作为普通说明文字随意改写；变更必须说明 migration、兼容、权限和回滚边界。

它不是以下内容的事实来源:

- 客户需求、业务字段含义和验收口径: 见 [../business/README.md](../business/README.md)。
- 开发、调试和 skill 集成说明: 见 [../dev/README.md](../dev/README.md)。
- 部署、回退和生产 runbook: 见 [../ops/README.md](../ops/README.md)。
- 外部接口原始研究材料: 见 [../reference/README.md](../reference/README.md)。

## 架构总览

- [系统架构设计](./system-architecture-design.md)
- [项目架构契约](./project-architecture-contract.md)
- [项目结构与命名契约](./project-structure-contract.md)
- [Workflow 设计与拆分规范](./workflow-design-guidelines.md)
- [模块实现所有权契约](./module-ownership-contract.md)
- [飞书表 Adapter 与 Projection Mapper 契约](./feishu-table-adapter-projection-contract.md)
- [Runtime 控制面契约](./runtime-control-plane-contract.md)
- [Handler Contract 摘要](./handler-contract-summary.md)
- [Handler Contract 设计](./handler-contract-design.md)
- [入口与输出契约设计](./entry-output-contract-design.md)
- [数据库架构设计](./database-architecture-design.md)
- [Storage 架构设计](./storage-architecture-design.md)
- [Runtime DB Schema 设计](./runtime-db-schema-design.md)
- [Fact DB Schema 设计](./fact-db-schema-design.md)

## 业务流程设计

- [选品采集与关键词搜索选品写入 Workflow 设计](./workflow-selection-table-design.md)
- [达人同步 Workflow 设计](./workflow-influencer-pool-sync-design.md)
- [竞品采集与关键词搜索竞品写入 Workflow 设计](./workflow-competitor-table-design.md)
- [关键词搜索选品写入结构化重构说明](./refactor-search-keyword-selection-products.md)
- [关键词搜索竞品写入结构化重构说明](./refactor-search-keyword-competitor-products.md)
- [RuntimeStore Phase 2 重构说明](./refactor-runtime-store-phase-2.md)

正式商品流程命名:

| 业务流程 | task_code | 设计文档 |
| --- | --- | --- |
| 竞品采集 | `refresh_current_competitor_table` | [workflow-competitor-table-design.md](./workflow-competitor-table-design.md) |
| 选品采集 | `tiktok_fastmoss_product_ingest` | [workflow-selection-table-design.md](./workflow-selection-table-design.md) |
| 关键词搜索竞品写入 | `search_keyword_competitor_products` | [workflow-competitor-table-design.md](./workflow-competitor-table-design.md) |
| 关键词搜索选品写入 | `search_keyword_selection_products` | [workflow-selection-table-design.md](./workflow-selection-table-design.md) |

## 策略与设计草案

以下文档属于架构或数据策略讨论，不自动等同于当前实现事实:

| 文档 | 说明 |
| --- | --- |
| [数据采集策略与频率设计](./data-collection-strategy-design.md) | 采集频率、窗口数据和事实沉淀策略 |
| [TK 综合数据表设计](./future-tk-comprehensive-table-design.md) | 未来飞书业务数据模型草案，不替代当前飞书表事实或 Fact DB schema |

## 统一概念

- `Task`: 用户提交的一次顶层业务请求。
- `Workflow`: Task 的阶段编排定义。
- `Stage`: Workflow 中的一个阶段。
- `Job`: Runtime DB 中 worker 可 claim 的运行时执行单元。
- `Handler`: 处理某类 Job 的代码入口。
- `Flow`: Handler 内部复用的业务实现过程。
- `Runtime Control Plane`: RPC/CLI/daemon/config/watchdog/supervisor/reconciler/outbox 等运行控制入口和恢复机制。
- `Project Architecture`: 项目目录、模块归属和开发拆分契约，见 [project-architecture-contract.md](./project-architecture-contract.md)。
- `Implementation Pattern`: 新 workflow 开发时每类文件的固定职责、依赖方向和测试模式，见 [../dev/workflow-implementation-patterns.md](../dev/workflow-implementation-patterns.md)。
- `Module Ownership`: mapper/projection、capability handler、registry、common 的实现归属边界，见 [module-ownership-contract.md](./module-ownership-contract.md)。
- `Feishu Adapter/Projection Contract`: 飞书表读写中 source adapter 和 projection mapper 的字段策略、输入输出和禁止事项。

## 已删除的历史来源

以下历史 business 文档的架构内容已经被本目录拆分吸收，旧文件已从 `docs/business` 删除:

| 历史来源 | 当前事实来源 |
| --- | --- |
| 系统架构升级方案 | [system-architecture-design.md](./system-architecture-design.md)、[storage-architecture-design.md](./storage-architecture-design.md) |
| OpenClaw 输出协议 | [entry-output-contract-design.md](./entry-output-contract-design.md) |
| 状态流转图与进程交互时序图 | [runtime-db-schema-design.md](./runtime-db-schema-design.md)、[system-architecture-design.md](./system-architecture-design.md) |
| 系统升级数据库结构设计表 | [runtime-db-schema-design.md](./runtime-db-schema-design.md)、[database-architecture-design.md](./database-architecture-design.md) |
| 事实数据库 ERD 与表结构设计 | [fact-db-schema-design.md](./fact-db-schema-design.md) |
| TK 事实数据库升级影响评估 | [fact-db-schema-design.md](./fact-db-schema-design.md) |
| 当前系统架构 | [system-architecture-design.md](./system-architecture-design.md) 和具体 workflow 文档 |
