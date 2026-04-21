# FastMoss 已知接口

更新时间：`2026-04-17`

## 1. 说明

本文只记录本轮需求分析中已经实际验证过的 FastMoss 接口，不写入 `README`，单独沉淀在这里，方便后续按“尽量模拟真实浏览器访问”的方式复用。

本文里的“已知接口”分成两类：

- `页面实抓`：通过 `roxy-tiktok` 打开真实 FastMoss 页面后，监听 XHR / fetch 请求抓到的真实页面调用。
- `同域补充验证`：直接调用同域接口验证通过，但没有单独做页面抓包；这类接口会单独标注。

本次验证使用的样例：

- 商品详情页：`https://www.fastmoss.com/zh/e-commerce/detail/1729679758111249333`
- 商品详情页补充样例：`https://www.fastmoss.com/zh/e-commerce/detail/1729440407432826887`
- 达人搜索页：`https://www.fastmoss.com/zh/influencer/search?shop_window=1&page=1&words=anonymousbillionaires&words_search_type=1`
- 达人详情页：`https://www.fastmoss.com/zh/influencer/detail/7228697870020199470`
- 达人搜索关键词：`anonymousbillionaires`
- 达人 `uid`：`7228697870020199470`

## 2. 通用请求约定

### 2.1 通用返回壳层

本轮已验证的大多数 FastMoss JSON 接口都遵循同一层壳结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {},
  "ext": {}
}
```

需要注意：

- `code` 不一定是 `200`。
- 即使 `code` 是 `MAG_AUTH_3002`、`MAG_AUTH_3017`，`data` 里依然经常会返回一部分可用字段。
- 因此不要只按 `code == 200` 才解析；应同时判断 `data` 是否有值。

本轮观察到的典型返回码：

- `200`：正常返回。
- `MAG_AUTH_3002`：当前账号/详情查看次数不足，但仍可能返回部分核心数据。
- `MAG_AUTH_3017`：权限不足，但仍可能返回部分列表或概览数据。
- `MAG_AUTH_3016`：商品页某些范围切换场景下出现过，表现为预览/权限受限。

### 2.2 真实页面公共参数

真实浏览器发出的 FastMoss 请求，几乎都会额外带上：

- `_time=<unix_ts_seconds>`
- `cnonce=<8位随机数字符串>`

例如：

```text
.../api/goods/v3/base?product_id=1729679758111249333&_time=1776149705&cnonce=79776009
```

结论：

- 这两个参数不是最小可用参数，直接请求时通常可以省略。
- 但如果目标是“尽量模拟真实浏览器访问”，建议保留。

### 2.3 常见参数含义

| 参数 | 含义 | 例子 |
| --- | --- | --- |
| `product_id` | 商品 ID | `1729679758111249333` |
| `uid` | FastMoss 达人详情页 ID | `7228697870020199470` |
| `words` | 搜索词，一般放达人 ID 或商品关键词 | `anonymousbillionaires` |
| `words_search_type` | 搜索类型，达人精确匹配时用 `1` | `1` |
| `page` | 页码 | `1` |
| `pagesize` | 每页条数 | `5`、`10` |
| `region` | 站点国家 | `US` |
| `order` | 排序规则，格式通常是 `字段,方向` | `sold_count,2` |
| `d_type` | 商品详情页时间范围 | `7`、`14`、`28`、`90` |
| `date_type` | 达人相关列表时间范围 | `28` |
| `start_date` / `end_date` | 商品概览自定义日期范围 | `2026-04-13` |
| `shop_window` | 达人搜索页筛选参数 | `1` |
| `ecommerce_type` | 商品关联达人列表筛选 | `all` |
| `live_type` | 商品关联直播列表筛选 | `all` |
| `is_promoted` | 商品视频列表广告筛选 | `-1` |
| `field_type` | 达人趋势字段类型 | `follower`、`sold_count` |
| `from` | 类目来源维度 | `video` |
| `type` | 分类或收藏状态类型 | `product_count`、`sold_count`、`2`、`4` |

## 3. 商品详情页接口

### 3.1 商品基础信息 `/api/goods/v3/base`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/base?product_id=1729679758111249333&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`

- 返回结构：

```json
{
  "code": "200 | MAG_AUTH_3002",
  "msg": "success",
  "data": {
    "product": {},
    "shop": {}
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.product`: `title`, `cover_list`, `real_price`, `original_price`, `commission_rate`, `sold_count`, `sale_amount`, `review_count`, `product_rating`, `author_count`, `aweme_count`, `live_count`, `detail_url`, `category_name`, `region`
  - `data.shop`: `seller_id`, `name`, `avatar`, `sale_amount`, `sold_count`, `rank`, `region`

### 3.2 商品概览 `/api/goods/v3/overview`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。

- 页面默认打开时的真实请求：

```text
https://www.fastmoss.com/api/goods/v3/overview?product_id=1729679758111249333&d_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 页面切换区间时已实抓到的参数形态：
  - `d_type=7`
  - `d_type=28`
  - `d_type=90`
  - `start_date=2026-04-13&end_date=2026-04-13`

- 已验证但不一定有页面按钮的参数：
  - `d_type=14`

- 最小可用调用参数：
  - `product_id`
  - `d_type` 或 `start_date + end_date`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "overview": {},
    "total": {},
    "chart_list": [],
    "ads_distribution": {},
    "channel_distribution": {},
    "content_distribution": {},
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.overview`: `sold_count`, `sale_amount`, `real_sold_count`, `real_sale_amount`, `author_count`, `aweme_count`, `live_count`, `video_sale_amount`, `live_sale_amount`
  - `data.chart_list[]`: `dt`, `sold_count`, `sale_amount`, `author_count`, `aweme_count`, `live_count`, `inc_sold_count`, `inc_sale_amount`
  - `data.ads_distribution`: 成交投放占比，包含广告流量 / 非广告流量的销量与 GMV 占比。
  - `data.channel_distribution`: 成交渠道占比，包含商品卡 / 店铺自播或店铺账号 / 达人联盟等来源的销量与 GMV 占比。
  - `data.content_distribution`: 成交内容占比，包含短视频 / 直播 / 商品卡等内容类型的销量与 GMV 占比。

- 三类成交占比字段结构：
  - `*.units_sold.total_count`: 当前时间窗口内该分布口径的总销量。
  - `*.units_sold.list[]`: 销量分布明细，常见字段为 `category` 或 `source`, `propotion`, `sold_count`, `sold_count_show`。
  - `*.gmv.total_count`: 当前时间窗口内该分布口径的总 GMV。
  - `*.gmv.list[]`: GMV 分布明细，常见字段为 `category` 或 `source`, `propotion`, `sale_amount`, `currency`, `sale_amount_show`。
  - 注意字段名是 FastMoss 原始拼写 `propotion`，不是 `proportion`。

- `d_type=28` 在补充样例 `1729440407432826887` 上实测到的三类成交占比：

