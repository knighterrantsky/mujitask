# 业务流程需求文档索引

更新时间：`2026-05-05`

本目录用于承载从主需求文档中拆分出的流程级需求文档。主需求文档仍然作为总览、共用字段口径和索引入口；单个业务流程的需求、采集回写、交付形式和验收口径在本目录独立维护。

## 事实来源边界

`docs/business/requirements` 是单个业务流程需求、采集回写规则、交付形式和验收口径的事实来源。

它不承载系统实现设计、Runtime 状态机、handler 拆分、部署 runbook 或外部接口研究。对应内容分别见 [../../arch/README.md](../../arch/README.md)、[../../dev/README.md](../../dev/README.md)、[../../ops/README.md](../../ops/README.md) 和 [../../reference/README.md](../../reference/README.md)。

## 正式流程

| 业务流程 | task_code | 触发方式 | 需求文档 |
| --- | --- | --- | --- |
| 竞品表定时刷新 | `refresh_current_competitor_table` | 每天定时任务 | [refresh-current-competitor-table.md](./refresh-current-competitor-table.md) |
| 关键词新增竞品 | `search_keyword_competitor_products` | OpenClaw 对话输入 | [search-keyword-competitor-products.md](./search-keyword-competitor-products.md) |
| 竞品到达人池同步 | `sync_tk_influencer_pool` | 每天定时任务 | [sync-tk-influencer-pool.md](./sync-tk-influencer-pool.md) |
| 选品表数据采集 | `tiktok_fastmoss_product_ingest` | OpenClaw 定时/手动触发 | [tk-selection-collection-expand.md](./tk-selection-collection-expand.md) |
| 关键词新增选品 | `search_keyword_selection_products` | OpenClaw 对话输入 | [search-keyword-selection-products.md](./search-keyword-selection-products.md) |

## 维护规则

1. 表结构、自动维护字段、非自动维护字段等共用口径，以 [../business-requirements.md](../business-requirements.md) 为准。
2. 单个流程的业务规则变化，优先只修改对应流程文档。
3. 只有当变更影响多条流程、飞书表结构、共用字段口径或正式流程索引时，才同步修改主需求文档。
4. 新流程从待澄清需求转为正式需求后，在本目录新增独立文档，并回填主需求文档索引。
