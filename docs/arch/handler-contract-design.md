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

- [系统架构设计](./system-architecture-design.md)
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
  "source_table_ref": "feishu://mujitask/TK竞品收集",
  "feishu_table": {
    "app_token_ref": "secret://feishu/app_token/main",
    "table_id": "tblpzuTZXHtDq83t",
    "view_id": "vewT6AtfED"
  },
  "field_names": ["产品链接", "SKU-ID", "商品状态", "达人查找状态", "备注"],
  "filter_spec": {
    "conjunction": "and",
    "conditions": [
      {"field": "商品状态", "op": "not_in", "value": ["已下架/区域不可售"]},
      {"field": "达人查找状态", "op": "in_or_empty", "value": ["待查找", "失败重试", "处理中"]}
    ]
  },
  "pagination": {
    "page_size": 100,
    "cursor": "",
    "max_pages": 20
  },
  "adapter_code": "influencer_pool_source_adapter",
  "adapter_options": {
    "drop_empty_rows": true,
    "dedupe_by": ["product_id", "normalized_product_url"]
  },
  "snapshot_policy": {
    "store_raw_rows": true,
    "raw_snapshot_namespace": "feishu/competitor/read"
  },
  "cursor_context": {
    "updated_after": null
  }
}
```

result 示例:

```json
{
  "raw_rows": [
    {
      "record_id": "recKwc9Y7r",
      "fields": {
        "产品链接": {
          "text": "https://www.tiktok.com/shop/pdp/1731194997356205027",
          "link": "https://www.tiktok.com/shop/pdp/1731194997356205027"
        },
        "SKU-ID": "1731194997356205027",
        "商品状态": "",
        "达人查找状态": "待查找",
        "备注": "毕业季Top1"
      },
      "created_time": 1713849600000,
      "updated_time": 1713936000000
    }
  ],
  "source_rows": [
    {
      "source_record_id": "recKwc9Y7r",
      "source_table_ref": "feishu://mujitask/TK竞品收集",
      "product_identity": {
        "product_id": "1731194997356205027",
        "product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
        "normalized_product_url": "https://www.tiktok.com/view/product/1731194997356205027",
        "fastmoss_product_url": "https://www.fastmoss.com/zh/e-commerce/detail/1731194997356205027"
      },
      "business_fields": {
        "holiday": "毕业季",
        "product_status": "",
        "influencer_search_status": "待查找"
      },
      "writeback_context": {
        "target_table_ref": "feishu://mujitask/TK竞品收集",
        "record_id": "recKwc9Y7r"
      },
      "source_snapshot_ref": "artifact://feishu/competitor/read/req-001/recKwc9Y7r.json"
    }
  ],
  "schema": {
    "field_names": ["产品链接", "SKU-ID", "商品状态", "达人查找状态", "备注"]
  },
  "pagination": {
    "next_page_token": "",
    "has_more": false
  },
  "raw_snapshot_ref": "artifact://feishu/competitor/read/req-001/page-1.json",
  "candidate_keys": ["product:1731194997356205027"],
  "adapter_summary": {
    "input_row_count": 1,
    "source_row_count": 1,
    "dropped_empty_count": 0,
    "deduped_count": 0
  }
}
```

### 5.1.1 P0 冻结样例

P0 冻结以下边界，后续 P1 Feishu common 只能兼容新增字段，不能改变字段语义:

| 字段 | 冻结语义 |
| --- | --- |
| `source_table_ref` | 业务稳定表引用，executor、dedupe、summary 使用它，不直接依赖真实 `table_id`。 |
| `feishu_table` | handler 执行前可由配置解析得到；payload 中可以只给 `source_table_ref`，但真实执行时必须能解析 `app_token_ref/table_id/view_id`。 |
| `raw_rows` | 飞书传输层原始标准化行；只做 record/time/fields 外壳标准化，不做业务字段解释。 |
| `source_rows` | 当 `adapter_code` 存在时输出的业务候选行；adapter 只做字段解析、筛选、去重、writeback context 组装，不写外部系统。 |
| `raw_snapshot_ref` | 一次读取的页级或批次级快照，用于重放和 `achieve` 对比。 |
| `source_snapshot_ref` | 单行快照，可用于行级排障；允许和 `raw_snapshot_ref` 指向同一 artifact。 |

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

字段投影读取 FastMoss 指标时必须保留窗口上下文。`fastmoss_product_fetch` 对 `goods.overview` 传入 `d_type=90` 后，返回的 `overview.real_sold_count` / `overview.sold_count` 代表该窗口汇总，handler 必须标准化为 `sales_90d` 并在 raw response 的 `request_params.d_type` 与 metric snapshot 的 `window_days` 中留下审计证据。投影层只消费标准化窗口指标；只有 `chart_list` 满 90 个日点时才允许按日增量求和兜底。

```json
{
  "target_table_ref": "feishu://mujitask/TK竞品收集",
  "feishu_table": {
    "app_token_ref": "secret://feishu/app_token/main",
    "table_id": "tblpzuTZXHtDq83t"
  },
  "write_mode": "batch_upsert",
  "mapper_code": "competitor_table_projection_mapper",
  "records": [
    {
      "op": "update",
      "record_id": "recKwc9Y7r",
      "business_entity_key": "product:1731194997356205027",
      "fields": {
        "SKU-ID": "1731194997356205027",
        "产品链接": {
          "text": "https://www.tiktok.com/shop/pdp/1731194997356205027",
          "link": "https://www.tiktok.com/shop/pdp/1731194997356205027"
        },
        "标题": "Graduation party decoration set",
        "Fastmoss价格": "$12.99",
        "昨日销量": "38",
        "近7天销量": "412",
        "近90天销量": "2310",
        "记录日期": "2026-04-24"
      },
      "source_context": {
        "source_record_id": "recKwc9Y7r",
        "workflow_code": "refresh_current_competitor_table",
        "projection_type": "competitor_detail_writeback"
      }
    }
  ],
  "idempotency_context": {
    "dedupe_key": "req-001:feishu_table_write:recKwc9Y7r",
    "upsert_keys": ["record_id", "business_entity_key"],
    "on_conflict": "update"
  },
  "write_policy": {
    "batch_size": 50,
    "partial_success_allowed": true,
    "validate_schema": true
  },
  "raw_capture_policy": {
    "store_raw_response": true
  }
}
```

result 示例:

```json
{
  "written_count": 1,
  "skipped_count": 0,
  "failed_count": 0,
  "target_record_ids": ["recKwc9Y7r"],
  "records": [
    {
      "business_entity_key": "product:1731194997356205027",
      "record_id": "recKwc9Y7r",
      "op": "update",
      "status": "success",
      "fields_written": ["SKU-ID", "产品链接", "标题", "Fastmoss价格", "昨日销量", "近7天销量", "近90天销量", "记录日期"],
      "raw_result_ref": "artifact://feishu/competitor/write/req-001/recKwc9Y7r.json"
    }
  ],
  "writeback_context": {
    "target_table_ref": "feishu://mujitask/TK竞品收集",
    "mapper_code": "competitor_table_projection_mapper"
  },
  "raw_response_ref": "artifact://feishu/competitor/write/req-001/batch-1.json"
}
```

### 5.3.1 P0 冻结样例

P0 冻结以下边界:

| 字段 | 冻结语义 |
| --- | --- |
| `records[].op` | `append`、`update`、`upsert` 三类稳定写命令；真实 Feishu API 的 create/update 细节不外泄给 workflow。 |
| `records[].fields` | 已由 projection mapper 产出的飞书字段名和值；handler 只负责 schema 校验、附件/日期等传输格式转换和写入。 |
| `business_entity_key` | 业务幂等键，新增行时用于去重或冲突处理；更新行优先使用 `record_id`。 |
| `idempotency_context.dedupe_key` | Runtime job 重跑时避免重复副作用的稳定键。 |
| `records[].status` | 行级结果，允许 `success`、`skipped`、`failed`；父 workflow 根据行级结果汇总 `partial_success`。 |

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

### 5.4.1 Projection Mapper 输入 / 输出契约

Projection mapper 是纯函数边界，不直接读写 Feishu、Fact DB 或对象存储。它可以被 executor、handler 或后续验收工具调用，但不能作为 `handler_code` 注册。

输入样例:

```json
{
  "mapper_code": "influencer_pool_projection_mapper",
  "projection_type": "influencer_pool_upsert",
  "source_context": {
    "workflow_code": "sync_tk_influencer_pool",
    "source_record_id": "recKwc9Y7r",
    "source_table_ref": "feishu://mujitask/TK竞品收集",
    "target_table_ref": "feishu://mujitask/TK达人池",
    "product_id": "1731194997356205027",
    "holiday": "毕业季"
  },
  "fact_projection": {
    "entities": {
      "creator": {
        "entity_key": "fastmoss_creator:7228697870020199470",
        "creator_id": "7228697870020199470",
        "unique_id": "anonymousbillionaires",
        "nickname": "Anonymous Billionaires",
        "avatar_asset_ref": "asset://creator/7228697870020199470/avatar"
      }
    },
    "relations": [
      {
        "relation_key": "creator_product:7228697870020199470:1731194997356205027",
        "relation_type": "creator_promotes_product",
        "metrics": {
          "sold_count": 72,
          "sale_amount": 1299
        }
      }
    ],
    "observations": [
      {"metric_name": "follower_count", "metric_value": 128000, "observed_at": "2026-04-24T00:00:00Z"},
      {"metric_name": "video_sale_amount", "metric_value": 32000, "currency": "USD", "window_days": 28}
    ]
  },
  "workflow_context": {
    "request_id": "req-001",
    "record_date": "2026-04-24"
  }
}
```

输出样例:

```json
{
  "target_table_ref": "feishu://mujitask/TK达人池",
  "write_mode": "batch_upsert",
  "mapper_code": "influencer_pool_projection_mapper",
  "records": [
    {
      "op": "upsert",
      "business_entity_key": "creator:7228697870020199470",
      "upsert_key": {
        "field": "达人ID",
        "value": "7228697870020199470"
      },
      "fields": {
        "达人ID": "7228697870020199470",
        "达人头像": [{"asset_ref": "asset://creator/7228697870020199470/avatar"}],
        "粉丝数": "13W",
        "带货视频 GMV": "3W",
        "带货直播 GMV": "小于1W",
        "带货商品图": [{"asset_ref": "asset://product/1731194997356205027/main-image"}],
        "关联商品销量": "72",
        "关联节日": ["毕业季"],
        "合作店铺": ["Graduation Shop"],
        "达人联系方式": "hello@example.com",
        "记录日期": "2026-04-24",
        "更新日期": "2026-04-24"
      },
      "source_context": {
        "source_record_id": "recKwc9Y7r",
        "product_id": "1731194997356205027",
        "relation_key": "creator_product:7228697870020199470:1731194997356205027"
      }
    }
  ],
  "idempotency_context": {
    "dedupe_key": "req-001:influencer_pool_projection_mapper:creator:7228697870020199470"
  }
}
```

已冻结 mapper 输出:

| Mapper | 输出用途 | 必须输出的稳定键 |
| --- | --- | --- |
| `competitor_seed_projection_mapper` | 关键词搜索候选写入 `TK竞品收集` 种子行 | `business_entity_key=product:{product_id}`、`fields.SKU-ID`、`fields.产品链接`、`fields.备注` |
| `competitor_table_projection_mapper` | 商品详情、媒体和指标写回 `TK竞品收集` | `source_record_id` 或 `business_entity_key`、自动维护字段、`projection_type=competitor_detail_writeback` |
| `influencer_pool_projection_mapper` | 达人事实和商品关系写入 `TK达人池` | `business_entity_key=creator:{creator_id}`、`upsert_key.达人ID` |
| `competitor_influencer_status_projection_mapper` | 达人同步结束后回写竞品表状态 | `source_record_id`、`fields.达人查找状态` |

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
- TikTok 商品页安全验证滑块必须优先使用 framework v0.3.8 的 `SliderCaptchaResolver`，业务仓库只配置 selector、尝试次数、拖动修正参数和可选 `DdddOcrCaptchaProvider` 模型路径。
- 默认 selector 为 `#tts_web_captcha_container`、`#captcha-verify-image`、`.captcha_verify_img_slide`、`.secsdk-captcha-drag-icon`、`.secsdk_captcha_refresh`；handler payload 可通过 `slider_captcha_selectors` / `tiktok_slider_captcha_selectors` 覆盖。
- 当使用 `dddd_trainer` 训练自定义模型时，业务仓库只传 `slider_captcha_provider_config.import_onnx_path` 和 `charsets_path`，不直接依赖训练工具链。
- browser fallback 结果必须保留 `slider_captcha_resolution` 与 `slider_captcha_audit_artifact_refs`，用于审计 ddddocr 原始坐标、浏览器渲染坐标、缩放换算、拖动距离、前后截图和原始图片 artifact。
- 验证码等待只在已识别 TikTok 商品页风控信号后发生；正常商品页抓取不得进入滑块等待。
- `image_timeout_ms` 是元素出现的最大等待，不是固定 sleep；滑动后最多轮询 5 秒验证结果，popup 消失或成功 selector 出现后再延迟 2 秒二次确认。
- TikTok 商品页默认 `simple_target=false`，以已经验收的 framework `match` 行为为准；若业务 payload 显式覆盖 `simple_target`、`drag_scale` 或 `drag_offset_x`，result 必须保留生效配置和坐标换算证据。
- 如果页面出现 `Unable to verify. Please try again.` 等失败态文本，或二次确认时 popup 重新出现，本次滑块 attempt 不得标记为 resolved。

