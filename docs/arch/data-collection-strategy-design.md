# 数据采集策略与频率设计

更新时间：`2026-04-21`

状态：架构策略文档。本文用于指导采集频率、窗口数据和事实沉淀策略；当前客户需求以 `docs/business` 为准，当前 Fact DB schema 以 [fact-db-schema-design.md](./fact-db-schema-design.md) 为准。

本文基于当前已验证的 TikTok PDP 和 FastMoss 商品/达人/视频/店铺接口，定义一套更节省资源的数据采集策略。核心目标是：每天只抓真正必要的原子事实，窗口结构数据按商品价值和业务节点采集，避免把 `7天 / 28天 / 90天` 窗口重复请求和重复保存成资源浪费。

相关文档：

- [../reference/fastmoss-known-interfaces.md](../reference/fastmoss-known-interfaces.md)
- [../reference/fastmoss-visualization-analysis.md](../reference/fastmoss-visualization-analysis.md)
- [../reference/fastmoss-four-entities-interface-design.md](../reference/fastmoss-four-entities-interface-design.md)
- [fact-db-schema-design.md](./fact-db-schema-design.md)

## 1. 核心结论

采集策略应该从“每天抓一堆窗口快照”改成：

```text
每日原子事实优先
窗口观察值作为外部平台口径
结构占比和排行按需/按层级采集
复盘节点做不可变快照
```

更具体：

1. 商品总销量、GMV、价格趋势，优先落到 `product_daily_metrics`，后续在数据库里按日期 filter 动态计算 `近7天 / 近28天 / 近90天`。
2. FastMoss 的 `d_type=7/28/90` 不是数据库必须每天持久化的基础粒度，而是 FastMoss 按窗口算好的外部观察值。
3. 成交渠道、成交内容、成交投放、SKU 占比、达人排行、视频排行，只有在本地有对应维度的每日事实时，才能完全本地计算。
4. 如果本地没有每日渠道/SKU/达人/视频维度事实，就需要保留 FastMoss 的窗口结构数据，但不需要每天对所有商品全量保存。
5. 每日默认只跑低成本核心任务；结构数据和排行数据按商品分层、节日节点、人工触发来跑。

## 2. FastMoss 接口资源判断

### 2.1 商品概览接口是同一个接口，不同参数

已验证商品概览接口：

