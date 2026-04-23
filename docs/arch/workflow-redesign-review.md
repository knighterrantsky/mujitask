# 四个 Workflow 设计评估与新架构重设计

日期: 2026-04-23

状态: 正式架构评审文档。本文评估当前四个正式业务 workflow 的设计合理性，并给出按当前新架构口径重设计后的目标形态。

相关文档:

- [当前整体系统架构设计](./current-system-architecture-design.md)
- [新增 Workflow 设计与拆分规范](./workflow-design-guidelines.md)
- [Runtime DB Schema 设计](./runtime-db-schema-design.md)
- [竞品表 Workflow 设计](./workflow-competitor-table-design.md)
- [选品分析 Workflow 设计](./workflow-selection-analysis-design.md)
- [达人同步 Workflow 设计](./workflow-influencer-pool-sync-design.md)

## 1. 评审结论

当前四个正式 workflow 的业务颗粒度整体合理，应继续作为顶层 Task 保留:

| workflow / task_code | 当前定位 | 结论 |
| --- | --- | --- |
| `refresh_current_competitor_table` | 刷新已有 `TK竞品收集` 记录 | 保留。逐行补全的 job 颗粒度合理，plan 阶段需拆薄。 |
| `search_keyword_competitor_products` | 按关键词发现竞品、插入种子行、补全详情 | 保留。阶段划分合理，seed insert 应从 executor 内部动作演进为 API job。 |
| `sync_tk_influencer_pool` | 从竞品商品扩展达人池 | 保留。product / author / finalizer 颗粒度最接近新架构，需统一 handler 与失败语义。 |
| `tiktok_fastmoss_product_ingest` | 单商品 TikTok + FastMoss 采集、事实沉淀、选品表写回 | 保留。API job + browser fallback 方向合理，WorkflowSpec 需要从单个 orchestrate step 演进为显式 stage 定义。 |

总体判断:

- 业务拆分方向是对的。
- Runtime DB 作为任务状态和可靠队列是必要的。
- 当前主要问题不在 workflow 业务边界，而在代码承载方式还没有完全对齐新架构。
- `business/workflows/*.py` 仍偏旧框架适配层；真正的编排逻辑大量集中在 flow 文件里。
- 新架构下应把 Task / Stage / Job / Handler / Flow 的边界显式化，并让 worker 只按 job 路由执行 handler。

## 2. 新架构评审标准

本文按以下标准评估 workflow 设计。

### 2.1 Task

Task 必须代表一次用户可理解的顶层业务请求，具备:

- 可审计输入。
- 可查询最终状态。
- 可生成 summary。
- 可通过 outbox 回复。
- 可在进程重启后继续推进或明确失败。

### 2.2 Stage

Stage 是业务阶段，不是代码函数。适合作为 stage 的信号:

- 需要等待一批子 job 完成。
- 输入来源和输出目标明显不同。
- 失败策略不同。
- 需要人工可见进度。
- 需要从 API 能力切换到 browser 能力。

### 2.3 Job

Job 是 Runtime DB 中可被 worker 独立 claim、retry、timeout 和审计的执行单元。

一个动作适合拆成 job，当它满足任意条件:

- 需要独立重试。
- 需要独立超时。
- 需要独立并发。
- 失败不应拖垮整批。
- 有外部副作用，需要清晰幂等边界。
- 需要占用 browser profile 等资源。

### 2.4 Handler / Flow

```text
Job = Runtime DB 中的一条待执行数据
Handler = 处理某类 job 的代码入口
Flow = handler 内部复用的业务实现过程
```

约束:

- worker 只根据 `job_code` / `item_code` 找 handler。
- handler 可以调用一个或多个 flow，但不承担父 workflow 的全局编排职责。
- flow 不应偷偷推进父 Task，除非它明确属于该 handler 的职责。

## 3. 当前共性问题

### 3.1 WorkflowSpec 与真实架构表达脱节

当前 `WorkflowSpec` 有两种形态:

- `refresh_current_competitor_table` 和 `search_keyword_competitor_products` 仍保留多 step 兼容流程。
- `sync_tk_influencer_pool` 和 `tiktok_fastmoss_product_ingest` 基本是一个 `orchestrate_*` step。

这会导致:

- `WorkflowSpec` 不能稳定表达真实 stage / job 设计。
- 新人读代码时会以为 step 就是业务 workflow，但实际状态机在 Runtime DB 和 flow 函数中。
- workflow 演进依赖大函数分支，难以做结构化评审。