### 6.2.1 `fastmoss_security_browser_resolve`

定位: FastMoss 任一受控 API 返回 `MSG_SAFE_0001` 后的浏览器解风控能力。该能力按 provider + security resolve 拆分，服务 FastMoss 搜索、商品详情、达人、店铺和视频接口，但不承担 TikTok 商品页 browser fallback。

关键契约:

| 项 | 约定 |
| --- | --- |
| worker | `browser_worker` |
| runtime table | `task_execution` |
| input | `verification_request` 原始失败 FastMoss API 请求、fallback_source_job_id、FastMoss browser profile / resource_code |
| output | 原始失败 FastMoss API 请求验证结果、slider resolution evidence、`fastmoss_session_cookie_cache` 脱敏 metadata |
| idempotency | request_id + original request digest + fastmoss_security_browser_fallback |
| side effects | browser 解风控、Runtime DB cookie cache 写入 |

约束:

- 只能由 workflow 或行级主 job 在 FastMoss API handler 返回 `fallback_required` 后派发，API worker 不直接驱动浏览器。
- 成功判据必须是原始失败的 FastMoss API 请求不再返回 `MSG_SAFE_0001`。搜索场景的原始请求是 `/api/goods/V2/search`；商品详情页不能作为搜索风控解除成功判据。
- handler 只持久化 FastMoss cookies，不是 API token；summary/result/log 不得包含 cookie value。
- fallback 对同一原始请求最多一次，失败后由 workflow 终态落 `fastmoss_security_verification_required`。
- FastMoss 和 TikTok 商品页的验证码业务逻辑必须独立：FastMoss handler 只围绕原始 FastMoss API 请求解风控和刷新 FastMoss cookie cache；TikTok handler 只围绕商品详情页 browser fallback 解商品页安全验证。
- FastMoss 滑块使用独立的 FastMoss/Tencent resolver 逻辑，selector profile 使用 `#tcaptcha_transform_dy`、`.tencent-captcha-dy__verify-bg-img`、`.tencent-captcha-dy__fg-item`、`.tencent-captcha-dy__slider-block`、`.tencent-captcha-dy__footer-icon--refresh`，不得复用 TikTok 商品页 selector 作为成功前提。FastMoss/Tencent 默认取图策略是背景取 CSS `background-image` 原图、拼图取元素截图，默认 `simple_target=false`，距离按目标中心点减当前拼图中心点计算。
- handler payload 可通过 `fastmoss_slider_captcha_selectors`、`fastmoss_slider_captcha_provider_config`、`fastmoss_slider_captcha_resolver_config` 和 `fastmoss_slider_captcha_audit_dir` 覆盖 selector、`DdddOcrCaptchaProvider` 模型路径、拖动修正参数和审计目录。
- FastMoss result 中的 `slider_resolution` 必须保留 resolver 名称、ddddocr 原始坐标、坐标换算、拖动距离、二次确认结果和 `slider_captcha_audit_artifact_refs`，用于判断失败原因是识别错误、距离计算错误还是鼠标拖动执行异常。每次 attempt 必须同时保存 `before_screenshot`、`target_position_screenshot` 和 `after_screenshot`；`target_position_screenshot` 在鼠标已移动到计算出的目标终点、`mouse.up()` 释放之前捕获，用于复盘实际落点偏差。
- FastMoss 验证码等待只在原始 FastMoss API 请求触发 `MSG_SAFE_0001` 且 workflow 已进入 `fastmoss_security_browser_fallback` 后发生；正常 FastMoss API 请求不得进入滑块等待。
- FastMoss 滑动后同样按“滑动后最多轮询 5 秒验证结果，弹窗消失后延迟 2 秒二次确认”处理；二次确认只证明浏览器弹层稳定消失，最终成功判据仍然必须回到原始失败的 FastMoss API 请求不再返回 `MSG_SAFE_0001`。
- FastMoss 每次识别前必须确认 Tencent 滑块不处于 loading/verifying 状态、背景图已可用、拼图块和手柄已回到可识别起点；失败重试前必须先等待上一轮 loading 结束，再刷新并进入下一次识别，禁止把 loading/spinner 画面或上一轮残留位置直接送入 ddddocr。
- FastMoss 默认拖动轨迹不得使用过慢的固定长轨迹；默认 profile 为 36 steps、每步 0.012s，仍允许 payload 通过 `fastmoss_slider_captcha_resolver_config.drag_steps` 和 `drag_step_delay_seconds` 覆盖。
- `fastmoss_product_fetch`、`fastmoss_creator_fetch`、`fastmoss_shop_fetch`、`fastmoss_video_fetch` 遇到 `MSG_SAFE_0001` 时，必须返回 `fallback_required`，并在 result 中给出脱敏 `verification_request.method/path/params/referer/region/stage`；不得把该场景归类为普通 `fastmoss_http_failure`。
- `verification_request.path` 可以是 `/api/goods/V2/search`、`/api/goods/v3/base`、达人详情、店铺详情或视频详情等 FastMoss 受控 API。browser resolve 只验证该原始失败请求，成功后由原调用方重试原 handler 一次。