```text
GET /api/goods/v3/overview?product_id=<product_id>&d_type=28
GET /api/goods/v3/overview?product_id=<product_id>&d_type=7
GET /api/goods/v3/overview?product_id=<product_id>&d_type=90
GET /api/goods/v3/overview?product_id=<product_id>&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

它们是同一个接口，不同参数。

重要结论：

- 请求 `d_type=28`，不会同时返回 `d_type=7` 和 `d_type=90`。
- 请求单日 `start_date=end_date`，不会顺便返回 `7天 / 28天 / 90天`。
- 请求 `d_type=28` 时，会返回当前 28 天窗口下的 `overview`、`chart_list` 和三类成交占比。

`d_type=28` 返回结构：

| 字段 | 含义 | 是否适合每天解析 |
| --- | --- | --- |
| `data.overview` | 28 天窗口汇总 | 可以作为校验值，不一定每天追加历史 |
| `data.chart_list` | 28 天内每日明细 | 适合每天 upsert 到每日事实表 |
| `data.channel_distribution` | 28 天成交渠道占比 | 重点商品可保存，普通商品可只更新最新值 |
| `data.content_distribution` | 28 天成交内容占比 | 同上 |
| `data.ads_distribution` | 28 天成交投放占比 | 同上 |

### 2.2 SKU、视频、达人是额外接口

这些不是 `overview` 顺带返回的完整明细，需要单独请求：

| 数据 | 接口 | 资源特征 |
| --- | --- | --- |
| SKU 完整清单、价格、库存 | `/api/goods/v3/productSku` | 每商品 1 次 |
| SKU 销量/GMV/库存占比 | `/api/goods/productSku?d_type=28` 或相关 SKU 分布接口 | 每商品 1 次 |
| 商品关联视频排行 | `/api/goods/v3/video` | 分页，抓全量成本高 |
| 商品关联达人排行 | `/api/goods/v3/author` | 分页，抓全量成本高 |
| 商品相关直播 | `/api/goods/v3/live` | 分页，按需 |
| 广告视频表现 | `/api/goods/V3/adsVideo` | 按需 |

样例商品 `1732183068040729370` 中：

- `/api/goods/v3/video` 返回 `total=87`。
- 当前页面实抓 `pagesize=5`，如果抓全量需要约 `18` 页。
- 所以视频/达人排行不应该对所有商品每天全量抓。

### 2.3 TikTok PDP 适合低频基础补齐

TikTok PDP HTML 可以拿：

- 商品标题。
- 商品主图和侧边栏图片。
- SKU 和规格。
- SKU 库存。
- 商品评分和评论数。
- 店铺 ID。

但这些不适合每天全量重复抓图片。建议：

- 新商品入库时抓一次。
- 商品状态变化或手动刷新时抓一次。
- 重点商品每周轻量检查一次。
- 图片下载只在 URL 变化或文件缺失时执行。

## 3. 哪些可以本地算

如果本地每天保存商品每日事实：

```text
product_id
metric_date
inc_sold_count
inc_sale_amount
price
```

那么这些都可以数据库动态计算：

| 指标 | 本地计算条件 | 是否需要每天抓窗口 |
| --- | --- | --- |
| 近 7 天销量 | 有每日销量 | 不需要 |
| 近 28 天销量 | 有每日销量 | 不需要 |
| 近 90 天销量 | 有 90 天每日销量 | 不需要 |
| 近 7/28/90 天 GMV | 有每日 GMV | 不需要 |
| 日均销量 | 有每日销量 | 不需要 |
| 峰值日 | 有每日销量 | 不需要 |
| 价格趋势 | 有每日价格 | 不需要 |

示例查询：

```sql
select
  product_id,
  sum(inc_sold_count) as sold_count_28d,
  sum(inc_sale_amount) as sale_amount_28d,
  avg(price) as avg_price_28d
from product_daily_metrics
where metric_date between current_date - interval '27 days' and current_date
group by product_id;
```

## 4. 哪些不能只靠商品每日总表算

如果本地只保存 `商品 + 日期 + 销量 + GMV`，就无法本地计算结构归因。

| 指标 | 本地计算需要的原子粒度 |
| --- | --- |
| 成交渠道占比 | `product_id + date + channel + sold_count + gmv` |
| 成交内容占比 | `product_id + date + content_type + sold_count + gmv` |
| 成交投放占比 | `product_id + date + ads_type + sold_count + gmv` |
| SKU 销量占比 | `product_id + date + sku_id + sold_count + gmv` |
| 达人排行 | `product_id + date + uid + sold_count + gmv` |
| 视频排行 | `product_id + date + video_id + sold_count + gmv` |
| ROAS | `product_id/video_id + date + ad_cost + gmv` |

所以“平台窗口结构占比能不能本地处理”的答案是：

```text
能，但前提是你每天采到了同等粒度的原子事实。
如果没有，就只能保留 FastMoss 的窗口观察值。
```

## 5. 推荐数据保存口径

### 5.1 每日事实表

每天稳定保存：

```text
product_daily_metrics
```

来源：

```text
/api/goods/v3/overview?product_id=<id>&d_type=28 -> data.chart_list[]
```

为什么用 `d_type=28` 而不是单日：

- 一次请求能拿最近 28 天每日明细。
- 如果前一天任务失败，第二天还能回补最近 28 天内的缺口。
- 后续 `近7天 / 近28天` 可以本地算。
- 对商品级趋势来说，单日接口价值不如 `d_type=28` 稳。

入库策略：

- 按 `product_id + metric_date + source_platform` upsert。
- 同一天重复采集时，保留最新采集值，同时记录 `last_collected_at`。
- 不为 `7天 / 28天 / 90天` 另建每日重复窗口事实。

### 5.2 窗口观察值

窗口观察值不是每日分析基础，而是外部平台口径。

建议拆成两类：

```text
window_latest
window_history
```

`window_latest`：

- 用于保存某商品当前最新 `d_type=28` 观察值。
- 每日任务可以覆盖更新。
- 不无限追加，避免大量重叠 28 天窗口污染历史。

`window_history`：

- 只在关键节点追加。
- 例如节日复盘、手动刷新、每周结构快照、异常排查。
- 用于“当时 FastMoss 是这么看的”。

推荐字段：

| 字段 | 说明 |
| --- | --- |
| `product_id` | 商品 |
| `source_platform` | fastmoss |
| `source_endpoint` | `goods.v3.overview` |
| `window_days` | 7 / 28 / 90 |
| `window_start` | 窗口开始 |
| `window_end` | 窗口结束 |
| `collected_at` | 实际采集时间 |
| `observation_reason` | daily_cache / weekly_refresh / manual_review / onboarding / backfill |
| `is_persisted_snapshot` | 是否作为历史快照长期保留 |

### 5.3 结构占比数据

成交渠道、内容、投放占比来自 `overview` 同一次响应，不增加额外接口请求。

但是否每天保存历史，要看商品层级：

| 商品层级 | 保存策略 |
| --- | --- |
| S 级重点商品 | 可每天追加 28 天结构观察值 |
| A 级观察商品 | 每周追加，平时只更新 latest |
| B 级普通商品 | 只更新 latest 或手动复盘时追加 |

注意：

- 如果只是每天请求 `d_type=28`，拿到的是每天滚动的 28 天结构占比。
- 这不是每日渠道事实，而是“截至当天的近 28 天窗口占比”。
- 用连续 28 天窗口占比不能严格还原每天渠道占比。

如果未来真的需要“本地计算任意窗口渠道占比”，需要单独采集：

```text
/api/goods/v3/overview?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

