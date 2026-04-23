# Fact DB Schema 设计

日期: 2026-04-23

## 1. 定位

Fact DB 是系统的业务事实面，负责沉淀 TikTok / FastMoss / 飞书流程中产生的主体、关系、指标、原始响应和媒体资产。

它不负责 worker 调度、重试、lease、heartbeat，也不作为任务是否完成的判断来源。任务执行状态只以 Runtime DB 为准。

核心原则:

> Fact DB 用稳定业务键做 upsert，用 raw response 做证据追溯，用 observation/latest 分开承载历史和当前快照。

### 1.1 Schema 变更治理

Fact DB 是跨 workflow 共享的业务事实层，schema 和 upsert key 不能随业务代码自由变化。

生产约束:

- 采集 worker 和业务 handler 使用运行账号写入 Fact DB，不拥有 DDL 权限。
- Fact DB schema 变更必须通过 migration 流程执行，并由 migration 账号完成。
- 应用启动或任务执行前可以校验 schema / migration version；版本不匹配时应拒绝继续执行会写入 Fact DB 的 job。
- 本地开发可以保留初始化或建表便利能力，但生产运行路径必须依赖已发布 migration。

Fact schema 变更必须评估:

| 变更项 | 必须说明 |
| --- | --- |
| 主体表新增字段 | 来源接口、默认值、是否进入 `facts_json` 兼容期 |
| 主体唯一键变更 | 历史数据迁移、重复主体合并、下游关系表影响 |
| 关系表 key 变更 | 重复关系去重、旧 relation_key 迁移 |
| latest / observation 表变更 | 当前读取和历史趋势查询影响 |
| raw response 结构变更 | 证据追溯、payload 存储、归档策略影响 |
| 飞书 binding / projection 变更 | 是否影响业务投影，不应反向改变主体事实主档 |

推荐账号模型:

| 账号 | 使用者 | 权限 |
| --- | --- | --- |
| `mujitask_runtime_user` | API/browser worker、facts handler | Fact 表读写，不含 DDL |
| `mujitask_migration_user` | CI/CD migration 或人工发布 | Fact schema DDL |
| `mujitask_readonly_user` | 数据分析、排障 | Fact 表只读 |

## 2. 数据边界: 事实 / 关系 / 观测 / 业务投影

Fact DB 的职责不是保存某个 workflow 的“过程状态”，而是沉淀跨 workflow 可复用的数据资产。当前四个 workflow 中，TikTok 商品 request/browser 采集、FastMoss 商品/达人/视频采集、媒体资产同步，都应优先沉淀为通用事实，再由业务 mapper 做关系、快照和飞书投影。

需要明确区分四类数据:

| 数据类型 | 示例 | 主要存储 | Fact DB 规则 |
| --- | --- | --- | --- |
| 事实实体 | 商品、SKU、店铺、达人、视频、媒体资产 | Fact DB 主体/媒体表 | 跨 workflow 共享，按稳定业务键 upsert |
| 关系数据 | 商品-店铺、达人-商品、达人-视频、视频-商品、来源飞书记录-商品 | Fact DB 关系表 / 后续业务 binding 表 | 体现上下文和连接，不能只塞进实体 `facts_json` |
| 观测/快照 | 某次采集看到的销量、价格、粉丝数、窗口指标、raw response、页面原文摘要 | Fact observation/latest 表、raw response 表、Runtime artifact | `latest` 给当前读取，`observations/raw` 给历史和追溯 |
| 业务投影 | `TK竞品收集`、`TK达人池`、`TK选品收集` 的写回字段、状态、摘要 | Feishu / Runtime result / 后续同步日志 | 面向运营视图，不是 Fact DB 主档，不作为任务状态真相 |

边界原则:

- 商品、达人、视频、店铺和媒体资产一旦可归一化，就应进入 Fact DB 主体或媒体层。
- 业务流程差异主要体现在关系、观测窗口、来源上下文和飞书字段投影。
- 飞书表是外部业务视图，不能作为内部事实主档的唯一来源。
- Runtime DB 保存任务状态和执行结果，Fact DB 保存可复用业务事实，两者不能互相替代。
- Raw response 和 observation 表达“某次采集看到什么”，主体表表达“当前已知事实是什么”。

### 2.1 通用事实采集 job 到 Fact DB 的映射

