# TK选品收集表自动采集扩展需求

更新时间：`2026-04-30`

- 入口任务：`tiktok_fastmoss_product_ingest`
- 触发方式：由 OpenClaw 侧配置定时或手动触发，workflow 本身只提供执行入口
- 业务主表：`TK选品收集`
- 共用口径：表结构、自动维护字段、非自动维护字段以 [../business-requirements.md](../business-requirements.md) 为准。

## 1. 流程定位

当前 `tiktok_fastmoss_product_ingest` 工作流已完成 TikTok 商品详情 + FastMoss 商品数据的完整采集，但写回 `TK选品收集` 时只输出 3 个字段（`商品ID`、`商品链接`、`记录日期`）。

本次扩展：

1. 将 14 个标记为 `not_written_by_current_ingest` 的字段中可自动化的部分纳入自动维护范围
2. 增加"扫描选品表全部记录 → 发现空字段 → URL 验证 → 触发采集"的批量补齐能力
3. 图表类字段在写回时按需渲染，数据库只保存元数据

## 2. 客户需求

1. 扫描 `TK选品收集` **全部记录**，检查自动维护字段是否存在空值
2. 自动维护字段已全部填充的行跳过
3. 对存在空字段的候选行，先验证 `商品链接` 是否有效：
   - 域名为 `tiktok.com`
   - URL 格式为合法 TikTok 商品链接
   - 商品可访问（非下架/区域不可售）
4. URL 格式不正确时，回写 `商品状态` 为"链接不可访问"
5. 商品已下架/区域不可售时，按已有逻辑回写 `商品状态`
6. 采集完成后，将空字段补齐；已有值的字段不覆盖
7. 图表类字段（出单种类占比图、销量趋势图、SKU 销量占比图）在飞书写回前按需渲染为 PNG 写入飞书单元格，DB/MinIO 不落存储，只保存元数据供后续重新渲染
8. 父体规格、父体图片只来源于 FastMoss SKU 分析中的有效 `best_sku`；规格可单独写入，图片仅在能匹配到该 `best_sku` 对应图片时写入
9. `文本`、`关键词`、`差评整理` 不纳入自动维护范围

## 3. 信息采集与回写逻辑

### 3.1 扫描与候选筛选

1. 系统读取 `TK选品收集` 中的**全部记录**
2. 每条记录检查第 3.3 节约定的自动维护字段（共 17 个）是否存在空值
3. 17 个字段**全部已填充**的记录，跳过
4. **至少一个字段为空**的记录，进入候选队列
5. `商品状态` 为"已下架/区域不可售"或"链接不可访问"的记录，跳过
6. `文本`、`关键词`、`差评整理` 不参与空值判断

### 3.2 URL 有效性验证

进入候选队列后，先验证 URL：

1. 取 `商品链接` 字段值
2. `商品链接` 为空时，尝试通过 `商品ID` 拼装：`https://www.tiktok.com/shop/pdp/{商品ID}`
3. `商品链接` 和 `商品ID` 均为空，标记"无法定位"，跳过
4. 对 URL 执行以下检查：
   - **域名校验**：域名必须为 `tiktok.com`（含子域名）
   - **格式校验**：URL 为合法 TikTok 商品链接格式
5. 域名或格式校验失败 → 回写 `商品状态` 为"链接不可访问"，跳过
6. URL 校验通过后进入采集流程，如果商品页面返回下架/区域不可售 → 按已有逻辑回写 `商品状态` 为"已下架/区域不可售"，跳过

### 3.3 本期自动维护字段（共 17 个）

**已有字段（3 个，保持不变）：**

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `商品ID` | TikTok 商品详情 | `fill_missing_or_refresh_identity` |
| `商品链接` | TikTok 商品详情 | `fill_missing_or_refresh_identity` |
| `记录日期` | 系统写回日期 | 本次有实际字段写入时才刷新 |

