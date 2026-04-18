# FastMoss 可视化分析

更新时间：`2026-04-17`

本文单独记录 FastMoss 商品详情页里常用图表的渲染逻辑、数据源接口、字段映射和落表建议。接口字段的更完整说明见 [fastmoss已知接口.md](./fastmoss已知接口.md)。

## 1. 总体结论

FastMoss 商品详情页的可视化不是后端返回图片，而是前端拿 JSON 数据后在浏览器里渲染。

本轮在商品详情页运行时观察到：

- 页面技术栈：`Next.js + React`
- 图表容器：`echarts-for-react`
- 图表实例标记：DOM 上存在 `_echarts_instance_`
- 实际绘制方式：`canvas`
- 数据请求：主要来自 `/api/goods/v3/overview` 和 `/api/goods/productSku`

因此，复刻这些可视化时可以采用同一条链路：

```text
FastMoss API JSON
  -> 数据清洗和口径映射
  -> ECharts option
  -> 折线图 / 环形图 / 排名条形图
```

## 2. 通用请求口径

### 2.1 商品详情页核心参数

| 参数 | 含义 | 示例 |
| --- | --- | --- |
| `product_id` | FastMoss / TikTok Shop 商品 ID | `1729440407432826887` |
| `d_type` | 统计窗口 | `7`, `28`, `90`, `180` |
| `start_date` / `end_date` | 自定义统计日期 | `2026-03-20` |

### 2.2 时间窗口建议

| 页面按钮 | 建议参数 |
| --- | --- |
| 近 7 天 | `d_type=7` |
| 近 28 天 | `d_type=28` |
| 近 90 天 | `d_type=90` |
| 近 180 天 | `d_type=180`，页面上存在按钮时再验证 |
| 单日 / 自定义区间 | `start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` |

### 2.3 占比字段处理

FastMoss 原始字段里占比拼写为 `propotion`，不是 `proportion`。

建议统一转换：

```text
"85%" -> 0.85
"3%" -> 0.03
"NAN%" -> null
```

建议同时保留：

- 原始展示值：`propotion`, `sold_count_show`, `sale_amount_show`
- 可计算数值：`占比`, `销量`, `GMV`

## 3. 销量 / 销售额趋势折线图

### 3.1 页面图表

对应页面里的商品概览趋势图，展示某个时间窗口内每天的销量和销售额变化。

推荐图表：

- ECharts `line`
- X 轴：日期
- Y 轴左侧：销量
- Y 轴右侧：销售额
- tooltip：同时展示销量、销售额、价格、累计销量、累计销售额

### 3.2 数据源接口

```text
GET /api/goods/v3/overview?product_id=<product_id>&d_type=28
```

自定义日期：

```text
GET /api/goods/v3/overview?product_id=<product_id>&start_date=2026-03-20&end_date=2026-04-16
```

### 3.3 对应接口字段

| 图表字段 | 接口字段 | 说明 |
| --- | --- | --- |
| 日期 | `data.chart_list[].dt` | X 轴 |
| 当日销量 | `data.chart_list[].inc_sold_count` | 销量折线 |
| 当日销售额 | `data.chart_list[].inc_sale_amount` | 销售额折线 |
| 累计销量 | `data.chart_list[].sold_count` | tooltip 可展示 |
| 累计销售额 | `data.chart_list[].sale_amount` | tooltip 可展示 |
| 当日价格 | `data.chart_list[].price` | tooltip 或价格趋势 |
| 当日达人增量 | `data.chart_list[].inc_author_count` | 辅助分析 |
| 当日视频增量 | `data.chart_list[].inc_aweme_count` | 辅助分析 |
| 窗口销量 | `data.overview.sold_count` | 指标卡 |
| 窗口销售额 | `data.overview.sale_amount` | 指标卡 |
| 日均销量 | `data.overview.avg_sold_count` | 指标卡 |
| 日均销售额 | `data.overview.avg_sale_amount` | 指标卡 |

### 3.4 渲染逻辑

```js
const rows = overview.data.chart_list || [];

const option = {
  tooltip: { trigger: "axis" },
  legend: { data: ["销量", "销售额"] },
  xAxis: {
    type: "category",
    data: rows.map((row) => row.dt),
  },
  yAxis: [
    { type: "value", name: "销量" },
    { type: "value", name: "销售额" },
  ],
  series: [
    {
      name: "销量",
      type: "line",
      smooth: true,
      yAxisIndex: 0,
      data: rows.map((row) => row.inc_sold_count || 0),
    },
    {
      name: "销售额",
      type: "line",
      smooth: true,
      yAxisIndex: 1,
      data: rows.map((row) => row.inc_sale_amount || 0),
    },
  ],
};
```

