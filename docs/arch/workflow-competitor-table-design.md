# 竞品表 Workflow 设计

日期: 2026-04-23

## 1. 流程定位

竞品表相关流程当前主要包含两类:

- `refresh_current_competitor_table`: 补全/刷新当前 `TK竞品收集` 中待处理记录。
- `search_keyword_competitor_products`: 按关键词在 FastMoss 搜索竞品，插入飞书种子行，再补全详情。

这两类都属于 `TK竞品收集` 的运营主表维护流程。重构后它们不再依赖业务专用的单行补全或种子行写入 handler，而是统一使用通用表读写能力和行级 pipeline 能力:

- `feishu_table_read`
- `feishu_table_write`
- `fastmoss_product_search`
- `competitor_row_refresh`
- `tiktok_product_browser_fetch`

其中 `competitor_row_refresh` 是一条竞品记录的行级主 job，内部串行调用 TikTok request、media sync、FastMoss product fetch、Fact DB upsert 和飞书写回能力。`tiktok_product_browser_fetch` 只在行级主 job 确认需要浏览器兜底时作为 child `task_execution` 创建。

本 workflow 只决定竞品表来源行如何筛选、每行采集什么商品、以及最终写回 `TK竞品收集` 的哪些字段。商品、店铺、媒体、FastMoss 指标、关系和 raw response 的事实入库必须遵守 [fact-db-schema-design.md](./fact-db-schema-design.md) 与 [workflow-design-guidelines.md](./workflow-design-guidelines.md) 的统一事实采集 contract，不能在竞品表流程里另写一套私有事实写入逻辑；商品媒体物化边界同时受 `contracts/facts/product-fact-collection.yaml` 约束。

## 2. Task

| Task | 当前 task_code | 入口类 | 作用 |
| --- | --- | --- | --- |
| 竞品表刷新 | `refresh_current_competitor_table` | `RefreshCurrentCompetitorTableTask` | 读取竞品候选行，采集商品事实，写回竞品表投影 |
| 关键词竞品入库 | `search_keyword_competitor_products` | `SearchKeywordCompetitorProductsTask` | FastMoss 商品 API 搜索，通过通用飞书写入创建种子行，再采集商品事实并写回投影 |

## 3. Workflow: 竞品表刷新

目标 workflow_code 为 `refresh_current_competitor_table`。当前代码中的历史 `WorkflowSpec` ID 可以作为兼容实现事实保留，但目标 Runtime workflow contract 不在 code 名称中追加版本后缀。

```mermaid
flowchart TD
    A["Task: refresh_current_competitor_table"] --> B["submit_refresh_request"]
    B --> C["read_competitor_rows<br/>feishu_table_read"]
    C --> D["dispatch_row_refresh_jobs"]
    D --> E["competitor_row_refresh<br/>one job per Feishu row"]
    E --> F["TikTok request"]
    F --> G{"明确需要 browser fallback?"}
    G -->|是| H["tiktok_product_browser_fetch<br/>child task_execution"]
    G -->|否| I["media sync"]
    H --> I
    I --> J["FastMoss fetch"]
    J --> K["Fact DB upsert"]
    K --> L["Feishu writeback"]
    L --> M["ready_for_summary"]
    M --> N["notification_outbox"]
```

### 3.1 Stage 设计

| Stage code | 作用 | Runtime 表 |
| --- | --- | --- |
| `submitted` | 创建顶层 `task_request` | `task_request` |
| `read_competitor_rows` | 读取 `TK竞品收集`，只输出 12 个自动维护字段存在空值且未被跳过的候选行 | `api_worker_job` |
| `dispatch_row_refresh_jobs` | 根据候选行创建行级采集 job；每条飞书记录最多创建一个 `competitor_row_refresh` | `task_request` |
| `refresh_competitor_rows` | 串行消费行级 pipeline job；job 内部完成 TikTok request、必要 browser fallback、media sync、FastMoss、Fact DB、飞书写回 | `api_worker_job` / `task_execution` |
| `ready_for_summary` | executor 汇总所有行结果并写通知 outbox | `task_request` / `notification_outbox` |