并把返回的单日 `channel_distribution / content_distribution / ads_distribution` 存为每日结构事实。但这只建议对 S 级商品开启。

### 5.4 SKU 占比数据

SKU 占比不是 `overview` 完整给出的，需要额外请求 SKU 接口。

推荐策略：

| 商品层级 | SKU 占比采集频率 |
| --- | --- |
| S 级重点商品 | 每天或每 2 天 |
| A 级观察商品 | 每周 |
| B 级普通商品 | 新品入库、复盘、手动触发 |

理由：

- SKU 占比用于判断主销规格、缺货风险。
- 对普通商品每天抓 SKU 占比，业务收益不高。
- SKU 基础清单可以从 TikTok PDP 或 FastMoss SKU 清单低频补齐。

### 5.5 视频/达人排行

视频和达人排行容易分页放大请求量。

推荐只抓 Top，不默认全量：

| 数据 | 默认策略 |
| --- | --- |
| 视频 Top 5 / Top 10 | S 级每天，A 级每周 |
| 视频全量 | 节日复盘、手动触发 |
| 达人 Top 5 / Top 10 | S 级每天或每 2 天，A 级每周 |
| 达人全量 | 节日复盘、手动触发 |

原因：

- 商品是否值得跟进，通常 Top 视频/Top 达人已经足够判断。
- 全量视频/达人主要用于复盘和达人池扩展，不适合所有商品每日执行。

## 6. 商品分层策略

推荐把商品分成三层：

| 层级 | 定义 | 典型场景 |
| --- | --- | --- |
| S 级 | 重点爆品、节日周期内、正在复盘、人工重点跟踪 | 情人节/复活节/毕业季核心商品 |
| A 级 | 潜力商品、观察商品、刚入库但未确认价值 | 近 7/28 天有增长但未确定 |
| B 级 | 普通沉淀商品、历史商品、低优先级商品 | 已复盘、低销量、暂不跟进 |

可用判定规则：

| 条件 | 建议层级 |
| --- | --- |
| 人工标记 `重点跟踪` | S |
| 节日前后 45 天内且销量增长明显 | S |
| 近 7 天销量超过阈值 | S 或 A |
| 新入库商品 | A |
| 最近 28 天无明显销量 | B |
| 已放弃 / 已复盘完成 | B |

层级不是永久的，应每天根据最新事实和人工状态重新评估。

## 7. 推荐任务设计

### 7.1 `daily_core_job`

每天运行。

目的：

- 保持商品每日销量、GMV、价格趋势。
- 低成本支撑大部分可视化窗口查询。

请求：

```text
GET /api/goods/v3/overview?product_id=<id>&d_type=28
```

保存：