### 3.5 飞书落表建议

| 飞书表 | 字段 |
| --- | --- |
| `商品统计快照` | `窗口销量`, `窗口GMV`, `日均销量`, `当前总销量`, `峰值日`, `峰值日销量` |
| `每日销量趋势` | `日期`, `当日销量`, `当日GMV`, `当日达人数量`, `当日视频数`, `是否峰值日` |
| `价格趋势明细` | `日期`, `价格`, `价格类型`, `关联商品`, `关联快照` |

## 4. 成交渠道占比

### 4.1 页面图表

成交渠道占比回答的是“订单来自哪个成交渠道”。例如达人联盟、商品卡、店铺账号。

推荐图表：

- ECharts `pie`
- `radius: ["55%", "75%"]` 环形图
- 指标切换：销量占比 / GMV 占比
- 排序：按销量或 GMV 从高到低

### 4.2 数据源接口

```text
GET /api/goods/v3/overview?product_id=<product_id>&d_type=28
```

### 4.3 对应接口字段

根字段：

```text
data.channel_distribution
```

销量口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总销量 | `data.channel_distribution.units_sold.total_count` |
| 渠道原始 key | `data.channel_distribution.units_sold.list[].source` |
| 销量占比 | `data.channel_distribution.units_sold.list[].propotion` |
| 销量 | `data.channel_distribution.units_sold.list[].sold_count` |
| 销量展示值 | `data.channel_distribution.units_sold.list[].sold_count_show` |

GMV 口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总 GMV | `data.channel_distribution.gmv.total_count` |
| 渠道原始 key | `data.channel_distribution.gmv.list[].source` |
| GMV 占比 | `data.channel_distribution.gmv.list[].propotion` |
| GMV | `data.channel_distribution.gmv.list[].sale_amount` |
| 币种 | `data.channel_distribution.gmv.list[].currency` |
| GMV 展示值 | `data.channel_distribution.gmv.list[].sale_amount_show` |

### 4.4 业务映射

| FastMoss key | 建议中文名 |
| --- | --- |
| `common.goods.affiliate` | 达人联盟 / 达人橱窗 |
| `common.goods.product_card` | 商品卡 |
| `common.goods.shop_account` | 店铺账号 |
| 其他未知值 | 其他 |

### 4.5 渲染逻辑

```js
const units = overview.data.channel_distribution.units_sold.list || [];
const gmv = overview.data.channel_distribution.gmv.list || [];

const gmvBySource = new Map(gmv.map((row) => [row.source, row]));

const rows = units.map((row) => {
  const gmvRow = gmvBySource.get(row.source) || {};
  return {
    key: row.source,
    name: mapChannelName(row.source),
    sales: row.sold_count || 0,
    salesShare: parsePercent(row.propotion),
    gmv: gmvRow.sale_amount || 0,
    gmvShare: parsePercent(gmvRow.propotion),
  };
});

const option = {
  tooltip: { trigger: "item" },
  series: [
    {
      name: "成交渠道占比",
      type: "pie",
      radius: ["55%", "75%"],
      data: rows.map((row) => ({
        name: row.name,
        value: row.sales,
      })),
    },
  ],
};
```

## 5. 成交内容占比

### 5.1 页面图表

成交内容占比回答的是“订单来自哪类内容形态”。例如短视频、直播、商品卡。

推荐图表：

- ECharts `pie`
- 环形图
- 指标切换：销量占比 / GMV 占比
- 可和成交渠道占比放在同一组 tab 里

### 5.2 数据源接口

```text
GET /api/goods/v3/overview?product_id=<product_id>&d_type=28
```

### 5.3 对应接口字段

根字段：

```text
data.content_distribution
```

销量口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总销量 | `data.content_distribution.units_sold.total_count` |
| 内容类型 key | `data.content_distribution.units_sold.list[].category` |
| 销量占比 | `data.content_distribution.units_sold.list[].propotion` |
| 销量 | `data.content_distribution.units_sold.list[].sold_count` |
| 销量展示值 | `data.content_distribution.units_sold.list[].sold_count_show` |