| 口径 | 原始字段 | 明细 key | 销量占比示例 | GMV 占比示例 |
| --- | --- | --- | --- | --- |
| 成交投放占比 | `ads_distribution` | `category` | `common.goods.adTraffic` 54% / `common.goods.otherTraffic` 46% | `common.goods.adTraffic` 55% / `common.goods.otherTraffic` 45% |
| 成交渠道占比 | `channel_distribution` | `source` | `common.goods.affiliate` 87% / `common.goods.product_card` 12% / `common.goods.shop_account` 1% | `common.goods.affiliate` 89% / `common.goods.product_card` 10% / `common.goods.shop_account` 1% |
| 成交内容占比 | `content_distribution` | `category` | `video.name` 85% / `common.goods.product_card` 12% / `live.name` 3% | `video.name` 86% / `common.goods.product_card` 10% / `live.name` 4% |

- 建议业务映射：
  - `video.name` -> `短视频`
  - `live.name` -> `直播`
  - `common.goods.product_card` -> `商品卡` 或当前飞书表里的 `自然流量`
  - `common.goods.affiliate` -> `达人橱窗` / `达人联盟`
  - `common.goods.shop_account` -> `店铺账号`
  - `common.goods.adTraffic` -> `广告`
  - `common.goods.otherTraffic` -> `非广告流量`

- 备注：
  - 商品详情页的 `7天 / 28天 / 90天 / 昨天销量` 都可以从这个接口拿。
  - `昨天销量` 用 `start_date=end_date=昨天日期` 实现，更接近真实页面行为。

### 3.3 商品 SKU 列表 `/api/goods/v3/productSku`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/productSku?product_id=1729679758111249333&d_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `d_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "sku_detail": {},
    "sku_list": [],
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.sku_list[]`: `sku_id`, `real_price`, `original_price`, `discount`, `stock`, `sku_sale_props`
  - `data.sku_detail[]`: 规格维度定义，例如 `prop_name=quantity`, `sale_prop_values[]`。

- 备注：
  - 这个 `v3` 接口适合拿完整 SKU 清单、规格属性、价格、库存。
  - 真实验证中，`v3` 接口没有直接返回 SKU 级销量 / GMV 占比；SKU 销量占比需要看旧版 `/api/goods/productSku`。

### 3.4 商品 SKU 销量 / GMV / 库存分布 `/api/goods/productSku`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/productSku?product_id=1729440407432826887&d_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`

- 建议调用参数：
  - `product_id`
  - `d_type=28`

- 返回结构：

```json
{
  "code": "200",
  "msg": "success",
  "data": {
    "sku_list": [],
    "sku_detail": [],
    "sku_stock": {},
    "sku_units_sold": {},
    "sku_gmv": {},
    "best_sku": {}
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.sku_list[]`: 与 `/api/goods/v3/productSku` 类似，包含 `sku_id`, `real_price`, `original_price`, `stock`, `sku_sale_props`。
  - `data.sku_units_sold.<规格名>.total_count`: 当前时间窗口内 SKU 分布可归因销量合计。
  - `data.sku_units_sold.<规格名>.list[]`: SKU 销量占比明细，字段包含 `source`, `propotion`, `sold_count`, `sold_count_show`。
  - `data.sku_gmv.<规格名>.total_count`: 当前时间窗口内 SKU 分布可归因 GMV 合计。
  - `data.sku_gmv.<规格名>.list[]`: SKU GMV 占比明细，字段包含 `source`, `propotion`, `sale_amount`, `currency`, `sale_amount_show`。
  - `data.sku_stock.<规格名>.list[]`: SKU 库存占比明细，字段包含 `source`, `propotion`, `sold_count`, `sold_count_show`；这里的 `sold_count` 实际表示库存数量。
  - `data.best_sku`: 当前窗口主销规格，字段包含 `sku_name`, `sku_value`, `sold_count`, `sale_amount`, `currency`, `price`, `stock`。

- `d_type=28` 在补充样例 `1729440407432826887` 上实测：
  - `best_sku.sku_value=60pcs`
  - `best_sku.sold_count=416`
  - `best_sku.sale_amount=13312`
  - `sku_units_sold.quantity.total_count=2627`
  - `sku_units_sold.quantity.list[]` Top 项包含 `60pcs`, `200pcs`, `144Pcs`, `48pcs`, `Other`
  - `sku_gmv.quantity.list[]` Top 项包含 `144Pcs`, `200pcs`, `60pcs`, `160pcs`, `Other`

- 备注：
  - 页面也会请求不带 `d_type` 的 `/api/goods/productSku?product_id=...`，但补充样例里不带 `d_type` 时销量与 GMV 分布为 0；做近 28 天 SKU 占比时应显式传 `d_type=28`。
  - `sku_units_sold.total_count` 可能小于 `/api/goods/v3/overview` 的 `overview.sold_count`，代表 FastMoss 当前 SKU 分布接口可归因到规格的销量口径，不要强行等同于商品总销量。
  - `Other` 是 FastMoss 的聚合项，不对应单一 `sku_id`；写入飞书规格表时建议单独建一个聚合规格或只写备注。

### 3.5 商品关联达人列表 `/api/goods/v3/author`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/author?product_id=1729679758111249333&order=2,2&pagesize=5&ecommerce_type=all&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `order`
  - `pagesize`
  - `ecommerce_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "page": 1,
    "total": 0,
    "live_author_count": 0,
    "video_author_count": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `uid`, `unique_id`, `nickname`, `avatar`, `follower_count`, `sold_count`, `sale_amount`, `product_id`, `videos`, `region`

- 备注：
  - 这个接口已经确认能拿到商品关联的达人 ID 列表。
  - 当前未登录或低权限状态下更像“预览列表”，翻页是否稳定全量返回要继续看登录态。

### 3.6 商品关联达人分布 `/api/goods/v3/authorChart`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/authorChart?product_id=1729679758111249333&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "level_distribution": [],
    "type_distribution": [],
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.level_distribution[]`: 达人粉丝量级分布，字段包含 `follower_level`, `follower_count`, `follower_count_show`。
  - `data.type_distribution[]`: 达人类型分布，字段包含 `type`, `percent`, `percent_show`。

### 3.7 商品带货视频列表 `/api/goods/v3/video`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/video?page=1&product_id=1729679758111249333&order=1,2&d_type=0&pagesize=5&is_promoted=-1&date_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `product_id`
  - `order`
  - `pagesize`
  - `is_promoted`
  - `date_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "page": 1,
    "total": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `video_id`, `video`, `author`, `uid`, `product_id`, `play_count`, `digg_count`, `comment_count`, `share_count`, `sold_count`, `sale_amount`, `engagement_rate`, `create_date`, `is_ad`

### 3.8 商品带货直播列表 `/api/goods/v3/live`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/v3/live?product_id=1729679758111249333&page=1&d_type=28&order=2,2&pagesize=5&live_type=all&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `page`
  - `d_type`
  - `order`
  - `pagesize`
  - `live_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "page": 1,
    "total": 0,
    "affiliate_count": 0,
    "shop_count": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `room_id`, `uid`, `author`, `title`, `cover_oss`, `create_time`, `finish_time`, `product_count`, `sold_count`, `sale_amount`, `user_count`, `max_user_count`, `total_user`

