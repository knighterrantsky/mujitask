# 需求文档

更新时间：`2026-07-23`

## 1. 文档目的

本文件是客户需求的总览和索引入口，主要描述客户背景、业务目标、当前飞书表结构、共用字段维护口径、待澄清需求和正式业务流程文档索引。  
具体到单个业务流程的客户需求、信息采集与回写逻辑、最终交付形式和验收口径，不在本文件中展开，统一拆分到 `docs/business/requirements/` 下的流程级需求文档。

这样拆分后，后续单一业务流程发生变更时，优先只修改对应流程文档；只有变更影响飞书表结构、共用字段口径、正式流程列表或跨流程状态定义时，才同步修改本总览文档。

## 2. 客户背景与业务目标

客户当前希望通过 `OpenClaw` 驱动自动化流程，持续抓取 `TK` 和 `AWS` 的商业数据，并把结果沉淀到现有飞书多维表中，用于后续选品、竞品分析、达人运营和业务决策。

当前阶段已经明确的业务目标主要有五类：

1. 通过定时任务持续更新飞书中的 `TK竞品收集` 数据，保证已有竞品信息保持最新。
2. 通过 `OpenClaw` 对话输入业务指令，按关键词或其他入口新增 `TK` 竞品或选品数据。
3. 通过定时任务把 `TK竞品收集` 中的商品继续扩展到 `TK达人池`，形成达人画像与运营沉淀。
4. 通过定时或手动检查 `TK达人建联表`，跟踪达人是否已为对应商品发布视频，并回写视频链接与发布时间。
5. 以飞书 `AMAZON_PRODUCTS` 来源行中的美国站 ASIN 为入口，采集 Amazon 商品详情、变体、Offer、媒体和排名事实，并把受控字段写回同一来源行。

## 3. 客户当前飞书多维表

本节字段信息不再引用本地分析文档，而是基于飞书 `table URL + FEISHU_ACCESS_TOKEN` 实时拉取当前 Base 的最新 schema 后整理。

### 3.1 当前已实时验证的 5 张 TikTok 飞书表

- 飞书 Base 链接：`https://ecncxlbv3k1g.feishu.cn/base/KzJXbZWunalHHVs4OkYcvk5gnxc`
- 当前 Base 实时返回了 10 张表；本需求当前只覆盖以下 5 张：

| 表名 | table_id | 当前角色 |
| --- | --- | --- |
| `TK选品收集` | `tblpF46y6SkmVCE5` | 商品选品研究与素材沉淀 |
| `TK竞品收集` | `tblpzuTZXHtDq83t` | TikTok / FastMoss 竞品运营主表 |
| `TK达人池` | `tblwLYl59TkfVFLe` | 达人画像池与合作沉淀 |
| `TK达人建联表` | `tblpK4zCGaaL6h6v` | 达人建联执行台账 |
| `TK合作爆款视频` | `tblP9S5mRrirutDT` | 爆款视频案例库 |

Amazon 竞品表单商品流程另使用配置别名 `AMAZON_PRODUCTS`，用户侧表名为 `Amazon竞品表`。其 `table_id/view_id` 由部署环境显式配置，当前未把它计入上述 TikTok Base 的 5 张实时 schema 快照；字段口径以 [feishu-amazon-products.yaml](../../contracts/fields/feishu-amazon-products.yaml) 为机器事实来源。

### 3.2 五张表的当前字段

#### 3.2.1 TK选品收集

当前实时字段为：

- `文本`
- `商品链接`
- `记录日期`
- `关键词`
- `商品ID`
- `店铺名称`
- `标题`
- `当前价格`
- `评论数`
- `评分`
- `商品主图`
- `商品侧边栏图片`
- `出单种类占比图`
- `总销量`
- `上架日期`
- `180天销量`
- `销量趋势图`
- `SKU销量占比图`
- `父体规格`
- `父体图片`
- `差评整理`

#### 3.2.2 TK竞品收集

当前实时字段为：

- `产品链接`
- `关键词`
- `SKU-ID`
- `图片`
- `标题`
- `节日`
- `卖家`
- `价格`
- `Fastmoss价格`
- `佣金率`
- `昨日销量`
- `近7天销量`
- `近90天销量`
- `记录日期`
- `备注`
- `开售时间`
- `第一波高峰期`
- `第二波高峰期`
- `价格趋势`
- `商品状态`
- `达人查找状态`

#### 3.2.3 TK达人池

当前实时字段为：

