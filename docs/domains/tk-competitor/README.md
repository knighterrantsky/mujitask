# TK Competitor Domain

## 本域覆盖

- `refresh_current_competitor_table`
- `search_keyword_competitor_products`
- `TK竞品收集` 表字段语义
- 竞品行刷新、关键词新增、商品状态、记录日期

## 修改本域前先读

1. [../../business/business-requirements.md](../../business/business-requirements.md)
2. [../../business/requirements/refresh-current-competitor-table.md](../../business/requirements/refresh-current-competitor-table.md)
3. [../../business/requirements/search-keyword-competitor-products.md](../../business/requirements/search-keyword-competitor-products.md)
4. [../../arch/workflow-competitor-table-design.md](../../arch/workflow-competitor-table-design.md)
5. [../../../contracts/fields/feishu-tk-competitor.yaml](../../../contracts/fields/feishu-tk-competitor.yaml)
6. [../../../contracts/states/tk-competitor-product-status.yaml](../../../contracts/states/tk-competitor-product-status.yaml)
7. [../../../contracts/workflow/refresh_current_competitor_table.yaml](../../../contracts/workflow/refresh_current_competitor_table.yaml)
8. [../../../contracts/workflow/search_keyword_competitor_products.yaml](../../../contracts/workflow/search_keyword_competitor_products.yaml)

## 本域不可破坏的不变量

- 12 个自动维护字段才参与 pending 判断。
- `商品状态` 不参与 pending 判断，但商品明确不可访问、已下架或区域不可售时必须允许系统写回。
- `前台截图` / `Fastmoss截图` 当前不采集、不写回、不参与 pending 判断。
- 一条候选飞书记录最多创建一个 `competitor_row_refresh` 主 job。
- TikTok request、media sync、FastMoss、Fact DB upsert、飞书写回属于行级主 job 内部步骤，不能按 API 调用粒度拆成 sibling jobs。