### 3.9 商品投放概览 `/api/goods/V3/investment`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/V3/investment?product_id=1729679758111249333&d_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `d_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "overview": {},
    "trends": [],
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.overview`: `sold_count`, `sale_amount`, `total_sale_amount`, `ad_sale_percent`, `estimate_cost_amount`, `avg_estimate_cost_amount`, `play_count`, `avg_play_count`, `video_count`, `roas`, `currency`。
  - `data.trends[]`: `dt`, `sold_count`, `sale_amount`, `estimate_cost_amount`, `play_count`, `video_count`, `roas`, `currency`。

- 备注：
  - 这个接口描述的是广告投放相关表现，不等同于 `/api/goods/v3/overview` 里的 `ads_distribution`。
  - 如果只需要“成交投放占比”，优先使用 `/api/goods/v3/overview` 的 `ads_distribution`；如果需要投放成本、ROAS、广告视频趋势，再用本接口。

### 3.10 商品广告视频列表 `/api/goods/V3/adsVideo`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/V3/adsVideo?product_id=1729679758111249333&d_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `d_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "region": "US",
    "category_id": 0,
    "second_category_id": 0,
    "leaf_category_id": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `id`, `video_id`, `uid`, `advertiser`, `cover`, `desc`, `estimate_cost_amount`, `sale_amount`, `play_count`, `roas`, `tiktok_url`

### 3.11 商品评论列表 `/api/goods/reviewList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/reviewList?product_id=1729679758111249333&page=1&pagesize=5&near_day=0&is_like=0&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `product_id`
  - `page`
  - `pagesize`
  - `near_day`
  - `is_like`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "rate": {},
    "review_list": [],
    "total": 0,
    "update_at": 0
  },
  "ext": {}
}
```

### 3.12 商品详情页辅助接口清单

以下接口在商品详情页打开时也会出现，当前先记录用途，不作为选品复盘主数据源：

| 接口 | 方法 | 已观察用途 |
| --- | --- | --- |
| `/api/info/handle` | `GET` | 页面配置 / 信息流辅助数据 |
| `/api/collect/collectStatus` | `GET` | 商品收藏状态，参数包含 `id`, `type` |
| `/api/ai/productReviewExample/getConsumerPortrait` | `GET` | 商品评论 / 消费者画像相关 AI 辅助内容 |
| `/api/info/pagerInfo` | `GET` | 页面辅助信息 |
| `/api/user/index/userInfo` | `GET` | 当前登录态检查 |
| `/api/author/index/country` | `GET` | 国家 / 地区选项 |
| `/api/user/user` | `GET` | 当前用户信息 |
| `/api/ai/omni/getUserCards` | `GET` | AI 卡片 / 账户权益辅助信息 |
| `/api/user/userPayTrial` | `GET` | 试用 / 付费权益状态 |
| `/api/export/getExportTimes` | `GET` | 导出次数 / 导出权限检查 |
| `/api/notify/index` | `POST` | 站内通知 |

## 4. 达人搜索与达人详情接口

### 4.1 达人搜索 `/api/author/search`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/search?page=1&pagesize=10&df=ZnNfaHR0cHM6Ly93d3cuZmFzdG1vc3MuY29tX3Bz&region=US&order=12,2&shop_window=1&words=anonymousbillionaires&words_search_type=1&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `pagesize`
  - `region`
  - `order`
  - `shop_window`
  - `words`
  - `words_search_type`

- 页面实抓里额外观察到的参数：
  - `df=ZnNfaHR0cHM6Ly93d3cuZmFzdG1vc3MuY29tX3Bz`

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "author_list": [],
    "result_cnt": 0,
    "result_cnt_show": "0",
    "total": 0,
    "total_cnt": 0,
    "total_cnt_show": "0",
    "ext": {}
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.author_list[]`: `uid`, `unique_id`, `nickname`, `avatar`, `follower_count`, `aweme_28d_count`, `video_sale_amount`, `live_sale_amount`, `contact`, `fansPortrait`, `region`, `category`, `first_video_time`

- 备注：
  - 达人 ID 精确匹配时，推荐固定用 `words_search_type=1`。
  - 这一步最适合做 `达人ID -> uid` 映射。

### 4.2 达人基础信息 `/api/author/v3/detail/baseInfo`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/baseInfo?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "uid": 0,
    "unique_id": "",
    "nickname": "",
    "avatar": "",
    "signature": "",
    "region": "US",
    "region_name": "",
    "category_name": "",
    "first_video_time": 0,
    "verify_type": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `avatar`
  - `unique_id`
  - `nickname`
  - `signature`
  - `region`
  - `category_name`

### 4.3 达人概览指标 `/api/author/v3/detail/authorIndex`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/authorIndex?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "follower_count": 0,
    "follower_count_show": "0",
    "aweme_28_count": 0,
    "aweme_28_count_show": "0",
    "live_28_count": 0,
    "last_video_time": 0,
    "region": "US",
    "region_name": "",
    "carry_index": 0,
    "flow_index": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `follower_count`
  - `aweme_28_count`
  - `live_28_count`
  - `last_video_time`

- 备注：
  - `达人头像`、`粉丝数`、`28天视频数` 这类字段，主要从 `baseInfo + authorIndex` 拿。

### 4.4 达人成交统计 `/api/author/v3/detail/getStatInfo`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/getStatInfo?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "video_sale_amount": 0,
    "video_sale_amount_show": "0",
    "live_sale_amount": 0,
    "live_sale_amount_show": "0",
    "goods_sale_amount": 0,
    "goods_sale_amount_show": "0",
    "aweme_count": 0,
    "live_count": 0,
    "update_at": 0
  },
  "ext": {}
}
```

### 4.5 达人联系方式 `/api/author/v3/detail/authorContact`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/authorContact?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "update_at": 0
  },
  "ext": {}
}
```

- 备注：
  - 当前权限下没有稳定拿到联系方式正文。
  - 因此 `达人联系方式` 暂时不能作为稳定自动补全字段。

### 4.6 达人粉丝画像 `/api/author/v3/detail/fansPortrait`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/fansPortrait?uid=7228697870020199470&date_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `date_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "follower_ages": [],
    "follower_ages_max": 0,
    "follower_genders": [],
    "follower_genders_max": 0,
    "state_distribution": [],
    "state_distribution_max": 0,
    "update_at": 0
  },
  "ext": {}
}
```

### 4.7 达人活跃时间 `/api/author/v3/detail/authorActiveRange`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/authorActiveRange?uid=7228697870020199470&date_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `date_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "active_periods": [],
    "high_interaction_periods": [],
    "hourly_stats": [],
    "comprehensive_active": {},
    "date_range": {},
    "region": "US",
    "region_name": "",
    "update_at": 0
  },
  "ext": {}
}
```

### 4.8 达人趋势序列 `/api/author/v3/detail/dataList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。