### 6.2.2 FastMoss platform session recovery

FastMoss session/cookie 恢复属于 `infrastructure/fastmoss` 平台策略，不属于具体业务 handler。`fastmoss_product_search`、`fastmoss_product_fetch`、`fastmoss_creator_fetch`、`fastmoss_shop_fetch`、`fastmoss_video_fetch` 只创建 FastMoss session 并接入统一 cookie cache；不得复制登录刷新、cookie 持久化或单点登录冲突判断。

关键约束:

- `fastmoss_session_cookie_cache` 复用必须检查 `expires_at` 和 `last_auth_failed_at`；已标记 `last_auth_failed_at` 的 cookie 不得继续复用。
- 任意 FastMoss API 遇到明确 auth 失效时，平台层在账号级 lock 内刷新登录并保存新 cookie；保存成功后清空 `last_auth_failed_at`。
- 如果 DB 中存在不同 digest 的较新 cookie，平台层可先复用并验证；仍 auth 失败时再登录刷新一次。
- `MSG_SAFE_0001` 本身是风控，不等同 auth 失效；搜索场景保留一次登录刷新重试，但刷新和持久化必须由 infrastructure 统一入口完成。
- 刷新后原请求仍 auth 失败，handler 透出 `fastmoss_session_conflict_or_external_login`，不进入 browser fallback，也不无限重试。

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
  "page_request_delay_seconds": 1,
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
- 当 `order=2,2` 且存在 `output_conditions.business_conditions.min_day7_sold_count` 时，live 翻页按整页销量阈值提前截断；若当前页所有可解析 `day7_sold_count` 的最大值仍低于阈值，停止后续翻页并返回 `pagination.stop_reason=below_min_day7_sold_count`。
- `max_candidates=0` 只取消候选数量上限，不取消分页、FastMoss total、空页、无新商品、`below_min_day7_sold_count` 或 `max_pages` 等停止条件。

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

