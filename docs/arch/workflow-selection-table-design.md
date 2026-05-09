# 选品采集与关键词搜索选品写入 Workflow 设计

日期: 2026-04-30

状态: 当前架构设计文档

## 1. 流程定位

本设计是 `TK选品收集` 的 workflow 设计事实源，覆盖两类正式流程：

- 选品采集（`tiktok_fastmoss_product_ingest`）：扫描或手动提交选品表商品，补齐 `TK选品收集` 自动维护字段。
- 关键词搜索选品写入（`search_keyword_selection_products`）：按关键词搜索 FastMoss 商品，新增选品种子行，再复用同一行级采集 pipeline 补齐详情。

每一条选品表候选行只创建一个行级主 job（`selection_row_refresh`），内部串行完成 TikTok 采集 → FastMoss 采集 → 事实入库 → 飞书写回。`search_keyword_selection_products` 的详情补齐阶段必须复用这条行级 pipeline，不能另写一套 fallback 或 summary 规则。

当前设计按必填补全字段、系统运行字段和可选补充字段分层，补齐 URL 验证、必填字段缺失扫描、父体数据写入、图表渲染等能力。

关联需求文档:

- [../../business/requirements/tk-selection-collection.md](../../business/requirements/tk-selection-collection.md)
- [../../business/requirements/search-keyword-selection-products.md](../../business/requirements/search-keyword-selection-products.md)

## 2. Task

| task_code | 作用 | 执行形态 |
| --- | --- | --- |
| `tiktok_fastmoss_product_ingest` | 选品采集 | 读取候选行或 direct ingest，进入 `selection_row_refresh` 行级 pipeline |
| `search_keyword_selection_products` | 关键词搜索选品写入 | `keyword_seed_import` 新增种子行后，只对新增成功行派发 `selection_row_refresh` |

共同运行边界:

- 顶层表: `task_request`
- 编排者: `executor_daemon`
- 执行 worker: `api_worker`（主），`browser_worker`（TikTok/FastMoss fallback）
- 最终结果: 商品事实、FastMoss 数据、媒体资产、选品字段分层飞书写回、summary/outbox

## 3. Workflow

### 3.1 Stage 设计

选品表 workflow 使用 4 个 stage：

| Stage code | 进入条件 | 编排动作 | 派生 Job | 退出条件 |
| --- | --- | --- | --- | --- |
| `read_selection_rows` | 开启 TK selection table mode | 派发飞书读取 job，`selection_table_source_adapter` 执行必填补全字段缺失扫描和 URL 格式/域名校验 | `feishu_table_read` | 得到候选行 / 跳过 / 失败 |
| `dispatch_selection_row_refresh` | 存在候选行 | 初始化 row cursor，按 `row_pipeline_concurrency` 派发第一批行级主 job；客户可见选品表采集默认 `row_pipeline_concurrency=1` | `selection_row_refresh` | 至少一个 active row pipeline 已创建，或候选全部跳过 |
| `collect_selection_rows` | 存在 active / queued row pipeline | 等待 active 行级 pipeline 产出 `status=finished` + `result_status`；当前行终态后再放行下一行 | `selection_row_refresh`（后续行）/ browser child execution | 所有候选行都有最终行级结果 |
| `ready_for_summary` | 所有候选行都有最终行级结果，且不存在 active wait/fallback 引用 | 汇总 row result，写 outbox | `notification_outbox` | `completed` / `partial_success` / `failed` |

### 3.2 流程图

```mermaid
flowchart TD
    A["Task: tiktok_fastmoss_product_ingest"] --> B{"selection_table_ref?"}
    B -->|是| C["read_selection_rows<br/>feishu_table_read<br/>+ 必填字段缺失扫描<br/>+ 候选筛选"]
    B -->|否 direct-ingest| D["dispatch_selection_row_refresh<br/>单行 selection_row_refresh"]
    C --> C1{"有候选行?"}
    C1 -->|是| D
    C1 -->|否| E["ready_for_summary<br/>全部已填充，跳过"]
    D --> F["collect_selection_rows<br/>按 row cursor 等待当前行终态<br/>再放行下一行"]
    F --> E
    E --> G["summary / outbox"]
```