- 已实抓到的真实页面请求：

```text
https://www.fastmoss.com/api/author/v3/detail/dataList?uid=7228697870020199470&field_type=follower&date_type=28&_time=<unix_ts>&cnonce=<nonce>
https://www.fastmoss.com/api/author/v3/detail/dataList?uid=7228697870020199470&field_type=sold_count&date_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `field_type`
  - `date_type`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "list": [
      {
        "key": "",
        "value": 0
      }
    ],
    "update_at": 0
  },
  "ext": {}
}
```

### 4.9 达人带货汇总 `/api/author/v3/detail/cargoSummary`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/cargoSummary?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "goods_count": 0,
    "shop_count": 0,
    "total_sale_amount": 0,
    "total_sold_count": 0,
    "video_sale_amount": 0,
    "video_sale_amount_show": "0",
    "live_sale_amount": 0,
    "live_sale_amount_show": "0",
    "video_sold_count": 0,
    "live_sold_count": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 备注：
  - `带货视频 GMV`、`带货直播 GMV`、`合作商品数` 这类字段最适合从这里拿。

### 4.10 达人商品筛选 `/api/author/v3/detail/goodsFilter`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/goodsFilter?uid=7228697870020199470&date_type=28&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `date_type`

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "category": []
  },
  "ext": {}
}
```

### 4.11 达人关联商品列表 `/api/author/v3/detail/goodsList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/goodsList?page=1&uid=7228697870020199470&date_type=28&order=sold_count,2&pagesize=5&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `uid`
  - `date_type`
  - `order`
  - `pagesize`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "page": 1,
    "total": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `product_id`, `title`, `cover`, `sold_count`, `sale_amount`, `shop_title`, `seller_id`, `real_price`, `commission_rate`, `product_rating`, `region`

- 备注：
  - `带货商品图` 可以直接拿 `cover`。
  - `关联商品销量` 可以从这里拿 `sold_count`，但先要定义“取哪一个关联商品”。

### 4.12 达人合作店铺列表 `/api/author/v3/detail/shopList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面默认 `TOP 5 合作店铺` 请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/shopList?page=1&uid=7292741711510946859&region=US&order=sold_count,2&pagesize=5&_time=<unix_ts>&cnonce=<nonce>
```

- 这次通过 `Roxy` 在真实达人页 `https://www.fastmoss.com/zh/influencer/detail/7292741711510946859` 抓到的排序切换请求：

```text
# 默认 Top 5 合作店铺：按带货总销量倒序
https://www.fastmoss.com/api/author/v3/detail/shopList?page=1&uid=7292741711510946859&region=US&order=sold_count,2&pagesize=5&_time=<unix_ts>&cnonce=<nonce>

# 点击“合作商品数”列头后：按合作商品数倒序
https://www.fastmoss.com/api/author/v3/detail/shopList?page=1&uid=7292741711510946859&region=US&order=product_count,2&pagesize=5&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `uid`
  - `region`
  - `order`
  - `pagesize`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `id`, `name`, `img`, `product_count`, `product_cnt`, `sold_count`, `sale_amount`, `author_cnt`, `aweme_cnt`, `shop_rating`, `region`

- 备注：
  - 当前页面的 `TOP 5 合作店铺` 默认就是这条接口，固定用 `order=sold_count,2` 和 `pagesize=5`。
  - 真实请求头里会带：`referer=https://www.fastmoss.com/zh/influencer/detail/{uid}`、`lang=ZH_CN`、`source=pc`、`region=US`、`fm-sign=<动态签名>`。
  - `合作店铺` 可以从 `name` 拿。
  - `合作商品数` 如果要按店铺维度，也可以从 `product_count / product_cnt` 拿。

### 4.13 达人标签列表 `/api/author/v3/detail/labelList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/labelList?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "label_list": [],
    "label_max": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 备注：
  - 这里能拿到话题 / 标签，但不建议直接把它当成稳定的 `关联节日` 字段。

### 4.14 达人类目分布 `/api/author/v3/detail/categoryList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。

- 已实抓到的真实页面请求：

```text
https://www.fastmoss.com/api/author/v3/detail/categoryList?uid=7228697870020199470&type=product_count&from=video&_time=<unix_ts>&cnonce=<nonce>
https://www.fastmoss.com/api/author/v3/detail/categoryList?uid=7228697870020199470&type=sold_count&from=video&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `type`
  - `from`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "list": [
      {
        "category_id": 0,
        "key": "",
        "show": "",
        "value": 0
      }
    ],
    "max": 0,
    "update_at": 0
  },
  "ext": {}
}
```

### 4.15 达人视频列表 `/api/author/v3/detail/videoList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/videoList?region=US&order=sold_count,2&uid=7228697870020199470&date_type=28&pagesize=5&page=1&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `region`
  - `order`
  - `uid`
  - `date_type`
  - `pagesize`
  - `page`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "count": 0,
    "list": [],
    "page": 1,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `video_id`, `video_desc`, `cover`, `create_time`, `play_count`, `digg_count`, `comment_count`, `share_count`, `interaction_rate`, `product_count`, `sold_count`, `sale_amount`, `product_info`

### 4.16 达人直播列表 `/api/author/v3/detail/liveList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/liveList?page=1&region=US&order=create_time,2&date_type=28&uid=7228697870020199470&pagesize=5&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `region`
  - `order`
  - `date_type`
  - `uid`
  - `pagesize`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "list": [],
    "page": 1,
    "total": 0,
    "update_at": 0
  },
  "ext": {}
}
```

- 已观察到的关键字段：
  - `data.list[]`: `room_id`, `title`, `cover`, `create_time`, `live_time`, `product_count`, `sold_product_count`, `sold_count`, `sale_amount`, `total_user_count`, `uv_price`, `product_info`

### 4.17 相似达人列表 `/api/author/v3/detail/similarityList`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/similarityList?uid=7228697870020199470&page=1&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`
  - `page`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3002 | 200",
  "msg": "...",
  "data": {
    "update_at": 0
  },
  "ext": {}
}
```

### 4.18 达人粉丝分析概览 `/api/author/v3/detail/authorFansAnalysis`

- 来源：`页面实抓`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 真实页面请求示例：

```text
https://www.fastmoss.com/api/author/v3/detail/authorFansAnalysis?uid=7228697870020199470&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `uid`

- 返回结构：

```json
{
  "code": "MAG_AUTH_3017 | 200",
  "msg": "...",
  "data": {
    "update_at": 0
  },
  "ext": {}
}
```