风控约束:

- 当 FastMoss 商品详情接口例如 `/api/goods/v3/base` 返回 `MSG_SAFE_0001` 时，handler 返回 `fallback_required`，error_code 固定为 `fastmoss_security_verification_required`，result.reason 固定为 `fastmoss_api_security_verification`。
- result 必须包含脱敏 `verification_request`，用于 `fastmoss_security_browser_resolve` 在浏览器中围绕原始失败请求解除风控、持久化 `fastmoss_session_cookie_cache`，再由调用方重试 `fastmoss_product_fetch` 一次。

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

风控约束:

- `fastmoss_creator_fetch`、`fastmoss_shop_fetch`、`fastmoss_video_fetch` 与 `fastmoss_product_fetch` 使用同一 FastMoss provider 级风控契约。对应 API 返回 `MSG_SAFE_0001` 时，handler 返回 `fallback_required` 和脱敏 `verification_request`，由 `fastmoss_security_browser_resolve` 验证原始失败的 FastMoss API 请求并刷新 cookie cache。
- 成功后调用方只能重试原 handler 一次；再次返回 `MSG_SAFE_0001` 时终态错误为 `fastmoss_security_verification_required`。

命名约束:

- FastMoss 达人主体在 Fact DB 中统一称为 `creator`，handler 使用 `fastmoss_creator_fetch`。
- 不使用 `fastmoss_author_fetch` 作为目标 handler 名称。
- 不新增 `influencer_pool_author` 这类历史业务专用采集 handler；达人池 workflow 的 `influencer_creator_sync` 业务 job 内部通过 `fastmoss_creator_fetch` 获取达人事实。

