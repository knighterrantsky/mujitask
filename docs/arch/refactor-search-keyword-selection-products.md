# Search Keyword Selection Products Refactor

日期: 2026-05-07

状态: 第一阶段结构化重构说明

## 保持不变的边界

本次重构不改变运行拓扑，不新增 daemon、service 或进程角色。`executor`、`api_worker`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 仍按现有 Runtime DB 推进。

稳定 routing key 保持不变:

- `task_code`: `search_keyword_selection_products`
- `workflow_code`: `search_keyword_selection_products`
- `stage_code`: `keyword_seed_import`、`fastmoss_security_browser_fallback`、`dispatch_selection_row_refresh_jobs`、`refresh_selection_rows`、`selection_row_browser_fallback`、`resume_selection_rows_after_browser_fallback`、`ready_for_summary`
- `job_code` / `handler_code`: 沿用既有 workflow 和 job contract

Task 文件仍是入口壳，workflow 文件仍是 declarative stage 定义。业务实现不进入 `apps/**`、`skills/**` 或 generic capability handler。

## 新的 Flow Package

`domains/tiktok/flows/search_keyword_selection_products.py` 迁移为同名 package:

```text
domains/tiktok/flows/search_keyword_selection_products/
  __init__.py
  orchestrator.py
  context.py
  errors.py
  summary.py
  stages/
    keyword_seed_import.py
    fastmoss_security_browser_fallback.py
    dispatch_selection_row_refresh_jobs.py
    refresh_selection_rows.py
    selection_row_browser_fallback.py
    resume_selection_rows_after_browser_fallback.py
    ready_for_summary.py
  policies/
    candidate_filter.py
    dedupe.py
    fallback.py
```

`__init__.py` 只保留原 runtime public entrypoints 的 re-export，确保 workflow registry 的旧 import surface 可继续工作。

`orchestrator.py` 只负责 runtime public entrypoint、stage dispatch 和 child completion release glue，不继续承载各 stage 的内部业务实现。

`stages/**` 使用稳定 stage code 作为代码结构锚点。每个 stage 模块承接自己的推进逻辑，读取 RuntimeStore façade 暴露的状态、判断业务分支、创建下一批 runtime job / browser execution，并返回下一步 stage decision。

`summary.py` 承接最终 summary/result/outbox payload 组装，避免 summary aggregation 继续留在 orchestrator。

涉及 Fact DB、媒体资产和 object storage 的事实沉淀边界仍以 [product-fact-collection.yaml](../../contracts/facts/product-fact-collection.yaml) 为准；本次重构只移动 selection keyword workflow 的编排 owner，不改变 product fact/media contract。

`policies/**` 开始承接业务策略:

- `candidate_filter.py`: 搜索候选过滤、max candidate、allowed/excluded product 规则。
- `dedupe.py`: 搜索 digest、商品 identity 归一化、业务 entity key。
- `fallback.py`: row fallback/resume 决策对象和 fallback key。

## RuntimeStore 第一阶段 Facade

`RuntimeStore` 的 public class path 保持不变。第一阶段只抽出低风险 persistence 边界:

- `infrastructure/runtime/bootstrap.py`: 显式 runtime schema bootstrap 入口。
- `infrastructure/runtime/queries/request_status_query.py`: task request 状态读取、execution/outbox/artifact 读取。
- `infrastructure/runtime/queries/watchdog_query.py`: watchdog scan row 查询。
- `infrastructure/runtime/repositories/task_request_repo.py`: task request repository 入口。
- `infrastructure/runtime/repositories/notification_outbox_repo.py`: outbox 创建和读取。
- `infrastructure/runtime/repositories/resource_lease_repo.py`: resource lease 过期清理。

`RuntimeStore.__init__` 不执行 DDL；本地开发或测试需要建表时必须显式调用 `RuntimeStore.bootstrap_schema()` 或 bootstrap/migration 入口。

## 本阶段保留的兼容逻辑

下一阶段适合继续迁移:

- `context.py` 中仍为兼容旧 cursor / job result shape 保留的 seed context 与 row state 读取逻辑。
- RuntimeStore 中 api worker、browser execution、outbox claim/retry、influencer pool job 和 artifact persistence 的剩余 SQL。

这些剩余项不改变 routing key 或 runtime topology。
