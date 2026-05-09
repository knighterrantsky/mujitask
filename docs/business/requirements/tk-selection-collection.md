# 选品采集需求

更新时间：`2026-05-08`

- 入口任务：`tiktok_fastmoss_product_ingest`
- 触发方式：OpenClaw 定时或手动触发；关键词搜索选品写入流程写入新行后也复用同一行级采集口径
- 业务主表：`TK选品收集`
- 共用口径：表结构、自动维护字段、非自动维护字段以 [../business-requirements.md](../business-requirements.md) 为准。

## 1. 流程定位

本流程描述 `TK选品收集` 的商品数据采集需求：系统从选品表中的商品身份信息出发，采集 TikTok 商品详情、FastMoss 商品数据、商品媒体与图表素材，并按选品表字段口径写回。

该流程是 `TK选品收集` 的稳定数据采集口径，服务两类入口：

1. 扫描 `TK选品收集` 中已有记录，补齐自动维护字段。
2. 其他选品入口新建选品行后，复用同一行级采集逻辑补齐详情字段。

## 2. 客户需求

系统需要满足：

1. 读取 `TK选品收集` 中需要采集或补齐的商品记录。
2. 基于 `商品链接` 或 `商品ID` 定位 TikTok 商品。
3. 对商品链接做域名、格式和可访问性校验。
4. 商品有效时，采集 TikTok 商品详情、FastMoss 商品指标、SKU 信息、媒体素材和图表素材。
5. 将采集结果写回 `TK选品收集` 的自动维护字段。
6. 已有人工维护或已有值的字段不被覆盖，除身份字段按统一口径刷新外。
7. 商品链接无效、商品下架或区域不可售时，写入对应 `商品状态`，并跳过不适用的后续采集步骤。
8. 图表类字段写回前按需渲染为 PNG；数据库只保存图表所需元数据，不把渲染图表落入 MinIO。
9. `文本`、`关键词`、`差评整理` 不纳入本流程自动维护范围。

## 3. 信息采集与回写逻辑

### 3.1 候选记录识别

1. 系统读取 `TK选品收集` 记录。
2. 表级扫描模式下，检查第 3.4 节约定的必填补全字段是否存在空值。
3. 必填补全字段已全部填充的记录跳过。
4. 至少一个必填补全字段为空的记录进入候选队列。
5. `商品状态` 为"已下架/区域不可售"或"链接不可访问"的记录跳过。
6. `商品链接`、`记录日期`、可选补充字段、`文本`、`关键词`、`差评整理` 不参与空值判断。
7. 关键词搜索选品写入流程新建的选品行，直接进入同一行级采集逻辑；已存在行不触发采集 fan-out。

### 3.2 商品定位与 URL 校验

进入候选队列后，系统按以下规则定位商品：

1. 优先读取 `商品链接`。
2. `商品链接` 为空时，尝试通过 `商品ID` 拼装：`https://www.tiktok.com/shop/pdp/{商品ID}`。
3. `商品链接` 和 `商品ID` 均为空时，本行标记为无法定位并跳过。
4. 对 URL 执行域名和格式校验：
   - 域名必须为 `tiktok.com` 或其子域名。
   - URL 必须是合法 TikTok 商品链接格式。
5. 域名或格式校验失败时，回写 `商品状态=链接不可访问`，并跳过本行后续采集。
6. URL 校验通过后进入 TikTok 商品可访问性检查；商品已下架或区域不可售时，回写 `商品状态=已下架/区域不可售`，并跳过媒体、FastMoss 和图表采集。

### 3.3 采集来源

本流程采集来源包括：

1. TikTok 商品详情：商品身份、标题、价格、评论数、评分、描述、店铺名称、主图和侧边栏图片。
2. FastMoss 商品基础信息：商品累计总销量、预估上架日期和价格兜底。
3. FastMoss 商品概览：近 180 天销量、商品分布和每日销量趋势。
4. FastMoss SKU 数据：SKU 清单、SKU 销量/GMV/库存分布和主销 SKU。
5. 媒体同步结果：用于写回飞书附件字段的商品图片素材。
6. Fact DB 快照元数据：用于图表字段写回前重新渲染 PNG。

### 3.4 字段分层

`TK选品收集` 当前字段分为必填补全字段、系统运行字段、可选补充字段和非自动维护字段。