### 6.5.1 `fastmoss_creator_fetch` P0 冻结样例

`fastmoss_creator_fetch` 负责 FastMoss 达人详情采集和标准化，不负责判断达人是否应该入池，也不负责写 `TK达人池`。

payload 示例:

```json
{
  "creator_identity": {
    "creator_id": "7228697870020199470",
    "uid": "7228697870020199470",
    "unique_id": "anonymousbillionaires",
    "profile_url": "https://www.fastmoss.com/zh/influencer/detail/7228697870020199470"
  },
  "region": "US",
  "detail_level": "profile_metrics_contact_goods",
  "source_context": {
    "workflow_code": "sync_tk_influencer_pool",
    "source_record_id": "recKwc9Y7r",
    "source_table_ref": "feishu://mujitask/TK竞品收集",
    "product_id": "1731194997356205027",
    "holiday": "毕业季",
    "matched_product_sold_count": 72
  },
  "fetch_plan": {
    "date_type": 28,
    "endpoints": ["base_info", "author_index", "stat_info", "contact", "cargo_summary", "goods_list"],
    "goods_list": {
      "page": 1,
      "page_size": 20,
      "max_pages": 5,
      "order": "sold_count,2"
    }
  },
  "relation_policy": {
    "include_source_product_relation": true,
    "min_source_product_sold_count": 50
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

result 示例:

`contact` 字段标准化时按邮箱优先选择；没有邮箱时选择 FastMoss 返回的第一个有效联系方式；没有任何联系方式时 `available=false`，后续飞书写入不覆盖已有联系方式。

```json
{
  "entities": {
    "creators": [
      {
        "entity_key": "fastmoss_creator:7228697870020199470",
        "creator_id": "7228697870020199470",
        "uid": "7228697870020199470",
        "unique_id": "anonymousbillionaires",
        "nickname": "Anonymous Billionaires",
        "avatar_url": "https://cdn.fastmoss.com/avatar.jpg",
        "region": "US",
        "profile_url": "https://www.fastmoss.com/zh/influencer/detail/7228697870020199470",
        "metrics": {
          "follower_count": 128000,
          "aweme_28d_count": 16,
          "video_sale_amount": 32000,
          "live_sale_amount": 0,
          "goods_count": 24,
          "shop_count": 3
        },
        "contact": {
          "raw": "hello@example.com",
          "normalized_text": "hello@example.com",
          "available": true
        }
      }
    ],
    "products": [
      {
        "entity_key": "fastmoss_product:1731194997356205027",
        "product_id": "1731194997356205027",
        "title": "Graduation party decoration set",
        "image_url": "https://cdn.fastmoss.com/product.jpg"
      }
    ],
    "shops": [
      {
        "entity_key": "fastmoss_shop:7496166867916327706",
        "seller_id": "7496166867916327706",
        "shop_name": "Graduation Shop"
      }
    ]
  },
  "relations": [
    {
      "relation_key": "creator_product:7228697870020199470:1731194997356205027",
      "relation_type": "creator_promotes_product",
      "from_entity_key": "fastmoss_creator:7228697870020199470",
      "to_entity_key": "fastmoss_product:1731194997356205027",
      "source": "fastmoss",
      "metrics": {
        "sold_count": 72,
        "sale_amount": 1299,
        "commission_rate": 0.18
      },
      "source_context": {
        "source_record_id": "recKwc9Y7r",
        "holiday": "毕业季"
      }
    }
  ],
  "observations": [
    {
      "entity_key": "fastmoss_creator:7228697870020199470",
      "metric_name": "follower_count",
      "metric_value": 128000,
      "observed_at": "2026-04-24T00:00:00Z",
      "source": "fastmoss"
    },
    {
      "entity_key": "fastmoss_creator:7228697870020199470",
      "metric_name": "video_sale_amount",
      "metric_value": 32000,
      "currency": "USD",
      "window_days": 28,
      "observed_at": "2026-04-24T00:00:00Z",
      "source": "fastmoss"
    }
  ],
  "media_refs": [
    {
      "entity_key": "fastmoss_creator:7228697870020199470",
      "media_type": "avatar",
      "source_url": "https://cdn.fastmoss.com/avatar.jpg"
    },
    {
      "entity_key": "fastmoss_product:1731194997356205027",
      "media_type": "product_image",
      "source_url": "https://cdn.fastmoss.com/product.jpg"
    }
  ],
  "raw_response_refs": [
    "artifact://fastmoss/creator/7228697870020199470/base-info.json",
    "artifact://fastmoss/creator/7228697870020199470/author-contact.json",
    "artifact://fastmoss/creator/7228697870020199470/cargo-summary.json",
    "artifact://fastmoss/creator/7228697870020199470/goods-list-page-1.json"
  ],
  "quality": {
    "contact_available": true,
    "degraded_preview": false,
    "missing_optional_fields": []
  },
  "creator_fact_bundle": {
    "entity_key": "fastmoss_creator:7228697870020199470"
  },
  "product_relations": [
    {
      "relation_key": "creator_product:7228697870020199470:1731194997356205027"
    }
  ]
}
```

`creator_fact_bundle` 和 `product_relations` 是当前 runtime 过渡期的兼容别名；长期消费方应以 `entities`、`relations`、`observations`、`media_refs` 为准。

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
| input | `fact_bundle`、source_job_ids、observation_context |
| output | persisted_entities、persisted_relations、persisted_observations、warnings |
| idempotency | 主体按业务键 upsert，关系按 relation_key upsert，latest upsert，observations/raw 追加或 digest 去重 |
| side effects | Fact DB |

### 6.7.1 Fact projection P0 冻结样例

Fact projection 指 “将采集 handler 输出的标准事实 bundle 写入 Fact DB，并产出后续飞书 mapper 可消费的只读投影上下文”。它不是新的 handler 名称；正式 Runtime job 仍是 `fact_bundle_upsert`。

`fact_bundle_upsert` 不接收事实层 mapper，也不按业务场景分支；TikTok / FastMoss / media handler 或 workflow 必须在调用前产出标准 `fact_bundle`。Feishu 写回仍通过 projection mapper 处理不同表字段。

payload 示例:

```json
{
  "source_job_ids": ["api-job-tiktok-001", "api-job-fastmoss-001"],
  "fact_bundle": {
    "entities": {
      "products": [
        {
          "entity_key": "tiktok_product:1731194997356205027",
          "source": "tiktok",
          "product_id": "1731194997356205027",
          "normalized_product_url": "https://www.tiktok.com/view/product/1731194997356205027",
          "title": "Graduation party decoration set",
          "shop_name": "Graduation Shop",
          "price": {"amount": 12.99, "currency": "USD", "display": "$12.99"}
        }
      ]
    },
    "relations": [
      {
        "relation_key": "same_product:tiktok:1731194997356205027:fastmoss:1731194997356205027",
        "relation_type": "same_product",
        "from_entity_key": "tiktok_product:1731194997356205027",
        "to_entity_key": "fastmoss_product:1731194997356205027"
      }
    ],
    "observations": [
      {
        "entity_key": "fastmoss_product:1731194997356205027",
        "metric_name": "day7_sold_count",
        "metric_value": 412,
        "window_days": 7,
        "observed_at": "2026-04-24T00:00:00Z",
        "source": "fastmoss"
      }
    ],
    "media_refs": [
      {
        "entity_key": "tiktok_product:1731194997356205027",
        "media_type": "product_image",
        "asset_ref": "asset://product/1731194997356205027/main-image"
      }
    ],
    "raw_refs": [
      "artifact://tiktok/product/1731194997356205027/request.json",
      "artifact://fastmoss/product/1731194997356205027/overview.json"
    ]
  },
  "relation_context": {
    "workflow_code": "refresh_current_competitor_table",
    "source_record_id": "recKwc9Y7r",
    "source_table_ref": "feishu://mujitask/TK竞品收集"
  },
  "projection_policy": {
    "emit_competitor_table_projection": true,
    "emit_influencer_pool_projection": false
  }
}
```

result 示例:

```json
{
  "persisted_entities": [
    "tiktok_product:1731194997356205027",
    "fastmoss_product:1731194997356205027"
  ],
  "persisted_relations": [
    "same_product:tiktok:1731194997356205027:fastmoss:1731194997356205027"
  ],
  "persisted_observations": [
    "obs:fastmoss_product:1731194997356205027:day7_sold_count:2026-04-24"
  ],
  "raw_refs": [
    "artifact://fastmoss/product/1731194997356205027/overview.json"
  ],
  "projections": {
    "competitor_table_projection": {
      "projection_type": "competitor_detail_writeback",
      "source_record_id": "recKwc9Y7r",
      "business_entity_key": "product:1731194997356205027",
      "fields": {
        "SKU-ID": "1731194997356205027",
        "标题": "Graduation party decoration set",
        "卖家": "Graduation Shop",
        "价格": "$12.99",
        "Fastmoss价格": "$12.99",
        "近7天销量": "412",
        "记录日期": "2026-04-24"
      },
      "asset_refs": {
        "图片": ["asset://product/1731194997356205027/main-image"],
        "前台截图": ["asset://product/1731194997356205027/tiktok-screenshot"],
        "Fastmoss截图": ["asset://product/1731194997356205027/fastmoss-screenshot"]
      }
    }
  },
  "warnings": []
}
```

约束:

- `fact_bundle_upsert` 可以产出 `projections`，但不直接写 Feishu。
- Projection mapper 可以消费 `projections.*`，也可以直接消费 `entities/relations/observations`；两者字段语义必须保持一致。
- `raw_refs` 和 `artifact://` 引用只用于审计、排障和 `achieve` 对比，不作为业务主键。