| Generic Job | 主要写入 | 说明 |
| --- | --- | --- |
| `tiktok_product_request_fetch` | `tk_products`、raw response、product observations | 默认 TikTok 商品采集路径，输出 normalized product result |
| `tiktok_product_browser_fetch` | raw response / Runtime artifact，后续同样映射到 `tk_products` | fallback 路径，输出必须和 request 结果同 contract |
| `fastmoss_product_search` | raw response、候选商品标准化结果；候选入库后再进入 product facts | 搜索候选，不直接替代商品详情事实 |
| `fastmoss_product_fetch` | `tk_products`、`tk_shops`、商品指标 latest/observations、raw response | 商品和店铺事实、窗口指标 |
| `fastmoss_creator_fetch` | `tk_creators`、达人指标 observations、raw response | 达人主档和达人指标 |
| `fastmoss_shop_fetch` | `tk_shops`、店铺指标 observations、raw response | 店铺主档和店铺指标 |
| `fastmoss_video_fetch` | `tk_videos`、视频相关关系、raw response | 视频主档和视频指标 |
| `media_asset_sync` | `tk_media_assets`、`tk_entity_media_assets` | 图片、封面、头像、视频封面等资产索引 |
| `fact_bundle_upsert` | 主体、关系、latest、observations、raw links | 将 normalized result 统一落库 |
| `feishu_table_write` | 不直接写 Fact 主档；可写 binding/sync log | 飞书写回是业务投影，不是事实实体 upsert |

### 2.2 业务投影与 Fact DB 的关系

飞书写回字段通常来自 Fact DB 的实体、关系和 latest 指标，但飞书表本身不是 Fact DB 的替代品。

建议后续增加同步日志或 binding 表来记录:

| 字段 | 说明 |
| --- | --- |
| `entity_type` | `product / creator / video / shop` |
| `entity_key` | Fact DB 业务唯一键 |
| `feishu_table_code` | `TK竞品收集 / TK达人池 / TK选品收集` 等逻辑表 |
| `feishu_record_id` | 飞书记录 ID |
| `source_record_id` | 来源记录 ID，可选 |
| `workflow_code` | 由哪个 workflow 建立或更新绑定 |
| `last_synced_at` | 最近一次写回时间 |
| `projection_json` | 最近一次投影字段摘要 |

在 binding 表落地前，飞书 record id 可以暂存在 Runtime result、关系表元数据或 workflow-specific result 中，但文档和代码应明确这是“业务投影绑定”，不是实体主档的一部分。

## 3. 事实库总体 ERD

```mermaid
erDiagram
    tk_products ||--o{ tk_product_skus : has
    tk_products ||--o{ tk_product_shop_relations : sold_by
    tk_shops ||--o{ tk_product_shop_relations : sells

    tk_creators ||--o{ tk_creator_product_relations : promotes
    tk_products ||--o{ tk_creator_product_relations : promoted_by

    tk_creators ||--o{ tk_creator_video_relations : publishes
    tk_videos ||--o{ tk_creator_video_relations : authored_by

    tk_videos ||--o{ tk_video_product_relations : mounts
    tk_products ||--o{ tk_video_product_relations : mounted_on

    tk_shops ||--o{ tk_shop_creator_relations : has_creator
    tk_creators ||--o{ tk_shop_creator_relations : contributes

    tk_products ||--o{ tk_product_daily_metrics : has_daily_metric
    tk_products ||--o{ tk_product_window_latest : has_latest_window
    tk_products ||--o{ tk_product_window_observations : has_window_history
    tk_products ||--o{ tk_product_distribution_window_latest : has_latest_distribution
    tk_products ||--o{ tk_product_sku_window_latest : has_latest_sku_window

    tk_media_assets ||--o{ tk_entity_media_assets : linked_to_entities
    tk_raw_api_responses ||--o{ tk_raw_entity_links : supports_entities
```

当前 schema 主要靠业务唯一键、唯一索引和 upsert 维护一致性，未强依赖数据库外键。这种方式更适合采集型系统的增量演进，但要求 upsert key 必须稳定。

## 4. 表分层

### 4.1 主体主档层

| 表 | 唯一业务键 | 作用 |
| --- | --- | --- |
| `tk_products` | `product_id` | 商品主档 |
| `tk_product_skus` | `sku_key` | 商品 SKU 主档 |
| `tk_shops` | `shop_key` | 店铺主档 |
| `tk_creators` | `creator_key` | 达人/创作者主档 |
| `tk_videos` | `video_key` | 视频主档 |

通用字段:

- `id` / 主键 ID。
- 业务唯一键。
- 可展示字段，例如 title、nickname、shop_name、product_url。
- `platform`, `country_region`, `source_platform`, `status`。
- `facts_json` 承接尚未结构化的扩展事实。
- `first_seen_at`, `last_seen_at`, `created_at`, `updated_at`。

### 4.2 媒体层