### 3.3 行级 Pipeline（`selection_row_refresh` 内部）

```mermaid
flowchart TD
    A["selection_row_refresh 开始"] --> B["1. URL 验证（tiktok handler 内部）"]
    B --> B1{"URL 有效?"}
    B1 -->|域名/格式无效| B2["回写 商品状态=链接不可访问<br/>返回 skipped"]
    B1 -->|有效| C["2. TikTok request fetch"]
    C --> C1{"采集成功?"}
    C1 -->|wait/fallback signal| D["3. 创建/等待 TikTok browser task_execution<br/>行级 job status=waiting"]
    D --> D1{"browser 成功?"}
    D1 -->|否| D2["返回 failed"]
    C1 -->|失败且不可 fallback| D2
    C1 -->|成功| E["4. Media sync"]
    D1 -->|成功| D3["使用 browser normalized result<br/>继续当前行级 pipeline"]
    D3 --> E
    C1 -->|商品不可访问| C2["跳过 media/FastMoss/图表<br/>Fact DB upsert + 回写 商品状态=已下架/区域不可售"]
    C2 --> G
    E --> F["5. FastMoss fetch"]
    F --> F1{"API 成功?"}
    F1 -->|风控/MSG_SAFE_0001| F2["5b. FastMoss browser task_execution<br/>刷新 cookie cache → 重试原 FastMoss API"]
    F2 --> F3{"重试成功?"}
    F3 -->|是| G
    F3 -->|否| G["6. Fact DB upsert<br/>FastMoss 失败不影响 TikTok 侧"]
    F1 -->|成功| G
    F1 -->|失败| G
    G --> H["7. Feishu writeback<br/>必填/可选字段 fill_missing_only<br/>可售商品先渲染必填图表"]
    H --> I["返回 success / partial_success"]
```

### 3.4 行级逻辑阻塞与 Browser Fallback 通信

`selection_row_refresh` 是业务上的行级主执行单元。它可以物理拆出 `task_execution` 给 `browser_worker`，但浏览器子任务只是当前行 pipeline 的一个中间步骤，不是独立业务结果。默认 `row_pipeline_concurrency=1`，`stage_cursor_json` 只允许当前 active row 完成后再放行下一条。

约束:

- `fallback_required` 是兼容期 handler wait signal，不是 Runtime DB `status`，也不是行级终态；当前行等待 browser 时，行级主 job 或顶层 request 使用 `status=waiting` 和 `result_json.wait` 记录 child `task_execution` 引用。
- TikTok browser fallback 属于替代当前 TikTok 采集 stage 输出。Browser `task_execution` 成功后，normalized result、artifact evidence 和 fallback metadata 保存在 `task_execution.result_json` / `artifact_object`；原行 pipeline 按引用继续执行 media sync、FastMoss、Fact DB、Feishu writeback，并产出最终 `row_status`。
- FastMoss security fallback 属于解除阻塞后重跑当前 FastMoss stage。Browser handler 成功后只持久化 `fastmoss_session_cookie_cache` 并返回脱敏 metadata；原 FastMoss handler 从 cookie cache 重新请求原 API 一次，不通过 payload 传递 cookie value。
- Browser `task_execution` 失败时，当前行按业务规则标记 `failed` 或 `partial_success`，然后 row cursor 才能放行下一行；失败不能被父任务 summary 当成成功吞掉。
- 客户现场 / 生产默认 `row_pipeline_concurrency=1`。当前行未形成最终 `row_status` 前，workflow 不应推进下一条候选行的采集。后续如需提升吞吐，只能通过 workflow contract 显式声明 bounded concurrency、FIFO/lane、幂等边界和 summary gate。
- `ready_for_summary` 的入口条件是所有候选行都有最终 `row_status`，且不存在未处理 wait/fallback 引用、active browser execution 或未收敛的行级主 job。
- 父任务汇总必须使用行级业务结果，不得只按 `api_worker_job.status` 或 `task_execution.status` 汇总。Browser 子任务 success 只能作为当前行解除等待的输入证据。
- Browser 阶段产生的截图、HTML、raw page dump 等物理资产可以在 browser handler 内先写对象存储并由 `artifact_object` 索引；商品媒体资产、Fact DB upsert 和 Feishu projection 的最终一致性仍由 `selection_row_refresh` 行级主 job 承担。正式 workflow 缺 Fact DB 或 MinIO/S3 对象存储配置时必须 fail fast，不能用 `dry_run` 或 `local` 成功替代。

