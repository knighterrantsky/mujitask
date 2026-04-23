# Handler Contract 设计

日期: 2026-04-23

状态: 当前架构设计文档

## 1. 定位

本文定义 Runtime job 到代码 handler 的契约边界。它回答:

- worker claim 到一条 job 后，应该如何找到 handler。
- handler 的 payload / result / error / retry / idempotency 应如何表达。
- 通用能力 handler、表级 adapter、业务 handler、mapper/policy/renderer 如何分工。
- 飞书表读取/写入这种“每张表逻辑不同”的能力，应该如何避免过度抽象。

相关文档:

- [当前整体系统架构设计](./current-system-architecture-design.md)
- [四个 Workflow 重设计评审](./workflow-redesign-review.md)
- [选品分析 Workflow 设计](./workflow-selection-analysis-design.md)
- [Runtime DB Schema 设计](./runtime-db-schema-design.md)
- [Fact DB Schema 设计](./fact-db-schema-design.md)

## 2. 核心结论

需要抽象 handler 契约，但不能把所有业务逻辑都抽成一种“通用 handler”。

正式架构口径:

```text
Workflow / Stage
  编排业务阶段和依赖关系

Job
  Runtime DB 中的可 claim 执行单元

Handler
  执行某类 job

  Capability Handler
    通用外部能力，不理解业务表含义

  Business Handler
    有独立运行价值的业务动作

Table Adapter / Projection Mapper / Policy / Renderer
  业务定制组件，可被 handler 调用
```

关键原则:

> Capability handler 负责稳定外部能力，Business handler 负责可恢复的业务动作，Mapper/Policy/Renderer 负责轻量业务语义转换。

## 3. 分层定义

| 层级 | 例子 | 是否通用 | 是否通常进 Runtime job |
| --- | --- | --- | --- |
| Capability Handler | `feishu_table_read`、`feishu_table_write`、`tiktok_product_request_fetch`、`tiktok_product_browser_fetch`、`fastmoss_product_search`、`fastmoss_product_fetch`、`fastmoss_creator_fetch`、`fastmoss_shop_fetch`、`fastmoss_video_fetch`、`media_asset_sync`、`fact_bundle_upsert` | 是 | 是 |
| Table Adapter | `selection_table_source_adapter`、`competitor_table_source_adapter`、`influencer_pool_write_adapter` | 否，表级定制 | 视复杂度决定 |
| Business Handler Candidate | `selection_analysis_summary`、`fastmoss_echarts_render`、`business_snapshot_write` | 否，业务定制 | 进入第 9 节准入清单后才可以 |
| Projection Mapper | `selection_table_projection_mapper`、`competitor_table_projection_mapper`、`influencer_pool_projection_mapper` | 否，业务定制 | 通常不是，作为纯函数被调用 |
| Policy / Validator | `influencer_match_policy`、`selection_candidate_validator` | 否，业务定制 | 通常不是 |
| Renderer | `fastmoss_echarts_renderer`、`summary_html_renderer` | 否，业务定制 | 生成 artifact 且需重试时是 |

判断一个动作是否应成为独立 Runtime job:

| 条件 | 建议 |
| --- | --- |
| 需要独立 retry / timeout | 做成 job |
| 会写外部系统或生成 artifact | 做成 job |
| 结果被后续 stage 等待 | 做成 job |
| 执行时间长或失败率高 | 做成 job |
| 只是纯字段映射 / 校验 / 打分 | 不一定做 job，可作为 mapper/policy |

## 4. Handler 标准契约

每个 handler 必须定义以下内容:

| 字段 | 说明 |
| --- | --- |
| `handler_code` | job 路由键，对应 `job_code` / `item_code` |
| `worker_type` | `api_worker` / `browser_worker` / `outbox_dispatcher` |
| `runtime_table` | `api_worker_job` / `task_execution` |
| `purpose` | handler 的能力边界 |
| `payload_schema` | 最小输入、可选输入、禁止输入 |
| `result_schema` | 成功、跳过、部分成功时的标准输出 |
| `error_schema` | 错误类型、错误码、是否可重试、是否可 fallback |
| `retry_policy` | 哪些错误重试、最大次数、退避策略 |
| `timeout_policy` | 单次执行最大时间和 hard timeout |
| `idempotency_policy` | 重复执行如何避免重复副作用 |
| `side_effects` | 写哪些外部系统或数据库 |
| `progress_policy` | 何时更新 progress / heartbeat / stage cursor |
| `reconciler_contract` | 父 workflow 如何消费 result 推进下一阶段 |