- `data.chart_list[]` -> `product_daily_metrics`
- `data.overview` -> `product_window_latest`
- `channel/content/ads_distribution` -> 对 S 级追加 history，对 A/B 只更新 latest

频率：

| 商品层级 | 频率 |
| --- | --- |
| S | 每天 |
| A | 每天或每 2 天 |
| B | 每周或手动 |

### 7.2 `structure_refresh_job`

按层级运行。

目的：

- 更新 SKU 占比。
- 更新主销规格。
- 更新重点商品的结构归因。

请求：

```text
GET /api/goods/productSku?product_id=<id>&d_type=28
GET /api/goods/v3/productSku?product_id=<id>&d_type=28
```

保存：

- `product_sku_window_observations`
- `product_skus`
- `product_sku_images`

频率：

| 商品层级 | 频率 |
| --- | --- |
| S | 每天或每 2 天 |
| A | 每周 |
| B | 手动 / 复盘 |

### 7.3 `ranking_top_job`

按层级运行。

目的：

- 保留视频和达人贡献 Top 数据。
- 支撑达人建联和爆款视频判断。

请求：

```text
GET /api/goods/v3/video?page=1&product_id=<id>&pagesize=5&date_type=28
GET /api/goods/v3/author?page=1&product_id=<id>&pagesize=5
```

保存：

- `fact_video_product_window_performance`
- `fact_creator_product_window_performance`
- `videos`
- `creators`

频率：

| 商品层级 | 频率 |
| --- | --- |
| S | 每天或每 2 天 |
| A | 每周 |
| B | 手动 |

### 7.4 `review_snapshot_job`

手动触发或节日节点触发。

目的：

- 生成节日复盘的不可变快照。
- 保留当时看到的窗口口径。
- 抓全量视频、达人、SKU、结构占比。

请求：

```text
overview d_type=28 或自定义 start_date/end_date
productSku
video 全量分页
author 全量分页
必要时 shop / creator / video 详情
```

保存：

- 所有窗口表都以 `observation_reason=manual_review` 或 `holiday_review` 追加历史。
- 这类数据不要被 daily job 覆盖。

触发时机：

- 节日前预热复盘。
- 节日当天。
- 节后第 1 天、第 7 天、第 14 天、第 28 天。
- 人工点选某个 SKU 做深度复盘。

### 7.5 `onboarding_job`

新商品入库时运行。

目的：

- 建立商品基础主档。
- 下载主图和侧边栏图。
- 初始化 SKU、店铺、评分、评论数。

请求：

```text
TikTok PDP HTML
FastMoss overview d_type=7
FastMoss overview d_type=28
FastMoss overview d_type=90
FastMoss productSku，按需要
```

窗口用途：

- `d_type=7` 用于近 7 天销量字段和短周期校验。
- `d_type=28` 用于常规商品概览、趋势和结构数据。
- `d_type=90` 用于新商品首次入库时做 90 天回填，补齐近 90 天销量字段。
- 不建议之后每天抓 `d_type=90`。

### 7.6 `daily_distribution_job`

默认不开启，只对 S 级商品启用。

目的：

- 获取单日成交渠道/内容/投放结构。
- 让本地未来可以严格计算任意窗口结构占比。

请求：

```text
GET /api/goods/v3/overview?product_id=<id>&start_date=<day>&end_date=<day>
```

保存：

- `product_distribution_daily_metrics`

说明：

- 这会额外增加请求量。
- 只有当业务真的需要“本地计算渠道结构趋势”时才开。
- 如果只做节日复盘，直接保留 FastMoss 28 天窗口结构通常已经够用。

## 8. 推荐频率矩阵

| 数据 | S 级 | A 级 | B 级 |
| --- | --- | --- | --- |
| `overview d_type=28` | 每天 | 每天/每 2 天 | 每周 |
| `chart_list` 入每日事实 | 每天 | 每天/每 2 天 | 每周 |
| 28 天结构占比 history | 每天/每 2 天 | 每周 | 手动/复盘 |
| 28 天结构占比 latest | 每天 | 每天/每 2 天 | 每周 |
| SKU 占比 | 每天/每 2 天 | 每周 | 手动/复盘 |
| SKU 基础清单 | 每周或变更时 | 每周/每月 | 入库/手动 |
| 视频 Top | 每天/每 2 天 | 每周 | 手动 |
| 达人 Top | 每天/每 2 天 | 每周 | 手动 |
| 视频/达人全量 | 复盘/手动 | 复盘/手动 | 不默认抓 |
| TikTok PDP 图片 | 入库/变更/每周检查 | 入库/每月检查 | 入库/手动 |
| `d_type=90` | 入库/复盘 | 入库/复盘 | 不默认抓 |