**TikTok 侧新增（8 个）：**

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `店铺名称` | `logical_fields.shop_name` | `fill_missing_only` |
| `商品标题` | `logical_fields.title` | `fill_missing_only` |
| `商品当前价格` | `logical_fields.price_text`，兜底 FastMoss `overview.front_price` | `fill_missing_only` |
| `商品评论数` | `logical_fields.review_count` | `fill_missing_only` |
| `商品评分` | `logical_fields.rating` | `fill_missing_only` |
| `商品描述` | `logical_fields.description` | `fill_missing_only` |
| `商品主图` | `logical_fields.main_image_url`，兜底媒体同步结果 | `fill_missing_only` |
| `商品侧边栏图片` | `logical_fields.gallery_images` | `fill_missing_only` |

**FastMoss 侧新增（6 个）：**

| 字段名 | 数据来源 | 写入策略 |
| --- | --- | --- |
| `今年总销量` | FastMoss `goods.overview(d_type=28).overview.sold_count` | `fill_missing_only` |
| `出单种类占比图` | FastMoss 分布快照元数据 → 写回前渲染 PNG | `fill_missing_only` |
| `销量趋势图` | FastMoss 每日指标元数据 → 写回前渲染 PNG | `fill_missing_only` |
| `SKU销量占比图` | FastMoss SKU 分析快照元数据；仅在存在有效 `best_sku` 时渲染 PNG | `fill_missing_only` |
| `父体规格` | FastMoss SKU 分析 `best_sku.sku_value` | `fill_missing_only`（有效 `best_sku` 有规格值则写，无则跳过） |
| `父体图片` | 有效 `best_sku` 对应 SKU 的 `sku_sale_props.image` 或同一规格值绑定图片 | `fill_missing_only`（能匹配到图片则写，无则跳过） |

说明：

- `今年总销量` 字段名不变，实际写入近 28 天销量数据；使用 FastMoss `overview.sold_count`，与销量趋势图 `inc_sold_count` / 分布图 `sold_count` 口径一致，不使用 `real_sold_count`
- 有效 `best_sku` 定义为 `sku_value` 有业务值且 `sold_count > 0`；`Default`、`默认`、`Specification`、空值、单 SKU 或第一条 SKU 都不能作为父体字段兜底来源。
- 没有有效 `best_sku` 时，跳过 `SKU销量占比图`、`父体规格`、`父体图片`，不阻塞其他字段。
- `父体规格` 可在图片缺失时单独写入；`父体图片` 只在能通过 `sku_id` 或 `prop_value_id` 匹配到该 `best_sku` 对应图片时写入。
- 三个截图/图表字段：DB 只存元数据（分布快照、每日指标、SKU 指标），写回飞书前按需渲染为 PNG，不入 MinIO

### 3.4 不纳入自动维护的字段（共 4 个）

| 字段名 | 原因 |
| --- | --- |
| `文本` | 客户自行维护标记，系统绝不写入 |
| `关键词` | 种子上下文，保留已有值不覆盖 |
| `商品状态` | 仅读写回策略：链接不可访问 / 已下架/区域不可售 |
| `差评整理` | 需人工分析整理 |

### 3.5 写回策略

1. 以 `商品ID` 作为 upsert 主键
2. 新增字段统一 `fill_missing_only`：已有值不覆盖
3. `商品ID`、`商品链接` 每次写回始终刷新
4. `记录日期` 只在本次确实产生至少一个字段写入时才刷新
5. FastMoss 采集失败时，只有本次写回必填字段已经由原表或投影结果满足，才允许继续写回
6. TikTok 采集 + 浏览器兜底均失败时，不执行写回
7. 写入飞书前必须校验 `商品主图`、`商品侧边栏图片`、`出单种类占比图`、`销量趋势图`；任一字段缺失则整条写回任务失败，不写入半截数据
8. 无有效 `best_sku`、父体图片无法匹配不阻塞其余字段；`SKU销量占比图` 仍以有效 `best_sku` 为业务门槛