标准 result 外壳:

```json
{
  "status": "success",
  "handler_code": "example_handler",
  "request_id": "request-id",
  "job_id": "job-id",
  "summary": {},
  "result": {},
  "warnings": [],
  "next_action": {
    "type": "none"
  }
}
```

标准 error 外壳:

```json
{
  "status": "failed",
  "handler_code": "example_handler",
  "error": {
    "error_type": "upstream_error",
    "error_code": "rate_limited",
    "message": "request was rate limited",
    "retryable": true,
    "fallback_allowed": false,
    "fallback_reason": ""
  }
}
```

通用 `status`:

| status | 说明 |
| --- | --- |
| `success` | handler 完成，result 可被后续 stage 消费 |
| `skipped` | 输入合法但业务判断无需执行 |
| `partial_success` | 有可用结果，但部分可选能力失败 |
| `failed` | 当前 job 失败，按 retry policy 判断是否重试 |
| `fallback_required` | 当前 job 不应继续重试，建议派发 fallback job |

### 4.1 Contract 变更治理

Handler contract 是 worker、executor、reconciler、watchdog 和业务 mapper 之间的运行时 API，不能随实现细节自由破坏。

命名约束:

- `handler_code`、`job_code`、`item_code` 使用稳定语义名称，不追加 `v1`、`v2`、`legacy`、`new` 这类版本后缀。
- `handler_code` 只能表达 worker 可执行的通用能力或明确的执行动作，不能使用 workflow 编排函数名。
- 禁止把 `orchestrate_*`、`run_*_workflow`、`run_sync_*`、`*_orchestrator` 这类 workflow 编排入口写成 handler、job handler、registry key 或目标 handler 文件名。
- 例如 `orchestrate_sync_tk_influencer_pool` 只能作为历史兼容入口或当前实现事实被记录，不能出现在目标 handler contract 或 handler registry 中。
- payload/result 字段也不通过字段名追加版本号表达演进，例如不使用 `candidates_v1`、`filters_v2`。
- 兼容演进通过新增可选字段、默认值、adapter、migration 或 `contract_revision` 元数据完成。
- 如果必须做破坏性变更，应先清理或迁移旧 Runtime job，再发布新 contract；不要让同一个字段在不同运行窗口表达不同语义。

允许直接兼容变更:

- 新增可选 payload 字段，并提供默认行为。
- 新增 result 字段，旧消费者忽略后不影响流程。
- 新增更细的 warning / metadata 字段。
- 新增语义不同的 handler_code，不改变既有 handler 行为。

需要显式评审或迁移策略的变更:

- 删除 payload/result 字段。
- 将可选字段改为必填字段。
- 修改字段类型或字段语义。
- 修改 `status`、`next_action`、`error.retryable`、`fallback_allowed` 的含义。
- 修改 retry、timeout、idempotency、side_effects 规则。
- 修改同一个 `handler_code` 的 worker 类型或 runtime 表。

推荐演进策略:

| 场景 | 策略 |
| --- | --- |
| 兼容新增字段 | 保持原 handler_code，新增可选字段和默认行为 |
| 破坏性 payload/result 变更 | 新增 `contract_revision` 元数据、迁移 adapter 或语义不同的新 handler_code |
| 老 job 尚未消费完 | 保留 adapter，直到 Runtime DB 中旧 job 清空 |
| 外部副作用语义变化 | 同步修改 idempotency policy 和 migration/回滚说明 |

worker 执行前应校验 handler contract 的最低要求。校验失败时应把 job 标记为不可重试配置错误，而不是进入无限 retry。

## 5. Feishu Handler 边界

飞书表读取和写入是最容易过度抽象的地方。每张飞书表字段、过滤、状态流转、写回策略都不同，因此必须拆成两层:

```text
feishu_table_read / feishu_table_write
  通用传输 handler

table-specific adapter / projection mapper
  表级业务语义
```

### 5.1 `feishu_table_read`

`feishu_table_read` 只负责“怎么稳定读取飞书表”，不负责“这张表的业务含义是什么”。

负责:

| 能力 | 说明 |
| --- | --- |
| 连接飞书 API | app token、table id、view id、auth |
| 分页读取 | page token、page size、全量/增量读取 |
| 基础过滤 | 飞书 API 支持的 filter 条件 |
| 字段选择 | 读取指定字段，返回原始字段值 |
| 限流和重试 | rate limit、timeout、网络错误 |
| 原始行标准化 | `record_id`、`fields`、`created_time`、`updated_time` |
| source snapshot | 保存本次读取到的源行快照 |
| 错误分类 | `auth_error`、`rate_limited`、`schema_missing`、`timeout` |

