# Search Keyword Competitor Products Refactor

日期: 2026-05-07

状态: 第二个 keyword workflow 结构化重构说明

## 保持不变的边界

本次重构不改变运行拓扑，不新增 daemon、service 或进程角色。`executor`、`api_worker`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 仍按现有 Runtime DB 推进。

稳定 routing key 保持不变:

- `task_code`: `search_keyword_competitor_products`
- `workflow_code`: `search_keyword_competitor_products`
- `stage_code`: `keyword_seed_import`、`fastmoss_security_browser_fallback`、`dispatch_row_refresh_jobs`、`refresh_competitor_rows`、`browser_fallback`、`resume_competitor_rows_after_browser_fallback`、`ready_for_summary`
- `job_code` / `handler_code`: 沿用既有 workflow 和 job contract

Task 文件仍是入口壳，workflow 文件仍是 declarative stage 定义。业务实现不进入 `apps/**`、`control_plane/**`、`capabilities/**` 或 infrastructure runtime repository。

## 新的 Flow Package

`domains/tiktok/flows/search_keyword_competitor_products.py` 迁移为同名 package:

```text
domains/tiktok/flows/search_keyword_competitor_products/
  __init__.py
  orchestrator.py
  context.py
  errors.py
  summary.py
  stages/
    keyword_seed_import.py
    fastmoss_security_browser_fallback.py
    dispatch_row_refresh_jobs.py
    refresh_competitor_rows.py
    browser_fallback.py
    resume_competitor_rows_after_browser_fallback.py
    ready_for_summary.py
  policies/
    candidate_filter.py
    dedupe.py
    fallback.py
    resume.py
```

`__init__.py` 只保留原 runtime public entrypoints 的 re-export，确保 workflow registry 的旧 import surface 可继续工作。

`orchestrator.py` 只负责 runtime public entrypoint、stage dispatch 和 child completion release glue，不承载每个 stage 的业务推进逻辑。

`stages/**` 使用稳定 stage code 作为代码结构锚点:

- `keyword_seed_import.py`: keyword seed import job 派发、FastMoss 搜索风控 fallback 判断、seed context cursor 写入。
- `fastmoss_security_browser_fallback.py`: 原始 FastMoss 搜索风控 browser execution 派发与结果回灌。
- `dispatch_row_refresh_jobs.py`: 成功 seed row 到 `competitor_row_refresh` row job 的 fan-out。
- `refresh_competitor_rows.py`: row job 终态检查与 browser fallback 分支判断。
- `browser_fallback.py`: row-level browser fallback execution 派发、fallback candidate/resume candidate 识别。
- `resume_competitor_rows_after_browser_fallback.py`: browser success 后只重试对应 row job。
- `ready_for_summary.py`: summary stage 的最小推进壳。

`summary.py` 承接最终 summary、result 和 outbox payload 组装，避免 final aggregation 留在 orchestrator。

涉及 Fact DB、媒体资产和 object storage 的事实沉淀边界仍以 [product-fact-collection.yaml](../../contracts/facts/product-fact-collection.yaml) 为准；本次重构只移动 keyword competitor workflow 的编排 owner，不改变 product fact/media contract。

## 本阶段保留的本地重复

本阶段故意不创建 `domains/tiktok/flows/keyword_shared/`、`domains/tiktok/shared/` 或任何跨 workflow shared kernel。第二个 exemplar 的目标是先把 competitor workflow 独立拆干净，让 selection 和 competitor 两个样本都稳定后，再评估共享抽取。

已观察但推迟的共享候选:

- keyword seed import 的 FastMoss 搜索风控 fallback 分支。
- seed context / candidate context 读取兼容逻辑。
- row-level browser fallback candidate/resume candidate 结构。
- final summary 中 search filter、seed write、row result 的通用外壳。

这些重复仍留在各自 workflow package 内，避免过早抽象出不清晰的 shared kernel。
