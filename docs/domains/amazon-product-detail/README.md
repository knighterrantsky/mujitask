# Amazon Product Detail Domain

## 本域覆盖

- Amazon 美国站单商品行级采集任务 `refresh_amazon_product_row_by_asin`。
- Amazon竞品表 `采集标签=T` 批量任务 `refresh_current_amazon_product_table`。
- ASIN 身份归一化、浏览器详情采集、Amazon Fact DB 持久化、对象证据和飞书原行投影。
- 批量入口复用单商品任务；搜索入口仍不在当前实现范围。

## 默认上下文

1. [正式业务需求](../../business/requirements/amazon-product-detail-collection.md)
2. [架构设计](../../arch/workflow-amazon-product-detail-design.md)
3. [飞书字段契约](../../../contracts/fields/feishu-amazon-products.yaml)
4. [采集状态契约](../../../contracts/states/amazon-product-collection-status.yaml)
5. [单行 Workflow 契约](../../../contracts/workflow/refresh_amazon_product_row_by_asin.yaml)
6. [批量 Workflow 契约](../../../contracts/workflow/refresh_current_amazon_product_table.yaml)
7. [商品事实采集契约](../../../contracts/facts/product-fact-collection.yaml)
8. [长期业务对象存储契约](../../../contracts/facts/durable-business-object-storage.yaml)
9. 当前相关源码和测试。

## 条件展开

- 修改 Amazon Fact 表、migration 或对象 prefix 时，展开 Fact DB 与 Storage 设计。
- 修改页面字段解析、浏览器 profile 或 evidence 时，展开 Amazon workflow 设计的浏览器章节。
- 修改飞书字段、状态或 workflow stage 时，必须同步更新根级机器契约和对应测试。

## 本域不可破坏的不变量

- 首期 marketplace 固定为 `US`，商品唯一身份为 `(marketplace_code, asin)`。
- 浏览器是 Amazon 正常采集阶段，不复用 TikTok 的 fallback 语义。
- Browser handler 成功/partial success 只生成 normalized capture 与媒体来源，blocked/captcha/access-blocked 只生成受控终态截图；Fact DB、媒体和飞书副作用由持久化阶段负责。
- Amazon 事实写入 `amazon_*` 表，不写入 `tk_*` 表；对象复用 bucket 但使用 Amazon 独立 prefix。
- 完整 capture 和 HTML 不进入 Runtime DB；HTML、page/network data 和普通截图只留本地。成功/partial success 的 Runtime 只保存 normalized capture/商品媒体紧凑完整引用，blocked 只保存受控截图引用。
- 飞书写回只针对来源 `source_record_id`；缺失字段不得清空已有值。