## 5. 页面伴随请求

下面这些请求在真实页面打开时也会一起出现。它们不一定是本次业务写飞书时的核心数据源，但如果目标是“尽量贴近浏览器行为”，建议知道它们的存在。

### 5.1 商品详情页伴随请求

已实抓到：

- `GET /api/info/handle?type=200032`
- `GET /api/collect/collectStatus?id=<product_id>&type=2`
- `GET /api/info/pagerInfo?type=500000`
- `GET /api/user/index/userInfo`
- `GET /api/author/index/country?pagesize=100`
- `GET /api/user/user`
- `GET /api/user/userPayTrial`
- `GET /api/export/getExportTimes?check_type=2`
- `GET /api/ai/omni/getUserCards`
- `GET /api/ai/productReviewExample/getConsumerPortrait`
- `POST https://tracking.fastmoss.com/api/notify/index`

其中跟踪上报请求的 body 结构为：

```json
{
  "time": 1776149705,
  "system": "mac",
  "platform": 1,
  "track_uid": 11776057,
  "id": 206,
  "type": 1,
  "ext": {
    "uri": "https://www.fastmoss.com/zh/e-commerce/detail/1729679758111249333",
    "_src": null,
    "referrer": ""
  }
}
```

### 5.2 达人搜索页伴随请求

已实抓到：

- `GET /api/author/filterInfoNew`
- `GET /api/info/handle?type=200032`
- `GET /api/info/pagerInfo?type=500000`
- `GET /api/user/index/userInfo`
- `GET /api/author/index/country?pagesize=100`
- `GET /api/user/user`
- `GET /api/user/userPayTrial`
- `GET /api/export/getExportTimes?check_type=2`
- `GET /api/search/history?type=1`
- `GET /api/ai/omni/getUserCards`
- `POST https://tracking.fastmoss.com/api/notify/index`

### 5.3 达人详情页伴随请求

已实抓到：

- `GET /api/info/handle?type=200032`
- `GET /api/info/handle?type=200037`
- `GET /api/collect/collectStatus?id=<uid>&type=4`
- `GET /api/info/pagerInfo?type=500000`
- `GET /api/user/index/userInfo`
- `GET /api/author/index/country?pagesize=100`
- `GET /api/user/user`
- `GET /api/user/userPayTrial`
- `GET /api/ai/omni/getUserCards`
- `POST https://tracking.fastmoss.com/api/notify/index`

### 5.4 商品搜索页伴随请求

已实抓到：

- `GET /api/info/handle?type=200032`
- `GET /api/goods/filterInfo?region=US`
- `GET /api/info/pagerInfo?type=500000`
- `GET /api/user/index/userInfo`
- `GET /api/author/index/country?pagesize=100`
- `GET /api/user/user`
- `GET /api/user/userPayTrial`
- `GET /api/search/history?type=3`
- `GET /api/goods/V2/search?page=1&pagesize=10&order=2,2&region=US`
- `POST https://tracking.fastmoss.com/api/notify/index`

搜索关键词后，真实页面请求会变成：

```text
https://www.fastmoss.com/api/goods/V2/search?page=1&pagesize=10&order=2,2&region=US&words=Halloween+decoration&_time=<unix_ts>&cnonce=<nonce>
```

## 6. 当前最建议复用的链路

如果目标是做“达人池补全”，当前最稳的 HTTP 链路是：

1. `达人ID -> /api/author/search`
2. 从返回中拿 `uid`
3. `uid -> /api/author/v3/detail/baseInfo`
4. `uid -> /api/author/v3/detail/authorIndex`
5. `uid -> /api/author/v3/detail/cargoSummary`
6. 按需要补充 `goodsList / shopList / labelList`

如果目标是做“商品详情补全”，当前最稳的 HTTP 链路是：

1. `product_id -> /api/goods/v3/base`
2. `product_id -> /api/goods/v3/overview`
3. `product_id -> /api/goods/v3/author`
4. 按需要补充 `video / live / productSku / reviewList`

如果目标是做“商品关键词搜索”，当前最稳的链路是：

1. 在已登录 FastMoss 会话中调用 `/api/goods/V2/search`
2. 固定请求参数：`page`、`pagesize`、`order=2,2`、`region=US`、`words=<关键词>`
3. 从 `product_list[]` 提取 `product_id`
4. 本地按 `product_id` 去重后翻页
5. 对候选商品再进入 `goods/v3/base` 或商品详情页补全后续数据

## 7. 当前已确认的业务限制

- `达人联系方式`：当前权限下没有稳定正文，不能当成稳定自动维护字段。
- `关联节日`：没有看到稳定的直接字段，只能从 `labelList` 低置信推断，不建议直接自动写入。
- `商品关联达人全量翻页`：在低权限或未登录状态下更像预览结果，是否能稳定拿全量要继续结合登录态验证。
- `14天商品销量`：接口支持 `d_type=14`，但页面默认可见按钮并未观察到 `近14天`。
- `商品关键词搜索翻页`：只有带登录态时 `page` 才是真分页；匿名请求下 `page=1/2/3` 会重复返回同一批 5 条预览数据。

## 8. 商品关键词搜索接口

### 8.1 商品搜索 `/api/goods/V2/search`

- 来源：`页面实抓` + `同域补充验证`
- 方法：`GET`
- 登录依赖：必须依赖有效的 `fd_tk` Cookie；未携带 `fd_tk` 时会退回游客态或受限预览。
- 搜索页入口：

```text
https://www.fastmoss.com/zh/e-commerce/search?region=US
```

- 真实页面请求示例：

```text
https://www.fastmoss.com/api/goods/V2/search?page=1&pagesize=10&order=2,2&region=US&words=Halloween+decoration&_time=<unix_ts>&cnonce=<nonce>
```

- 已实抓到的不带关键词首屏请求：

```text
https://www.fastmoss.com/api/goods/V2/search?page=1&pagesize=10&order=2,2&region=US&_time=<unix_ts>&cnonce=<nonce>
```

- 最小可用调用参数：
  - `page`
  - `pagesize`
  - `order`
  - `region`
  - `words`

- 当前实测固定参数：
  - `pagesize=10`
  - `order=2,2`
  - `region=US`

- 返回结构：

```json
{
  "code": "200 | MAG_AUTH_3001",
  "msg": "success! | Sorry, insufficient search times.",
  "data": {
    "product_list": [],
    "result_cnt": 0,
    "result_cnt_show": "0",
    "total": 0,
    "total_cnt": 0,
    "total_cnt_show": "0",
    "ext": {
      "is_ok": true,
      "took": 0,
      "total_cnt_show": "0"
    },
    "update_at": 0
  },
  "ext": {
    "is_login": 0
  }
}
```