### 3.5 关键词搜索选品写入入口

`search_keyword_selection_products` 是同一张 `TK选品收集` 的上游写入入口，不拥有独立详情采集模型。

```mermaid
flowchart TD
    A["Task: search_keyword_selection_products"] --> B["keyword_seed_import<br/>FastMoss search + selection_seed_projection_mapper"]
    B --> C{"FastMoss search wait/fallback signal?"}
    C -->|是| D["fastmoss_security_browser_resolve<br/>task_execution"]
    D --> E["重试 keyword_seed_import<br/>原 /api/goods/V2/search"]
    C -->|否| F["seed rows insert_if_absent"]
    E --> F
    F --> G{"新增成功行?"}
    G -->|否| H["ready_for_summary<br/>只汇总搜索/跳过结果"]
    G -->|是| I["dispatch_selection_row_refresh_jobs"]
    I --> J["refresh_selection_rows<br/>复用 selection_row_refresh"]
    J --> K{"row wait/fallback signal?"}
    K -->|是| L["selection_row_browser_fallback<br/>task_execution"]
    L --> J
    K -->|否| N{"全部新增行有最终 row_status?"}
    N -->|否| J
    N -->|是| H
    H --> O["summary / outbox"]
```

约束:

- `keyword_seed_import` 遇到 FastMoss `MSG_SAFE_0001` 时，只能记录 wait/fallback signal；FastMoss browser `task_execution` success 只允许解除阻塞并重试原始搜索请求，不能直接让关键词任务进入 success summary。
- 已存在选品行按 `insert_if_absent` 跳过，不能触发 `selection_row_refresh`。
- 新增成功的选品行进入与 `tiktok_fastmoss_product_ingest` 完全相同的 `selection_row_refresh` pipeline；TikTok / FastMoss row-level fallback 必须通过 `selection_row_browser_fallback` 等待 child `task_execution` 终态，再按 fallback 类型继续当前行。
- Browser fallback 结束后不进入独立恢复 stage；executor 只结清当前 `selection_row_refresh` 的 wait 引用，并让同一个行级主 job 重新可 claim。
- `ready_for_summary` 必须同时等待 seed import 终态和所有新增行最终 `row_status`；不存在未处理 wait/fallback 引用、active browser execution 或未收敛行级主 job 时才允许汇总。
- 关键词选品 summary 的详情成功 / 失败数必须来自最终行级业务结果，不能把 browser 子任务 success 或 seed write success 当成详情采集 success。

## 4. Job 设计

| Job | Runtime 表 | Worker | Handler | 说明 |
| --- | --- | --- | --- | --- |
| `feishu_table_read` | `api_worker_job` | `api_worker` | `feishu_table_read` | 读取选品表全部记录，`selection_table_source_adapter` 执行候选筛选 |
| `selection_row_refresh` | `api_worker_job` | `api_worker` | `selection_row_refresh` | 行级 pipeline 主 job，内部串行执行完整采集链路 |
| `tiktok_product_browser_fetch` | `task_execution` | `browser_worker` | `tiktok_product_browser_fetch` | TikTok request 需要兜底时由当前行 pipeline 创建；结果由原行按引用消费 |
| `fastmoss_security_browser_resolve` | `task_execution` | `browser_worker` | `fastmoss_security_browser_resolve` | FastMoss 风控解除；成功判据必须回到原 FastMoss API 或等价业务验证 |
| `notification_outbox` | `notification_outbox` | `outbox_dispatcher` | `outbox_dispatch` | 最终通知 |

### 4.1 `selection_row_refresh` Payload