GMV 口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总 GMV | `data.content_distribution.gmv.total_count` |
| 内容类型 key | `data.content_distribution.gmv.list[].category` |
| GMV 占比 | `data.content_distribution.gmv.list[].propotion` |
| GMV | `data.content_distribution.gmv.list[].sale_amount` |
| 币种 | `data.content_distribution.gmv.list[].currency` |
| GMV 展示值 | `data.content_distribution.gmv.list[].sale_amount_show` |

### 5.4 业务映射

| FastMoss key | 建议中文名 |
| --- | --- |
| `video.name` | 短视频 |
| `live.name` | 直播 |
| `common.goods.product_card` | 商品卡 |
| 其他未知值 | 其他 |

### 5.5 渲染逻辑

```js
const units = overview.data.content_distribution.units_sold.list || [];
const gmv = overview.data.content_distribution.gmv.list || [];

const gmvByCategory = new Map(gmv.map((row) => [row.category, row]));

const rows = units.map((row) => {
  const gmvRow = gmvByCategory.get(row.category) || {};
  return {
    key: row.category,
    name: mapContentName(row.category),
    sales: row.sold_count || 0,
    salesShare: parsePercent(row.propotion),
    gmv: gmvRow.sale_amount || 0,
    gmvShare: parsePercent(gmvRow.propotion),
  };
});
```

## 6. 成交投放占比

### 6.1 页面图表

成交投放占比回答的是“订单是否来自广告流量”。它和广告投放概览不同：

- 成交投放占比：来自 `/api/goods/v3/overview` 的 `ads_distribution`
- 广告投放概览：来自 `/api/goods/V3/investment`，用于看广告成本、ROAS、广告视频趋势

推荐图表：

- ECharts `pie`
- 环形图
- 指标切换：销量占比 / GMV 占比

### 6.2 数据源接口

```text
GET /api/goods/v3/overview?product_id=<product_id>&d_type=28
```

### 6.3 对应接口字段

根字段：

```text
data.ads_distribution
```

销量口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总销量 | `data.ads_distribution.units_sold.total_count` |
| 投放类型 key | `data.ads_distribution.units_sold.list[].category` |
| 销量占比 | `data.ads_distribution.units_sold.list[].propotion` |
| 销量 | `data.ads_distribution.units_sold.list[].sold_count` |
| 销量展示值 | `data.ads_distribution.units_sold.list[].sold_count_show` |

GMV 口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 总 GMV | `data.ads_distribution.gmv.total_count` |
| 投放类型 key | `data.ads_distribution.gmv.list[].category` |
| GMV 占比 | `data.ads_distribution.gmv.list[].propotion` |
| GMV | `data.ads_distribution.gmv.list[].sale_amount` |
| 币种 | `data.ads_distribution.gmv.list[].currency` |
| GMV 展示值 | `data.ads_distribution.gmv.list[].sale_amount_show` |

### 6.4 业务映射

| FastMoss key | 建议中文名 |
| --- | --- |
| `common.goods.adTraffic` | 广告流量 |
| `common.goods.otherTraffic` | 非广告流量 |
| 其他未知值 | 其他 |

## 7. SKU 销量 / 销售额占比

### 7.1 页面图表

SKU 占比回答的是“哪个规格卖得最多、贡献 GMV 最高”。这部分不使用 `/api/goods/v3/productSku`，因为 `v3` 接口主要给完整 SKU 清单、价格和库存，不直接给 SKU 销量占比。

推荐图表：

- Top 规格：横向条形图或排行榜
- 占比结构：环形图
- 指标切换：销量 / GMV
- `Other` 聚合项保留为单独项，不要强行拆到具体 SKU

### 7.2 数据源接口

```text
GET /api/goods/productSku?product_id=<product_id>&d_type=28
```

### 7.3 对应接口字段

根字段：

```text
data.sku_units_sold
data.sku_gmv
data.best_sku
```

SKU 销量口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 规格维度 | `data.sku_units_sold.<规格名>`，例如 `quantity` |
| 可归因总销量 | `data.sku_units_sold.<规格名>.total_count` |
| SKU 名称 | `data.sku_units_sold.<规格名>.list[].source` |
| SKU 销量占比 | `data.sku_units_sold.<规格名>.list[].propotion` |
| SKU 销量 | `data.sku_units_sold.<规格名>.list[].sold_count` |
| SKU 销量展示值 | `data.sku_units_sold.<规格名>.list[].sold_count_show` |

SKU GMV 口径：

