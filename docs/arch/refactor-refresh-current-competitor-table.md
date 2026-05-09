# Refresh Current Competitor Table Refactor

日期: 2026-05-07

状态: Phase 5 结构化重构说明

## 保持不变的边界

本次重构不改变运行拓扑，不新增 daemon、service 或进程角色。`executor`、`api_worker`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 仍按现有 Runtime DB 推进。

稳定 routing key 保持不变:

- `task_code`: `refresh_current_competitor_table`
- `workflow_code`: `refresh_current_competitor_table`
- `stage_code`: `read_competitor_rows`、`dispatch_product_collection`、`collect_product_data`、`browser_fallback`、`resume_competitor_rows_after_browser_fallback`、`ready_for_summary`
- `job_code` / `handler_code`: 沿用既有 workflow 和 job contract

`refresh_competitor_row_by_url` 仍复用同一 runtime surface；本阶段不拆 `competitor_row_refresh/` 这类 row-level leaf flow package。

## 新的 Flow Package

`domains/tiktok/flows/refresh_current_competitor_table/` 已迁移为同名 package:

```text
domains/tiktok/flows/refresh_current_competitor_table/
  __init__.py
  orchestrator.py
  context.py
  errors.py
  summary.py
  stages/
    read_competitor_rows.py
    dispatch_product_collection.py
    collect_product_data.py
    browser_fallback.py
    resume_competitor_rows_after_browser_fallback.py
    ready_for_summary.py
  policies/
    row_selection.py
    fallback.py
    summary_rules.py
```

`__init__.py` re-export 原 public entrypoints，保持 workflow registry 和旧 import surface 可用。

`orchestrator.py` 只负责 public runtime entrypoint、stage module dispatch 和 child completion release glue。

`stages/**` 使用稳定 stage code 作为代码结构锚点:

- `read_competitor_rows.py`: 飞书竞品行读取、空行删除清理和读结果推进。
- `dispatch_product_collection.py`: row-level `competitor_row_refresh` fan-out。
- `collect_product_data.py`: row job 终态检查和 fallback 分支选择。
- `browser_fallback.py`: TikTok/FastMoss browser fallback execution 派发。
- `resume_competitor_rows_after_browser_fallback.py`: fallback 成功后只恢复对应 row job。
- `ready_for_summary.py`: summary stage 的最小推进壳。

`summary.py` 承接最终 summary、result、row result 和 outbox payload 组装。

## 本阶段保留的本地逻辑

候选行上下文、projection 字段组装、browser resume candidate 和 row result 兼容读取仍留在本 package 的 `context.py`，不抽到 shared kernel。

涉及 Fact DB、媒体资产和 object storage 的事实沉淀边界仍以 [product-fact-collection.yaml](../../contracts/facts/product-fact-collection.yaml) 为准；本阶段只移动 workflow 编排 owner。

## 后续阶段

后续再评估:

- `competitor_row_refresh/` row-level leaf flow package 的进一步拆分。
- selection/competitor/product ingest 三类 row fallback 的共享候选抽取。
- RuntimeStore phase 3 的剩余 SQL owner 迁移。