必填补全字段参与表级候选判断。以下 13 个字段全部有值时，表级扫描认为该记录不需要作为候选项：

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `商品ID` | TikTok 商品详情 | `fill_missing_or_refresh_identity` |
| `店铺名称` | TikTok `logical_fields.shop_name` | `fill_missing_only` |
| `标题` | TikTok `logical_fields.title` | `fill_missing_only` |
| `当前价格` | TikTok `logical_fields.price_text`，兜底 FastMoss `overview.front_price` | `fill_missing_only` |
| `评论数` | TikTok `logical_fields.review_count` | `fill_missing_only` |
| `评分` | TikTok `logical_fields.rating` | `fill_missing_only` |
| `商品主图` | TikTok `logical_fields.main_image_url`，兜底媒体同步结果 | `fill_missing_only` |
| `商品侧边栏图片` | TikTok `logical_fields.gallery_images` | `fill_missing_only` |
| `总销量` | FastMoss `goods.base.product.sold_count` | `fill_missing_only` |
| `上架日期` | FastMoss `goods.base.product.launch_time` 转换为商品站点日期 | `fill_missing_only` |
| `180天销量` | FastMoss `goods.overview(d_type=180).overview.sold_count` | `fill_missing_only` |
| `出单种类占比图` | FastMoss 分布快照元数据，写回前渲染 PNG | `fill_missing_only` |
| `销量趋势图` | FastMoss 每日指标元数据，写回前渲染 PNG | `fill_missing_only` |

系统运行字段由流程维护，但不参与候选判断：

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `商品链接` | TikTok 商品详情 | `fill_missing_or_refresh_identity`；表级扫描默认已有 |
| `记录日期` | 系统写回日期 | 本次有实际字段写入时才刷新 |

可选补充字段有有效数据时写入，缺失时不阻塞其他字段，也不参与候选判断：

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `SKU销量占比图` | FastMoss SKU 分析快照元数据；仅在存在有效 `best_sku` 时渲染 PNG | `fill_missing_only` |
| `父体规格` | FastMoss SKU 分析 `best_sku.sku_value` | `fill_missing_only` |
| `父体图片` | 有效 `best_sku` 对应 SKU 的图片 | `fill_missing_only` |

补充说明：

- `总销量` 不再使用 FastMoss 近 28 天 overview 数据；实际口径改为 FastMoss 商品基础接口中的累计销量 `goods.base.product.sold_count`。
- `上架日期` 来源于 FastMoss 商品基础接口 `goods.base.product.launch_time`，写入飞书前转换为 `YYYY-MM-DD` 日期；Roxy 实抓样例 `1731566133878820985` 返回 `launch_time=1755612244`，页面展示为 `2025-08-19 (GMT-5)`。
- `180天销量` 来源于 FastMoss 商品概览接口 `goods.overview(d_type=180).overview.sold_count`；该字段用于替代历史把近 28 天销量写入总销量字段的逻辑。
- 有效 `best_sku` 定义为 `sku_value` 有业务值且 `sold_count > 0`；`Default`、`默认`、`Specification`、空值、单 SKU 或第一条 SKU 都不能作为父体字段兜底来源。
- 没有有效 `best_sku` 时，跳过 `SKU销量占比图`、`父体规格`、`父体图片`，不阻塞其他字段，也不让记录重新进入候选队列。
- `父体规格` 可在图片缺失时单独写入；`父体图片` 只在能通过 `sku_id` 或 `prop_value_id` 匹配到该 `best_sku` 对应图片时写入。
- `出单种类占比图`、`销量趋势图` 和 `SKU销量占比图` 写回飞书前按需渲染为 PNG；数据库只保存渲染所需元数据。

### 3.5 非自动维护字段

| 字段名 | 规则 |
| --- | --- |
| `文本` | 客户自行维护标记，系统绝不写入 |
| `关键词` | 种子上下文字段；表级采集时保留已有值，不覆盖 |
| `商品状态` | 仅用于写入"链接不可访问"或"已下架/区域不可售"这类终态标记 |
| `差评整理` | 需人工分析整理，系统不采集、不写入 |

### 3.6 写回策略