### 3.6 图表渲染（不入 DB / MinIO）

1. `出单种类占比图`：从 Fact DB 中读取 `product_distribution_snapshots` 元数据，写回前渲染为 PNG，插入飞书单元格
2. `销量趋势图`：从 Fact DB 中读取 `product_daily_metrics` 元数据，写回前渲染为趋势图 PNG，插入飞书单元格
3. `SKU销量占比图`：从 Fact DB 中读取 `product_sku_metric_snapshots` 元数据，写回前渲染为结构化图表 PNG，插入飞书单元格
4. 渲染产物不入 DB、不入 MinIO，DB 只保留元数据，可随时按需重新渲染
5. `出单种类占比图`、`销量趋势图` 渲染失败或 renderer 依赖不可用时，本行写回失败；不得跳过必填图表继续写入

## 4. 最终交付形式

对现有 `tiktok_fastmoss_product_ingest` 工作流做以下变更：

| # | 变更点 | 说明 |
| --- | --- | --- |
| 1 | 扫描筛选 | `read_selection_rows` 增加 `missing_auto_fields` 检查，17 个字段全填充的行跳过；`商品状态` 为不可访问的行跳过 |
| 2 | URL 验证 | `collect_product_data` 前增加域名/格式校验；失败则回写 `商品状态=链接不可访问`；可访问性由采集流程中 TikTok handler 判定 |
| 3 | 扩展 logical_fields | TikTok handler 增加 `review_count`、`rating`、`description`、`gallery_images` |
| 4 | 传递完整数据 | `writeback_selection_rows` 回查 `collect_product_data`、`sync_media` 的 handler 结果 |
| 5 | 重写 projection mapper | 按 3.3 节映射 17 个字段 |
| 6 | 图表渲染 | 基于 Fact DB 快照元数据，写回前按需渲染 PNG 插入飞书单元格 |
| 7 | 契约更新 | `feishu-tk-selection.yaml` 中 14 个字段从 `not_written_by_current_ingest` 改为 `fill_missing_only` |

## 5. 验收口径

1. 扫描选品表全部记录，17 个自动维护字段全填充的记录被跳过
2. `商品链接` 域名非 `tiktok.com` 或格式不合法 → 回写 `商品状态=链接不可访问`，跳过
3. 既无 `商品链接` 也无 `商品ID` → 标记"无法定位"，跳过
4. 商品已下架/区域不可售 → 回写 `商品状态=已下架/区域不可售`，跳过
5. 有效商品采集完成后，所有空白自动维护字段被正确填入
6. 已有值的字段不被覆盖（`商品ID`、`商品链接` 除外）
7. 无有效 `best_sku` 或父体图片无法匹配 → 跳过对应字段不报错
8. FastMoss 采集失败 → TikTok 侧字段仍正常写回
9. 图表渲染失败 → 对应截图字段跳过不阻塞
10. 本次无实际字段写入 → `记录日期` 不刷新
11. `文本`、`关键词`、`差评整理` 不受影响
12. 图表 PNG 不入 DB/MinIO，DB 只存元数据

## 6. 变更影响边界

本流程文档可独立变更以下内容：

1. 自动维护字段的新增、移除和写入策略调整
2. 字段数据来源和兜底逻辑
3. 截图/图表的渲染方式和数据来源
4. URL 验证规则和状态标记逻辑
5. `记录日期` 的刷新条件
6. 本流程的交付动作和验收口径

以下变更需要同步更新主需求文档：

1. `TK选品收集` 表结构变化
2. 自动维护字段或非自动维护字段的共用口径变化
3. `TK选品收集` 与其他表的字段联动关系变化

## 7. 关联文档

- [../business-requirements.md](../business-requirements.md)
- [../../arch/workflow-selection-analysis-design.md](../../arch/workflow-selection-analysis-design.md)
- [../../contracts/fields/feishu-tk-selection.yaml](../../contracts/fields/feishu-tk-selection.yaml)