目标设计:

- `WorkflowSpec` 保留为外部框架兼容层。
- 新增内部 `WorkflowDefinition`，显式描述 `workflow_code`、`version`、`stages`、`jobs`、`transitions`、`summary_policy`。
- executor 只读取内部 workflow definition 推进 Runtime DB。

### 3.2 executor 仍承担部分业务执行

当前有些阶段在 executor 中直接做业务动作，例如:

- 竞品表刷新中的链接清理和待处理行扫描。
- 关键词竞品入库中的 seed row 插入。

短期可以接受，因为这些动作相对轻量，且历史实现已稳定。

目标设计:

- 如果动作耗时、可失败、需要重试或有外部副作用，应拆成 `api_worker_job`。
- executor 只负责阶段推进、job fan-out、父子收敛和 summary。

### 3.3 worker 与业务 handler 绑定过深

当前 worker 仍包含较多业务分支:

- browser worker 按 `item_code` 直接分支调用具体 flow。
- API worker 按 `job_code` 直接分支调用具体 handler。
- 达人同步还存在领域队列的特殊 worker 查找路径。

目标设计:

- 引入 `api_handler_registry` 和 `browser_handler_registry`。
- worker 只做 claim、supervisor、handler lookup、result/error 写回。
- handler 拆到独立模块，并明确 payload / result / retry / timeout / idempotency。

### 3.4 Reconciler / Watchdog 需要显式化

当前已经有基于 Runtime DB 的父任务收敛，但仍分散在业务函数里。

目标设计:

- `Reconciler` 作为明确职责存在，可由 worker 完成后触发，也可由 executor/watchdog 扫描触发。
- `Watchdog Scanner` 补齐 lease expired、hard timeout、stale progress、orphan running、waiting_children 已终态但未推进等兜底。

## 4. Workflow 逐项评估与目标设计

## 4.1 `refresh_current_competitor_table`

### 当前合理性

该 workflow 解决的是“刷新已有竞品表记录”，业务边界清楚。

当前合理点:

- 顶层 task 代表一次定时刷新请求。
- 每条竞品记录的详情补全是一个 `feishu_single_row_update` browser job。
- 单行失败不会拖垮整张表。
- browser profile 可通过 `resource_lease` 串行保护。
- summary 能展示每行成功、失败、跳过状态。

当前问题:

- cleanup 和 pending rows scan 在 executor 内同步执行。
- `plan_refresh_work` 同时做清理、扫描、fan-out，职责偏厚。
- `current_stage="waiting_children"` 语义偏泛，无法直接看出正在等待哪类 child job。

### 目标 Stage

| Stage | 进入条件 | executor 动作 | 派生 Job | 退出条件 |
| --- | --- | --- | --- | --- |
| `submitted` | task 创建成功 | 初始化 stage cursor | 无 | 进入 `cleanup_and_scan` |
| `cleanup_and_scan` | pending task | 派发或执行轻量 cleanup/scan | 可选 `feishu_competitor_cleanup_scan` API job | 得到 target rows |
| `dispatch_row_updates` | 有待补全 rows | 为每行派发 browser job | `feishu_single_row_update` in `task_execution` | 进入 `waiting_row_updates` |
| `waiting_row_updates` | row jobs active | 不阻塞等待 | 无 | row jobs 全部终态 |
| `ready_for_summary` | 子任务已终态 | 汇总结果、写 outbox | `notification_outbox` | completed |

### 目标 Job

| Job | Runtime 表 | Worker | Handler | 幂等边界 |
| --- | --- | --- | --- | --- |
| `feishu_competitor_cleanup_scan` | `api_worker_job` 或 executor 轻量动作 | `api_worker` / `executor_daemon` | `competitor_cleanup_scan_handler` | 基于 table url + request id + normalized url |
| `feishu_single_row_update` | `task_execution` | `browser_worker` | `feishu_single_row_update_handler` | 基于 record_id / normalized product url |
| `task_request.completed` | `notification_outbox` | `outbox_dispatcher` | outbox handler | `task_request.completed:{request_id}` |

### 设计建议

短期:

- 保留 cleanup/scan 在 executor 内，但在文档和代码中明确它是轻量编排动作。
- 将 `waiting_children` 的 `stage_cursor.resume_action` 改成更明确的 `waiting_row_updates` 语义。

中期:

- 将 cleanup/scan 拆成 API job。
- 将 `feishu_single_row_update` browser handler 从 browser runloop 分支中抽出。