```json
{
  "request_payload": {},
  "stage_code": "collect_selection_rows",
  "source_record_id": "rec_xxx",
  "source_table_ref": "tblpF46y6SkmVCE5",
  "product_identity": {
    "product_id": "1730892854181139253",
    "product_url": "https://www.tiktok.com/shop/pdp/1730892854181139253",
    "normalized_product_url": "https://www.tiktok.com/shop/pdp/1730892854181139253"
  },
  "source_context": {},
  "fallback_allowed": true,
  "fastmoss_overview_window_days": [28],
  "fastmoss_sku_window_days": 28,
  "writeback_enabled": true,
  "target_table_ref": "tblpF46y6SkmVCE5"
}
```

### 4.2 `selection_row_refresh` Result

```json
{
  "source_record_id": "rec_xxx",
  "row_status": "success | partial_success | unavailable | url_invalid | failed",
  "normalized_product_result": {},
  "product_fact_bundle": {},
  "fact_upsert": {},
  "writeback_projection": {"fields": {}},
  "writeback_result": {},
  "step_timeline": [],
  "runtime_evidence": {}
}
```

## 5. Handler 与 Flow 边界

### 5.1 共享能力（Handler 层）

以下能力作为 handler 内部逻辑，所有 TikTok/FastMoss 抓取 workflow 共享：

| 能力 | 实现位置 | 影响范围 |
| --- | --- | --- |
| URL 域名/格式验证 | `tiktok_product_request_fetch` handler 内部 | 选品表、竞品表、关键词搜索 |
| 商品可访问性判定 | `tiktok_product_request_fetch` handler 内部 | 已有，保持不变 |
| FastMoss browser fallback | `fastmoss_product_fetch` handler / `run_competitor_row_refresh_flow` 内部 | 选品表、竞品表、达人池同步 |

### 5.2 本 Workflow 专属

| 组件 | Code | 所有权 |
| --- | --- | --- |
| Source Adapter | `selection_table_source_adapter` | 选品表业务语义（必填补全字段缺失扫描、身份字段、跳过规则） |
| Projection Mapper | `selection_table_projection_mapper` | 必填补全字段、系统运行字段、可选补充字段映射、`fill_missing_only` 策略、图表渲染 |
| Row Refresh Flow | `run_selection_row_refresh_flow` | 行级 pipeline 串行编排 |

### 5.3 Adapter / Mapper 默认业务语义

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `required_candidate_fields` | 11 个必填补全字段（见需求文档 3.4 节） | 全部已填充则跳过；可选补充字段缺失不触发候选 |
| `skip_statuses` | `["已下架/区域不可售", "链接不可访问"]` | 不可访问记录跳过 |
| `upsert_key` | `商品ID` | 写回主键 |
| `fill_missing_only` | `true` | 所有新增字段不覆盖已有值 |
| `refresh_identity_fields` | `["商品ID", "商品链接"]` | 身份字段始终刷新 |
| `recorded_date_conditional` | `true` | 有实际字段写入才刷新 `记录日期` |

### 5.4 竞品表同步更新

本次共享能力变更需同步更新竞品表 workflow：

| 变更 | 竞品表影响 |
| --- | --- |
| URL 验证（TikTok handler 内部） | `competitor_row_refresh` 自动获得 URL 验证能力，无效 URL 回写 `商品状态=链接不可访问` |
| FastMoss browser fallback | `competitor_row_refresh` 已有此能力，保持不变 |

竞品表 workflow 结构不变（已是行级 pipeline），仅 handler 层共享能力增强。