不负责:

| 不该负责 | 应放在哪里 |
| --- | --- |
| 判断竞品表哪些行要处理 | `competitor_table_source_adapter` |
| 判断达人同步候选竞品 | `influencer_pool_source_adapter` |
| 解析 `TK选品收集` 的商品 URL | `selection_table_source_adapter` |
| 根据业务状态决定是否跳过 | table-specific validator / workflow stage |
| 把事实结果映射成飞书写回字段 | projection mapper |

payload 示例:

```json
{
  "app_token": "base-token",
  "table_id": "table-id",
  "view_id": "view-id",
  "field_names": ["商品链接", "状态", "备注"],
  "filter_expr": {},
  "page_size": 100,
  "cursor": "",
  "snapshot_policy": {
    "store_raw_rows": true
  }
}
```

result 示例:

```json
{
  "rows": [
    {
      "record_id": "recxxx",
      "fields": {},
      "created_time": 0,
      "updated_time": 0
    }
  ],
  "schema": {},
  "raw_snapshot_ref": "artifact://...",
  "next_page_token": "",
  "has_more": false
}
```

### 5.2 Table Source Adapter

Table adapter 消费 `feishu_table_read` 的 rows，输出业务候选或业务上下文。

例子:

```text
selection_table_source_adapter
  feishu rows
  -> product_candidates

competitor_table_source_adapter
  feishu rows
  -> competitor_product_candidates

influencer_pool_source_adapter
  feishu rows
  -> influencer_pool_product_candidates
```

`selection_table_source_adapter` 输出示例:

```json
{
  "product_candidates": [
    {
      "source_record_id": "recxxx",
      "product_url": "https://...",
      "product_id": "123",
      "writeback_context": {
        "table_code": "TK选品收集",
        "record_id": "recxxx"
      },
      "source_snapshot": {}
    }
  ]
}
```

如果 adapter 只是纯字段转换，可以作为 mapper 被调用。如果它要分页读取多张表、复杂筛选、查重或产出 fan-out 数量，可以评审为 Business Handler Candidate；只有进入第 9 节准入清单后，才能成为 Runtime job handler。

### 5.3 `feishu_table_write`

`feishu_table_write` 只负责执行写入命令，不负责字段业务含义。

负责:

| 能力 | 说明 |
| --- | --- |
| update record | 根据 `record_id` 更新 |
| append record | 新增记录 |
| batch write | 批量写入 |
| retry / rate limit | 限流、重试、错误分类 |
| raw write result | 保存飞书响应 |
| 幂等支持 | 优先 `record_id`，其次业务 dedupe key |

不负责:

| 不该负责 | 应放在哪里 |
| --- | --- |
| 竞品表字段怎么填 | `competitor_table_projection_mapper` |
| 达人池字段怎么填 | `influencer_pool_projection_mapper` |
| 选品表状态怎么更新 | `selection_table_projection_mapper` |
| 哪些失败字段要写回 | workflow summary / projection policy |
| 新增前按什么业务键查重 | table-specific write adapter |

payload 示例:

```json
{
  "app_token": "base-token",
  "table_id": "table-id",
  "commands": [
    {
      "op": "update",
      "record_id": "recxxx",
      "fields": {}
    }
  ],
  "dedupe_key": "feishu-write:request-id:recxxx"
}
```

### 5.4 Projection Mapper

Projection mapper 把 facts / relations / observations / workflow context 转换成飞书写回命令。

例子:

```text
selection_table_projection_mapper
  facts + observations + source_context
  -> feishu write commands

influencer_pool_projection_mapper
  creator facts + creator-product relation + source context
  -> TK达人池 write commands
```

mapper 通常不单独进 Runtime DB，除非它执行耗时、需要查重、写业务快照或生成可审计 artifact。

## 6. 通用事实采集 Handler

### 6.1 `tiktok_product_request_fetch`

定位: TikTok 商品数据默认采集路径，通过 request / HTTP / 已知接口优先获取商品数据。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `api_worker` |
| runtime table | `api_worker_job` |
| input | `product_url` / `product_id`、region、detail_level、fallback_policy |
| output | normalized product result、raw_response_ref、quality_score、missing_fields |
| fallback | request 不可解析、关键字段缺失、风控阻断时返回 `fallback_required=true` |
| 不 fallback | URL 无法归一化、缺少 product key、商品明确不存在 |
| idempotency | `product_id` / normalized url |