- 已观察到的关键字段：
  - `data.product_list[]`: `product_id`, `title`, `shop_name`, `img`, `price`, `ori_price`, `crate`, `crate_show`, `sold_count`, `sold_count_show`, `sale_amount`, `sale_amount_show`, `yday_sold_count`, `day7_sold_count`, `day14_sold_count`, `day28_sold_count`, `relate_author_count`, `relate_video_count`, `relate_live_count`, `product_rating`, `detail_url`, `shop_info`, `trend`

- `trend[]` 已观察到的关键字段：
  - `dt`
  - `inc_sold_count`
  - `inc_sale_amount`
  - `region`
  - `region_name`

- 这次关键词 `Halloween decoration` 的真实会话样例：
  - 页面 URL：`https://www.fastmoss.com/zh/e-commerce/search?region=US&page=1&words=Halloween%20decoration`
  - 返回：`code=200`
  - `total=5000`
  - `page=1` 首批 5 个 `product_id`：
    - `1731194997356205027`
    - `1730737877704544295`
    - `1731417288404275846`
    - `1731608488516227718`
    - `1731468796968603932`

- 备注：
  - `title` 里会带高亮 HTML，例如 `<span style='color:red'>Halloween</span>`，入库前要去标签。
  - 按当前结果看，`order=2,2` 很像按近 7 天销量倒序，但这条结论是基于结果推断，不是页面文案直接声明。

### 8.2 商品搜索翻页行为

本轮已做分页对照验证：

- 真实登录会话下：
  - `page=1`：`code=200`，`count=10`
  - `page=2`：`code=200`，`count=10`
  - `page=3`：`code=200`，`count=10`
  - 三页首条 `product_id` 不同，说明 `page` 是真分页

- 匿名直调下：
  - `page=1/2/3` 都返回 `code=MAG_AUTH_3001`
  - 每页都只返回 `5` 条
  - 三页内容相同，说明匿名态下分页被降级成固定预览结果

当前建议的翻页策略：

1. 必须复用同一个已登录 FastMoss 会话
2. 固定 `words / order / region / pagesize`
3. 逐页递增 `page`
4. 每页提取 `product_id`
5. 本地按 `product_id` 去重
6. 遇到以下任一条件停止：
   - `product_list` 为空
   - 当前页没有新增 `product_id`
   - `page * pagesize >= total`
   - 达到业务设定的最大页数上限

## 9. 登录身份如何带入请求

### 9.1 真实页面请求头特征

对 `/api/goods/V2/search` 的真实页面请求，已观察到以下 header：

- `lang: ZH_CN`
- `source: pc`
- `region: US`
- `referer: https://www.fastmoss.com/zh/e-commerce/search?...`
- `user-agent: Chrome 145 on macOS`
- `accept: application/json, text/plain, */*`
- `fm-sign: <动态值>`

结论：

- 这些 header 有助于贴近浏览器行为。
- 但从“身份是谁”这个问题看，它们不是核心凭证。

### 9.2 当前已确认的身份凭证

当前本文涉及的 FastMoss 业务接口，如需稳定拿到正式数据，登录身份主载体是：

- `fd_tk` Cookie

本轮已确认：

- `fd_tk` 是 `HttpOnly` Cookie
- `document.cookie` 读不到它
- 浏览器同域请求会自动带上它

### 9.3 本地存储的作用

页面 `localStorage` 里有一个 `auth-store`，其中可以看到：

- `userInfo`
- `ext.is_login`
- `code`
- `msg`

结论：

- `auth-store` 更像前端展示用的登录态缓存
- 它不是请求真正依赖的身份凭证
- 不能只复制 `localStorage` 而不带 Cookie

### 9.4 已验证的身份对照实验

本轮已经做过 4 组对照：

1. 浏览器内 `fetch(..., { credentials: 'include' })`
   返回 `code=200`

2. 浏览器内 `fetch(..., { credentials: 'omit' })`
   返回 `code=MAG_AUTH_3001`

3. `requests` 只带 `fd_tk` Cookie
   返回 `code=200`

4. `requests` 不带 Cookie，或只带 `region` 这类普通 Cookie
   返回 `code=MAG_AUTH_3001`

结论：

- 在当前已验证的 FastMoss 业务接口上，`fd_tk` 已经足以把“当前用户身份”带入请求
- `region`、`referer`、`lang`、`source`、`fm-sign` 这些信息可以增强浏览器相似度，但不是当前身份生效的必要条件

### 9.5 当前实现建议

如果后续要把搜索能力搬到服务端，建议按下面方式实现：

1. 从真实 FastMoss 浏览器会话中获取 `fd_tk`
2. 放入服务端的 `requests.Session()` Cookie Jar
3. 后续搜索与翻页都复用同一个 Session
4. 同时补齐 `User-Agent`、`Referer`、`lang`、`source`、`region`
5. 不要把 `fd_tk` 写进版本库或文档正文

### 9.6 登录主接口与后续身份确认接口

`2026-04-14` 已在 `roxy-tiktok` 中从退出状态重跑 FastMoss 登录流程，并同步做了纯 HTTP 重放验证。

真实登录流程里，和“密码登录 + 身份确认”直接相关的接口如下：

- `POST /api/user/login`
- `GET /api/user/index/userInfo`
- `GET /api/user/user`
- `GET /api/user/userPayTrial`

登录弹窗默认还会触发微信登录相关接口：

- `GET /api/wechat/index/getQrcode`
- `POST /api/wechat/index/getWxLoginToken?scene_id=`

但这两条不是手机号密码登录的必要接口。

### 9.7 密码登录接口 `/api/user/login`

- 来源：`页面实抓 + 纯 HTTP 直接验证`
- 方法：`POST`
- 作用：提交手机号密码，触发服务端创建登录会话并下发 `fd_tk`
- 登录依赖：这是“建立登录态”的入口，本身不依赖预先存在的 `fd_tk`

- 真实页面请求示例：

```text
https://www.fastmoss.com/api/user/login?_time=<unix_ts>&cnonce=<nonce>
```

- 真实页面请求头示例：

```json
{
  "content-type": "application/json",
  "referer": "https://www.fastmoss.com/zh/account/center",
  "lang": "ZH_CN",
  "source": "pc",
  "region": "Global",
  "fm-sign": "<动态值>"
}
```

- 真实页面请求体结构：