## 6. 进程间调度时序图

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

    Entry->>DB: insert task_request(tiktok_fastmoss_product_ingest)
    Exec->>DB: claim task_request

    alt TK selection table mode
        Exec->>DB: enqueue api_worker_job(feishu_table_read)
        API->>DB: claim feishu_table_read
        API->>Feishu: read TK selection rows
        Note over API: selection_table_source_adapter:<br/>必填字段缺失扫描<br/>skip_statuses 过滤
        API->>DB: mark read job terminal
        Exec->>DB: reconcile candidate rows
    end

    Exec->>DB: initialize row cursor / candidate queue
    loop 每条候选行
        Exec->>DB: enqueue current api_worker_job(selection_row_refresh)
        API->>DB: claim selection_row_refresh
        Note over API: 1. URL 验证（handler 内部）<br/>2. TikTok request fetch
        opt TikTok browser fallback
            API->>DB: mark row job status=waiting with browser wait ref
            Exec->>DB: enqueue task_execution(tiktok_product_browser_fetch)
            Browser->>DB: claim task_execution
            Browser->>Obj: store page artifacts
            Browser->>DB: mark browser job terminal
            Exec->>DB: mark same row job pending with browser result ref
            API->>DB: reclaim selection_row_refresh
            Note over API: consume browser normalized result by ref
        end
        Note over API: 3. Media sync → Obj
        Note over API: 4. FastMoss fetch + fallback
        API->>Fact: upsert entities / relations
        Note over API: 5. 图表渲染（按需）
        API->>Feishu: write required/optional fields (fill_missing_only)
        API->>DB: mark row job terminal
        Exec->>DB: release next row only after current row terminal
    end

    Exec->>DB: reconcile final row results
    Exec->>DB: finalize task_request and insert notification_outbox
    Outbox->>DB: claim notification_outbox
    Outbox->>Entry: send summary
```

## 7. 数据写入

### 7.1 Runtime DB
- `task_request`：父任务状态
- `api_worker_job`：`feishu_table_read`、`selection_row_refresh`（每条候选行一个）
- `task_execution`：`tiktok_product_browser_fetch`（fallback 时）、`fastmoss_security_browser_resolve`（FastMoss fallback 时）

### 7.2 Fact DB
- 商品、店铺、SKU、媒体资产、关系、指标快照、每日指标、分布快照（统一走 `fact_bundle_upsert`）

### 7.3 Feishu
- `TK选品收集`：必填补全字段、系统运行字段和可选补充字段，`fill_missing_only` 策略
- `商品状态`：仅在不可访问时写入"链接不可访问"或"已下架/区域不可售"

### 7.4 MinIO / Object Store
- 商品主图、侧边栏图片（media sync）
- 图表 PNG 不入 MinIO，写回时直接渲染后插入飞书单元格

## 8. SKU 绑定规则

TikTok 和 FastMoss 各自返回 SKU 数据，但字段命名和结构不同。本节定义两平台 SKU 的绑定规则，确保父体规格、父体图片、最佳 SKU 等字段能正确关联。

### 8.1 绑定键

绑定使用两个维度：`sku_id` 和 `prop_value_id`。

| 维度 | TikTok 字段 | FastMoss 字段 | 说明 |
| --- | --- | --- | --- |
| SKU 唯一标识 | `skus[].sku_id` | `sku_list[].sku_id` | 两平台共享相同值（FastMoss 数据源为 TikTok） |
| 规格值标识 | `sku_images[].sku_property_key` | `sku_list[].sku_sale_props[].prop_value_id` | TikTok 内部数字 ID，两平台一致 |

**关键发现**：`sku_property_key` 和 `prop_value_id` 都是 TikTok 内部为每个规格值分配的数字 ID（如 `7630828784765421326`），而非可读文本（如 "Golden"）。这使得跨平台绑定不需要文本匹配。

### 8.2 图片关联

TikTok 的 `sku_property_image_map` 将 `sku_property_key` 映射到图片 URL。FastMoss 的 `sku_sale_props[].image` 也包含相同的图片（相同图片 ID，不同 CDN 域名）。

```
TikTok sku_images[0]:
  sku_property_key: "7630828784765421326"
  source_url: "https://p19-.../6993942ee660440da03c482b2371076c~..."

FastMoss sku_list[0].sku_sale_props[0]:
  prop_value_id: "7630828784765421326"   ← 匹配 sku_property_key
  image: "https://p16-.../6993942ee660440da03c482b2371076c~..."  ← 相同图片 ID