## 9. 请求量估算

假设商品数为 `N`。

最低成本每日任务：

```text
N 次 / 天
```

每个商品只请求：

```text
overview d_type=28
```

中等成本任务：

```text
2N - 4N 次 / 天
```

每个重点商品请求：

```text
overview d_type=28
productSku
video page 1
author page 1
```

高成本任务：

```text
N * 全量分页
```

例如样例商品有 `87` 个视频，若 `pagesize=5`：

```text
ceil(87 / 5) = 18 次视频请求
```

所以全量视频/达人不应每天对所有商品执行。

## 10. 数据库设计调整建议

基于这个采集策略，建议把 ERD 中的“快照”概念进一步拆清楚：

```text
product_daily_metrics
product_window_latest
product_window_observations
product_distribution_window_latest
product_distribution_window_observations
product_sku_window_latest
product_sku_window_observations
fact_video_product_window_performance
fact_creator_product_window_performance
```

其中：

| 表 | 作用 |
| --- | --- |
| `product_daily_metrics` | 本地动态计算窗口总量的核心事实 |
| `product_window_latest` | 每个商品当前最新窗口观察值，覆盖更新 |
| `product_window_observations` | 节点型历史快照，追加保存 |
| `product_distribution_window_latest` | 当前最新结构占比 |
| `product_distribution_window_observations` | 历史结构占比观察 |
| `product_sku_window_latest` | 当前最新 SKU 结构 |
| `product_sku_window_observations` | 历史 SKU 结构 |

不要把每天滚动的 `28天窗口` 都无脑当成永久历史快照。
否则会出现大量高度重叠的数据，后续分析时反而混乱。

## 11. 设计理由

### 11.1 避免接口资源浪费

每天抓 `d_type=7 + d_type=28 + d_type=90` 是典型浪费，因为商品级总销量和 GMV 可以从 daily 表计算。

推荐只保留：

```text
daily_core: overview d_type=28
backfill/review: d_type=90 或自定义窗口
```

### 11.2 避免存储语义混乱

每天保存一个滚动 28 天窗口，会产生：

```text
2026-04-01 的 28 天窗口
2026-04-02 的 28 天窗口
2026-04-03 的 28 天窗口
```

这些窗口大量重叠。它们不是每日事实，而是外部观察值。
如果不区分 `latest` 和 `history`，后续很容易把窗口数据误当成可加总数据。

### 11.3 保留 FastMoss 的不可还原口径

FastMoss 的渠道、内容、投放、SKU、达人、视频归因口径，不一定能从商品每日总销量还原。

所以：

- 总量趋势本地算。
- 结构归因按需保留 FastMoss 观察值。
- 如果未来要完全本地计算结构归因，再增加每日维度事实采集。

### 11.4 支持节日玩具业务周期

节日玩具不是全年均匀关注，每个节日有明显周期：

- 节前预热。
- 爆发期。
- 节后复盘。
- 历史沉淀。

因此用商品分层和复盘节点触发，比全量每日深采更适合。

## 12. 最终建议

第一版生产策略：

```text
每天：
  S/A 商品抓 overview d_type=28
  B 商品每周抓 overview d_type=28
  chart_list 入 product_daily_metrics
  overview 只更新 latest，S 级可追加窗口 history

每周：
  A/S 商品刷新 SKU 占比
  A/S 商品刷新视频 Top 和达人 Top

手动/复盘：
  抓完整 SKU
  抓全量视频
  抓全量达人
  保存不可变 review snapshot

新商品入库：
  抓 TikTok PDP 基础信息和图片
  抓 overview d_type=28
  必要时抓 d_type=90 回填
```

一句话：

```text
每日抓趋势，周期抓结构，复盘抓全量。
```