## 4.2 `search_keyword_competitor_products`

### 当前合理性

该 workflow 解决的是“按关键词发现竞品并写入竞品表”，与刷新已有记录是不同 Task，保留独立入口是合理的。

当前合理点:

- 关键词 discovery 是一个独立 browser job。
- discovery 完成后再处理候选、插入 seed rows、派发详情补全 jobs。
- 详情补全复用 `feishu_single_row_update`，避免重复实现。

当前问题:

- seed row insert 当前在 executor resume 阶段循环执行，存在外部副作用但不具备独立 retry/timeout。
- discovery 与 detail update 都使用 `task_execution`，但 stage code 仍主要靠 `resume_action` 表达。
- 候选处理、seed 插入、detail fan-out 混在一个 resume 函数中。

### 目标 Stage

| Stage | 进入条件 | executor 动作 | 派生 Job | 退出条件 |
| --- | --- | --- | --- | --- |
| `submitted` | task 创建成功 | 初始化关键词参数 | 无 | 进入 `dispatch_keyword_discovery` |
| `dispatch_keyword_discovery` | 有 search keyword | 派发 discovery browser job | `fastmoss_keyword_candidate_discovery` | 进入 `waiting_keyword_discovery` |
| `waiting_keyword_discovery` | discovery active | 等待 reconciler | 无 | discovery 终态 |
| `process_candidates` | discovery success | 解析候选并派发 seed jobs | `feishu_seed_row_insert` | 进入 `waiting_seed_insert` |
| `waiting_seed_insert` | seed jobs active | 等待 seed jobs | 无 | seed jobs 全部终态 |
| `dispatch_detail_updates` | 有新增 seed rows | 派发 row update jobs | `feishu_single_row_update` | 进入 `waiting_detail_updates` |
| `waiting_detail_updates` | detail jobs active | 等待 row jobs | 无 | row jobs 全部终态 |
| `ready_for_summary` | 子任务终态 | 汇总 discovery / seed / detail | `notification_outbox` | completed |

### 目标 Job

| Job | Runtime 表 | Worker | Handler | 幂等边界 |
| --- | --- | --- | --- | --- |
| `fastmoss_keyword_candidate_discovery` | `task_execution` | `browser_worker` | `keyword_candidate_discovery_handler` | request_id + keyword + filters |
| `feishu_seed_row_insert` | `api_worker_job` | `api_worker` | `feishu_seed_row_insert_handler` | target table + product_id / normalized url |
| `feishu_single_row_update` | `task_execution` | `browser_worker` | `feishu_single_row_update_handler` | record_id / normalized product url |

### 设计建议

短期:

- 将 seed insert 的结果 schema 固化，避免 summary 依赖松散 item 字段。
- 在 `stage_cursor` 中分开记录 discovery result、seed insert summary、detail fan-out summary。

中期:

- 把 seed insert 拆为 API job，支持单商品级重试。
- 给 keyword discovery 和 seed insert 增加标准化 error schema。

## 4.3 `sync_tk_influencer_pool`

### 当前合理性

这是四个 workflow 中最接近目标架构的一个。它天然需要领域 job，因为 product job 和 author job 的状态机与普通 API job 不完全一样。

当前合理点:

- 顶层 task 负责一次达人同步请求。
- product job 负责一条竞品记录的商品级达人发现。
- author job 负责一个达人详情采集和写入。
- product finalizer 基于 Runtime DB 汇总 author jobs。
- 失败可以限制在单个达人或单个商品，不拖垮整批。

当前问题:

- product / author / finalizer 目前通过 `run_sync_tk_influencer_pool(queue_mode=worker)` 处理，API worker 对这类领域 job 有特殊路径。
- handler 边界不够显式。
- 父任务 finalizer 当前需要更清晰地区分 `success`、`partial_success`、`failed`。
- product/author job 与通用 `api_worker_job` 的生命周期字段尚未完全统一。

### 目标 Stage

| Stage | 进入条件 | executor 动作 | 派生 Job | 退出条件 |
| --- | --- | --- | --- | --- |
| `dispatch_product_jobs` | task pending | 扫描候选竞品并创建 product jobs | `influencer_pool_product_job` | 进入 `waiting_product_jobs` |
| `waiting_product_jobs` | product jobs active | 不阻塞等待 | 无 | product jobs 全部终态 |
| `ready_for_summary` | product jobs 终态 | 汇总 product / author 状态 | `notification_outbox` | completed / partial_success / failed |