```

### 8.3 实际数据示例

以商品 `1732355931137544633`（TikTok URL: `https://www.tiktok.com/shop/pdp/1732355931137544633`）为例：

**TikTok 侧数据**（来自 `sku_images` + `skus`）：

| 序号 | sku_id | sku_name | sku_property_key | 图片 URL |
| --- | --- | --- | --- | --- |
| 1 | 1732355931814793657 | 03-9307 | 7630828784765421326 | `.../6993942ee660440da03c482b2371076c~...` |
| 2 | 1732355931816365377 | 03-9306 | 7630828784765392614 | `.../95fde6d364f8473c823f4a6f3ccf5bb4~...` |
| 3 | 1732355931816889657 | 03-9305 | 7630828784765363878 | `.../f2dc23e4b53b4848915b0e1aa5d3a198~...` |
| 4 | 1732355931816889658 | 03-9304 | 7630828784765335142 | `.../91a1ed8b572f431d8180ca6e1b5b58c1~...` |
| 5 | 1732355931816889659 | 03-9303 | 7630828784765306406 | `.../cb15f52b1cb9403d9e3e8d137ba8720d~...` |

**FastMoss 侧数据**（来自 `sku_list`）：

| 序号 | sku_id | sku_name | spec_name | prop_value_id | image |
| --- | --- | --- | --- | --- | --- |
| 1 | 1732355931814793657 | Golden - 12 Pack | Golden - 12 Pack | 7630828784765421326 | `.../6993942ee660440da03c482b2371076c~...` |
| 2 | 1732355931816365377 | Blue - 12 Pack | Blue - 12 Pack | 7630828784765392614 | `.../95fde6d364f8473c823f4a6f3ccf5bb4~...` |
| 3 | 1732355931816889657 | Red - 12 Pack | Red - 12 Pack | 7630828784765363878 | `.../f2dc23e4b53b4848915b0e1aa5d3a198~...` |
| 4 | 1732355931816889658 | Green - 12 Pack | Green - 12 Pack | 7630828784765335142 | `.../91a1ed8b572f431d8180ca6e1b5b58c1~...` |
| 5 | 1732355931816889659 | Burgundy - 12 Pack | Burgundy - 12 Pack | 7630828784765306406 | `.../cb15f52b1cb9403d9e3e8d137ba8720d~...` |

**绑定结果**：通过 `sku_id` 精确匹配（如 `1732355931814793657`），或通过 `prop_value_id = sku_property_key` 匹配（如 `7630828784765421326`），可将 FastMoss 的可读规格名（"Golden - 12 Pack"）绑定到 TikTok 的规格图片。FastMoss 的 `sku_name`/`spec_name` 可覆盖 TikTok 的 `sku_name`（TikTok 侧为内部编码如 "03-9307"）。

### 8.4 best_sku 父体字段绑定策略

`父体规格`、`父体图片` 的唯一业务来源是 FastMoss SKU 分析中的有效 `best_sku`，不得从 `product_skus[0]`、单 SKU、`Default`、`默认`、`Specification`、空 SKU 或任意第一条 SKU 兜底生成。

有效 `best_sku` 必须同时满足：

1. `best_sku.sku_value` 有业务值，且不属于 `Default`、`默认`、`Specification` 等无区分度规格值。
2. `best_sku.sold_count > 0`。

父体图片匹配优先级：

1. 以 `best_sku` 描述的主销规格值作为入口。
2. 在 FastMoss SKU row 中查找同一主销 SKU。
3. 优先用 `sku_id` 精确匹配。
4. 其次用 `prop_value_id = sku_property_key` 匹配规格值维度图片。
5. 无匹配则跳过 `父体图片`，但可保留已确认的 `父体规格`。

当前 `tk_fact_ingestion_service._match_fastmoss_sku_reference` 仅使用 `sku_id`、`sku_name`、`prop_value`、`prop_name: prop_value` 作为匹配键，**未包含 `prop_value_id`**。需补充此键以支持规格值维度的绑定。

### 8.5 数据流缺口

当前实现中，TikTok 解析器（`product_page.py`）已提取 `sku_images`，但 `normalized_product_result.logical_fields` 未包含此字段。导致：