1. `商品ID` 是选品表商品身份主键。
2. `商品ID`、`商品链接` 每次写回始终刷新为标准化身份信息。
3. 其他必填补全字段和可选补充字段统一采用 `fill_missing_only`：已有值不覆盖。
4. `记录日期` 只在本次确实产生至少一个字段写入时才刷新。
5. TikTok 采集失败且无法获得必要商品身份时，不执行字段写回。
6. FastMoss 采集失败时，只有本次写回必填补全字段已经由原表、TikTok 投影结果或已有 FastMoss 数据满足，才允许继续写回。
7. 可售商品写回前必须校验 13 个必填补全字段；任一必填字段缺失时，本行写回失败，不写入半截字段。
8. 无有效 `best_sku`、父体图片无法匹配不阻塞其余字段；`SKU销量占比图` 仍以有效 `best_sku` 为业务门槛。

### 3.7 图表渲染

1. `出单种类占比图`：从 Fact DB 中读取 `product_distribution_snapshots` 元数据，写回前渲染为 PNG。
2. `销量趋势图`：从 Fact DB 中读取 `product_daily_metrics` 元数据，写回前渲染为 PNG。
3. `SKU销量占比图`：从 Fact DB 中读取 `product_sku_metric_snapshots` 元数据，且仅在存在有效 `best_sku` 时渲染为 PNG。
4. 渲染产物不入 DB、不入 MinIO，DB 只保留元数据，可随时按需重新渲染。
5. 可售商品的 `出单种类占比图` 或 `销量趋势图` 渲染失败、renderer 依赖不可用、FastMoss overview payload 缺失时，本行写回失败。

## 4. 最终交付形式

最终交付形式是 OpenClaw skills 和 Runtime workflow：

1. 用户可以通过 OpenClaw 手动触发 `TK选品收集` 数据采集。
2. OpenClaw 可以按配置定时触发整张选品表扫描。
3. 关键词搜索选品写入流程写入新选品行后，自动复用本流程的行级数据采集逻辑。
4. 每条候选记录独立采集、独立写回；单行失败不应阻塞其他行。

## 5. 验收口径

1. 系统能扫描 `TK选品收集`，跳过必填补全字段已完整的记录。
2. 系统能通过 `商品链接` 或 `商品ID` 定位商品。
3. `商品链接` 域名非 `tiktok.com` 或格式不合法时，回写 `商品状态=链接不可访问` 并跳过。
4. 商品已下架或区域不可售时，回写 `商品状态=已下架/区域不可售` 并跳过媒体、FastMoss 和图表采集。
5. 有效商品采集完成后，空白必填补全字段被正确填入。
6. 已有值的字段不被覆盖，`商品ID` 和 `商品链接` 除外。
7. `总销量` 使用 FastMoss `goods.base.product.sold_count`，不得再使用 FastMoss 近 28 天 `overview.sold_count` 映射总销量。
8. `上架日期` 使用 FastMoss `goods.base.product.launch_time` 转换后的日期。
9. `180天销量` 使用 FastMoss `goods.overview(d_type=180).overview.sold_count`。
10. 无有效 `best_sku` 时，跳过 `SKU销量占比图`、`父体规格`、`父体图片`，其他字段正常写回，且这些可选字段缺失不触发表级候选。
11. 可售商品缺失必填补全字段或必填图表渲染失败时，本行写回失败，不写入半截字段。
12. 本次无实际字段写入时，`记录日期` 不刷新。
13. `文本`、`关键词`、`差评整理` 不受表级数据采集影响。
14. 图表 PNG 不入 DB/MinIO，DB 只存元数据。

## 6. 变更影响边界

本流程文档可独立变更以下内容：

1. 选品表候选记录识别规则。
2. 字段数据来源和兜底逻辑。
3. 自动维护字段的新增、移除和写入策略调整。
4. 截图/图表的渲染方式和数据来源。
5. URL 验证规则和状态标记逻辑。
6. `记录日期` 的刷新条件。
7. 本流程的交付动作和验收口径。

以下变更需要同步更新主需求文档：

1. `TK选品收集` 表结构变化。
2. 自动维护字段或非自动维护字段的共用口径变化。
3. `TK选品收集` 与其他表的字段联动关系变化。
4. 新增或废弃正式业务流程。

## 7. 关联文档

- [../business-requirements.md](../business-requirements.md)
- [search-keyword-selection-products.md](./search-keyword-selection-products.md)
- [../../arch/workflow-selection-table-design.md](../../arch/workflow-selection-table-design.md)
- [../../contracts/fields/feishu-tk-selection.yaml](../../contracts/fields/feishu-tk-selection.yaml)
