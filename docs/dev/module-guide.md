# 模块阅读指南

更新时间: 2026-04-29

本文帮助开发者快速定位 Mujitask 中不同类型代码的位置。正式结构契约以 [project-structure-contract.md](../arch/project-structure-contract.md) 为准。

## 1. 核心目录

| 路径 | 说明 |
| --- | --- |
| `src/automation_business_scaffold/domains/tiktok/` | TikTok 业务域实现 |
| `src/automation_business_scaffold/capabilities/` | 通用 handler 能力 |
| `src/automation_business_scaffold/control_plane/` | Runtime 控制面 |
| `src/automation_business_scaffold/contracts/` | 代码内 contract model 和 manifest |
| `src/automation_business_scaffold/infrastructure/` | 外部系统客户端、Runtime Store、Fact Store、Object Store |
| `contracts/` | 字段、状态、workflow 等机器契约 |
| `skills/mujitask-tiktok-feishu-sync/` | Agent skill bundle 源 |
| `scripts/execution_control/` | Runtime DB、daemon、launchd、测试辅助脚本 |
| `docs/` | 项目文档 |

## 2. 从任务入口定位代码

```text
task_code
  -> domains/tiktok/tasks/{task_code}.py
  -> domains/tiktok/workflows/{workflow_code}.py
  -> domains/tiktok/jobs/{job_code}.py
  -> capabilities/{category}/{system}/{handler_code}_handler.py
```

## 3. 修改飞书字段映射

读表逻辑:

```text
domains/tiktok/mappers/
```

写表逻辑:

```text
domains/tiktok/projections/
```

不要把表字段逻辑写进通用 handler。

## 4. 修改 Runtime 控制面

优先看:

```text
src/automation_business_scaffold/control_plane/
src/automation_business_scaffold/apps/daemons/
src/automation_business_scaffold/apps/cli/
```

## 5. 修改部署逻辑

优先看:

```text
scripts/deploy/
scripts/execution_control/
docs/ops/
```

## 6. 修改 Skill

优先看:

```text
skills/mujitask-tiktok-feishu-sync/
```

Skill 只负责意图识别、参数提取、提交顶层 task 和返回 request_id，不负责 workflow 主编排。

## 7. 6 个 Task 快速索引

| task_code | workflow 文件 | flow 文件 |
| --- | --- | --- |
| `refresh_current_competitor_table` | `workflows/refresh_current_competitor_table.py` | `flows/refresh_current_competitor_table/` |
| `search_keyword_competitor_products` | `workflows/search_keyword_competitor_products.py` | `flows/search_keyword_competitor_products/` |
| `search_keyword_selection_products` | `workflows/search_keyword_selection_products.py` | `flows/search_keyword_selection_products/` |
| `sync_tk_influencer_pool` | `workflows/sync_tk_influencer_pool.py` | `flows/sync_tk_influencer_pool/` |
| `tiktok_fastmoss_product_ingest` | `workflows/tiktok_fastmoss_product_ingest.py` | `flows/tiktok_fastmoss_product_ingest/` |
| `refresh_competitor_row_by_url` | `workflows/refresh_competitor_row_by_url.py` | `flows/competitor_row_refresh.py` |

## 8. 5 个 Daemon 快速索引

| daemon | 入口 | 控制逻辑 |
| --- | --- | --- |
| executor | `apps/daemons/executor/main.py` | `control_plane/executor/runner.py` |
| api-worker | `apps/daemons/api_worker/main.py` | `control_plane/executor/runner.py` |
| browser-runloop | `apps/daemons/browser_worker/main.py` | `control_plane/executor/runner.py` |
| outbox-dispatcher | `apps/daemons/outbox/main.py` | `control_plane/outbox/dispatcher.py` |
| watchdog | `apps/daemons/watchdog/main.py` | `control_plane/watchdog/scanner.py` |