### 3.2 Job / Handler / Flow

| Job | item_code / job_code | Worker | Handler | Flow / Mapper |
| --- | --- | --- | --- | --- |
| 竞品表读取 | `feishu_table_read` | `api_worker` | `feishu_table_read` | `competitor_table_source_adapter` |
| 行级竞品刷新 | `competitor_row_refresh` | `api_worker` | `competitor_row_refresh` | TikTok request flow -> media sync -> FastMoss product flow -> fact upsert -> `competitor_table_projection_mapper` |
| TikTok browser fallback | `tiktok_product_browser_fetch` | `browser_worker` | `tiktok_product_browser_fetch` | 只由 `competitor_row_refresh` 在明确需要 fallback 时创建并等待 |
| 通知发送 | outbox message | `outbox_dispatcher` | `outbox_dispatch` | 飞书/OpenClaw/console 发送 |

`competitor_row_refresh` 是后续实现的目标行级 job_code。TikTok request、media sync、FastMoss fetch、Fact DB upsert 和飞书写回是该 job 内部步骤，不再作为同一条飞书记录的并行 sibling jobs。browser fallback 仍使用独立 `task_execution`，因为它需要独占 browser profile 资源，但它必须引用当前行级 job 和触发 fallback 的 TikTok request attempt。

TikTok request 必须实际发起并写入 attempt 证据。只有返回明确风控、登录、验证码、访问受限或缺少商品详情脚本时，`competitor_row_refresh` 才能创建 `tiktok_product_browser_fetch` 子执行。商品不可访问、已下架或区域不可售是 request 阶段可判定的终态，应直接写回 `商品状态=已下架/区域不可售` 并停止该行后续 browser fallback、媒体同步和 FastMoss 补齐；普通网络失败、超时、5xx、429 或代理临时异常先按 retry policy 重试，不能直接 fallback。

### 3.2.1 反面教材: 不要按 Handler / API 调用粒度拆 Job

这个 workflow 必须明确一条红线: handler 不是 job 颗粒度，API 调用更不是 job 颗粒度。

错误拆法:

- 因为已经有 `tiktok_product_request_fetch`、`fastmoss_product_fetch`、`media_asset_sync`、`fact_bundle_upsert`、`feishu_table_write` 这些现成 handler，就把它们各自提升成同一行记录的 sibling jobs。
- 因为希望观察每一次 API 调用结果，就把 TikTok request、FastMoss request、媒体同步、Fact DB 写入、飞书写回分别入队。
- 最终形成 `候选记录数 x 内部步骤数` 的 fan-out 队列模型。

这个错误在竞品表刷新里已经出现过一次，可以作为反面教材:

- 飞书实际读出 `68` 条候选记录。
- 旧模型同时派发 `68` 条 `tiktok_product_request_fetch` 和 `68` 条 `fastmoss_product_fetch`。
- 再加 `1` 条 `feishu_table_read`，子 job 总数达到 `137`；连同父 `task_request`，运行时看到的是 `138` 条记录。

为什么这是错误设计:

- 队列资源占用不再由业务筛选结果控制，而是被内部 API 步骤数放大。
- 同一行记录的严格执行顺序被拆散，队列只能看到一堆同层级 sibling jobs，很难表达“这一条记录的一次刷新”。
- browser fallback、media sync、Fact DB、飞书写回都要靠跨 job 拼接上下文，失败恢复和审计都变得脆弱。
- 同一行的重试、延迟、风控证据和最终结果被切碎，验收时无法直接回答“这条飞书记录到底完整跑到了哪一步”。

因此，本流程的正确约束是:

- 一条候选飞书记录最多创建一个 `competitor_row_refresh` 主 job。
- TikTok request、media sync、FastMoss、Fact DB upsert、飞书写回都是该主 job 的内部步骤，不得再按 API 调用粒度拆成 sibling jobs。
- 只有 `tiktok_product_browser_fetch` 这种确实需要独立 browser 资源生命周期的步骤，才允许作为 child `task_execution` 从主 job 内派生。
- 行级主 job 可以决定执行顺序和 fallback，但不能改变 TikTok / FastMoss / media / Fact DB 的统一事实 contract。
- 飞书写回 mapper 只负责业务投影字段，不负责事实入库。

### 3.2.2 竞品表 Adapter / Common 边界

本流程的飞书来源表业务语义由 `competitor_table_source_adapter` 承担，不能散落到 `common` helper、handler registry 或 skill submit 参数中。

`competitor_table_source_adapter` 必须内聚以下默认业务规则:

- `TK竞品收集` 的 12 个自动维护字段定义。
- 商品身份提取规则，例如 `SKU-ID` 在本流程中映射为商品 ID / `product_id`。
- 候选判断规则，即“只有 12 个自动维护字段存在空值的记录才进入刷新候选集”。
- `商品状态 = 已下架/区域不可售` 的跳过规则。
- 空行、坏行、重复行的丢弃与去重规则。
- `source_rows`、`candidate_keys`、`writeback_context`、`adapter_summary` 的构造规则。

`competitor_table_projection_mapper` 必须内聚以下目标表写回规则:

- 12 个自动维护字段的写回映射。
- 哪些字段允许系统覆盖，哪些字段默认不覆盖人工值。
- `商品状态` 不属于 12 个自动维护字段，也不参与 pending 判断。
- `商品状态` 属于系统状态投影；当商品明确不可访问、已下架或区域不可售时，mapper 必须允许写回 `商品状态=已下架/区域不可售`。
- `商品状态` 是系统覆盖字段，不属于人工保留字段；人工修改 `产品链接` 或 `SKU-ID` 后，需要人工清空该字段才重新进入抓取。

`feishu_table_read` / `feishu_table_write` 及其 `common` helper 只负责:

- `table_url` / `view_id` 解析。
- Feishu API 读写、分页、schema 校验和错误分类。
- 原始记录标准化和通用结果 envelope。

它们不负责:

- 定义竞品表的 12 个自动维护字段。
- 定义 `已下架/区域不可售` 的业务跳过语义。
- 决定一行是否属于待刷新候选。
- 决定竞品表写回时哪些字段属于系统默认覆盖。

因此，`refresh_current_competitor_table` 的外部入口只应提供真正可变的运行输入，例如 `table_url`、认证上下文、显式指定的 `record_ids` 或运营批准的强制重刷选项。像 `candidate_policy = missing_auto_maintained_fields` 这类用于启用默认竞品筛选语义的内部 mode，不应成为 skill / CLI 调用方必须传入的前置条件。若未来允许 override，workflow 文档必须单独列出允许的 override 项、默认值和缺省行为。

### 3.3 进程间调度时序图

本图只表达竞品表刷新在进程间如何调度，不展开 source adapter、projection mapper 或 handler 内部函数。行内普通 API 调用由 `competitor_row_refresh` 串行执行，不再由 executor 一次性拆出 TikTok / FastMoss / media / fact / writeback sibling jobs。

```mermaid
sequenceDiagram
    participant Entry as Entry
    participant DB as Runtime DB
    participant Exec as executor_daemon
    participant API as api_worker
    participant Browser as browser_worker
    participant Feishu as Feishu
    participant Fact as Fact DB
    participant Obj as MinIO
    participant Outbox as outbox_dispatcher

    Entry->>DB: insert task_request(refresh_current_competitor_table)
    Exec->>DB: claim task_request
    Exec->>DB: enqueue api_worker_job(feishu_table_read)
    API->>DB: claim feishu_table_read
    API->>Feishu: read TK competitor rows
    API->>DB: store rows missing one of 12 auto-maintained fields
    API->>DB: mark read job success
    Exec->>DB: enqueue one competitor_row_refresh per candidate row
    API->>DB: claim competitor_row_refresh in FIFO order
    API->>DB: record TikTok request attempt evidence
    alt browser fallback required
        API->>DB: enqueue task_execution(tiktok_product_browser_fetch)
        Browser->>DB: claim task_execution
        Browser->>Obj: store page / network artifacts
        Browser->>DB: mark browser result terminal
        API->>DB: resume competitor_row_refresh after browser result
    end
    API->>Obj: sync current row media assets when needed
    API->>DB: fetch current row FastMoss product data
    API->>Fact: upsert product facts and observations
    API->>Feishu: write current row projection
    API->>DB: mark competitor_row_refresh terminal
    Exec->>DB: finalize task_request and insert notification_outbox
    Outbox->>DB: claim notification_outbox
    Outbox->>Entry: send summary
```