- `达人ID`
- `带货商品图`
- `关联节日`
- `关联商品销量`
- `达人头像`
- `粉丝数`
- `28天视频数`
- `带货视频 GMV`
- `带货直播 GMV`
- `合作店铺`
- `合作商品数`
- `达人联系方式`
- `检查达人名称是否重复`
- `记录日期`
- `更新日期`
- `跟我们合作过的节日`
- `出爆款视频（>20w）or 成交件数>50 的产品`
- `毕业季建联`
- `达人地址`
- `达人电话`

#### 3.2.4 TK达人建联表

当前实时字段为：

- `序号`
- `SKUID`
- `建联时间`
- `达人ID`
- `粉丝数`
- `达人类型`
- `佣金`
- `视频链接`
- `检查时间`
- `视频发布时间`
- `建联店铺`
- `建联产品`
- `播放量`
- `备注`

#### 3.2.5 TK合作爆款视频

当前实时字段为：

- `视频来源`
- `视频码`
- `Fastmoss访问链接`
- `节日`
- `SKU ID`
- `达人ID`
- `视频发布的日期`
- `视频播放量-AI`
- `产品`
- `备注`

### 3.3 已明确需求表的自动维护字段

当前已经明确自动维护口径的表包括 `TK竞品收集`、`TK达人池`、`TK选品收集` 和 `TK达人建联表`。

#### 3.3.1 TK竞品收集

现阶段系统自动维护的字段固定为以下 13 个：

- `产品链接`
- `SKU-ID`
- `图片`
- `标题`
- `节日`
- `卖家`
- `价格`
- `Fastmoss价格`
- `佣金率`
- `昨日销量`
- `近7天销量`
- `近90天销量`
- `记录日期`

其中：

- `佣金率` 来源于 FastMoss 商品详情基础数据中的商品佣金率，按 FastMoss 返回的百分比文本写入，例如 `10%`。
- FastMoss 明确返回 `-` 时，`佣金率` 原样写入 `-`，表示当前没有可用佣金率；接口未返回该字段时不伪造百分比值。
- `佣金率` 与其他商品自动维护字段一致，采用 `fill_missing_only` 策略；已有值不覆盖。

#### 3.3.2 TK达人池

现阶段系统自动维护的字段固定为以下 12 个：

- `带货商品图`
- `关联节日`
- `关联商品销量`
- `达人头像`
- `粉丝数`
- `28天视频数`
- `带货视频 GMV`
- `带货直播 GMV`
- `合作店铺`
- `达人联系方式`
- `记录日期`
- `更新日期`

补充说明：

- `达人ID` 是 `TK达人池` 的 upsert 主键；系统新建达人行时必须写入，但它不单独归类为画像维护字段。
- `粉丝数`、`带货视频 GMV`、`带货直播 GMV` 在数据库和实体快照中保留接口返回的实际数字；写入飞书表做最终展示时，数值大于等于 `10000` 的统一显示为整数 `W` 单位，并按四舍五入处理，例如 `15400 -> 2W`、`155500 -> 16W`、`2442300 -> 244W`，小于 `10000` 的统一显示为 `小于1W`。
- `视频播放量` 或后续视频相关流程中的同类播放量字段，如果被纳入系统自动写回，也沿用相同的整数 `W` 单位展示规则，小于 `10000` 时显示为 `小于1W`。
- `达人联系方式` 来自 FastMoss 达人联系方式；达人存在多个联系方式时优先写入邮箱地址，没有邮箱地址时写入第一个有效联系方式，没有任何联系方式时不写入该字段，也不覆盖已有联系方式。
- `记录日期` 表示达人首次进入 `TK达人池` 的日期，首次插入时写入，后续不覆盖；`更新日期` 表示该达人行最近一次因系统同步产生新商品合并或字段更新的日期，首次插入时也写入。

#### 3.3.3 TK达人建联表

现阶段系统自动维护字段固定为以下 3 个：

- `视频链接`
- `视频发布时间`
- `检查时间`

补充说明：

- `SKUID` 和 `达人ID` 是建联检查的输入字段，不由本流程自动维护。
- `达人ID` 当前按 FastMoss/TikTok `unique_id` 使用，不按 FastMoss 数值型 `uid` 使用。
- `视频链接` 基于 FastMoss 商品关联视频接口返回的 `unique_id` 和 `video_id` 生成 TikTok 官方视频链接。
- `视频链接` 已有内容时不覆盖；只有空值行参与检查和回写。
- `视频发布时间` 写入匹配视频的 `create_date`；同一商品同一达人匹配多条视频时，选择发布时间最早的一条。
- `检查时间` 只在该商品视频列表抓取成功后更新；如果 FastMoss 抓取失败，该商品对应行不更新检查时间。

#### 3.3.4 TK选品收集