result 中必须包含:

```json
{
  "normalized_product": {},
  "raw_response_ref": "artifact://...",
  "fallback_required": false,
  "fallback_reason": "",
  "missing_fields": [],
  "quality_score": 1.0
}
```

### 6.2 `tiktok_product_browser_fetch`

定位: TikTok 商品 request 失效后的 browser fallback。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `browser_worker` |
| runtime table | `task_execution` |
| input | `product_url`、fallback_source_job_id、browser profile / resource_code |
| output | HTML/network/page data refs、normalized product result |
| idempotency | request_id + normalized product url |
| side effects | browser artifact、screenshot、raw page dump |

约束:

- 只能在 request handler 明确返回 fallback 可恢复时派发。
- 输出必须和 `tiktok_product_request_fetch` 的 normalized product result 同 contract。
- 后续 `fact_bundle_upsert` 不应关心数据来自 request 还是 browser。

### 6.3 `fastmoss_product_search`

定位: FastMoss 商品搜索通用能力，通过 `/api/goods/V2/search` 或后续等价搜索 endpoint，根据 keyword、region、排序、分页、输入 filter 和输出 condition 搜索候选商品。关键词竞品入库只是该 handler 的一个调用场景。

正式约束:

- handler 名称固定为 `fastmoss_product_search`，不使用 `fastmoss_product_search_v1` / `fastmoss_product_search_v2`。
- payload/result 字段使用完整稳定名称，后续能力通过兼容新增字段迭代。
- 原始 FastMoss request/response 不直接成为 workflow contract；handler 必须输出标准化 candidate。
- 原始响应必须保存为 `raw_response_ref` 或 `raw_item_ref`，供排障和字段回放。
- 参考资料见 [../reference/fastmoss-known-interfaces.md](../reference/fastmoss-known-interfaces.md) 的“商品关键词搜索接口”章节。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `api_worker` |
| runtime table | `api_worker_job` |
| input | `search_mode`、`keyword`、`region`、`filters`、`sort`、`pagination`、`output_conditions`、`session_policy`、`raw_capture_policy` |
| source endpoint | 当前已验证 `/api/goods/V2/search` |
| output | normalized candidate products、condition evaluation、raw_response_ref、pagination result、auth/degraded state |
| idempotency | request_id + normalized search filters digest |
| retry | 网络、限流、上游 5xx 可重试 |

payload schema:

```json
{
  "search_mode": "keyword",
  "keyword": "Halloween decoration",
  "region": "US",
  "filters": {
    "category_ids": [],
    "price_range": {"min": null, "max": null, "currency": "USD"},
    "sales_range": {"window_days": 7, "min": 200, "max": null},
    "commission_rate_range": {"min": null, "max": null},
    "extra": {}
  },
  "sort": {
    "field": "day7_sold_count",
    "direction": "desc",
    "source_order": "2,2"
  },
  "pagination": {
    "page": 1,
    "page_size": 10,
    "max_pages": 50,
    "stop_when_no_new_product": true
  },
  "output_conditions": {
    "max_candidates": 20,
    "required_fields": ["product_id", "normalized_product_url", "title"],
    "dedupe_by": ["product_id", "normalized_product_url"],
    "strip_title_html": true,
    "min_quality_score": 0.8,
    "business_conditions": {
      "min_day7_sold_count": 200
    }
  },
  "session_policy": {
    "require_login": true,
    "cookie_namespace": "fastmoss",
    "degraded_preview_allowed": false
  },
  "raw_capture_policy": {
    "store_raw_response": true,
    "store_raw_items": false
  }
}
```

字段说明:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `search_mode` | 是 | 当前支持 `keyword`；后续类目、店铺、榜单搜索通过兼容扩展表达 |
| `keyword` | `keyword` 模式必填 | 映射 FastMoss `words` |
| `region` | 是 | 当前默认 `US`，同时进入请求 query 和 header |
| `filters` | 否 | 输入侧筛选条件；当前接口未确认的 filter 可放入 `extra` 并由 handler 显式忽略或报配置错误 |
| `sort` | 是 | 系统标准排序字段；当前 `day7_sold_count desc` 映射到 FastMoss `order=2,2` |
| `pagination` | 是 | 当前接口使用 `page` / `pagesize`，不是 cursor |
| `output_conditions` | 否 | 输出侧筛选、去重和交付条件；需要外部状态的条件由 workflow policy 处理 |
| `session_policy` | 是 | 是否要求登录态；商品搜索正式数据必须要求 `fd_tk` |
| `raw_capture_policy` | 否 | 原始响应保存策略 |