### 3.4 状态收敛

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: executor claim
    running --> waiting_children: dispatch row refresh jobs
    waiting_children --> ready_for_summary: competitor_row_refresh jobs 全部终态
    ready_for_summary --> success: executor finalize
    success --> [*]
```

## 4. Workflow: 关键词竞品入库

目标 workflow_code 为 `search_keyword_competitor_products`。

```mermaid
flowchart TD
    A["Task: search_keyword_competitor_products"] --> B["submit_keyword_request"]
    B --> C["keyword_seed_import<br/>FastMoss search + seed write"]
    C --> D["dispatch_row_refresh_jobs"]
    D --> E["competitor_row_refresh<br/>same row-level pipeline"]
    E --> F["ready_for_summary"]
    F --> G["notification_outbox"]
```

### 4.1 Stage 设计

| Stage code | 作用 | Runtime 表 |
| --- | --- | --- |
| `submitted` | 创建顶层 `task_request` | `task_request` |
| `keyword_seed_import` | 根据结构化关键词/filter 生成 FastMoss 搜索参数，调用通用搜索能力，按返回的 normalized candidates 顺序写入竞品种子行 | `api_worker_job` |
| `dispatch_row_refresh_jobs` | 根据成功 seed rows 创建行级采集 job | `task_request` |
| `refresh_competitor_rows` | 使用与竞品表刷新一致的 `competitor_row_refresh` 行级 pipeline 补齐详情 | `api_worker_job` / `task_execution` |
| `ready_for_summary` | 汇总搜索、种子写入、商品采集和详情写回结果，并写通知 outbox | `task_request` / `notification_outbox` |

### 4.2 Job / Handler / Flow

| Job | item_code / job_code | Worker | Handler | Flow / Mapper |
| --- | --- | --- | --- | --- |
| 关键词种子入库 | `keyword_seed_import` | `api_worker` | `keyword_seed_import` | `keyword_search_parameter_mapper` -> `fastmoss_product_search` -> candidate iteration -> `feishu_table_write` + `competitor_seed_projection_mapper` |
| 行级竞品刷新 | `competitor_row_refresh` | `api_worker` | `competitor_row_refresh` | 与竞品表刷新相同的行级 pipeline |
| TikTok browser fallback | `tiktok_product_browser_fetch` | `browser_worker` | `tiktok_product_browser_fetch` | 只由行级 pipeline 在明确需要 fallback 时创建并等待 |
| 通知发送 | outbox message | `outbox_dispatcher` | `outbox_dispatch` | 飞书/OpenClaw/console 发送 |

`keyword_seed_import` 是关键词入库前半段的业务 job。它不是新的 FastMoss 专用搜索能力，也不是新的飞书表格写入能力；它只负责把一次业务搜索请求串行编排为:

1. 使用 `keyword_search_parameter_mapper` 把结构化业务条件映射为 `fastmoss_product_search` payload。
2. 调用通用 `fastmoss_product_search`，取得 normalized candidates。
3. 按 candidates 顺序逐条调用 `feishu_table_write`，并指定 `competitor_seed_projection_mapper`、`write_mode=insert_if_absent`；候选写入之间默认间隔 1 秒。
4. 记录每条 candidate 的 seed write 结果，包括 `success`、`skip_existing`、`failed` 和失败原因。

`fastmoss_product_search` 的原始响应只作为排障证据保存，不直接进入竞品表 mapper。search 结果已经是 normalized candidates，因此本 workflow 不再单独定义 search result mapper；业务 job 只按返回顺序逐条调用种子写入。种子写入的已存在判断沿用 `competitor_seed_projection_mapper` 输出的 `upsert_key`: 优先使用 `SKU-ID` / `product_id`，缺少 product_id 时才按标准化 `产品链接` 兜底。

`fastmoss_product_search` 真实翻页请求之间默认间隔 1 秒，避免连续请求触发 FastMoss 风控。

### 4.3 进程间调度时序图

本图只表达关键词竞品入库在进程间如何调度，不展开 FastMoss 搜索条件解析、候选过滤或飞书字段映射。

```mermaid
sequenceDiagram
    participant Entry as Entry
    participant DB as Runtime DB
    participant Exec as executor_daemon
    participant API as api_worker
    participant Browser as browser_worker
    participant Feishu as Feishu
    participant Fact as Fact DB
    participant Obj as MinIO
    participant Outbox as outbox_dispatcher

    Entry->>DB: insert task_request(search_keyword_competitor_products)
    Exec->>DB: claim task_request
    Exec->>DB: enqueue api_worker_job(keyword_seed_import)
    API->>DB: claim keyword_seed_import
    API->>DB: map structured filters to FastMoss search parameters
    API->>DB: call fastmoss_product_search and store normalized candidates
    loop each normalized candidate in order
        API->>Feishu: feishu_table_write with competitor_seed_projection_mapper
        API->>DB: record seed write result
    end
    API->>DB: mark keyword_seed_import terminal
    Exec->>DB: enqueue one competitor_row_refresh per successful seed row
    API->>DB: claim competitor_row_refresh in FIFO order
    API->>DB: record TikTok request attempt evidence
    alt browser fallback required
        API->>DB: enqueue task_execution(tiktok_product_browser_fetch)
        Browser->>DB: claim task_execution
        Browser->>Obj: store page / network artifacts
        Browser->>DB: mark browser result terminal
        API->>DB: resume competitor_row_refresh after browser result
    end
    API->>Obj: sync current row media assets when needed
    API->>DB: fetch current row FastMoss product data
    API->>Fact: upsert product facts and observations
    API->>Feishu: write competitor detail projection
    API->>DB: mark competitor_row_refresh terminal
    Exec->>DB: finalize task_request and insert notification_outbox
    Outbox->>DB: claim notification_outbox
    Outbox->>Entry: send summary