现阶段系统自动维护字段按用途分层：必填补全字段参与表级候选判断；系统运行字段由流程维护但不参与候选判断；可选补充字段有有效数据时写入，缺失时不阻塞采集、不触发候选。

必填补全字段固定为以下 13 个：

- `商品ID`
- `店铺名称`
- `标题`
- `当前价格`
- `评论数`
- `评分`
- `商品主图`
- `商品侧边栏图片`
- `总销量`
- `上架日期`
- `180天销量`
- `出单种类占比图`
- `销量趋势图`

系统运行字段为：

- `商品链接`
- `记录日期`

可选补充字段为：

- `SKU销量占比图`
- `父体规格`
- `父体图片`

补充说明：

- `商品ID` 是 `TK选品收集` 的 upsert 主键；系统新建选品行时必须写入，每次写回始终刷新。
- `商品链接` 是选品表商品入口，表级扫描默认已有；每次写回始终刷新，保持与最新采集结果一致，但不作为候选判断字段。
- `总销量` 来源为 FastMoss `goods.base.product.sold_count`，代表商品累计销量；原先把 FastMoss 近 28 天 `overview.sold_count` 写入总销量字段的逻辑取消。
- `上架日期` 来源为 FastMoss `goods.base.product.launch_time`，写入飞书前转换为 `YYYY-MM-DD` 日期；Roxy 实抓样例 `1731566133878820985` 返回 `launch_time=1755612244`，页面展示为 `2025-08-19 (GMT-5)`。
- `180天销量` 来源为 FastMoss `goods.overview(d_type=180).overview.sold_count`，不是 `goods.base.product.sold_count`，也不是近 28 天销量。
- `父体规格` 和 `父体图片` 只来源于 FastMoss SKU 分析中的有效 `best_sku`（`sku_value` 有业务值且 `sold_count > 0`）；`父体规格` 可单独写入，`父体图片` 仅在能匹配到该 `best_sku` 对应图片时写入。
- 没有有效 `best_sku` 时跳过 `SKU销量占比图`、`父体规格`、`父体图片`；不得使用单 SKU、`Default`、`默认`、`Specification`、空 SKU 或第一条 SKU 兜底生成父体字段；这些字段缺失不参与候选判断。
- `出单种类占比图`、`销量趋势图`、`SKU销量占比图` 在写回前按需渲染为 PNG 插入飞书单元格，DB 只保存元数据，不入 MinIO。
- 除 `商品ID`、`商品链接` 这类身份字段外，自动维护字段统一采用 `fill_missing_only` 策略：已有值的字段不覆盖。
- `记录日期` 只在本次确实产生至少一个字段写入时才刷新。

### 3.4 已明确需求表的非自动维护字段

#### 3.4.1 TK竞品收集

以下字段不属于自动维护字段，也不参与待更新判断：

- `商品状态`
- `达人查找状态`
- `前台截图`
- `Fastmoss截图`
- `关键词`
- `备注`
- `开售时间`
- `第一波高峰期`
- `第二波高峰期`
- `价格趋势`

其中：

- `商品状态` 由系统写入，但它属于状态标记字段，不属于自动维护字段。
- `达人查找状态` 由系统写入，但它只服务于“竞品到达人池”的同步流程，不属于自动维护字段。
- `前台截图`、`Fastmoss截图` 当前流程暂不采集、不写回，不参与待更新判断。
- 其余字段属于客户自行维护字段，当前不纳入自动维护范围。

#### 3.4.2 TK达人池

以下字段当前不纳入自动维护范围：

- `检查达人名称是否重复`
- `跟我们合作过的节日`
- `出爆款视频（>20w）or 成交件数>50 的产品`
- `毕业季建联`
- `合作商品数`
- `达人地址`
- `达人电话`

其中：

- `检查达人名称是否重复` 是飞书公式字段，不参与任何自动写入。
- 其余字段当前仍视为历史沉淀字段或人工维护字段，不纳入本期达人池自动维护范围。

#### 3.4.3 TK达人建联表

以下字段当前不纳入自动维护范围：

- `序号`
- `SKUID`
- `建联时间`
- `达人ID`
- `粉丝数`
- `达人类型`
- `佣金`
- `建联店铺`
- `建联产品`
- `播放量`
- `备注`

其中：

- `SKUID` 和 `达人ID` 是本流程的匹配输入字段，必须由人工或上游流程提供。
- `播放量` 当前不纳入达人建联检查流程的自动回写范围；如后续要求补充播放量，需要另行确认视频播放量的数据来源和更新频率。
- 其余字段属于人工运营字段或历史沉淀字段，不纳入当前自动维护范围。