- `tk_fact_ingestion_service` 无法获取 TikTok 侧的 SKU 图片
- 父体图片无法从 SKU 维度绑定写回
- 仅能依赖 FastMoss 的 `sku_sale_props[].image`（如 API 未返回则为空）

**修复方向**：将 `sku_images` 纳入 `logical_fields` 或在 fact bundle 中单独传递。

### 8.6 best_sku 接口要求

`best_sku`（最佳 SKU）字段**仅由 SKU Distribution 接口返回**，v3 SKU List 接口不返回。

| 接口 | 路径 | 返回 best_sku | 说明 |
| --- | --- | --- | --- |
| SKU List (v3) | `GET /api/goods/v3/productSku` | 否 | 返回 `sku_list`、`sku_detail`，无销量分布 |
| SKU Distribution | `GET /api/goods/productSku` | **是** | 返回 `sku_list`、`sku_detail`、`best_sku`、`sku_gmv`、`sku_units_sold` |

`best_sku` 结构：

```json
{
  "sku_name": "Size",          // 规格维度名
  "sku_value": "Golden - 12 Pack",  // 规格值
  "sold_count": 35,            // 销量
  "sale_amount": 350,          // 销售额
  "currency": "USD",
  "price": "10.29"
}
```

**必须同时调用两个接口**：v3 SKU List 获取 `sku_sale_props`（含 `prop_value_id` 绑定键），SKU Distribution 获取 `best_sku`（含销量分布）。

### 8.7 无有效 best_sku 处理

当商品无销量或没有有效主销规格时，FastMoss 返回的 `best_sku` 字段可能存在但值为空：

```json
{
  "sku_name": "",
  "sku_value": "",
  "sold_count": 0,
  "sale_amount": 0,
  "price": ""
}
```

实测 13 个产品中 5 个存在此情况（均为新上架或零销量商品）。

**业务规则**：只有 `best_sku.sku_value` 有业务值且 `best_sku.sold_count > 0` 时，才允许生成以下字段：
- `SKU销量占比图`（图表渲染）
- `父体规格`（来自 `best_sku.sku_value`）
- `父体图片`（需要 best_sku 的 prop_value_id 绑定图片）

`Default`、`默认`、`Specification`、空 SKU、单 SKU 或第一条 SKU 不视为有效父体规格来源。没有有效 `best_sku` 时，三者均跳过；有有效 `best_sku` 但图片无法通过 `sku_id` 或 `prop_value_id` 匹配时，只跳过 `父体图片`。

### 8.8 Fact DB 持久化配置

当前 `fact_bundle_upsert` handler 需要 `fact_db_url` 才能实际写入数据库。若 payload 中未提供，handler 以 `dry_run` 模式运行（仅计算不持久化）。

**已知问题**：`selection_row_refresh` 和 `competitor_row_refresh` flow 均未在 `fact_bundle_upsert` 步骤的 payload 中传入 `fact_db_url`，导致 Fact DB 写入全部为 dry_run。

**修复方向**：在两个 flow 的 `_child_context` 构建 `fact_bundle_upsert` payload 时，从 `request_payload` 或环境变量中解析并传入 `fact_db_url`（或 `execution_control_db_url`）。

## 9. 状态收敛

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: executor claim
    running --> waiting_read: TK selection table mode
    waiting_read --> waiting_dispatch: read terminal
    running --> waiting_dispatch: direct ingest mode
    waiting_dispatch --> waiting_rows: dispatch complete
    waiting_rows --> waiting_browser_fallback: current row wait/fallback signal
    waiting_browser_fallback --> waiting_rows: browser task_execution success, row job pending
    waiting_browser_fallback --> row_terminal: browser task_execution failed
    waiting_rows --> row_terminal: current row terminal
    row_terminal --> waiting_rows: queued rows remain
    row_terminal --> ready_for_summary: all candidate rows have final row_status
    ready_for_summary --> finished_success: executor finalize success
    ready_for_summary --> finished_partial: some rows partial/failed
    ready_for_summary --> finished_failed: all rows failed
    finished_success --> [*]
    finished_partial --> [*]
    finished_failed --> [*]