```

## 5. 竞品表流程的 Job 颗粒度

竞品表刷新和关键词入库都不应该把整张表作为一个超大 job 执行，也不应该把同一条竞品记录机械拆成多个并行 API job。目标颗粒度是:

- 顶层 task 表示一次用户请求。
- `keyword_seed_import` / `read_competitor_rows` 是阶段性 job 或编排动作，只负责产生候选行和可继续处理的飞书行。
- 每条待处理竞品记录创建一个 `competitor_row_refresh` 行级主 job。
- `competitor_row_refresh` 内部按固定顺序串行执行 TikTok request、必要 browser fallback、media sync、FastMoss fetch、Fact DB upsert、飞书写回。
- browser fallback 是当前行级 job 派生并等待的子 `task_execution`，不是与当前行并行推进的 sibling job。
- `competitor_row_refresh` 绑定串行 queue lane，按 `available_at` / `queue_seq` / `created_at` FIFO claim；同一 lane 同一时刻最多一个 running job。
- TikTok、FastMoss 和飞书外部请求之间必须记录 request start/end、delay / cooldown 和 fallback reason 等 runtime evidence。
- 父 task 基于所有子 job 状态汇总。

这样可以做到:

- 单行失败不拖垮整张表。
- 单行可独立重试。
- 同一行的 TikTok / FastMoss / 飞书请求顺序可审计，不会因为 worker 并发乱序。
- 默认走 request/API；浏览器 profile 只在 TikTok product fallback 时使用。
- 最终 summary 可以保留每行成功/失败/跳过状态。

## 6. 与选品分析、达人同步的关系

竞品表是当前商品运营主表:

- 选品分析可以将商品采集结果写回 `TK选品收集`，也可以通过字段映射与竞品表联动。
- 达人同步以 `TK竞品收集` 作为来源表，从竞品商品出发生成达人发现和达人详情 job。
- 竞品表刷新维护商品基础数据质量，达人同步维护商品到达人池的关系沉淀。

## 7. P0 Contract Payload / Result 样例

本节只冻结 workflow 与通用 handler/mapper 的边界，不要求 P0 实现真实 handler。

### 7.1 竞品表刷新: `feishu_table_read`

stage: `read_competitor_rows`

payload:

```json
{
  "request_id": "req-refresh-001",
  "task_code": "refresh_current_competitor_table",
  "workflow_code": "refresh_current_competitor_table",
  "stage_code": "read_competitor_rows",
  "source_table_ref": "feishu://mujitask/TK竞品收集",
  "field_names": ["产品链接", "SKU-ID", "图片", "标题", "节日", "卖家", "价格", "Fastmoss价格", "昨日销量", "近7天销量", "近90天销量", "记录日期", "商品状态"],
  "filter_spec": {
    "candidate_policy": "missing_auto_maintained_fields",
    "skip_product_status": ["已下架/区域不可售"]
  },
  "adapter_code": "competitor_table_source_adapter",
  "snapshot_policy": {
    "store_raw_rows": true
  }
}
```

说明:

- 上述 `field_names` 和 `filter_spec` 是 `refresh_current_competitor_table` 在 `read_competitor_rows` stage 传给 `feishu_table_read` 的有效 payload，不代表外部 skill / CLI 必须显式提交这些内部筛选参数。
- 这些默认值表达的是竞品表 workflow 的固定业务语义，应由 workflow 或 `competitor_table_source_adapter` 在内部保证稳定生效；外部入口缺省时不能静默退化成“读取整表后不过滤”的另一套语义。

result:

```json
{
  "source_rows": [
    {
      "source_record_id": "recRefresh001",
      "source_table_ref": "feishu://mujitask/TK竞品收集",
      "product_identity": {
        "product_id": "1731194997356205027",
        "product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
        "normalized_product_url": "https://www.tiktok.com/view/product/1731194997356205027",
        "fastmoss_product_url": "https://www.fastmoss.com/zh/e-commerce/detail/1731194997356205027"
      },
      "missing_auto_fields": ["Fastmoss价格", "近7天销量", "近90天销量"],
      "writeback_context": {
        "target_table_ref": "feishu://mujitask/TK竞品收集",
        "record_id": "recRefresh001"
      },
      "source_snapshot_ref": "artifact://feishu/competitor/read/req-refresh-001/recRefresh001.json"
    }
  ],
  "candidate_keys": ["product:1731194997356205027"],
  "adapter_summary": {
    "input_row_count": 49,
    "source_row_count": 1,
    "skipped_complete_count": 41,
    "skipped_unavailable_count": 7
  }
}
```

### 7.2 竞品表刷新: Fact projection 到详情写回 / `competitor_row_refresh`

`competitor_row_refresh` 是单条竞品记录的主 job。它内部串行完成 TikTok request、必要 browser fallback、media sync、FastMoss fetch、Fact DB upsert 和飞书写回，只对外产出一个行级执行结果。截图可以作为内部 artifact 保存，但 `前台截图`、`Fastmoss截图` 不属于 12 个自动维护字段，也不参与待更新判断。

job result:

```json
{
  "source_record_id": "recRefresh001",
  "job_code": "competitor_row_refresh",
  "business_entity_key": "product:1731194997356205027",
  "step_timeline": [
    {
      "step": "tiktok_request",
      "status": "success",
      "attempted": true,
      "http_status": 200,
      "fallback_required": false,
      "fallback_reason": ""
    },
    {
      "step": "media_sync",
      "status": "success"
    },
    {
      "step": "fastmoss_fetch",
      "status": "success"
    },
    {
      "step": "fact_db_upsert",
      "status": "success"
    },
    {
      "step": "feishu_writeback",
      "status": "success"
    }
  ],
  "fact_upsert": {
    "persisted_entities": [
      "tiktok_product:1731194997356205027",
      "fastmoss_product:1731194997356205027"
    ],
    "persisted_observations": [
      "obs:fastmoss_product:1731194997356205027:day7_sold_count:2026-04-24"
    ]
  },
  "writeback_projection": {
    "fields": {
      "产品链接": {
        "text": "https://www.tiktok.com/shop/pdp/1731194997356205027",
        "link": "https://www.tiktok.com/shop/pdp/1731194997356205027"
      },
      "SKU-ID": "1731194997356205027",
      "图片": ["asset://product/1731194997356205027/main-image"],
      "标题": "Graduation party decoration set",
      "节日": "Graduation",
      "卖家": "Graduation Shop",
      "价格": "$12.99",
      "Fastmoss价格": "$12.99",
      "昨日销量": "38",
      "近7天销量": "412",
      "近90天销量": "2310",
      "记录日期": "2026-04-24"
    }
  },
  "runtime_evidence": {
    "created_browser_fallback": false,
    "browser_child_execution_id": "",
    "fallback_reason": "",
    "api_lane": "competitor_row_refresh",
    "claim_order": 1
  }
}
```

writeback payload:

```json
{
  "target_table_ref": "feishu://mujitask/TK竞品收集",
  "write_mode": "update_missing_auto_fields",
  "mapper_code": "competitor_table_projection_mapper",
  "records": [
    {
      "op": "update",
      "record_id": "recRefresh001",
      "business_entity_key": "product:1731194997356205027",
      "fields": {
        "产品链接": {
          "text": "https://www.tiktok.com/shop/pdp/1731194997356205027",
          "link": "https://www.tiktok.com/shop/pdp/1731194997356205027"
        },
        "SKU-ID": "1731194997356205027",
        "图片": ["asset://product/1731194997356205027/main-image"],
        "标题": "Graduation party decoration set",
        "节日": "Graduation",
        "卖家": "Graduation Shop",
        "价格": "$12.99",
        "Fastmoss价格": "$12.99",
        "昨日销量": "38",
        "近7天销量": "412",
        "近90天销量": "2310",
        "记录日期": "2026-04-24"
      },
      "source_context": {
        "source_record_id": "recRefresh001",
        "projection_type": "competitor_detail_writeback"
      }
    }
  ]
}
```

### 7.3 关键词竞品入库: `keyword_seed_import`

stage: `keyword_seed_import`

`keyword_seed_import` 是关键词入库前半段的业务 job。它接收结构化业务搜索条件，内部先通过 `keyword_search_parameter_mapper` 生成通用 `fastmoss_product_search` 参数，再按搜索返回的 normalized candidates 顺序调用 `feishu_table_write` 写入种子行。飞书表写入不复用 search mapper，而是通过 `competitor_seed_projection_mapper` 把单条 normalized candidate 映射成 seed write record。

payload:

```json
{
  "request_id": "req-keyword-001",
  "task_code": "search_keyword_competitor_products",
  "workflow_code": "search_keyword_competitor_products",
  "stage_code": "keyword_seed_import",
  "source_table_ref": "feishu://mujitask/TK竞品收集",
  "search_request": {
    "search_mode": "keyword",
    "keyword": "Halloween decoration",
    "region": "US",
    "filters": {
      "sales_range": {
        "window_days": 7,
        "min": 200,
        "max": null
      }
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
      "dedupe_by": ["product_id", "normalized_product_url"],
      "business_conditions": {
        "min_day7_sold_count": 200
      }
    }
  },
  "seed_write": {
    "target_table_ref": "feishu://mujitask/TK竞品收集",
    "write_mode": "insert_if_absent",
    "mapper_code": "competitor_seed_projection_mapper"
  },
  "mapper_refs": {
    "search_parameter_mapper": "keyword_search_parameter_mapper",
    "seed_write_mapper": "competitor_seed_projection_mapper"
  }
}
```

result:

```json
{
  "search_parameters": {
    "handler_code": "fastmoss_product_search",
    "search_mode": "keyword",
    "keyword": "Halloween decoration",
    "region": "US",
    "sort": {
      "field": "day7_sold_count",
      "direction": "desc",
      "source_order": "2,2"
    }
  },
  "normalized_candidates": [
    {
      "source": "fastmoss",
      "product_id": "1731194997356205027",
      "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027",
      "fastmoss_product_url": "https://www.fastmoss.com/zh/e-commerce/detail/1731194997356205027",
      "title": "Halloween decoration",
      "image_url": "https://cdn.fastmoss.com/product.jpg",
      "metrics": {
        "day7_sold_count": 412,
        "sold_count": 2310,
        "relate_author_count": 35
      },
      "matched_conditions": {
        "min_day7_sold_count": true
      },
      "dedupe_keys": {
        "product_id": "1731194997356205027",
        "normalized_product_url": "https://www.tiktok.com/shop/pdp/1731194997356205027"
      },
      "quality_score": 1.0,
      "raw_item_ref": ""
    }
  ],
  "search_summary": {
    "candidate_count": 1,
    "applied": {
      "min_day7_sold_count": 1
    },
    "rejected_count": 0
  },
  "pagination": {
    "page": 1,
    "has_more": true,
    "next_page": 2
  },
  "raw_response_ref": "artifact://fastmoss/search/req-keyword-001/page-1.json",
  "seed_write_records": [
    {
      "op": "insert_if_absent",
      "business_entity_key": "product:1731194997356205027",
      "upsert_key": {
        "field": "SKU-ID",
        "value": "1731194997356205027"
      },
      "fields": {
        "SKU-ID": "1731194997356205027",
        "产品链接": {
          "text": "https://www.tiktok.com/shop/pdp/1731194997356205027",
          "link": "https://www.tiktok.com/shop/pdp/1731194997356205027"
        },
        "备注": "通过搜索关键字：Halloween decoration"
      },
      "source_context": {
        "keyword": "Halloween decoration",
        "search_candidate_rank": 1,
        "fastmoss_product_url": "https://www.fastmoss.com/zh/e-commerce/detail/1731194997356205027"
      }
    }
  ],
  "seed_write_results": [
    {
      "business_entity_key": "product:1731194997356205027",
      "product_id": "1731194997356205027",
      "record_id": "recSeed001",
      "op": "append",
      "status": "success"
    }
  ],
  "written_count": 1,
  "skipped_count": 0,
  "failed_count": 0,
  "target_record_ids": ["recSeed001"],
  "writeback_context": {
    "seed_record_id_by_product_id": {
      "1731194997356205027": "recSeed001"
    }
  }
}
```

### 7.4 关键词竞品入库: 种子写入 mapper

search 参数映射和飞书写表映射是两个边界:

- `keyword_search_parameter_mapper`: 业务 filter / 关键词 -> `fastmoss_product_search` parameters。
- `competitor_seed_projection_mapper`: 单条 normalized candidate + 关键词上下文 -> `feishu_table_write` 单次写入 record。

`keyword_seed_import` 内部调用 `feishu_table_write` 时必须指定 `competitor_seed_projection_mapper`。该 mapper 的输入是一条 normalized candidate 加上关键词来源上下文，输出是一条可写入 `TK竞品收集` 的 seed write record。

mapper 输出必须满足:

- `op=insert_if_absent`
- `fields.SKU-ID={product_id}`
- `fields.产品链接` 为标准化 TikTok 商品链接
- `fields.备注=通过搜索关键字：{关键词}`
- `upsert_key` 优先使用 `SKU-ID`；缺少 product_id 时才使用标准化 `产品链接`
