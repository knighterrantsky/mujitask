# Sync TK Influencer Pool Refactor

日期: 2026-05-07

状态: Phase 5 结构化重构说明

## 保持不变的边界

本次重构不改变运行拓扑，不新增 daemon、service 或进程角色。`executor`、`api_worker`、`browser_runloop`、`outbox_dispatcher`、`watchdog` 仍按现有 Runtime DB 推进。

稳定 routing key 保持不变:

- `task_code`: `sync_tk_influencer_pool`
- `workflow_code`: `sync_tk_influencer_pool`
- `stage_code`: `read_competitor_candidates`、`dispatch_product_jobs`、`discover_related_creators`、`sync_influencer_pool`、`writeback_competitor_status`、`ready_for_summary`
- `job_code` / `handler_code`: 沿用既有 workflow 和 job contract

## 新的 Flow Package

`domains/tiktok/flows/sync_tk_influencer_pool.py` 迁移为同名 package:

```text
domains/tiktok/flows/sync_tk_influencer_pool/
  __init__.py
  orchestrator.py
  context.py
  errors.py
  summary.py
  stages/
    read_competitor_candidates.py
    dispatch_product_jobs.py
    discover_related_creators.py
    sync_influencer_pool.py
    writeback_competitor_status.py
    ready_for_summary.py
  policies/
    candidate_filter.py
    creator_dedupe.py
    summary_rules.py
```

`__init__.py` re-export 原 public entrypoints 和阶段常量，保持测试、registry 和 handler allowlist 兼容。

`orchestrator.py` 只负责 public runtime entrypoint、stage module dispatch 和 child completion release glue。旧实现中的内部 creator/fact/write pool 子阶段仍作为兼容逻辑保留，未升级为新的正式 top-level workflow stage。

`stages/**` 使用稳定 stage code 作为代码结构锚点:

- `read_competitor_candidates.py`: 读取竞品候选。
- `dispatch_product_jobs.py`: product discovery fan-out。
- `discover_related_creators.py`: product creator discovery 终态检查和 creator sync fan-out。
- `sync_influencer_pool.py`: creator sync 终态检查和竞品状态写回推进。
- `writeback_competitor_status.py`: 源竞品表状态写回收敛。
- `ready_for_summary.py`: summary stage 的最小推进壳。

`summary.py` 承接最终 product group summary、result payload 和 outbox payload 组装。

## 本阶段保留的本地逻辑

product group summary、creator dedupe、writeback projection payload 和内部兼容子阶段仍留在本 package，不抽到 shared kernel。

涉及 Fact DB、媒体资产和 object storage 的事实沉淀边界仍以 [product-fact-collection.yaml](../../contracts/facts/product-fact-collection.yaml) 为准；本阶段只移动 workflow 编排 owner。

## 后续阶段

后续再评估:

- influencer pool 内部 creator/fact/write pool 子阶段是否需要进一步显式 package 化。
- creator/product group 规则是否提升为 domain-level policy。
- RuntimeStore phase 3 的剩余 SQL owner 迁移。