#### 3.4.4 TK选品收集

以下字段当前不纳入自动维护范围：

- `文本`
- `关键词`
- `商品状态`
- `差评整理`

其中：

- `文本` 是客户自行维护的标记字段，系统绝不写入。
- `关键词` 保留已有值，不覆盖；关键词搜索选品写入流程创建种子行时可写入初始关键词来源。
- `商品状态` 仅在 URL 校验失败时写入"链接不可访问"，或商品不可访问时写入"已下架/区域不可售"，不参与待更新判断。
- `差评整理` 需人工分析，不纳入自动采集。

## 4. 业务流程需求索引

### 4.1 正式流程文档

| 业务流程 | task_code | 触发方式 | 涉及表 | 独立需求文档 | 关联设计文档 |
| --- | --- | --- | --- | --- | --- |
| 竞品采集 | `refresh_current_competitor_table` | 每天定时任务 | `TK竞品收集` | [requirements/refresh-current-competitor-table.md](./requirements/refresh-current-competitor-table.md) | [workflow-competitor-table-design.md](../arch/workflow-competitor-table-design.md) |
| 关键词搜索竞品写入 | `search_keyword_competitor_products` | OpenClaw 对话输入 | `TK竞品收集` | [requirements/search-keyword-competitor-products.md](./requirements/search-keyword-competitor-products.md) | [workflow-competitor-table-design.md](../arch/workflow-competitor-table-design.md) |
| 竞品到达人池同步 | `sync_tk_influencer_pool` | 每天定时任务 | `TK竞品收集`、`TK达人池` | [requirements/sync-tk-influencer-pool.md](./requirements/sync-tk-influencer-pool.md) | [workflow-influencer-pool-sync-design.md](../arch/workflow-influencer-pool-sync-design.md) |
| 选品采集 | `tiktok_fastmoss_product_ingest` | OpenClaw 定时/手动触发 | `TK选品收集` | [requirements/tk-selection-collection.md](./requirements/tk-selection-collection.md) | [workflow-selection-table-design.md](../arch/workflow-selection-table-design.md) |
| 关键词搜索选品写入 | `search_keyword_selection_products` | OpenClaw 对话输入 | `TK选品收集` | [requirements/search-keyword-selection-products.md](./requirements/search-keyword-selection-products.md) | [workflow-selection-table-design.md](../arch/workflow-selection-table-design.md) |
| 达人建联检查 | `tiktok_influencer_outreach_sync` | 定时任务或手动触发 | `TK达人建联表` | [requirements/tk-influencer-outreach.md](./requirements/tk-influencer-outreach.md) | [workflow-influencer-outreach-design.md](../arch/workflow-influencer-outreach-design.md) |
| Amazon 竞品表单商品采集（实施中） | `refresh_amazon_product_row_by_asin` | 指定飞书来源行手动/自动触发 | `Amazon竞品表`（`AMAZON_PRODUCTS`） | [requirements/amazon-product-detail-collection.md](./requirements/amazon-product-detail-collection.md) | [workflow-amazon-product-detail-design.md](../arch/workflow-amazon-product-detail-design.md) |
| Amazon 竞品表批量采集（实施中） | `refresh_current_amazon_product_table` | OpenClaw 手动触发，只处理 `采集标签=T` | `Amazon竞品表`（`AMAZON_PRODUCTS`） | [requirements/amazon-product-detail-collection.md](./requirements/amazon-product-detail-collection.md) | [workflow-amazon-product-detail-design.md](../arch/workflow-amazon-product-detail-design.md) |

### 4.2 变更隔离规则

1. 单个流程的客户需求、采集回写逻辑、交付动作和验收口径，优先只维护对应流程文档。
2. 表结构、自动维护字段、非自动维护字段、跨流程状态字段和正式流程清单，统一维护在本文档。
3. 新增正式流程时，先在 `docs/business/requirements/` 新建独立流程文档，再在本节索引中补充入口。
4. 待澄清需求在澄清完成前不拆成正式流程文档；一旦转为正式需求，按同一模板新增独立文档。
5. 设计实现、模块划分、接口、调度、浏览器自动化和登录实现仍属于设计文档范围，不写入需求文档。

## 5. 待澄清需求（文档占位表）

本节用于记录当前业务中尚未定稿的需求项。
这张表不是新的飞书真实业务表，也不参与现有五表的结构分析；它只用于在正式需求文档中沉淀待确认的业务决策，后续澄清完成后再拆回正式流程需求文档或设计文档。