result schema:

```json
{
  "query": {
    "search_mode": "keyword",
    "keyword": "Halloween decoration",
    "region": "US",
    "source_endpoint": "/api/goods/V2/search",
    "source_order": "2,2",
    "page": 1,
    "page_size": 10
  },
  "candidates": [
    {
      "source": "fastmoss",
      "source_endpoint": "/api/goods/V2/search",
      "product_id": "1731194997356205027",
      "normalized_product_url": "https://www.tiktok.com/view/product/1731194997356205027",
      "fastmoss_product_url": "https://www.fastmoss.com/zh/e-commerce/detail/1731194997356205027",
      "detail_url": "",
      "title": "Halloween decoration",
      "title_raw": "<span style='color:red'>Halloween</span> decoration",
      "image_url": "",
      "shop": {
        "seller_id": "",
        "shop_name": "",
        "raw": {}
      },
      "price": {
        "amount": null,
        "currency": "USD",
        "display": ""
      },
      "original_price": {
        "amount": null,
        "currency": "USD",
        "display": ""
      },
      "commission": {
        "rate": null,
        "display": ""
      },
      "metrics": {
        "sold_count": null,
        "sale_amount": null,
        "yday_sold_count": null,
        "day7_sold_count": null,
        "day14_sold_count": null,
        "day28_sold_count": null,
        "relate_author_count": null,
        "relate_video_count": null,
        "relate_live_count": null,
        "product_rating": null
      },
      "trend": [
        {
          "date": "2026-04-23",
          "inc_sold_count": 0,
          "inc_sale_amount": 0,
          "region": "US",
          "region_name": "United States"
        }
      ],
      "dedupe_keys": {
        "product_id": "1731194997356205027",
        "normalized_product_url": "https://www.tiktok.com/view/product/1731194997356205027"
      },
      "matched_conditions": {
        "min_day7_sold_count": true
      },
      "deferred_conditions": {},
      "quality_score": 1.0,
      "raw_item_ref": ""
    }
  ],
  "condition_summary": {
    "applied": {},
    "deferred": {},
    "rejected_count": 0
  },
  "pagination": {
    "page": 1,
    "page_size": 10,
    "total": 5000,
    "has_more": true,
    "next_page": 2,
    "stop_reason": ""
  },
  "auth_state": {
    "is_login": true,
    "degraded_preview": false,
    "source_code": "200",
    "source_msg": "success!"
  },
  "raw_response_ref": "artifact://...",
  "warnings": []
}
```

FastMoss raw 字段映射:

| Raw 字段 | 标准字段 |
| --- | --- |
| `data.product_list[].product_id` | `candidates[].product_id` |
| `data.product_list[].title` | `title_raw`，去 HTML 后进入 `title` |
| `data.product_list[].img` | `image_url` |
| `data.product_list[].shop_name` | `shop.shop_name` |
| `data.product_list[].shop_info` | `shop.raw`，如有稳定 `seller_id` 再映射 |
| `data.product_list[].price` | `price.display`，可解析时填 `price.amount` |
| `data.product_list[].ori_price` | `original_price.display` |
| `data.product_list[].crate` / `crate_show` | `commission.rate` / `commission.display` |
| `data.product_list[].sold_count` | `metrics.sold_count` |
| `data.product_list[].sale_amount` | `metrics.sale_amount` |
| `data.product_list[].yday_sold_count` | `metrics.yday_sold_count` |
| `data.product_list[].day7_sold_count` | `metrics.day7_sold_count` |
| `data.product_list[].day14_sold_count` | `metrics.day14_sold_count` |
| `data.product_list[].day28_sold_count` | `metrics.day28_sold_count` |
| `data.product_list[].relate_author_count` | `metrics.relate_author_count` |
| `data.product_list[].relate_video_count` | `metrics.relate_video_count` |
| `data.product_list[].relate_live_count` | `metrics.relate_live_count` |
| `data.product_list[].product_rating` | `metrics.product_rating` |
| `data.product_list[].detail_url` | `detail_url` |
| `data.product_list[].trend[]` | `trend[]` |
| `data.total` / `data.total_cnt` | `pagination.total` |
| `ext.is_login` | `auth_state.is_login` |
| `code` / `msg` | `auth_state.source_code` / `auth_state.source_msg` |

约束:

- handler 只负责搜索和标准化候选商品，不负责写飞书种子行。
- 候选商品是否进入 `TK竞品收集` 由 `competitor_seed_projection_mapper` 和 workflow policy 决定。
- 如果后续需要类目搜索、店铺搜索或榜单搜索，应优先扩展 `search_mode` / `filters`，不要新增业务专用搜索 handler。
- `MAG_AUTH_3001` 且只返回固定预览时，必须标记 `auth_state.degraded_preview=true`；如果 `session_policy.degraded_preview_allowed=false`，job 应失败为不可交付结果。
- 登录态搜索必须复用同一个 FastMoss session 翻页；匿名态翻页不可作为正式结果。
- `order=2,2` 当前按“近 7 天销量倒序”处理，但文档中必须保留这是基于实测推断的说明。

### 6.4 `fastmoss_product_fetch`

定位: 拉取 FastMoss 商品、店铺、指标、分布和扩展信息。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `api_worker` |
| input | product_id / fastmoss product key / region / detail_level |
| output | normalized product facts、shop facts、metrics、raw_response_ref |
| idempotency | product_id + source endpoint + detail_level |
| retry | 网络、限流、上游 5xx 可重试 |

### 6.5 `fastmoss_creator_fetch` / `fastmoss_shop_fetch` / `fastmoss_video_fetch`

定位: 拉取达人、店铺和视频事实，不绑定具体 workflow。

输出应统一成:

```json
{
  "entities": {},
  "relations": [],
  "observations": [],
  "raw_response_refs": []
}
```

达人同步、竞品表、选品分析可以按需消费这些 facts，再由业务 mapper 决定写哪些关系和投影。

命名约束:

- FastMoss 达人主体在 Fact DB 中统一称为 `creator`，handler 使用 `fastmoss_creator_fetch`。
- 不使用 `fastmoss_author_fetch` 作为目标 handler 名称。
- 不新增 `influencer_pool_author` 这类业务专用采集 handler；达人池 workflow 通过 `fastmoss_creator_fetch` 获取达人事实。

### 6.6 `media_asset_sync`

定位: 同步图片、头像、封面等媒体资产到 MinIO/local object store，并写入媒体事实索引。

关键契约:

| 项 | 约定 |
| --- | --- |
| input | source_url / file_token / local_path / desired object prefix |
| output | asset_key、object_key、content_type、size、checksum |
| idempotency | asset_key 或稳定 object_key |
| side effects | MinIO/local object store、`tk_media_assets` / artifact index |

### 6.7 `fact_bundle_upsert`

定位: 将 normalized entities / relations / observations / media / raw links 统一写入 Fact DB。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `api_worker` |
| input | source_job_ids、entities、relations、observations、raw_refs、relation_context |
| output | persisted_entities、persisted_relations、persisted_observations、warnings |
| idempotency | 主体按业务键 upsert，关系按 relation_key upsert，latest upsert，observations/raw 追加或 digest 去重 |
| side effects | Fact DB |

## 7. Business Handler

Business handler 负责有独立运行价值的业务动作。它可以调用 capability handler，也可以消费 capability handler 的 result。

业务 handler 不是默认准入。只有当它满足独立 retry / timeout / artifact / 外部副作用 / 审计需求，并且已经进入第 9 节“Handler Registry 唯一准入清单”时，才允许作为 Runtime job 的 `handler_code`。

候选 business handlers:

| Handler | 作用 | 当前准入建议 |
| --- | --- | --- |
| `influencer_pool_candidate_select` | 从竞品表源行筛选达人同步候选，并 fan-out product jobs | 当前不准入；优先由 `feishu_table_read` + `influencer_pool_source_adapter` + workflow dispatcher 表达 |
| `selection_analysis_summary` | 基于 facts/observations 生成选品分析摘要 | 候选；视耗时和是否生成 artifact 决定是否准入 |
| `fastmoss_echarts_render` | 生成 ECharts option / HTML / 图片 artifact | 候选；需要 artifact contract 后才能准入 |
| `product_candidate_filter` | 过滤 `fastmoss_product_search` 结果，生成 seed rows | 候选；优先复用 `fastmoss_product_search.output_conditions` |
| `business_snapshot_write` | 写业务快照或任务视角结果 | 候选；需要明确快照归属后才能准入 |

候选 business handler 可以非常定制化，但准入前仍必须遵守标准 handler contract:

- 明确 payload/result/error。
- 明确 retry 和 timeout。
- 明确幂等键。
- 明确是否写 Runtime result、Fact DB、Feishu 或 artifact。
- 明确为什么不能由既有 capability handler + adapter/mapper/policy 组合完成。

