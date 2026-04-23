---
name: mujitask-tiktok-feishu-sync
description: >-
  TikTok/TK competitor analysis and FastMoss product discovery for the current Feishu table.
  Use when the user asks to update TikTok competitor data, do TikTok竞品分析 / TK竞品分析,
  search or collect FastMoss / Fastmoss products, or write competitor results back to the current
  Feishu table, even if the user does not know this skill name. For keyword-search requests,
  extract the keyword from natural language and use a default 7-day sales threshold of 200 when
  the user does not specify one; if the user specifies a threshold such as 300, use that value.
metadata:
  short-description: TK竞品采集与更新
---

# mujitask-tiktok-feishu-sync

执行当前飞书表的 TK 竞品更新与 FastMoss 关键词找品。

## 触发条件

- 下面这些表达都应该触发本 skill：
  - `TikTok竞品分析`
  - `TK竞品分析`
  - `TikTok竞品`
  - `TK竞品`
  - `FastMoss`
  - `Fastmoss`
  - `竞品抓取`
  - `竞品找品`
  - `写入当前飞书表`
- 如果用户表达的是“更新当前表 / 同步当前竞品表 / 定时抓取竞品数据”，走竞品表刷新入口。
- 如果用户表达的是“搜索 / 查询 / 收集 FastMoss 商品并写入飞书”，走关键词搜索入口。

## 输入提取规则

- 只从用户输入中提取：
  - `关键词`
  - `7日销量阈值`
- 如果用户没有明确提供 `7日销量阈值`，默认使用 `200`。

## 固定配置

- 从 `skill.local.env` 读取：
  - `INSTALL_DIR`
  - `TABLE_URL`
  - `FEISHU_ACCESS_TOKEN`
  - `BROWSER_PROFILE_REF`
  - `FASTMOSS_PHONE`
  - `FASTMOSS_PASSWORD`
- 不要在对话中向用户索取这些固定配置。
- 不要手动 `source skill.local.env` 或自己拼接环境变量；wrapper 脚本会自行读取并解析它。

## 默认入口

- 竞品表刷新：
  - `bash skills/mujitask-tiktok-feishu-sync/run_refresh_current_competitor_table_step.sh`
- 关键词搜索：
  - `bash skills/mujitask-tiktok-feishu-sync/run_keyword_search_step.sh --search-keyword "<keyword>" --sales-7d-threshold <number>`
- 达人池同步：
  - `bash skills/mujitask-tiktok-feishu-sync/run_influencer_pool_sync_step.sh`
- 如果用户没有指定 `7日销量阈值`，关键词搜索默认传 `200`。

## 输出契约

- 这些入口都属于“同步提交、异步执行”：
  - 脚本只负责创建顶层 `task_request`
  - 首条回执必须返回 `request_id`
  - 后续执行和最终汇总由后台 `executor/browser/outbox` 推进
- 必须等待脚本返回 `__OPENCLAW_RESULT__` 后再回复用户。
- 禁止使用后台启动后短轮询一次的方式提前回复。
- 禁止输出过渡性话术，例如：
  - “还没吐出 request_id”
  - “我先让它继续跑”
  - “等返回编号后确认”
- 首条回执优先单独输出：
  - `request_id: <id>`
- 默认不要展开内部步骤、候选数、子任务分段细节。
- 最终结果由独立通知再次发送到飞书。

## 排障说明

- `run_cleanup_step.sh`
- `run_pending_rows_step.sh`
- `run_single_row_update_step.sh`
- `run_keyword_candidate_step.sh`
- `run_insert_seed_row_step.sh`
- `run_fastmoss_login_check_step.sh`
- `run_influencer_pool_worker_step.sh`

这些脚本仅用于人工排障，不再作为默认主流程。