| 表 | 唯一业务键 | 作用 |
| --- | --- | --- |
| `tk_media_assets` | `asset_key` | 图片、头像、封面、文件 token、对象 key 等媒体资产 |
| `tk_entity_media_assets` | `relation_key` | 媒体资产与商品/达人/视频等主体的绑定 |

媒体内容本身不建议存入数据库。数据库保存 `source_url`、`file_token`、`local_path`、`object_key`、`mime_type` 和元数据。

### 4.3 关系层

| 表 | 唯一业务键 | 关系 |
| --- | --- | --- |
| `tk_product_shop_relations` | `relation_key` | 商品 - 店铺 |
| `tk_creator_product_relations` | `relation_key` | 达人 - 商品 |
| `tk_creator_video_relations` | `relation_key` | 达人 - 视频 |
| `tk_video_product_relations` | `relation_key` | 视频 - 商品 |
| `tk_shop_creator_relations` | `relation_key` | 店铺 - 达人 |

关系表不只表达连接，也可以保存关系维度事实，例如:

- `relation_role`
- `source_record_id`
- `target_record_id`
- `holiday_name`
- `sold_count`
- `source_platform`
- `metadata_json`

### 4.4 原始证据层

| 表 | 作用 |
| --- | --- |
| `tk_raw_api_responses` | 保存一次采集的原始 API 响应 |
| `tk_raw_entity_links` | 将 raw response 与事实主体关联起来 |

`tk_raw_api_responses` 带有 `request_id`、`execution_id`、`run_id`，可以从事实追溯到运行时上下文，但它不反向参与任务调度。

### 4.5 指标层

| 表 | 类型 | 作用 |
| --- | --- | --- |
| `tk_product_daily_metrics` | 日粒度 upsert | 商品每日销量、销售额、价格等 |
| `tk_product_window_latest` | 窗口最新快照 | 商品窗口指标当前值 |
| `tk_product_window_observations` | 窗口历史观测 | 商品窗口指标历史采样 |
| `tk_product_distribution_window_latest` | 分布窗口最新快照 | 商品分布类指标当前值 |
| `tk_product_distribution_window_observations` | 分布窗口历史观测 | 商品分布类指标历史采样 |
| `tk_product_sku_window_latest` | SKU 窗口最新快照 | SKU 窗口表现当前值 |
| `tk_product_sku_window_observations` | SKU 窗口历史观测 | SKU 窗口表现历史采样 |
| `tk_video_product_window_performance` | 事件/观测记录 | 视频-商品窗口表现 |
| `tk_creator_product_window_performance` | 事件/观测记录 | 达人-商品窗口表现 |

指标层的核心区分:

- `latest` 表用于当前业务读取和飞书写回。
- `observations` 表用于历史追踪、排障、趋势分析。
- daily metric 以自然日期作为唯一维度。

## 5. Upsert 与幂等规则

### 5.1 主体 upsert

| 方法 | 表 | 唯一键 | 幂等规则 |
| --- | --- | --- | --- |
| `upsert_product` | `tk_products` | `product_id` | 同一商品重复采集只更新事实和 `last_seen_at` |
| `upsert_product_sku` | `tk_product_skus` | `sku_key` | `sku_key = product_id + sku_id/sku_name/spec`，重复采集更新 SKU 事实 |
| `upsert_shop` | `tk_shops` | `shop_key` | 由 shop id/name/url 等稳定字段构建 |
| `upsert_creator` | `tk_creators` | `creator_key` | 由 `creator_id`、`uid`、`unique_id` 构建，优先稳定 ID |
| `upsert_video` | `tk_videos` | `video_key` | 通常为 `video:{video_id}` |

主体表 upsert 应遵守:

- 首次发现写入 `first_seen_at`。
- 每次更新写入 `last_seen_at` 和 `updated_at`。
- 不稳定或待扩展字段进入 `facts_json`。
- 结构化字段优先放列，便于查询和索引。

### 5.2 媒体 upsert

| 方法 | 表 | 唯一键 | 幂等规则 |
| --- | --- | --- | --- |
| `upsert_media_asset` | `tk_media_assets` | `asset_key` | 由 `source_url`、`file_token`、`local_path` 或 `object_key` 形成稳定资产键 |
| `link_media_asset` | `tk_entity_media_assets` | `relation_key` | `entity_type + entity_external_id + media_role + asset_id` |

媒体幂等重点:

- 同一个图片/文件重复上传或重复发现，不应产生多条资产主档。
- 同一主体同一角色同一资产，不应重复绑定。
- 对象内容在 MinIO/local object store，Fact DB 只保存定位和元数据。

### 5.3 关系 upsert