| 图表字段 | 接口字段 |
| --- | --- |
| 可归因总 GMV | `data.sku_gmv.<规格名>.total_count` |
| SKU 名称 | `data.sku_gmv.<规格名>.list[].source` |
| SKU GMV 占比 | `data.sku_gmv.<规格名>.list[].propotion` |
| SKU GMV | `data.sku_gmv.<规格名>.list[].sale_amount` |
| 币种 | `data.sku_gmv.<规格名>.list[].currency` |
| SKU GMV 展示值 | `data.sku_gmv.<规格名>.list[].sale_amount_show` |

主销 SKU：

| 图表字段 | 接口字段 |
| --- | --- |
| 规格维度 | `data.best_sku.sku_name` |
| 主销规格值 | `data.best_sku.sku_value` |
| 主销销量 | `data.best_sku.sold_count` |
| 主销 GMV | `data.best_sku.sale_amount` |
| 主销价格 | `data.best_sku.price` |
| 主销库存 | `data.best_sku.stock` |

### 7.4 渲染逻辑

```js
const propName = "quantity";
const units = productSku.data.sku_units_sold[propName]?.list || [];
const gmv = productSku.data.sku_gmv[propName]?.list || [];

const gmvBySku = new Map(gmv.map((row) => [normalizeSku(row.source), row]));

const rows = units.map((row) => {
  const key = normalizeSku(row.source);
  const gmvRow = gmvBySku.get(key) || {};
  return {
    sku: row.source,
    sales: row.sold_count || 0,
    salesShare: parsePercent(row.propotion),
    gmv: gmvRow.sale_amount ?? null,
    gmvShare: parsePercent(gmvRow.propotion),
    isOther: key === "other",
  };
});
```

### 7.5 口径注意事项

- `sku_units_sold.<规格名>.total_count` 可能小于 `overview.sold_count`，代表 FastMoss 当前能归因到 SKU 规格的销量口径。
- `sku_units_sold` 和 `sku_gmv` 的 Top 项不一定完全一致，需要按 `source` 合并。
- `Other` 是 FastMoss 聚合项，不对应单一 `sku_id`。
- 若需要 SKU ID、价格、完整库存，需要再合并 `/api/goods/v3/productSku` 的 `data.sku_list[]`。

## 8. SKU 库存占比

### 8.1 页面图表

库存占比回答的是“当前库存集中在哪些规格”。它适合和 SKU 销量占比一起看，用来判断缺货风险和备货错配。

推荐图表：

- 横向条形图：更适合展示 Top 库存规格
- 环形图：适合展示库存结构
- 风险标记：销量高但库存低的规格标记为 `缺货风险`

### 8.2 数据源接口

Top 库存分布：

```text
GET /api/goods/productSku?product_id=<product_id>&d_type=28
```

完整 SKU 库存：

```text
GET /api/goods/v3/productSku?product_id=<product_id>&d_type=28
```

### 8.3 对应接口字段

Top 库存分布：

| 图表字段 | 接口字段 |
| --- | --- |
| 规格维度 | `data.sku_stock.<规格名>`，例如 `quantity` |
| 总库存 | `data.sku_stock.<规格名>.total_count` |
| SKU 名称 | `data.sku_stock.<规格名>.list[].source` |
| 库存占比 | `data.sku_stock.<规格名>.list[].propotion` |
| 库存数量 | `data.sku_stock.<规格名>.list[].sold_count` |
| 库存展示值 | `data.sku_stock.<规格名>.list[].sold_count_show` |

完整 SKU 库存：

| 图表字段 | 接口字段 |
| --- | --- |
| SKU ID | `data.sku_list[].sku_id` |
| 商品 ID | `data.sku_list[].product_id` |
| 当前价格 | `data.sku_list[].real_price` 或 `real_price_value` |
| 原价 | `data.sku_list[].original_price` 或 `original_price_value` |
| 库存 | `data.sku_list[].stock` |
| 规格属性 | `data.sku_list[].sku_sale_props[]` |
| 规格名 | `data.sku_list[].sku_sale_props[].prop_name` |
| 规格值 | `data.sku_list[].sku_sale_props[].prop_value` |

注意：`sku_stock.<规格名>.list[].sold_count` 在库存分布里实际表示库存数量，不是销量。

### 8.4 渲染逻辑