| 需求标题 | 来源 | 涉及表 | 目标说明 | 待澄清点 | 当前假设 | 状态 | 优先级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `选品采集` | `2026-04-14 新增四表需求` | `TK选品收集` | 扫描选品表记录，对自动维护字段存在空值的商品触发 TikTok + FastMoss 采集并补齐字段；URL 无效时标记"链接不可访问"；图表类字段在写回前按需渲染 PNG。 | 店铺入口等独立选品入口需求仍待后续澄清。 | 已提升为正式流程需求文档 [requirements/tk-selection-collection.md](./requirements/tk-selection-collection.md)；关键词搜索选品写入已提升为 [requirements/search-keyword-selection-products.md](./requirements/search-keyword-selection-products.md)。 | `已澄清` | `P1` |
| `TK达人池表扩展` | `2026-04-14 新增四表需求` | `TK达人池` | 基于 `TK竞品收集` 中可跳转到 FastMoss 商品详情的商品行，筛选并沉淀满足条件的达人，同时维护达人画像与关联商品字段。 | 当前范围内无新增待澄清点；如后续要求“达人联系方式必须非空”或“合作店铺自动新增新选项”，再单独开启下一轮澄清。 | `TK达人池` 保持一人一行，按 `达人ID` 做 upsert；筛选口径固定为“商品页达人销量 `sold_count > 50` 且粉丝数 `> 5000`”；同一达人命中多个商品时，在原行累加 `带货商品图`、`关联商品销量`、`关联节日`，并合并 `合作店铺`；`合作商品数` 不作为本期更新字段；`粉丝数`、`带货视频 GMV`、`带货直播 GMV` 写入飞书时按整数 `W` 单位四舍五入展示，小于 `10000` 显示 `小于1W`；`达人联系方式` 有多个时优先邮箱，否则第一个有效联系方式，没有则不写入；首次插入达人行时同时写 `记录日期` 和 `更新日期`，后续新商品合并时只刷新 `更新日期`；`检查达人名称是否重复` 不参与写入。 | `已澄清` | `P1` |
| `TK达人建联表扩展` | `2026-04-14 新增四表需求` | `TK达人建联表` | 以商品与达人建联为入口，跟踪达人是否按约发布视频，并回写视频链接和视频发布时间。 | 当前范围内无新增待澄清点；播放量自动回写、30 天未履约提醒和正式 task_code 后续单独确认。 | 已提升为正式流程需求文档 [requirements/tk-influencer-outreach.md](./requirements/tk-influencer-outreach.md)；当前按 `SKUID=FastMoss product_id`、`达人ID=unique_id` 匹配 FastMoss 商品关联视频。 | `已澄清` | `P1` |
| `TK合作爆款视频表扩展` | `2026-04-14 新增四表需求` | `TK合作爆款视频` | 根据客户提供的 `skuid` 进入 FastMoss 商品详情页，沉淀播放量大于 20 万的关联视频。 | 客户提供的 `skuid` 是商品 ID 还是变体 SKU 未定；关联视频筛选范围未定；回写字段口径未定。 | 先按商品详情页维度理解，一行代表一条满足阈值的视频记录，后续再确认 `skuid` 的真实定义。 | `待澄清` | `P1` |

## 6. 设计边界

本需求文档和流程级需求文档不展开以下内容：

- 程序架构与模块划分
- 具体任务编排方式
- 具体接口设计
- 具体浏览器自动化实现
- 具体登录实现
- 具体定时任务配置方式

以上内容统一放到 `docs/arch` 或 `docs/ops` 中说明。

## 7. 版本信息

- 需求版本：`v3.4`
- 文档版本：`v3.7.0`
- 版本日期：`2026-07-15`
- 本次变更：纳入 Amazon 美国站单商品采集正式需求索引，并区分已实时验证的 TikTok 表与部署配置的 `AMAZON_PRODUCTS` 路由。

## 8. 关联文档

- [requirements/README.md](./requirements/README.md)
- [requirements/refresh-current-competitor-table.md](./requirements/refresh-current-competitor-table.md)
- [requirements/search-keyword-competitor-products.md](./requirements/search-keyword-competitor-products.md)
- [requirements/sync-tk-influencer-pool.md](./requirements/sync-tk-influencer-pool.md)
- [requirements/tk-selection-collection.md](./requirements/tk-selection-collection.md)
- [requirements/search-keyword-selection-products.md](./requirements/search-keyword-selection-products.md)
- [requirements/tk-influencer-outreach.md](./requirements/tk-influencer-outreach.md)
- [requirements/amazon-product-detail-collection.md](./requirements/amazon-product-detail-collection.md)
- [../arch/README.md](../arch/README.md)