| 方法 | 表 | 唯一键 | 幂等规则 |
| --- | --- | --- | --- |
| `upsert_product_shop_relation` | `tk_product_shop_relations` | `product_id + shop_key + relation_role` | 同一商品-店铺-角色只保留一条关系 |
| `upsert_creator_product_relation` | `tk_creator_product_relations` | `creator_key + product_id` | 同一达人-商品关系重复写入时更新 sold_count、飞书记录 ID 等 |
| `upsert_creator_video_relation` | `tk_creator_video_relations` | `creator_key + video_key` | 同一达人-视频关系唯一 |
| `upsert_video_product_relation` | `tk_video_product_relations` | `video_key + product_id` | 同一视频-商品关系唯一 |
| `upsert_shop_creator_relation` | `tk_shop_creator_relations` | `shop_key + creator_key` | 同一店铺-达人关系唯一 |

关系 upsert 的意义:

- 支持同一个主体从多个 workflow 反复补全。
- 避免 repeated job 或 lease 回收导致重复关系。
- 将关系事实沉淀下来，而不是只藏在 `facts_json`。

### 5.4 Raw response 与 raw link

| 方法 | 表 | 幂等策略 |
| --- | --- | --- |
| `record_raw_api_response` | `tk_raw_api_responses` | 每次采集插入一条新 raw response，用于证据追溯 |
| `link_raw_entity` | `tk_raw_entity_links` | 将 raw response 绑定到主体，当前更偏审计记录 |

raw response 通常不做覆盖式 upsert，因为它表达的是“某次采集看到的原始证据”。如果后续存储压力变大，可以增加:

- payload digest 去重。
- 原始响应 TTL。
- 冷数据归档到对象存储。

### 5.5 指标 upsert 与 observation

| 方法 | 表 | 唯一键/写入方式 | 规则 |
| --- | --- | --- | --- |
| `upsert_product_daily_metric` | `tk_product_daily_metrics` | `(product_id, metric_date, source_platform)` | 同一天同来源保留最新值 |
| `upsert_product_window_latest` | `tk_product_window_latest` | `(product_id, source_platform, source_endpoint, window_days)` | 同窗口保留最新快照 |
| `record_product_window_observation` | `tk_product_window_observations` | insert | 每次观测保留历史 |
| `upsert_product_distribution_window_latest` | `tk_product_distribution_window_latest` | `(product_id, distribution_type, source_key, source_platform, window_days)` | 分布维度窗口最新值 |
| `record_product_distribution_window_observation` | `tk_product_distribution_window_observations` | insert | 分布维度历史观测 |
| `upsert_product_sku_window_latest` | `tk_product_sku_window_latest` | `(product_id, sku_key, source_platform, window_days)` | SKU 窗口最新值 |
| `record_product_sku_window_observation` | `tk_product_sku_window_observations` | insert | SKU 窗口历史观测 |

选择 `latest` 还是 `observation` 的规则:

- 飞书写回、当前推荐、当前分析结果读取 `latest`。
- 趋势分析、排障、回放读取 `observations`。
- daily metric 是自然日维度的事实，使用 upsert 避免同日重复行。

## 6. Workflow 写入路径

### 6.1 选品分析 Workflow

```mermaid
flowchart TD
    A["可选 Feishu TK选品收集读取"] --> B["tiktok_product_request_fetch"]
    B --> C{"request 是否有效?"}
    C -->|否| D["tiktok_product_browser_fetch"]
    C -->|是| E["normalized product result"]
    D --> E
    A --> F["fastmoss_product_fetch"]
    E --> G["fact_bundle_upsert"]
    F --> G
    G --> H["upsert products / skus / shops / relations"]
    G --> I["latest metrics + observations"]
    G --> J["raw responses / raw links"]
    G --> K["media asset upsert/link"]
    H --> L["可选 Feishu TK选品收集业务投影写回"]
    I --> L
```

幂等重点:

- 商品以 `product_id` 为主键。
- SKU、店铺、关系均使用稳定业务键。
- TikTok request 和 browser fallback 必须输出同一种 normalized product result，Fact DB 写入不区分来源。
- FastMoss、TikTok、媒体资产和 raw response 可以重复采集，主体和关系使用 upsert，raw/observation 追加。
- 写回飞书时应基于源 `record_id` 更新，不重复创建。
- 飞书写回是业务投影，不是 Fact DB 主档；后续建议通过 binding/sync log 记录投影关系。

### 6.2 达人同步 Workflow

