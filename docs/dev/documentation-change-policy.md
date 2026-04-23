# 文档修改治理规则

更新时间: 2026-04-23

状态: 开发维护约定

## 1. 定位

本文定义代码实现、架构调整、需求变更和运维变更时，哪些文档可以同步修改，哪些文档需要先确认，哪些文档普通业务开发不应修改。

本文不替代各目录 README 的事实来源边界，而是把这些边界整理成执行规则。

相关入口:

- [../../AGENT.MD](../../AGENT.MD)
- [../README.md](../README.md)
- [../business/README.md](../business/README.md)
- [../arch/README.md](../arch/README.md)
- [../ops/README.md](../ops/README.md)
- [../reference/README.md](../reference/README.md)

## 2. 快速决策表

| 文档/路径 | 默认规则 | 说明 |
| --- | --- | --- |
| `.platform/**` | 不直接修改 | platform-managed；需要 platform upgrade 口径 |
| `AGENT.MD` | 不直接修改 | 仓库级 agent 规则；普通业务实现不要改 |
| `docs/arch/current-system-architecture-design.md`、`workflow-*.md` | 可随实现同步修改 | 描述当前执行链路、workflow、stage/job/handler 拆分；stage/job/handler 命名约束是受控契约 |
| `docs/arch/runtime-db-schema-design.md`、`fact-db-schema-design.md` | 受控修改 | schema 设计事实来源；变更必须有 migration、兼容策略和权限边界 |
| `docs/arch/handler-contract-design.md`、`entry-output-contract-design.md` | 受控修改 | contract 事实来源；变更必须保持兼容，或显式说明 `contract_revision`、adapter、migration/回滚策略 |
| `docs/dev/**` | 可随开发维护同步修改 | 开发、调试、代码维护、skill 集成说明 |
| `docs/ops/**` | 可随部署/运维实现同步修改 | 部署、回退、巡检、runbook |
| `docs/reference/**` | 可补充参考资料 | 外部接口研究、页面分析、字段样例；不作为当前设计事实来源 |
| `docs/business/**` | 视内容决定，通常先确认 | 客户需求、业务规则、飞书字段口径、验收口径 |
| `README.md` | 可小改，慎重改入口口径 | 项目入口，不承载详细设计 |
| `docs/README.md` | 可小改索引 | 文档地图，不承载正文设计 |
| framework contract 文档 | 不在本仓库维护 | 直接读取 `automation-framework` 包或 framework 仓库 |

## 3. 不应直接修改的文档和路径

普通业务开发不要修改:

- `.platform/**`
- `AGENT.MD`
- framework contract 的本地复制文档

原因:

- `.platform/**` 和 `AGENT.MD` 定义仓库角色、模型工作规则、受保护路径和发布流程。
- framework public API、contract 和迁移说明不属于本仓库事实来源。
- 修改这些内容会改变协作规则，而不是单个业务功能。

如果确实需要修改:

1. 停止直接实现。
2. 明确说明这是 `platform_upgrade` 或仓库治理变更。
3. 先让用户确认修改范围。
4. 单独提交，避免混入普通业务实现。

## 4. 可以随代码实现同步修改的文档

### 4.1 `docs/arch`

当代码实现影响系统设计事实时，应同步更新 `docs/arch`。

| 代码变更 | 应更新文档 |
| --- | --- |
| 新增/修改 workflow、stage、job、handler | `workflow-*.md`、`workflow-redesign-review.md`、`handler-contract-design.md` |
| 修改 executor / worker / outbox / watchdog 架构 | `current-system-architecture-design.md` |
| 修改 Runtime 表、状态机、lease、retry、watchdog 字段 | `runtime-db-schema-design.md` |
| 修改 Fact DB 表、upsert、事实/关系/观测边界 | `fact-db-schema-design.md` |
| 修改 MinIO bucket、object prefix、artifact 生命周期 | `storage-architecture-design.md` |
| 修改入口/输出协议 | `entry-output-contract-design.md` |

原则:

- `docs/arch` 是系统设计事实来源。
- 设计文档应描述当前实现和目标架构的差异。
- 如果只是目标设计，必须标明“目标”或“演进建议”，不能写成当前事实。

### 4.1.1 Schema 与 Contract 的受控边界

`docs/arch` 可以随代码同步，但不是所有 arch 文档都可以像普通说明文档一样直接改。

以下内容属于受控设计契约:

| 契约 | 事实来源 | 变更要求 |
| --- | --- | --- |
| Runtime DB schema | `runtime-db-schema-design.md` | 必须有 migration、状态机影响说明、回滚/兼容策略 |
| Fact DB schema | `fact-db-schema-design.md` | 必须说明 upsert key、幂等规则、历史数据迁移和查询影响 |
| Handler contract | `handler-contract-design.md` | 必须说明 payload/result/error 是否兼容；破坏性变更要通过 `contract_revision`、migration、adapter 或新语义 handler 处理 |
| 入口/输出 contract | `entry-output-contract-design.md` | 必须说明调用方、返回结构、错误结构和兼容窗口 |

允许直接同步的情况:

- 文档补充当前真实实现。
- 新增可选字段，且旧调用方不受影响。
- 增加新 handler / job contract，不破坏旧 contract。
- 补充 migration 后的实际 schema 说明。

需要先确认或单独评审的情况:

- 删除字段。
- 修改字段类型或语义。
- 修改状态枚举含义。
- 修改 upsert key / dedupe key / idempotency key。
- 修改 handler 必填入参或标准 result/error 外壳。
- 在 `task_code`、`workflow_code`、`stage_code`、`job_code`、`handler_code` 或 payload/result 字段名中加入 `v1`、`v2`、`stage1`、`stage2B` 这类版本/顺序后缀。
- 将 `orchestrate_*`、`run_*_workflow`、`run_sync_*` 这类 workflow 编排入口写入 handler contract、handler registry、job handler 名称或目标 Job / Handler 映射表。
- 修改生产运行进程是否允许自动建表、改表、删表。

命名约束:

- Runtime workflow 和 handler contract 的稳定路由键不通过名称表达版本。
- 兼容新增字段可以随代码同步，但必须有默认行为。
- 破坏性变更需要先说明旧 Runtime job、旧 payload/result 消费方和迁移/回滚策略。
- 当前代码中的历史兼容 ID 可以作为实现事实记录，但新的架构设计和新增 workflow 必须使用稳定语义 code。

### 4.1.2 生产数据库 DDL 约束

生产运行进程不应拥有 DDL 权限。

约束如下:

- `executor_daemon`、`api_worker`、`browser_worker`、`outbox_dispatcher`、`watchdog` 使用运行账号，只允许读写运行数据。
- `CREATE TABLE`、`ALTER TABLE`、`DROP TABLE`、索引变更等 schema 操作只能由 migration 流程使用 migration 账号执行。
- 应用启动时可以检查 schema version，但不应在生产环境自动执行 schema 变更。
- 如果 schema version 不匹配，生产进程应 fail fast，阻止继续消费任务。
- 本地开发或一次性 bootstrap 可以保留建表能力，但必须和生产运行账号、生产启动路径隔离。

推荐数据库账号分层:

| 账号 | 使用者 | 权限 |
| --- | --- | --- |
| `mujitask_runtime_user` | daemon / worker / dispatcher / watchdog | `SELECT / INSERT / UPDATE / DELETE` |
| `mujitask_migration_user` | CI/CD migration 或人工发布 | `CREATE / ALTER / DROP / CREATE INDEX` |
| `mujitask_readonly_user` | 排查、BI、只读分析 | `SELECT` |

### 4.2 `docs/dev`

当代码实现影响开发、调试、测试、skill 集成和维护方式时，应同步更新 `docs/dev`。

适合内容:

- 本地开发流程。
- 调试步骤。
- 代码维护规则。
- skill 集成方式。
- 文档修改治理规则。

### 4.3 `docs/ops`

当代码实现影响部署、运行、巡检、回退和生产操作时，应同步更新 `docs/ops`。

适合内容:

- 部署脚本行为变化。
- launchd / systemd / cron 变更。
- 环境变量变更。
- smoke check、验收、回退步骤。
- 生产故障处理 runbook。

### 4.4 `docs/reference`

当调研外部接口、页面结构、字段样例或采集口径时，可以补充 `docs/reference`。

注意:

- `docs/reference` 是参考资料，不是当前需求或架构事实来源。
- 从 reference 推导为正式设计时，需要同步 `docs/arch`。
- 从 reference 推导为客户需求时，需要同步 `docs/business` 并通常先确认。

## 5. 需要先确认再修改的文档

### 5.1 `docs/business`

`docs/business` 是客户需求、业务规则、飞书业务字段含义和验收口径的事实来源。以下变更需要先确认:

| 变更 | 原因 |
| --- | --- |
| 改客户需求描述 | 会改变交付范围 |
| 改验收标准 | 会改变是否算完成 |
| 改飞书字段含义 | 会影响业务口径 |
| 新增正式业务流程 | 需要客户/业务确认 |
| 把需求池内容转为正式需求 | 需要确认优先级和范围 |
| 修改跨流程共用字段口径 | 会影响多个 workflow |