```

父任务 final status：

| 条件 | final_status |
| --- | --- |
| 所有行级 job 成功（含 skipped by all-filled） | `success` |
| 部分行成功、部分失败或 partial_success | `partial_success` |
| 所有行失败，或 read 阶段失败 | `failed` |

## 10. 失败兜底

| 场景 | 策略 |
| --- | --- |
| URL 格式/域名无效 | 回写 `商品状态=链接不可访问`，行级 job 标记 `skipped`，不阻塞其他行 |
| TikTok request 返回 wait/fallback signal | 标记当前行 `status=waiting`，引用 browser fallback；该状态不是行级终态，不进入父任务 success 计数 |
| TikTok browser fallback 成功但行级主 job尚未形成终态 | 继续等待行级主 job 消费 browser output；不得进入 `ready_for_summary` |
| TikTok request 失败 + browser fallback 失败 | 行级 job 标记 `failed`，不执行写回 |
| 商品已下架/区域不可售 | 回写 `商品状态=已下架/区域不可售`，行级 job 标记 `skipped` |
| FastMoss API 失败 | 行级 job 继续执行，TikTok 侧 8 个字段仍正常写回，标记 `partial_success` |
| FastMoss 风控 fallback 失败 | 同上，FastMoss 侧 6 个字段留空 |
| Fact DB upsert 失败 | 行级 job 标记 `failed`，可重试 |
| 飞书写回失败 | 行级 job 标记 `failed`，可重试 |
| 图表渲染失败 | 对应截图字段跳过，不阻塞其他字段 |
| best_sku 不存在、销量为 0、规格值为空或为 `Default`/`默认`/`Specification` | 跳过 `SKU销量占比图`、`父体规格`、`父体图片` 三个字段，其余字段正常写回 |
| best_sku 有效但无法匹配 SKU 图片 | 写入 `父体规格`，跳过 `父体图片`，其余字段正常写回 |

## 11. 实现所有权

| 文件/模块 | 职责 |
| --- | --- |
| `domains/tiktok/workflows/tiktok_fastmoss_product_ingest.py` | 定义选品采集 workflow stage 与 `selection_row_refresh` job 编排 |
| `domains/tiktok/workflows/search_keyword_selection_products.py` | 定义关键词搜索选品写入 workflow stage 与新增行详情采集 gate |
| `domains/tiktok/flows/tiktok_fastmoss_product_ingest/` | 推进行级队列、等待 browser fallback 引用收敛、汇总最终 row result |
| `domains/tiktok/flows/selection_row_refresh/` | 串行执行 TikTok request、browser result 消费、media sync、FastMoss、Fact DB、飞书写回 |
| `fact_sources/tiktok/product_request_fetch_handler.py` | 商品 URL 校验、TikTok 商品详情提取和 browser fallback 信号 |
| `fact_sources/fastmoss/product_fetch_handler.py` | FastMoss 商品数据提取和风控 fallback 信号 |
| `mappers/feishu_selection_row_mapper.py` | 选品候选扫描、`skip_statuses` 过滤和来源行标准化 |
| `projections/feishu_selection_projection.py` | 选品字段分层写回、图表渲染和 `fill_missing_only` 策略 |
| `contracts/fields/feishu-tk-selection.yaml` | 选品字段契约、必填补全字段、系统运行字段和可选补充字段 |
| `infrastructure/facts/tk_fact_ingestion_service.py` | 商品事实 upsert、SKU 绑定和 `prop_value_id` 匹配 |

## 12. 关联文档

- [../../business/requirements/tk-selection-collection.md](../../business/requirements/tk-selection-collection.md)
- [../../business/business-requirements.md](../../business/business-requirements.md)
- [workflow-design-guidelines.md](./workflow-design-guidelines.md)
- [workflow-competitor-table-design.md](./workflow-competitor-table-design.md)
- [../../contracts/fields/feishu-tk-selection.yaml](../../contracts/fields/feishu-tk-selection.yaml)