```mermaid
flowchart TD
    A["竞品/商品记录"] --> B["product job 发现达人"]
    B --> C["creator detail job 采集达人详情"]
    C --> D["upsert tk_creators"]
    C --> E["upsert tk_creator_product_relations"]
    C --> F["media asset upsert/link"]
    C --> G["record creator observations/raw responses"]
    D --> H["写入或更新飞书达人表业务投影"]
    E --> H
    H --> I["保存 target_record_id / snapshot_id"]
```

幂等重点:

- Runtime 层用 `(request_id, source_record_id, product_id, influencer_id)` 去重 creator detail job。
- Fact 层用 `creator_key` 去重达人主档。
- 关系层用 `creator_key + product_id` 去重达人-商品关系。
- 飞书写回用 `target_record_id` 或业务唯一键避免重复创建达人记录。
- 飞书达人表字段属于业务投影；达人事实和达人-商品关系必须先沉淀在 Fact DB。

### 6.3 竞品表 Workflow

```mermaid
flowchart TD
    A["竞品表读取或 fastmoss_product_search"] --> B["tiktok_product_request_fetch + fastmoss_product_fetch"]
    B --> C{"TikTok request 需要 fallback?"}
    C -->|是| D["tiktok_product_browser_fetch"]
    C -->|否| E["fact_bundle_upsert"]
    D --> E
    E --> F["upsert product/shop/metric facts"]
    E --> G["record raw responses"]
    F --> H["upsert relations / observations"]
    F --> I["飞书竞品表业务投影写回"]
    H --> I
```

幂等重点:

- 竞品写回以飞书源记录或产品链接/商品 ID 为定位。
- 关键词候选入库需要先查已有记录，避免重复创建。
- 事实库 upsert 可以承受 request/API job 和 browser fallback job 的重复执行。
- 竞品表状态、运营字段和备注属于业务投影；商品事实、店铺事实、指标和关系属于 Fact DB。

## 7. 幂等与一致性边界

### 7.1 Runtime DB 和 Fact DB 的边界

Runtime DB 负责 exactly-once 的调度近似，Fact DB 负责 at-least-once 执行下的重复写容忍。

实际生产中更现实的模型是:

```text
worker 可能重复执行 job
handler 可能重复写事实库
handler 可能写完事实库后还没来得及 mark success 就崩溃
watchdog 可能重新调度该 job
```

因此 Fact DB 必须允许重复写:

- 主体 upsert。
- 关系 upsert。
- latest 指标 upsert。
- observation/raw 追加记录。

### 7.2 外部副作用

飞书和对象存储属于外部副作用，需要单独幂等。

| 外部系统 | 幂等策略 |
| --- | --- |
| 飞书表更新 | 优先 update 已知 `record_id`；创建前用业务唯一键查重；写回后把 `target_record_id` 保存回 Runtime/Fact |
| MinIO/local object store | 使用稳定 `object_key`；可重复覆盖或检查已存在 |
| FastMoss/TikTok API | 原始响应可追加，标准化事实走 upsert |

### 7.3 事务边界

推荐事务边界:

- 单个 job 的 Runtime 状态更新应短事务完成。
- Fact DB upsert 可以在 handler 内部按一个业务实体或一批相关实体提交。
- 外部飞书写回无法和 Postgres 组成同一个事务，因此必须靠业务键和补偿逻辑保证幂等。
- job 成功标记应尽量发生在所有副作用完成后。

## 8. 当前 schema 的优点和风险

优点:

- 主体/关系/指标分层清楚。
- Upsert key 明确，适合重复采集。
- `facts_json` 和 `metadata_json` 给 schema 演进留了空间。
- raw response 可以支持排障和回放。

风险:

- 当前未强制数据库外键，脏关系需要靠写入逻辑控制。
- `creator_key`、`shop_key` 等构造规则必须稳定，一旦变更需要迁移。
- raw response 持续增长后需要归档策略。
- 飞书记录 ID 与事实主体的绑定需要更明确的同步日志或 binding 表。

## 9. 演进建议

第一阶段:

- 保持现有 TK fact schema。
- 明确所有 upsert key 的生成规则，写入文档和测试。
- 对 creator/shop/video/product relation 增加幂等测试。

第二阶段:

- 增加飞书 binding 表或同步日志表，用于记录 `entity_type + entity_key + feishu_table + record_id`。
- 对 raw response 增加 digest 字段，支持去重和归档。
- 对常用查询增加组合索引。

第三阶段:

- 如果分析查询变重，再拆出 BI Mart 或宽表。
- 如果全文搜索/相似检索成为核心需求，再引入搜索索引库。
- 如果事实回放变重要，将 raw response 大 payload 迁移到对象存储，Fact DB 保存 digest 和 object_key。
