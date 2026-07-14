# Amazon Product Detail Domain

## 本域覆盖

- Amazon 美国站单商品行级采集任务 `refresh_amazon_product_row_by_asin`。
- ASIN 身份归一化、浏览器详情采集、Amazon Fact DB 持久化、对象证据和飞书原行投影。
- 后续批量与搜索能力复用的行级基础边界；首期不实现批量或搜索入口。

## 默认上下文

1. [正式业务需求](../../business/requirements/amazon-product-detail-collection.md)
2. [架构设计](../../arch/workflow-amazon-product-detail-design.md)
3. [飞书字段契约](../../../contracts/fields/feishu-amazon-products.yaml)
4. [采集状态契约](../../../contracts/states/amazon-product-collection-status.yaml)
5. [单行 Workflow 契约](../../../contracts/workflow/refresh_amazon_product_row_by_asin.yaml)
6. [商品事实采集契约](../../../contracts/facts/product-fact-collection.yaml)
7. 当前相关源码和测试。

## 条件展开

- 修改 Amazon Fact 表、migration 或对象 prefix 时，展开 Fact DB 与 Storage 设计。
- 修改页面字段解析、浏览器 profile 或 evidence 时，展开 Amazon workflow 设计的浏览器章节。
- 修改飞书字段、状态或 workflow stage 时，必须同步更新根级机器契约和对应测试。

## 本域不可破坏的不变量

- 首期 marketplace 固定为 `US`，商品唯一身份为 `(marketplace_code, asin)`。
- 浏览器是 Amazon 正常采集阶段，不复用 TikTok 的 fallback 语义。
- Browser handler 只生成 capture 与 evidence；Fact DB、媒体和飞书副作用由持久化阶段负责。
- Amazon 事实写入 `amazon_*` 表，不写入 `tk_*` 表；对象复用 bucket 但使用 Amazon 独立 prefix。
- 完整 capture 和 HTML 不进入 Runtime DB，Runtime 只保存紧凑引用。
- 飞书写回只针对来源 `source_record_id`；缺失字段不得清空已有值。
