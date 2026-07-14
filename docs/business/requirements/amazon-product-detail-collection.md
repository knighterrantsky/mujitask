# Amazon 商品详情采集需求

日期: 2026-07-14

状态: 已批准，实施中

## 1. 业务目标

首期提供正式任务 `refresh_amazon_product_row_by_asin`：以飞书多维表格一条来源记录为入口，读取该行 ASIN，通过项目配置的 Chrome CDP 或指纹浏览器访问 Amazon 美国站商品页，沉淀采集证据与 Amazon 独立事实，并将结果写回同一飞书 record_id。

首期能力是后续批量和搜索流程共享的单商品基础能力；批量和搜索不在首期实现范围。后续批量流程只负责扫描候选行，搜索流程只负责发现 ASIN 并写入飞书，二者均复用本需求定义的行级任务。

## 2. 首期范围

- 站点仅支持美国站 `amazon.com`，marketplace code 固定为 `US`。
- 商品身份使用 ASIN，不使用 Seller SKU。
- ASIN 先去除首尾空格并转大写，再按 `^[A-Z0-9]{10}$` 校验。
- 系统只完整采集来源行对应的当前 ASIN；保存 Parent ASIN、页面暴露的 Child ASIN 和变体属性，但不逐个访问其他 Child ASIN。
- 采集内容包括标题、品牌、类目、卖点、描述、主图/图库、价格、评分、评论数、库存状态、Parent/Child ASIN、变体属性、卖家、配送方式、Buy Box、优惠券、促销、BSR 排名和技术参数。
- 不采集评论明细、问答明细、A+ Content 或全部第三方 Offer。

## 3. 输入与身份规则

正式任务业务输入仅包含：

- `table_ref`：指向配置别名 `AMAZON_PRODUCTS` 的飞书表引用。
- `source_record_id`：本次读取和写回的飞书来源行。

浏览器 profile、Runtime DB、Fact DB、对象存储地址及密钥不得进入正式任务 payload，由项目运行配置解析。系统根据规范化 ASIN 构造 `https://www.amazon.com/dp/{asin}`，不信任飞书链接中的跟踪参数。

请求 ASIN 与页面解析 ASIN 不一致时不得把页面商品字段写入来源行。页面明确不可售、下架或不存在时，仍需保存终态事实并写回 `unavailable`。

## 4. 业务流程

1. 读取 `source_record_id` 对应飞书行并校验 ASIN。
2. 以项目配置的浏览器 profile 访问美国站 canonical URL。
3. 按页面内嵌数据、同源页面响应、稳定语义 DOM、受控文本区块的顺序解析字段，并保存字段来源与完整度。
4. 将完整 capture、HTML、允许的数据片段和必要截图写入对象存储；Runtime DB 只保存紧凑引用。
5. 将商品、快照、Offer、变体、BSR、媒体和原始 capture 索引写入 Amazon 独立事实表。
6. 只将本次明确观察到的字段投影回来源行；`missing` 字段保留飞书旧值。
7. 写回 `采集状态`、`上次采集时间`、字段完整度和脱敏错误摘要。

## 5. 状态口径

- `pending`、`collecting`、`persisting` 为非终态。
- `success`、`partial_success`、`unavailable`、`blocked`、`failed` 为终态。
- `blocked` 表示验证码、机器人页或访问限制；必须保存证据，不允许自动绕过，并在 Runtime 层按失败结果收敛。
- `partial_success` 表示身份与事实已完成，但部分可选字段、媒体或飞书投影缺失。
- `unavailable` 是已成功持久化的商品终态事实，不等同于系统执行失败。

## 6. 数据与存储边界

- Amazon 使用同一 Fact DB 实例中的 `amazon_*` 独立表，不写入 `tk_*` 表，也不建立跨平台外键。
- 对象存储复用现有 bucket，通过 Amazon 专用 prefix 隔离；首期不新建 bucket。
- Runtime DB schema 不因本需求变化，只复用现有 task、execution、job、lease、artifact 和 outbox 能力。
- 生产 daemon/worker 不执行 DDL；表和索引只由 migration user 通过 migration 创建。

## 7. 验收口径

1. 合法飞书 ASIN 能触发四阶段单行 workflow，并将结果写回同一来源记录。
2. 同一来源行和 ASIN 重试不会产生重复商品主档、重复快照、重复变体关系或重复媒体关系。
3. 浏览器结果只在 Runtime DB 中保存身份、状态、完整度和对象引用，不内联完整 HTML 或标准化 capture。
4. `missing` 字段不清空飞书旧值；只有 `observed` 或 `explicitly_unavailable` 字段可写回。
5. 非美国站、非法 ASIN、身份不一致、blocked、Fact DB 失败、对象存储失败和飞书写回失败均按受控错误口径收敛。
6. 现有 TikTok / FastMoss workflow、`tk_*` 事实表和 browser fallback 语义不受影响。

架构与机器契约以 [Amazon 商品详情采集 Workflow 与事实存储设计](../../arch/workflow-amazon-product-detail-design.md) 及 `contracts/**` 为准；在相关 completion gate 通过前，不得声明首期能力完成。