## 8. Mapper / Policy / Renderer

Mapper / Policy / Renderer 是 handler 内部可复用组件。它们不一定是 Runtime job。

适合保持为轻量组件的场景:

- 纯字段映射。
- 纯校验。
- 纯打分或筛选。
- 不写外部系统。
- 执行很快，失败可由调用 handler 一并处理。

示例:

```text
selection_table_projection_mapper
competitor_table_projection_mapper
influencer_pool_projection_mapper
influencer_match_policy
selection_candidate_validator
fastmoss_echarts_renderer
```

如果 renderer 生成文件、图片、HTML artifact，且耗时或可能失败，应由 business handler 包裹成 Runtime job。

## 9. Handler Registry 唯一准入清单

本节是当前项目 handler registry 的唯一准入清单。worker claim 到 Runtime job 后，只能根据本节列出的 `handler_code` / `item_code` 查找 handler；未列入清单的名称必须被 registry 拒绝，并按不可重试的配置错误处理。

准入规则:

- Workflow 文档的 Job / Handler 映射只能引用本节清单中的 `handler_code`。
- `job_code` / `item_code` 通常应与 `handler_code` 一致；少数 system job 必须在清单中显式说明。
- Adapter / Mapper / Policy / Renderer 不是 registry key，不能直接作为 Runtime job 的 `handler_code`。
- 新增 handler 必须先修改本节清单，并补齐 payload/result/error/retry/timeout/idempotency/side effects contract。
- 删除或重命名 handler 必须先迁移或清空 Runtime DB 中未完成的旧 job。

### 9.1 准入清单

| Registry | 准入 `handler_code` / `item_code` | Worker | Runtime 表 | 类型 | 允许用途 | 契约章节 |
| --- | --- | --- | --- | --- | --- | --- |
| API | `feishu_table_read` | `api_worker` | `api_worker_job` | Capability | 通用飞书表读取，表级语义由 source adapter 处理 | 5.1 |
| API | `feishu_table_write` | `api_worker` | `api_worker_job` | Capability | 通用飞书表新增/更新/批量写入，字段映射由 projection mapper 处理 | 5.3 |
| API | `tiktok_product_request_fetch` | `api_worker` | `api_worker_job` | Capability | TikTok 商品 request-first 采集 | 6.1 |
| Browser | `tiktok_product_browser_fetch` | `browser_worker` | `task_execution` | Capability | TikTok 商品 request 失败后的浏览器兜底采集 | 6.2 |
| API | `fastmoss_product_search` | `api_worker` | `api_worker_job` | Capability | FastMoss 商品搜索，支持 keyword/filter/condition | 6.3 |
| API | `fastmoss_product_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 商品事实、店铺事实、商品指标采集 | 6.4 |
| API | `fastmoss_creator_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 达人事实和指标采集 | 6.5 |
| API | `fastmoss_shop_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 店铺事实和指标采集 | 6.5 |
| API | `fastmoss_video_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 视频事实和指标采集 | 6.5 |
| API | `media_asset_sync` | `api_worker` | `api_worker_job` | Capability | 图片、头像、封面等媒体资产同步到对象存储和事实索引 | 6.6 |
| API | `fact_bundle_upsert` | `api_worker` | `api_worker_job` | Capability | normalized entities / relations / observations 统一写入 Fact DB | 6.7 |
| Outbox | `outbox_dispatch` | `outbox_dispatcher` | `notification_outbox` | System | 发送最终通知和任务摘要，不参与业务事实采集 | 当前系统架构 / outbox |

### 9.2 未准入名称

以下名称不能作为目标 `handler_code`、`job_code`、`item_code`、registry key 或 handler 文件名出现:

| 名称 / 模式 | 处理方式 | 原因 |
| --- | --- | --- |
| `orchestrate_*`、`run_*_workflow`、`run_sync_*`、`*_orchestrator` | 禁止准入 | 这是 workflow 编排入口，不是 worker handler |
| `orchestrate_sync_tk_influencer_pool` | 禁止准入 | 只能作为历史兼容入口或当前实现事实被记录 |
| `feishu_single_row_update`、`feishu_seed_row_insert` | 禁止准入 | 飞书写入统一使用 `feishu_table_write` + projection mapper |
| `feishu_tk_selection_table_read`、`feishu_tk_selection_table_writeback` | 禁止准入 | 表级差异必须放到 adapter/mapper，不新增表级飞书 handler |
| `influencer_pool_product`、`influencer_pool_author`、`influencer_pool_finalizer` | 禁止准入 | 达人同步不新增业务专用 Runtime handler |
| `fastmoss_author_fetch` | 禁止准入 | FastMoss 达人主体统一称为 `creator`，使用 `fastmoss_creator_fetch` |
| `fastmoss_product_search_v1`、`fastmoss_product_search_v2` | 禁止准入 | handler 名称不通过版本后缀演进 |
| `selection_table_source_adapter`、`competitor_table_projection_mapper` 等 adapter/mapper | 禁止准入 | 它们是 handler 内部组件或 workflow 组件，不是 registry key |

