# TK Competitor Domain

## 本域覆盖

- `refresh_current_competitor_table`
- `search_keyword_competitor_products`
- `TK竞品收集` 表字段语义
- 竞品行刷新、关键词新增、商品状态、记录日期

## 默认上下文

普通实现 / 修复默认只读:

1. [../../dev/rewrite-state.yaml](../../dev/rewrite-state.yaml)
2. [../../arch/project-structure-contract.md](../../arch/project-structure-contract.md)
3. [../../../contracts/codex/task-routing.yaml](../../../contracts/codex/task-routing.yaml)
4. [../../../contracts/fields/feishu-tk-competitor.yaml](../../../contracts/fields/feishu-tk-competitor.yaml)
5. [../../../contracts/states/tk-competitor-product-status.yaml](../../../contracts/states/tk-competitor-product-status.yaml)
6. [../../../contracts/workflow/refresh_current_competitor_table.yaml](../../../contracts/workflow/refresh_current_competitor_table.yaml)
7. [../../../contracts/workflow/search_keyword_competitor_products.yaml](../../../contracts/workflow/search_keyword_competitor_products.yaml)
8. 当前相关源码和测试。

## 条件展开

- 修改客户需求、验收口径或字段业务含义时，才读 `docs/business/**`。
- 修改架构边界、Runtime contract 或目录归属时，才读 `docs/arch/**` 长文档。
- 修改字段语义、pending 判断或状态枚举时，必须同步读写 `contracts/fields/**` 或 `contracts/states/**`。
- 旧行为可参考 git history；legacy `business/` 目录已删除。

## 本域不可破坏的不变量

- 12 个自动维护字段才参与 pending 判断。
- `商品状态` 不参与 pending 判断，但商品明确不可访问、已下架或区域不可售时必须允许系统写回。
- `前台截图` / `Fastmoss截图` 当前不采集、不写回、不参与 pending 判断。
- 一条候选飞书记录最多创建一个 `competitor_row_refresh` 主 job。
- TikTok request、media sync、FastMoss、Fact DB upsert、飞书写回属于行级主 job 内部步骤，不能按 API 调用粒度拆成 sibling jobs。
- 新实现落在 `src/automation_business_scaffold/domains/tiktok/**`，legacy `business/` 目录已删除。
- 完成本任务后遵守 `AGENTS.md` 的 Stop Protocol，不输出无关下一步建议。