```json
{
  "phone": "<手机号>",
  "password": "<密码>",
  "account": "<手机号>",
  "area_code": "86",
  "action": 0,
  "source": "1",
  "type": "1"
}
```

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "token": "<token>",
    "first_register": 0,
    "user_info": {
      "uid": 0,
      "username": "",
      "nickname": "",
      "country": 86,
      "platform": 1,
      "region": "CN",
      "visitor_id": "",
      "login_source": "pc",
      "region_name": "中国"
    },
    "do_free_trial": 0,
    "send_info": [],
    "send_type": 0,
    "behavior": 1
  },
  "ext": {
    "is_login": 0
  }
}
```

- 已观察到的关键字段：
  - `data.token`
  - `data.user_info.uid`
  - `data.user_info.visitor_id`
  - `data.user_info.login_source`
  - `data.behavior`

- 重要结论：
  - 这条接口成功后，服务端会在响应头里通过 `Set-Cookie` 下发 `fd_tk`
  - `fd_tk` 不是客户端根据 `token`、`uid` 或 `visitor_id` 本地计算出来的
  - `data.token` 不能替代 `fd_tk` 直接承担后续业务接口的登录态

### 9.8 登录后身份确认接口

#### 9.8.1 `GET /api/user/index/userInfo`

- 作用：返回当前账号详情，是登录后最直接的身份确认接口
- 登录前实测：
  - `code=MSG_30001`
  - `msg=请登录`
  - `ext.is_login=0`
- 登录后实测：
  - `code=200`
  - `msg=success`
  - `ext.is_login=1`

- 返回结构：

```json
{
  "code": "MSG_30001 | 200",
  "msg": "请登录 | success",
  "data": {
    "uid": 0,
    "phone": "180****6348",
    "nickname": "FM11776057",
    "level": 2,
    "expire_at": 0,
    "avatar": "https://...",
    "is_set_password": 1,
    "bypass_type": 1
  },
  "ext": {
    "is_login": 0
  }
}
```

#### 9.8.2 `GET /api/user/user`

- 作用：返回当前账号是否登录、封禁状态、绑定信息、优惠券状态

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "uid": 0,
    "is_login": true,
    "status": 1,
    "blocked_msg": "",
    "third_bind": {
      "is_bind": false,
      "type": 0,
      "bind_email": ""
    },
    "coupon": {
      "status": 0,
      "is_pop": 0,
      "expire_at": 0
    }
  },
  "ext": {
    "is_login": 1
  }
}
```

#### 9.8.3 `GET /api/user/userPayTrial`

- 作用：返回试用和销售归因相关信息

- 返回结构：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "uid": 0,
    "is_first": 0,
    "free_trial": 0,
    "pay_trial": 0,
    "sales_code": "",
    "ref_code": "",
    "partner_code": ""
  },
  "ext": {
    "is_login": 1
  }
}
```

### 9.9 `fm-sign` 来源与算法

`fm-sign` 已在 FastMoss 前端 bundle 中定位到，来源文件：

- `https://www.fastmoss.com/_next/static/chunks/78101-764ba00a6fd57d46.js`

当前观察到的生成逻辑是：

1. 取 query 参数，并补上 `_time` 与 `cnonce`
2. 按 key 名排序
3. 对每个 key 拼接 `key + value + 固定 salt`
4. 再拼接请求体 JSON 字符串
5. 做一次 `MD5`
6. 对 MD5 十六进制字符串做一轮首尾字符异或拼接，得到最终 `fm-sign`

前端 bundle 中已经确认的固定 salt 为：

```text
LAA6edGHBkcc3eTiOIRfg89bu9ODA6PB
```

可还原的伪代码如下：

```python
def build_fm_sign(params: dict, body_text: str = "") -> str:
    salt = "LAA6edGHBkcc3eTiOIRfg89bu9ODA6PB"
    source = ""
    for key in sorted(params):
        source += f"{key}{params[key]}{salt}"
    md5_hex = md5((source + body_text).encode("utf-8")).hexdigest()
    left = 0
    right = len(md5_hex) - 1
    prefix = ""
    while left < right:
        prefix += format(int(md5_hex[left], 16) ^ int(md5_hex[right], 16), "x")
        left += 1
        right -= 1
    return prefix + md5_hex[left:]
```

但本轮验证也确认：

- `fm-sign` 不是当前密码登录成功的必要条件
- 它更像浏览器侧的请求签名/风控字段
- 如果后续平台收紧校验，建议保留这套算法作为可插拔实现

### 9.10 纯 HTTP 直接登录验证结论

`2026-04-14` 已对 `/api/user/login` 做了 5 组直接 HTTP 验证：

1. 不带 `fm-sign`
2. 带错误的 `fm-sign`
3. 带正确计算出的 `fm-sign`
4. 不带 `_time` / `cnonce`
5. 只带最小请求头：`User-Agent + Content-Type`

以上 5 组当前都能成功登录，表现一致：

- `HTTP 200`
- JSON `code=200`
- 响应头里下发 `Set-Cookie`
- `requests.Session()` 成功收到 `fd_tk`
- 随后调用 `/api/user/index/userInfo` 返回 `ext.is_login=1`

因此当前可落地结论是：

- 纯接口可以模拟密码登录过程
- 生成登录态的关键不是前端本地“算出 cookie”，而是成功调用登录接口并接收服务端下发的 `fd_tk`
- `_time`、`cnonce`、`fm-sign`、`referer`、`lang`、`source`、`region` 目前都不是这条登录接口成功的硬性前置条件
- 但如果目标是尽量模拟真实浏览器访问，建议保留 `_time`、`cnonce` 与浏览器风格请求头

### 9.11 后续代码如何获取并复用 `fd_tk`

后续代码如果要“基于登录过程生成 cookie”，正确做法应理解为：

1. 调用 `POST /api/user/login`
2. 从响应头的 `Set-Cookie` 自动接收 `fd_tk`
3. 把 `fd_tk` 保存在同一个 HTTP Session / Cookie Jar 中
4. 后续所有 FastMoss 业务接口都复用该 Session
5. 当接口重新退回 `MAG_AUTH_3001`、`MAG_AUTH_3002`、`MAG_AUTH_3017` 或 `userInfo` 显示未登录时，重新执行登录流程刷新 Cookie

不建议的做法：

- 不要尝试用 `data.token`、`uid`、`visitor_id` 本地推导 `fd_tk`
- 不要把抓到的 `fd_tk` 常量写死在代码或版本库中
- 不要只复制 `localStorage.auth-store` 代替 Cookie

最小 Python 思路示例：

```python
import requests

session = requests.Session()

resp = session.post(
    "https://www.fastmoss.com/api/user/login",
    json={
        "phone": "<手机号>",
        "password": "<密码>",
        "account": "<手机号>",
        "area_code": "86",
        "action": 0,
        "source": "1",
        "type": "1",
    },
    headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    },
    timeout=30,
)
resp.raise_for_status()

assert "fd_tk" in session.cookies

user_info = session.get(
    "https://www.fastmoss.com/api/user/index/userInfo",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30,
).json()
```

### 9.12 商品页滑块风控实抓结论

`2026-04-15` 已在 `roxy-tiktok` 的真实 FastMoss 页面中，针对商品详情页反复触发并实抓一次完整的滑块风控流程。

本轮实抓商品示例：

- `https://www.fastmoss.com/zh/e-commerce/detail/1729421576573456391`

#### 9.12.1 触发顺序

实抓到的顺序不是“先弹滑块、再接口报错”，而是：