```js
const propName = "quantity";
const stockRows = productSku.data.sku_stock[propName]?.list || [];

const rows = stockRows.map((row) => ({
  sku: row.source,
  stock: row.sold_count || 0,
  stockShare: parsePercent(row.propotion),
}));

const option = {
  tooltip: { trigger: "axis" },
  xAxis: { type: "value", name: "库存" },
  yAxis: {
    type: "category",
    data: rows.map((row) => row.sku),
  },
  series: [
    {
      name: "库存",
      type: "bar",
      data: rows.map((row) => row.stock),
    },
  ],
};
```

## 9. 飞书表设计建议

### 9.1 商品统计快照

用于存一段时间窗口的汇总指标。

| 飞书字段 | 来源 |
| --- | --- |
| `窗口销量` | `overview.sold_count` |
| `窗口GMV` | `overview.sale_amount` |
| `日均销量` | `overview.avg_sold_count` |
| `日均GMV` | `overview.avg_sale_amount`，当前表没有时可后续新增 |
| `峰值日` | `chart_list[].inc_sold_count` 最大值对应 `dt` |
| `峰值日销量` | `max(chart_list[].inc_sold_count)` |

### 9.2 每日销量趋势

用于折线图。

| 飞书字段 | 来源 |
| --- | --- |
| `日期` | `chart_list[].dt` |
| `当日销量` | `chart_list[].inc_sold_count` |
| `当日GMV` | `chart_list[].inc_sale_amount` |
| `当日达人数量` | `chart_list[].inc_author_count` |
| `当日视频数` | `chart_list[].inc_aweme_count` |

### 9.3 成交占比明细

建议后续可以把“成交渠道 / 成交内容 / 成交投放”统一成一张表，而不是拆三张结构相同的表。

| 字段 | 说明 |
| --- | --- |
| `占比类型` | `成交渠道`, `成交内容`, `成交投放` |
| `来源key` | FastMoss 原始 `source` 或 `category` |
| `来源名称` | 中文映射后的名称 |
| `销量` | `units_sold.list[].sold_count` |
| `销量占比` | `units_sold.list[].propotion` 转小数 |
| `GMV` | `gmv.list[].sale_amount` |
| `GMV占比` | `gmv.list[].propotion` 转小数 |
| `排名` | 按当前展示指标排序 |
| `关联快照` | 关联 `商品统计快照` |

### 9.4 SKU 销量占比

| 飞书字段 | 来源 |
| --- | --- |
| `SKU占比记录` | 商品 ID + 时间窗口 + SKU 名称 |
| `销量` | `sku_units_sold.<规格名>.list[].sold_count` |
| `GMV` | `sku_gmv.<规格名>.list[].sale_amount` |
| `占比` | `sku_units_sold.<规格名>.list[].propotion` |
| `当前库存` | 优先用 `v3 productSku.sku_list[].stock` 精确匹配；匹配不到时用 `sku_stock` |
| `判断` | 主销 / 长尾 / 缺货风险 / 待观察 |
| `关联规格SKU` | 匹配 `商品规格主档` |

### 9.5 商品规格主档

| 飞书字段 | 来源 |
| --- | --- |
| `规格SKU ID` | `v3 productSku.sku_list[].sku_id` |
| `Size` | `sku_sale_props[].prop_value` |
| `规格属性` | `sku_sale_props[].prop_name=prop_value` |
| `当前价格` | `real_price_value` 或解析 `real_price` |
| `当前库存` | `stock` |
| `当前状态` | `stock <= 0` 可标记为缺货 |

## 10. 实现优先级

建议按以下顺序实现：

1. `/api/goods/v3/overview` -> 销量 / 销售额趋势折线图。
2. `/api/goods/v3/overview` -> 成交渠道 / 内容 / 投放三类环形图。
3. `/api/goods/productSku?d_type=28` -> SKU 销量 / GMV 占比排行。
4. `/api/goods/v3/productSku` -> 完整规格、价格、库存。
5. SKU 销量占比 + 库存占比合并判断缺货风险。

## 11. 已知风险

- `propotion` 可能返回 `NAN%`，需要按 `null` 处理。
- `sku_units_sold` 的总销量可能小于商品概览总销量，不能强行对齐。
- `sku_units_sold` 和 `sku_gmv` Top 项可能不一致，合并时要允许某些 SKU 只有销量或只有 GMV。
- `Other` 是聚合项，不对应单一 SKU。
- `d_type` 不同接口支持程度可能不同；上线前需要用真实商品至少验证 `7`, `28`, `90` 三个窗口。
- 页面 ECharts 使用打包后的模块，不一定暴露 `window.echarts`，但可按 ECharts option 复刻渲染。
