# TK Influencer Pool Domain

## 本域覆盖

- `sync_tk_influencer_pool`
- `TK竞品收集` 到 `TK达人池` 的达人扩展
- `达人查找状态`
- `TK达人池` 字段语义、upsert 主键和 W 单位展示

## 修改本域前先读

1. [../../business/business-requirements.md](../../business/business-requirements.md)
2. [../../business/requirements/sync-tk-influencer-pool.md](../../business/requirements/sync-tk-influencer-pool.md)
3. [../../arch/workflow-influencer-pool-sync-design.md](../../arch/workflow-influencer-pool-sync-design.md)
4. [../../arch/migration-state.md](../../arch/migration-state.md)
5. [../../../contracts/fields/feishu-tk-competitor.yaml](../../../contracts/fields/feishu-tk-competitor.yaml)
6. [../../../contracts/fields/feishu-tk-influencer-pool.yaml](../../../contracts/fields/feishu-tk-influencer-pool.yaml)
7. [../../../contracts/states/tk-competitor-influencer-search-status.yaml](../../../contracts/states/tk-competitor-influencer-search-status.yaml)
8. [../../../contracts/workflow/sync_tk_influencer_pool.yaml](../../../contracts/workflow/sync_tk_influencer_pool.yaml)

## 本域不可破坏的不变量

- `TK达人池` 一人一行，按 `达人ID` upsert。
- `达人查找状态` 的正式状态值只有 `待查找 / 处理中 / 已完成 / 失败重试`。
- 空值、`待查找`、`失败重试` 和异常残留 `处理中` 可进入同步；`已完成` 默认跳过。
- `商品状态=已下架/区域不可售` 的竞品记录不进入达人查找。
- 达人同步不新增业务专用 Runtime job 表，执行单元统一进入 `api_worker_job`。