1. 商品详情核心接口先返回风控业务码 `MSG_SAFE_0001`
2. 页面随后开始加载腾讯验证码资源
3. 页面弹出滑块验证层
4. 用户完成滑块后，FastMoss 再调用自己的验证码确认接口
5. 商品核心接口恢复 `code=200`

#### 9.12.2 风控触发时的核心接口表现

本轮在登录态正常的前提下，商品页刚进入时抓到：

- `GET /api/goods/v3/base` -> `code=MSG_SAFE_0001`
- `GET /api/goods/v3/author` -> `code=MSG_SAFE_0001`
- `GET /api/goods/v3/overview` -> `code=MSG_SAFE_0001`
- `GET /api/goods/v3/authorChart` -> `code=MAG_AUTH_3017`

同时返回体里还出现了统一的风控标识：

```json
{
  "code": "MSG_SAFE_0001",
  "data": {
    "id": 132611
  }
}
```

这里的 `data.id=132611` 本轮多次复现，表现上更像一次风险校验会话 ID。

#### 9.12.3 滑块相关真实网络特征

当 `MSG_SAFE_0001` 出现后，页面会继续发起这组验证码链路：

- `GET https://turing.captcha.qcloud.com/TJCaptcha.js`
- `GET https://turing.captcha.gtimg.com/...`
- `POST /api/captcha/config`
- `GET cap_union_prehandle`
- `GET cap_union_new_getcapbysig`
- `GET tdc.js`

其中最关键的是：

- 商品接口出现 `MSG_SAFE_0001`
- 紧接着页面发出 `POST /api/captcha/config`

本轮可以把这组组合明确视为：

- `MSG_SAFE_0001 + /api/captcha/config` => 进入滑块风控

#### 9.12.4 页面 DOM 特征

滑块出现时，页面可稳定命中：

- `[class*="slider"]`
- `[id*="captcha"]`
- `[role="dialog"]`
- `[aria-modal="true"]`

可见文案示例：

- `Slide to complete the puzzle`
- `Generated by AI Verification`

#### 9.12.5 用户完成滑块后的请求

本轮人工完成滑块后，页面抓到的关键确认请求是：

- `POST https://turing.captcha.qcloud.com/cap_union_new_verify`
- `POST https://www.fastmoss.com/api/captcha/verify`

随后商品核心接口恢复：

- `GET /api/goods/v3/base` -> `code=200`
- `GET /api/goods/v3/author` -> `code=200`
- `GET /api/goods/v3/overview` -> `code=200`

但：

- `GET /api/goods/v3/authorChart` 仍然可能保持 `MAG_AUTH_3017`

#### 9.12.6 一个容易误判的点

本轮还确认了一个很关键的前端行为：

- 滑块验证通过后，商品核心接口可能已经恢复 `code=200`
- 但滑块 DOM 不一定会立刻消失

因此更可靠的“风控已解除”判断标准应是：

- `POST /api/captcha/verify` 已发出
- 且 `/api/goods/v3/base`、`/api/goods/v3/author` 等核心接口恢复 `code=200`

而不是只看：

- 滑块弹层是否已经从 DOM 中完全移除

#### 9.12.7 当前代码侧的判定建议

对当前纯 HTTP 方案，可以先用两层规则判断：

1. 纯 HTTP 直接判定：
   - 如果商品详情核心接口返回 `MSG_SAFE_0001`，应先归类为 `fastmoss_risk_control`
   - 如果命中的是 `/api/goods/v3/*`，可进一步标记为 `likely_slider_captcha`

2. 浏览器侧确认判定：
   - 如果同时看到 `MSG_SAFE_0001 + /api/captcha/config`
   - 可以明确判定为“已进入滑块风控”

## 10. 纯 HTTP 匿名态实测结果

以下结果来自 `2026-04-14` 的纯 HTTP 直接请求验证，测试前已经主动退出 FastMoss 账号，且请求未携带 `fd_tk`。

测试使用的公共请求头：

```json
{
  "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
  "Accept": "application/json, text/plain, */*",
  "lang": "ZH_CN",
  "source": "pc",
  "region": "US"
}
```

### 10.1 商品相关接口

| 接口 | HTTP 状态 | 业务码 | 匿名态表现 |
| --- | ---: | --- | --- |
| `/api/goods/V2/search` | `200` | `MAG_AUTH_3001` | 只返回 `5` 条固定预览数据，搜索翻页失效。 |
| `/api/goods/v3/base` | `200` | `MAG_AUTH_3002` | 仍返回 `data.product` 和 `data.shop` 的部分基础字段。 |
| `/api/goods/v3/overview` | `200` | `MAG_AUTH_3017` | 仍返回 `data.overview` 的部分概览字段，但属于受限结果。 |
| `/api/goods/v3/author` | `200` | `MAG_AUTH_3017` | 仍返回 `5` 条受限预览。 |
| `/api/goods/v3/video` | `200` | `MAG_AUTH_3017` | 仍返回 `5` 条受限预览。 |
| `/api/goods/v3/live` | `200` | `MAG_AUTH_3017` | 仍返回 `5` 条受限预览。 |

匿名态下，`/api/goods/V2/search` 的前 `5` 个预览 `product_id` 为：

- `1729398461940339414`
- `1729385034780414637`
- `1729385239712731370`
- `1729478839153234936`
- `1729477193790165820`

### 10.2 达人相关接口

| 接口 | HTTP 状态 | 业务码 | 匿名态表现 |
| --- | ---: | --- | --- |
| `/api/author/search` | `200` | `200` | 仍可正常按达人 ID 查到 `uid`。 |
| `/api/author/v3/detail/baseInfo` | `200` | `200` | 仍可拿到基础信息。 |
| `/api/author/v3/detail/authorIndex` | `200` | `MAG_AUTH_3002` | 仍返回粉丝数、28天视频数等部分字段。 |
| `/api/author/v3/detail/cargoSummary` | `200` | `MAG_AUTH_3002` | 仍返回商品数、视频 GMV、直播 GMV 等部分字段。 |
| `/api/author/v3/detail/authorContact` | `200` | `MAG_AUTH_3017` | 基本只剩 `update_at`，无法拿到联系方式正文。 |

### 10.3 匿名态结论

纯 HTTP 且未携带 `fd_tk` 时，FastMoss 当前表现为两类：

- 搜索与列表类接口：最容易退化成游客预览，典型表现是 `MAG_AUTH_3001` 或 `MAG_AUTH_3017`，并且结果条数明显受限。
- 基础详情类接口：不一定彻底失败，常见表现是 `MAG_AUTH_3002` / `MAG_AUTH_3017`，但 `data` 里仍保留部分可解析字段。

因此：

- 不能只看 `HTTP 200` 判断接口可用。
- 不能只看 `data` 非空就认为拿到了正式数据。
- 需要同时检查 `code`、返回条数、翻页行为，以及是否退化成固定预览结果。