### 9.3 候选业务 Handler 准入要求

业务 handler 只有在现有 capability handler + adapter/mapper/policy 无法清晰表达，且确实需要独立 Runtime 生命周期时才允许新增。候选名称不能先写进 workflow Job / Handler 映射表，必须先在本节准入。

当前候选但未准入:

| 候选 handler | 可能用途 | 准入前必须补齐 |
| --- | --- | --- |
| `fastmoss_echarts_render` | 生成 ECharts option / HTML / 图片 artifact | artifact schema、输入 facts 范围、MinIO prefix、重试/超时/幂等规则 |
| `selection_analysis_summary` | 基于 facts/observations 生成选品分析摘要 | summary schema、是否生成 artifact、是否需要单独 retry |
| `product_candidate_filter` | 过滤 FastMoss 搜索候选并生成 seed row 输入 | filter DSL、condition 输出、与 `fastmoss_product_search.output_conditions` 的边界 |
| `business_snapshot_write` | 写业务快照或任务视角结果 | 快照表归属、幂等键、与 Fact DB / Runtime result 的边界 |

### 9.4 目标目录结构

```text
business/handlers/api/
  registry.py
  feishu_table_read.py
  feishu_table_write.py
  tiktok_product_request_fetch.py
  fastmoss_product_search.py
  fastmoss_product_fetch.py
  fastmoss_creator_fetch.py
  fastmoss_shop_fetch.py
  fastmoss_video_fetch.py
  media_asset_sync.py
  fact_bundle_upsert.py

business/handlers/browser/
  registry.py
  tiktok_product_browser_fetch.py

business/handlers/outbox/
  registry.py
  outbox_dispatch.py

business/adapters/
  feishu/
    selection_table_source_adapter.py
    competitor_table_source_adapter.py
    influencer_pool_source_adapter.py
    selection_table_projection_mapper.py
    competitor_table_projection_mapper.py
    influencer_pool_projection_mapper.py
```

### 9.5 Worker 主循环目标

```text
claim job
handler = registry.get(job_code / item_code)
result = supervisor.run(handler, payload)
mark success / retry / failed
trigger reconciler
```

worker 不应该:

- import 某个具体业务 workflow 的大 flow 文件。
- 按业务 task_code 写分支。
- 理解飞书某张表字段含义。
- 决定业务投影字段。

## 10. 迁移顺序

第一阶段: 契约先行

- 固化本文 handler contract。
- 为第一批 generic handlers 写 payload/result/error schema。
- 在 workflow 文档中引用 handler_code，而不是大 flow 函数名。

第二阶段: Registry 骨架

- 引入 API handler registry。
- 引入 browser handler registry。
- 保持旧 handler 实现可被 registry 包裹，降低一次性迁移风险。

第三阶段: 选品分析迁移

- 拆出 `tiktok_product_request_fetch`。
- 保留 `tiktok_product_browser_fetch` 作为 fallback。
- 拆出 `fact_bundle_upsert`。
- 将飞书选品表写回改成 projection mapper + `feishu_table_write`。

第四阶段: 竞品表和达人同步迁移

- 抽 `competitor_table_source_adapter` / projection mapper。
- 抽 `influencer_pool_source_adapter` / projection mapper，并通过 workflow dispatcher 派生通用 `api_worker_job`。
- 将达人池写回改成 projection mapper + `feishu_table_write`。

## 11. 最终约束

以下约束作为后续代码重构的判断标准:

- `feishu_table_read` / `feishu_table_write` 只提供飞书传输能力，不承担表级业务语义。
- 表级语义必须由 table adapter / projection mapper 承担。
- 通用事实采集 handler 输出 normalized facts，不输出飞书业务字段。
- Business handler 可以定制化，但必须先进入第 9 节准入清单并具备明确契约。
- Mapper / Policy / Renderer 只有在需要 retry、timeout、artifact、审计或外部副作用时才升级为 Runtime job。
- Worker 只认 handler registry，不直接理解 workflow。