## 7. Business Handler

Business handler 负责有独立运行价值的业务动作。它可以调用 capability handler，也可以消费 capability handler 的 result。

业务 handler 不是默认准入。只有当它满足独立 retry / timeout / artifact / 外部副作用 / 审计需求，并且已经进入第 9 节“Handler Registry 唯一准入清单”时，才允许作为 Runtime job 的 `handler_code`。

候选 business handlers:

| Handler | 作用 | 当前准入建议 |
| --- | --- | --- |
| `influencer_pool_candidate_select` | 从竞品表源行筛选达人同步候选，并 fan-out product jobs | 当前不准入；优先由 `feishu_table_read` + `influencer_pool_source_adapter` + workflow dispatcher 表达 |
| `product_creator_discovery` | 每个竞品商品 1 个商品达人发现 job，内部复用 FastMoss 商品达人列表能力，输出 normalized creator candidates + product hit context | 准入；用于 `sync_tk_influencer_pool.discover_related_creators` |
| `influencer_creator_sync` | 每个 unique 达人 1 个达人同步 job，内部完成达人详情、事实入库、素材同步、达人池飞书 upsert，并在商品 group 终态时写回该商品达人查找状态 | 准入；用于 `sync_tk_influencer_pool.sync_influencer_pool` |
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
| API | `keyword_seed_import` | `api_worker` | `api_worker_job` | Business | 关键词竞品入库前半段，串行复用 FastMoss search 与飞书 seed 写入 | workflow-competitor-table-design 7.3 |
| API | `product_creator_discovery` | `api_worker` | `api_worker_job` | Business | 达人池同步商品发现 job；每个竞品商品 1 个，内部复用 FastMoss 商品达人列表并输出 normalized creator candidates | workflow-influencer-pool-sync-design 11.2 |
| API | `influencer_creator_sync` | `api_worker` | `api_worker_job` | Business | 达人池同步达人 job；每个 unique 达人 1 个，内部完成达人详情、事实入库、达人池飞书 upsert 和商品终态状态回写 | workflow-influencer-pool-sync-design 11.3 |
| API | `tiktok_product_request_fetch` | `api_worker` | `api_worker_job` | Capability | TikTok 商品 request-first 采集 | 6.1 |
| Browser | `tiktok_product_browser_fetch` | `browser_worker` | `task_execution` | Capability | TikTok 商品 request 失败后的浏览器兜底采集 | 6.2 |
| Browser | `fastmoss_security_browser_resolve` | `browser_worker` | `task_execution` | Capability | FastMoss API 风控后的浏览器解滑块与 cookie cache 刷新 | 6.2.1 |
| API | `fastmoss_product_search` | `api_worker` | `api_worker_job` | Capability | FastMoss 商品搜索，支持 keyword/filter/condition | 6.3 |
| API | `fastmoss_product_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 商品事实、店铺事实、商品指标采集 | 6.4 |
| API | `fastmoss_creator_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 达人事实和指标采集 | 6.5 |
| API | `fastmoss_shop_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 店铺事实和指标采集 | 6.5 |
| API | `fastmoss_video_fetch` | `api_worker` | `api_worker_job` | Capability | FastMoss 视频事实和指标采集 | 6.5 |
| API | `media_asset_sync` | `api_worker` | `api_worker_job` | Capability | 图片、头像、封面等媒体资产同步到对象存储和事实索引 | 6.6 |
| API | `fact_bundle_upsert` | `api_worker` | `api_worker_job` | Capability | normalized entities / relations / observations 统一写入 Fact DB | 6.7 |
| Outbox | `outbox_dispatch` | `outbox_dispatcher` | `notification_outbox` | System | 发送最终通知和任务摘要，不参与业务事实采集 | 当前系统架构 / outbox |

#### 9.1.1 `outbox_dispatch` Channel Contract

`outbox_dispatch` 是唯一准入的通知发送 handler。workflow 和业务 flow 只能写入 `notification_outbox`，不能直接调用飞书、OpenClaw 或 webhook。

| Channel | 行为 |
| --- | --- |
| `noop` / `disabled` | 明确跳过通知，返回 `delivery_state=skipped`。 |
| `stdout` / `console` | 本地输出消息；允许显式 `dry_run=true` 演练。 |
| `webhook` | 使用 `payload_json.webhook_url` POST。HTTP/network retryable 失败不得标 sent。 |
| `feishu_bot_api` / `feishu_direct_api` | 使用飞书 OpenAPI 发送 text message。账号由 `reply_target.accountId` 或默认账号选择。 |
| `openclaw_message` / `feishu_openclaw` | 调用 OpenClaw CLI `message send`，用于客户现场已有 OpenClaw 通道配置。 |

Feishu 账号配置优先级:

1. `MUJITASK_FEISHU_ACCOUNTS_JSON`
2. `MUJITASK_FEISHU_ACCOUNTS_FILE`
3. `OPENCLAW_CONFIG_PATH` 或 `~/.openclaw/openclaw.json`

`reply_target` 可以是 JSON object、Python dict repr 或简写字符串。结构化格式推荐:

```json
{"channel":"feishu","to":"user:ou_xxx","accountId":"default"}
```

handler 必须遵守:

- 未支持的 `channel_code` 必须失败，不能因为 `dry_run` 被模拟为成功。
- 真实通道只有外部系统确认成功后才返回 success。
- 配置缺失、接收目标缺失、CLI 缺失是 terminal failure。
- 网络超时、HTTP 5xx、飞书接口临时失败是 retryable failure。
- result、progress details、日志不得包含 `appSecret` 或 access token。

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

### 9.4 项目目录结构

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