可以不询问的小改:

- 错别字。
- 链接修复。
- 已确认需求的格式整理。
- 给已经实现且已确认的流程补关联设计链接。

### 5.2 根 README 和 docs 总索引

`README.md` 和 `docs/README.md` 可以小改，但如果会改变项目入口口径，应先确认。

需要先确认的情况:

- 改项目定位。
- 改正式业务入口列表。
- 改推荐部署方式。
- 改文档目录边界。
- 删除或合并重要入口链接。

### 5.3 删除、归档和迁移文档

以下动作需要先确认:

- 删除 active 文档。
- 把 active 文档移到 archive。
- 合并多份文档并删除原文。
- 改变某份文档是否是事实来源。

已明确 superseded 的历史文档，可以在用户确认清理后删除或迁移。

## 6. 不在本仓库维护的文档

framework 相关 contract 不在本仓库维护。

规则:

- 不新增 framework contract 本地副本文档。
- 不把 `automation-framework` 的 API contract 复制进 Mujitask 文档。
- 需要 framework API、contract、迁移说明时，直接读取已安装的 `automation-framework` 包或 framework 仓库。
- 本仓库只记录 Mujitask 如何使用 framework，不记录 framework 自身 contract。

## 7. 代码实现时的文档同步流程

每次实现代码时，按以下顺序判断文档:

1. 这次变更是否改变当前系统行为?
   - 是: 检查 `docs/arch` 或 `docs/dev` 是否要同步。
2. 是否改变 Runtime/Fact schema、状态机、upsert key 或 handler contract?
   - 是: 先补 migration/兼容策略/contract 影响说明，再改对应 arch 文档。
3. 是否会让生产运行进程执行 DDL?
   - 是: 停止。生产 DDL 必须迁移到 migration 流程，运行进程只能做 schema version 校验。
4. 是否改变客户可见需求、飞书字段口径或验收标准?
   - 是: 先询问，再改 `docs/business`。
5. 是否改变部署、运行、回退?
   - 是: 同步 `docs/ops`。
6. 是否只是外部接口研究或字段样例?
   - 是: 放 `docs/reference`。
7. 是否需要修改 `.platform/**` 或 `AGENT.MD`?
   - 是: 停止，先确认是否进入 platform upgrade。

提交建议:

- 与实现强相关的文档可以和代码同一个提交。
- 大范围文档重构应单独提交。
- 需求口径变更应单独提交，并在提交信息里说明已确认。
- 不要把未确认需求、架构目标和当前实现事实混在同一段文字里。

## 8. 示例

### 示例 1: 拆 `tiktok_product_request_fetch` handler

应同步:

- `docs/arch/handler-contract-design.md`
- `docs/arch/workflow-selection-analysis-design.md`
- 如影响 Fact 写入，更新 `docs/arch/fact-db-schema-design.md`

不应同步:

- `docs/business/**`，除非客户需求或验收口径变化。

### 示例 2: 新增飞书字段写回

先判断:

- 如果只是实现已确认字段: 更新对应 `docs/arch` 设计即可。
- 如果改变字段含义、验收口径或新增业务字段: 先确认，再更新 `docs/business`。

### 示例 3: 修改部署脚本新增环境变量

应同步:

- `docs/ops/deployment.md`
- 必要时更新根 `README.md` 的快速部署入口。

### 示例 4: FastMoss 新接口调研

应放:

- `docs/reference/fastmoss-known-interfaces.md`

如果该接口成为正式采集链路，再同步:

- `docs/arch/workflow-*.md`
- `docs/arch/handler-contract-design.md`

### 示例 5: 新增 Runtime DB 字段

应同步:

- Alembic migration 或等价 migration 脚本。
- `docs/arch/runtime-db-schema-design.md`。
- `docs/ops/deployment.md` 中的 migration / rollback 说明，如果生产发布流程变化。

必须说明:

- 字段默认值和旧数据兼容方式。
- 老 worker 是否可以继续消费旧 job。
- schema version 不匹配时应用如何 fail fast。

不允许:

- 让生产 daemon / worker 在正常消费任务时自动 `ALTER TABLE`。

## 9. 最终规则

一句话规则:

> 实现设计改 `docs/arch`，开发维护改 `docs/dev`，部署运行改 `docs/ops`，接口研究改 `docs/reference`，客户口径改 `docs/business` 前先确认；schema 和 contract 是受控契约，必须走 migration、兼容和权限边界；平台规则和 framework contract 不在普通业务实现中修改。