领域 job 内部阶段:

| Job | 状态流转 |
| --- | --- |
| product job | `pending -> discovering -> detail_pending -> completed/hard_failed` |
| author job | `pending -> running -> succeeded/skipped/failed_retry/hard_failed` |
| product finalizer | 聚合 author jobs，推进 product job 终态 |

### 目标 Job

| Job | Runtime 表 | Worker | Handler | 幂等边界 |
| --- | --- | --- | --- | --- |
| 商品达人列表发现 | `influencer_pool_product_job` | `api_worker` | `influencer_pool_product_handler` | request_id + source_record_id + product_id |
| 达人详情采集写入 | `influencer_pool_author_job` | `api_worker` | `influencer_pool_author_handler` | request_id + source_record_id + product_id + influencer_id |
| 商品级汇总 | product finalizer scan | `api_worker` | `influencer_pool_product_finalizer` | product_job_id |
| 父任务汇总 | `task_request` | `executor_daemon` | workflow finalizer | request_id |

### 设计建议

短期:

- 保留领域 job 表，不强行合并到通用 job 表。
- 明确 product / author / finalizer 三类 handler 的 payload 和 result schema。
- 父任务 summary 增加 `final_status_policy`:
  - 全部成功或跳过: `success`
  - 部分 hard_failed 但允许部分成功: `partial_success`
  - 命中硬停止或失败超过阈值: `failed`

中期:

- 将 API worker 对 influencer pool 的特殊路径改成 handler registry。
- 统一领域 job 的 `last_progress_at`、`progress_stage`、`max_execution_seconds`、`dead_letter_reason` 字段。

## 4.4 `tiktok_fastmoss_product_ingest`

### 当前合理性

该 workflow 解决的是单商品选品分析/事实采集，业务边界清楚。它的目标不是整表批处理，而是围绕一个 TikTok 商品 URL / SKU 完成采集、事实沉淀和可选飞书写回。

当前合理点:

- 使用 `api_worker_job` 承载飞书读取、商品采集、飞书写回。
- TikTok request 不可解析时可派发 browser fallback。
- 媒体上传和事实入库已经被纳入采集结果。
- 父任务通过 Runtime DB 汇总 API job 和 browser job 状态。

当前问题:

- 当前 `WorkflowSpec` 是单个 `orchestrate_tiktok_fastmoss_product_ingest` step，不利于表达真实阶段。
- 商品采集 job 内部包含 TikTok、FastMoss、媒体上传、事实入库等多个副作用，必须严格依赖幂等。
- browser fallback 完成后再派发第二次 product ingest API job，stage transition 需要更清晰。

### 目标 Stage

| Stage | 进入条件 | executor 动作 | 派生 Job | 退出条件 |
| --- | --- | --- | --- | --- |
| `read_selection_table` | 需要绑定 TK 选品表 | 派发飞书读取 job | `feishu_tk_selection_table_read` | 读取成功 / 跳过 |
| `collect_product_data` | 有 product url / id | 派发商品采集 job | `tiktok_fastmoss_product_ingest` | 成功 / fallback required / failed |
| `browser_fallback` | TikTok request 不可解析 | 派发 browser fetch job | `tiktok_product_browser_fetch` | browser fetch 成功 / failed |
| `collect_after_fallback` | browser fallback 成功 | 用 browser result 重新派发采集 job | `tiktok_fastmoss_product_ingest` | 采集成功 / failed |
| `writeback_selection_table` | 有来源飞书记录且需要写回 | 派发写回 job | `feishu_tk_selection_table_writeback` | 写回终态 |
| `ready_for_summary` | 子任务终态 | 汇总结果、写 outbox | `notification_outbox` | completed / failed |

### 目标 Job

| Job | Runtime 表 | Worker | Handler | 幂等边界 |
| --- | --- | --- | --- | --- |
| `feishu_tk_selection_table_read` | `api_worker_job` | `api_worker` | `selection_table_read_handler` | request_id + table + product key |
| `tiktok_fastmoss_product_ingest` | `api_worker_job` | `api_worker` | `product_ingest_handler` | product_id / normalized url + request_id suffix |
| `tiktok_product_browser_fetch` | `task_execution` | `browser_worker` | `tiktok_product_browser_fetch_handler` | request_id + product url |
| `feishu_tk_selection_table_writeback` | `api_worker_job` | `api_worker` | `selection_table_writeback_handler` | request_id + source_record_id |

### 设计建议

短期:

- 保留商品采集作为一个 API job，但补齐幂等说明:
  - Fact DB 使用业务唯一键 upsert。
  - MinIO object key 稳定或可覆盖。
  - 飞书写回只由 writeback job 执行。
- 在 stage cursor 中显式记录 fallback required、fallback execution、after fallback ingest job。

中期:

- 如果媒体上传或事实入库失败频率高，将 `media_upload_and_fact_persist` 从 product ingest job 拆成独立 API job。
- 将 `tiktok_fastmoss_product_ingest_v1` 的单步 WorkflowSpec 改为兼容入口，真实 stage 由内部 workflow definition 表达。

## 5. 统一目标架构

建议引入以下代码分层:

```text
business/workflow_defs/
  refresh_current_competitor_table.py
  search_keyword_competitor_products.py
  sync_tk_influencer_pool.py
  tiktok_fastmoss_product_ingest.py

business/orchestrators/
  executor.py
  stage_runner.py
  fanout.py

business/handlers/api/
  registry.py
  feishu_seed_row_insert.py
  feishu_tk_selection_table_read.py
  feishu_tk_selection_table_writeback.py
  product_ingest.py
  influencer_pool_product.py
  influencer_pool_author.py
  influencer_pool_finalizer.py

business/handlers/browser/
  registry.py
  feishu_single_row_update.py
  fastmoss_keyword_candidate_discovery.py
  tiktok_product_browser_fetch.py

business/reconcilers/
  task_request_reconciler.py
  api_worker_job_reconciler.py
  browser_execution_reconciler.py
  influencer_pool_reconciler.py

business/flows/
  只保留可复用业务实现过程
```

核心原则:

- `business/workflows/*.py` 可以继续作为 framework 兼容层。
- 内部 workflow definition 才是当前系统 stage/job 事实来源。
- worker 不直接写业务分支，只通过 registry 找 handler。
- Reconciler 基于 Runtime DB 聚合状态，不依赖内存 callback。
- Watchdog 独立扫描异常状态，不等待 worker 自己恢复。

## 6. 迁移计划

### 阶段 1: 文档和状态口径统一

目标:

- 保留四个顶层 task_code 不变。
- 固化每个 workflow 的 stage code、job code、payload schema、result schema。
- 在 README 和 workflow 文档中明确 `WorkflowSpec` 是兼容层。

验收:

- 每个 workflow 都能在文档中找到 Task / Stage / Job / Handler / Flow 映射。
- 新增 workflow 必须按 [新增 Workflow 设计与拆分规范](./workflow-design-guidelines.md) 评审。

### 阶段 2: Handler registry 抽取

目标:

- API worker 从 `if job_code == ...` 改成 registry lookup。
- Browser worker 从 `if item_code == ...` 改成 registry lookup。
- handler 统一返回标准 result / error。

验收:

- 新增一个 API job 不需要修改 worker 主循环。
- 新增一个 browser item_code 不需要修改 browser runloop 主循环。

### 阶段 3: Reconciler 显式化

目标:

- 将父任务推进逻辑从业务 flow 中抽到 reconciler。
- 支持 worker 完成后触发、executor 扫描触发、watchdog 兜底触发。

验收:

- 父任务从 `waiting_children` 到 `ready_for_summary` 完全依赖 Runtime DB 当前状态。
- 重复执行 reconciler 不会重复创建 outbox 或重复推进非法状态。

### 阶段 4: Watchdog / Supervisor 补齐

目标:

- 所有 job 表补齐 `last_progress_at`、`progress_stage`、`max_execution_seconds`、`dead_letter_reason`。
- Watchdog 处理 lease expired、stale progress、hard timeout、orphan running。
- Supervisor 负责 heartbeat、progress、timeout 和异常分类。

验收:

- worker 崩溃后 job 可自动重试或进入失败终态。
- handler 卡住但进程仍 heartbeat 时，可被 stale progress 策略兜底。
- 父任务不会长期停留在 children 已终态的 `waiting_children`。

## 7. 最终目标

四个 workflow 的最终目标不是把代码拆得更碎，而是让可靠性边界更清晰:

- 顶层 Task 面向用户和审计。
- Stage 面向业务进度和恢复。
- Job 面向独立调度、重试和超时。
- Handler 面向代码入口和执行保护。
- Flow 面向可复用业务实现。
- Runtime DB 面向状态真相。

这样新增业务时，不再从“写一个 handler”开始，而是先定义 Task、Stage、Job、Handler、Flow，再落到代码实现。
