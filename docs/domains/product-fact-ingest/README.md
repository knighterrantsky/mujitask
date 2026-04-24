# Product Fact Ingest Domain

## 本域覆盖

- `tiktok_fastmoss_product_ingest`
- TikTok 商品 request-first 采集
- FastMoss 商品详情采集
- 媒体同步、Fact DB upsert、可选飞书写回

## 修改本域前先读

1. [../../business/business-requirements.md](../../business/business-requirements.md)
2. [../../arch/workflow-selection-analysis-design.md](../../arch/workflow-selection-analysis-design.md)
3. [../../arch/fact-db-schema-design.md](../../arch/fact-db-schema-design.md)
4. [../../arch/storage-architecture-design.md](../../arch/storage-architecture-design.md)
5. [../../../contracts/workflow/tiktok_fastmoss_product_ingest.yaml](../../../contracts/workflow/tiktok_fastmoss_product_ingest.yaml)

## 本域不可破坏的不变量

- 商品、店铺、达人、视频、媒体资产属于通用事实，不属于选品分析专有数据。
- TikTok 商品数据采集优先走 request/API 路径。
- Browser 只作为 fallback，用于 request 失效、关键字段缺失或被风控阻断的场景。
- `tiktok_fastmoss_product_ingest` 当前不是已沉淀的正式客户流程需求；不要从设计文档反推业务验收。
