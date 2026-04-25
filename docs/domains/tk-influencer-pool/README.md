# TK Influencer Pool Domain

## 本域覆盖

- `sync_tk_influencer_pool`
- `TK竞品收集` 到 `TK达人池` 的达人扩展
- `达人查找状态`
- `TK达人池` 字段语义、upsert 主键和 W 单位展示

## 默认上下文

普通实现 / 修复默认只读:

1. [../../dev/rewrite-state.yaml](../../dev/rewrite-state.yaml)
2. [../../arch/project-structure-contract.md](../../arch/project-structure-contract.md)
3. [../../../contracts/codex/task-routing.yaml](../../../contracts/codex/task-routing.yaml)
4. [../../../contracts/fields/feishu-tk-competitor.yaml](../../../contracts/fields/feishu-tk-competitor.yaml)
5. [../../../contracts/fields/feishu-tk-influencer-pool.yaml](../../../contracts/fields/feishu-tk-influencer-pool.yaml)
6. [../../../contracts/states/tk-competitor-influencer-search-status.yaml](../../../contracts/states/tk-competitor-influencer-search-status.yaml)
7. [../../../contracts/workflow/sync_tk_influencer_pool.yaml](../../../contracts/workflow/sync_tk_influencer_pool.yaml)
8. 当前相关源码和测试。

## 条件展开

- 修改客户需求、验收口径或字段业务含义时，才读 `docs/business/**`。
- 修改架构边界、Runtime contract、迁移状态或目录归属时，才读 `docs/arch/**` 长文档。
- 修改字段语义、upsert key、状态枚举或 W 单位展示时，必须同步读写 `contracts/fields/**` 或 `contracts/states/**`。
- 排查旧行为时，可以读取 `src/automation_business_scaffold/business/**`，但不能把它作为新实现 owner。

## 本域不可破坏的不变量

- `TK达人池` 一人一行，按 `达人ID` upsert。
- `达人查找状态` 的正式状态值只有 `待查找 / 处理中 / 已完成 / 失败重试`。
- 空值、`待查找`、`失败重试` 和异常残留 `处理中` 可进入同步；`已完成` 默认跳过。
- `商品状态=已下架/区域不可售` 的竞品记录不进入达人查找。
- 达人同步不新增业务专用 Runtime job 表，执行单元统一进入 `api_worker_job`。
- 新实现落在 `src/automation_business_scaffold/domains/tiktok/**`，`business/**` 只是 legacy reference。
- 完成本任务后遵守 `AGENTS.md` 的 Stop Protocol，不输出无关下一步建议。
