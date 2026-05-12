---
name: "mujitask-tiktok-feishu-sync"
description: >-
  Submits OpenClaw task requests for the current TikTok/TK Feishu workflows:
  competitor-table refresh, competitor keyword search, batch keyword search,
  influencer-pool sync, selection-table ingest, and selection keyword search. Use only
  when the user explicitly asks to run, sync, update, complete, search, collect, or write
  data to TK竞品收集, TK选品收集, or TK达人池. Do not use for conceptual questions, strategy
  discussion, skill review, configuration support, or general FastMoss questions without
  an explicit table/workflow target.
metadata:
  short-description: "TK选品、竞品、达人池与FastMoss任务提交"
---

# mujitask-tiktok-feishu-sync

<!-- GENERATED FROM skill.spec.yaml. DO NOT EDIT SKILL.md BY HAND. -->

## Purpose

- Use this skill to submit one top-level OpenClaw task request for the current TikTok/TK Feishu business workflows.
- Use this skill to preview and then submit confirmed batch keyword-search requests; each confirmed keyword row submits one existing Runtime task request.
- Single-task submissions return one `request_id`; confirmed batch keyword submissions return one `request_id` per row.
- The actual execution, table writeback, retry behavior, and final Feishu notification are handled by Runtime workflow, workers, and outbox.

## Source of truth

Business overview:

- `docs/business/business-requirements.md`
- `docs/business/requirements/README.md`

Formal workflow requirements:

- `refresh_current_competitor_table` -> `docs/business/requirements/refresh-current-competitor-table.md`
- `search_keyword_competitor_products` -> `docs/business/requirements/search-keyword-competitor-products.md`
- `sync_tk_influencer_pool` -> `docs/business/requirements/sync-tk-influencer-pool.md`
- `tiktok_fastmoss_product_ingest` -> `docs/business/requirements/tk-selection-collection.md`
- `search_keyword_selection_products` -> `docs/business/requirements/search-keyword-selection-products.md`

Design documents:

- `refresh_current_competitor_table` -> `docs/arch/workflow-competitor-table-design.md`
- `search_keyword_competitor_products` -> `docs/arch/workflow-competitor-table-design.md`
- `sync_tk_influencer_pool` -> `docs/arch/workflow-influencer-pool-sync-design.md`
- `tiktok_fastmoss_product_ingest` -> `docs/arch/workflow-selection-table-design.md`
- `search_keyword_selection_products` -> `docs/arch/workflow-selection-table-design.md`

Do not copy detailed business rules, Runtime internals, credentials, table IDs, browser profiles, or troubleshooting runbooks into this skill. Use this skill as the routing and task-submission layer.

## When to use

Use this skill only when the user explicitly asks to submit one of these workflows:

- Refresh or manually run the current `TK竞品收集` competitor-table workflow.
- Search keyword products and write new competitor seed rows to `TK竞品收集`.
- Preview a batch of keyword-search rows for `TK竞品收集` or `TK选品收集`, then submit only after explicit confirmation.
- Sync influencer data from `TK竞品收集` to `TK达人池`.
- Ingest or complete data for `TK选品收集`.
- Search keyword products and write new selection seed rows to `TK选品收集`.

The user must express an execution action such as:

- `run`
- `sync`
- `update`
- `complete`
- `submit`
- `search and write`
- `collect and write`
- `手动跑一次`
- `提交一次`
- `补全`
- `同步`
- `更新`
- `写入`

## Do not use this skill

Do not use this skill when the user is only:

- asking what TikTok, TK, FastMoss, OpenClaw, or Feishu means
- asking how to design a table or workflow
- asking to analyze, edit, review, or debug this skill
- discussing TikTok competitor strategy without asking to update the current Feishu table
- discussing product-selection strategy without asking to write to `TK选品收集`
- asking about credentials, tokens, environment variables, browser profiles, Runtime DB, deployment, or troubleshooting
- saying only “FastMoss”, “TK竞品”, “写入飞书”, or “更新当前表” without a clear workflow or target table

## Required inputs

### `product_url`

TikTok product URL.

Rules:

- Extract only TikTok product URLs.
- If a single-URL workflow receives multiple URLs, ask the user to provide one URL.
- Do not infer competitor-table intent from URL alone.

### `search_keyword`

Keyword for FastMoss / TikTok product search.

Rules:

- Do not treat “FastMoss”, “找品”, “搜索”, “写入飞书”, “竞品”, or “选品” as the keyword.
- If keyword search is requested but no keyword is present, ask only for the keyword.

### `sales_7d_threshold`

Near-7-day sales threshold.

Defaults:

- `keyword_competitor_search`: `200`
- `keyword_selection_search`: `500`

Extraction examples:

- 7日销量300以上 -> 300
- 近7天销量超过300 -> 300
- 7d sales >= 300 -> 300
- 销量阈值300 -> 300

### `total_sales_threshold`

Total cumulative sales threshold.

Use only for `keyword_competitor_search` when the user explicitly says `总销量` or `累计销量`.

Rules:

- Extract from expressions such as `总销量超过 300`, `累计销量大于 200`, `total sales >= 300`.
- Do not map plain `销量阈值` or `销量超过 N` to this input unless the user explicitly says total/cumulative sales.
- When this input is present, do not add the default `sales_7d_threshold` unless the user also explicitly asks for a 7-day sales condition.

Extraction examples:

- 总销量超过300 -> 300
- 累计销量大于200 -> 200
- total sales >= 300 -> 300

### `price_range_max_threshold`

Price-range maximum threshold.

Use only for `keyword_selection_search`.

Defaults:

- `keyword_selection_search`: `10.99`

Rules:

- Extract from expressions such as `价格大于 10.99`, `price range maximum >= 10.99`, `价格区间最大值大于 12`.
- Price filtering is for candidate filtering only. Do not use it as a pagination stop condition.

### `max_candidates`

Maximum number of keyword-search candidates.

Defaults:

- `keyword_competitor_search`: `20`

Rules:

- User says `不限制候选数`, `全部`, or `所有满足条件商品` -> `0`.
- User says `最多 50 个`, `抓 50 条`, or `候选 50 条` -> `50`.

### `batch_keyword_items`

Confirmed batch keyword rows containing only keyword and sales threshold.

Rules:

- Use only after a batch keyword preview has been shown and the user explicitly confirms submission.
- Each row must contain `search_keyword` and may contain `threshold_type` plus `threshold_value`.
- Allowed `threshold_type` values are `sales_7d` and `total_sales`; `total_sales` is only for `TK竞品收集`.
- Do not include filters, max candidates, price threshold, or other optional parameters in v1 batch rows.
- If the user asks to change a row, regenerate the preview instead of submitting.

## Supported workflows

### `competitor_table_refresh`

- Kind: formal_workflow
- Task code: `refresh_current_competitor_table`
- Target table: `TK竞品收集`
- Trigger mode from requirements: daily scheduled task
- Conversation activation: explicit manual submission only

Use when the user explicitly asks to manually run, refresh, update, or sync the current competitor table.

Do not use for keyword search, product-selection search, influencer-pool sync, or general competitor-analysis discussion.

### `keyword_competitor_search`

- Kind: formal_workflow
- Task code: `search_keyword_competitor_products`
- Target table: `TK竞品收集`
- Trigger mode from requirements: OpenClaw conversation input

Use when the user asks to search or collect keyword products and write new competitor rows to `TK竞品收集`.

Do not use when the user says `选品`, `选品表`, or `TK选品收集`.

Default inputs:

- `sales_7d_threshold`: `200`
- `max_candidates`: `20`

### `influencer_pool_sync`

- Kind: formal_workflow
- Task code: `sync_tk_influencer_pool`
- Source table: `TK竞品收集`
- Target table: `TK达人池`
- Trigger mode from requirements: daily scheduled task
- Conversation activation: explicit manual submission only

Use when the user explicitly asks to sync influencer-pool data, expand influencers from competitor products, or update `TK达人池`.

### `selection_table_ingest`

- Kind: formal_workflow
- Task code: `tiktok_fastmoss_product_ingest`
- Target table: `TK选品收集`
- Trigger mode from requirements: OpenClaw scheduled or manual trigger

Use when the user asks to complete, ingest, update, or scan `TK选品收集`.

Business behavior summary:

- This workflow scans existing selection records and fills missing automatically maintained fields.

### `keyword_selection_search`

- Kind: formal_workflow
- Task code: `search_keyword_selection_products`
- Target table: `TK选品收集`
- Trigger mode from requirements: OpenClaw conversation input

Use when the user asks to search keyword products and write new selection seed rows to `TK选品收集`.

Business behavior summary:

- Search FastMoss product candidates by keyword.
- Filter candidates before insert.
- Skip existing products without overwrite, refresh, or detail fan-out.
- Insert new selection seed rows using `insert_if_absent`.
- Seed rows preserve keyword source.
- Only newly inserted rows trigger row-level `tiktok_fastmoss_product_ingest`.

Default inputs:

- `sales_7d_threshold`: `500`
- `price_range_max_threshold`: `10.99`

### `batch_keyword_search_submit`

- Kind: operational_sub_intent
- Parent task code: `search_keyword_competitor_products`
- Mode: confirmed_batch_keyword_search
- Target table: `TK竞品收集`, `TK选品收集`

Use only after the user explicitly confirms the latest formatted batch keyword-search preview. The preview itself is generated by the skill without running a command.

Business behavior summary:

- Submit one existing keyword Runtime task per confirmed row.
- Batch rows contain only keyword and sales threshold in v1.
- Runtime request FIFO handles execution order; this wrapper only submits rows sequentially.
- Return one request_id per successfully submitted row.

### `product_url_complete`

- Kind: operational_sub_intent
- Parent task code: `tiktok_fastmoss_product_ingest`
- Mode: single_product_url
- Target table: `TK选品收集`

Use only when the user provides one TikTok product URL and asks to complete that single product using selection-table semantics.

### `competitor_row_by_url`

- Kind: operational_sub_intent
- Parent task code: `refresh_current_competitor_table`
- Mode: single_competitor_row_by_url
- Target table: `TK竞品收集`

Use only when the user explicitly mentions competitor row, competitor URL, or `竞品表单行`, and provides one TikTok product URL.

## Workflow

1. Identify whether the user is asking to submit a task. If not, do not use this skill.
2. Select exactly one workflow.
3. For batch keyword-search requests, first generate a formatted preview in chat without running any command.
4. Batch keyword-search previews must show only target table, row count, keyword, and threshold.
5. Batch keyword-search previews must not expose filters, max candidates, price threshold, or other optional parameters in v1.
6. Submit a batch keyword search only after the user explicitly confirms the latest preview.
7. For confirmed batch keyword search, run `batch_keyword_search_submit` once with normalized keyword rows, then report one request_id per submitted row.
8. For non-batch workflows, extract only the inputs required by that workflow.
9. Apply workflow-specific defaults.
10. Validate missing or ambiguous inputs.
11. Run exactly one command for non-batch submissions and for confirmed batch submission.
12. Wait until the command exits and emits `__OPENCLAW_RESULT__`.
13. Parse `request_id` for single-task submissions or `request_ids` for batch submissions.
14. Reply using the required output format.
15. Do not poll Runtime jobs after task submission.

## Intent precedence

1. If the user confirms the latest formatted batch keyword-search preview, choose `batch_keyword_search_submit`.
2. If the user asks for multiple keyword searches in one request, generate a formatted batch preview first; do not submit until confirmation.
3. If the user explicitly says `竞品`, `竞品表`, or `TK竞品收集` and asks for keyword search or collection, choose `keyword_competitor_search` for one keyword or batch preview for multiple keywords.
4. If the user explicitly says `选品`, `选品表`, or `TK选品收集` and asks for keyword search or collection, choose `keyword_selection_search` for one keyword or batch preview for multiple keywords.
5. If the user asks to complete, ingest, scan, or update `TK选品收集` without keyword-search semantics, choose `selection_table_ingest`.
6. If the user asks to manually refresh, sync, or update the current competitor table, choose `competitor_table_refresh`.
7. If the user asks to sync influencer-pool data or expand influencers from competitor products, choose `influencer_pool_sync`.
8. If the message contains a TikTok product URL and explicitly mentions competitor row or competitor URL, choose `competitor_row_by_url`.
9. If the message contains a TikTok product URL and asks to complete a single product without competitor-table semantics, choose `product_url_complete`.
10. If the user asks for FastMoss keyword search or product collection but does not specify competitor table or selection table, ask which target table to write to. Do not submit a task.

## Commands

Prefer the dispatcher command below.

### `competitor_table_refresh`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "competitor_table_refresh"
```

### `keyword_competitor_search`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "keyword_competitor_search" --search-keyword "<search_keyword>" --sales-7d-threshold <sales_7d_threshold> --total-sales-threshold <total_sales_threshold> --max-candidates <max_candidates>
```

### `influencer_pool_sync`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "influencer_pool_sync"
```

### `selection_table_ingest`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "selection_table_ingest"
```

### `keyword_selection_search`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "keyword_selection_search" --search-keyword "<search_keyword>" --sales-7d-threshold <sales_7d_threshold> --price-range-max-threshold <price_range_max_threshold>
```

### `batch_keyword_search_submit`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "batch_keyword_search_submit" --target-intent "<keyword_competitor_search|keyword_selection_search>" --items-json '<batch_keyword_items>'
```

### `product_url_complete`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "product_url_complete" --product-url "<product_url>"
```

### `competitor_row_by_url`

```bash
bash skills/mujitask-tiktok-feishu-sync/run_task.sh --intent "competitor_row_by_url" --product-url "<product_url>"
```

## Output format

Successful task submission must reply exactly:

```text
request_id: <request_id>
```

Failed task submission must reply exactly:

```text
任务提交失败：<short safe reason>
```

Missing input may ask only for the missing field.

Examples:

```text
请提供要搜索的关键词。
```

```text
请确认写入目标表：TK竞品收集 还是 TK选品收集？
```

## Guardrails

- Do not submit a task unless the user explicitly asks to run, update, sync, complete, collect, search-and-write, or submit.
- Do not route `选品表` requests to competitor workflows.
- Do not route `竞品表` requests to selection workflows.
- Do not use a generic keyword-search workflow when the target table is ambiguous.
- Do not submit batch keyword searches during preview; wait for explicit confirmation.
- Do not accept filters, custom max candidates, price threshold, or other optional parameters in v1 batch keyword rows.
- Do not submit stale batch preview content if the user asks to modify any row; regenerate the preview first.
- Do not ask users for credentials, tokens, table IDs, browser profiles, Runtime DB, or deployment settings.
- Do not print configuration values.
- Do not expose cookies, tokens, env vars, stack traces, table IDs, or browser profile paths.
- Do not run legacy leaf steps or troubleshooting wrappers.
- Do not poll Runtime jobs after task submission.
- Do not promise to report final results in this chat.
- Do not include internal step counts, candidates, browser details, or worker details in the first reply.

## Edge cases

- Keyword search without keyword: ask only for the keyword.
- Keyword search without target table: ask whether to write to `TK竞品收集` or `TK选品收集`.
- Batch keyword search: preview only keyword and threshold, then wait for explicit confirmation.
- Batch keyword search with filters or custom max candidates: explain that v1 only supports keyword and threshold, then preview supported fields only or ask the user to simplify.
- Batch keyword search row modification after preview: regenerate preview and wait for confirmation again.
- Single-URL workflow with multiple URLs: ask for one URL.
- TikTok URL without table semantics: use `product_url_complete` only if the user asks to complete a product; otherwise ask for the intended workflow.
- Wrapper exits without `request_id`: treat as failed submission.
- Wrapper returns failed/error: return only the safe failure summary.
- Runtime / Feishu / FastMoss / browser unavailable: do not switch to another workflow.

## Final checks

Before replying, verify:

- Exactly one workflow was selected.
- Required inputs were extracted or requested.
- Defaults match the selected workflow.
- Target table matches the selected workflow.
- Formal workflow maps to a valid `task_code`.
- Operational sub-intent maps to a valid `parent_task_code`.
- The command emitted `__OPENCLAW_RESULT__`.
- Successful single-task reply contains only `request_id: <request_id>`.
- Successful batch submit reply lists each keyword row with its `request_id`.
- Failure reply uses `任务提交失败：<short safe reason>`.

## Examples

User: 手动跑一次竞品采集
Intent: `competitor_table_refresh`
Reply:

```text
request_id: <request_id>
```

User: 帮我查询关键字 east egg 的 7日销量大于200的 TK 商品，写入 TK竞品收集
Intent: `keyword_competitor_search`
Inputs:

- `search_keyword`: `east egg`
- `sales_7d_threshold`: `200`
- `max_candidates`: `20`

Reply:

```text
request_id: <request_id>
```

User: 帮我按关键词 east egg 搜索 7日销量大于500 且价格大于10.99 的 TK 商品，写入 TK选品表
Intent: `keyword_selection_search`
Inputs:

- `search_keyword`: `east egg`
- `sales_7d_threshold`: `500`
- `price_range_max_threshold`: `10.99`

Reply:

```text
request_id: <request_id>
```

User: 批量搜索关键词 dog toy 总销量大于300、cat toy 7日销量大于200，写入 TK竞品收集
Intent: `batch_keyword_search_submit`
Inputs:

- `batch_keyword_items`: `preview first, then confirmed rows with search_keyword and threshold`

Reply:

```text
已提交批量关键词搜索任务：
1. dog toy request_id: <request_id>
2. cat toy request_id: <request_id>
```

User: 补全TK选品表
Intent: `selection_table_ingest`
Reply:

```text
request_id: <request_id>
```

User: 达人池同步
Intent: `influencer_pool_sync`
Reply:

```text
request_id: <request_id>
```

User: 补全这个商品 https://www.tiktok.com/shop/pdp/123
Intent: `product_url_complete`
Inputs:

- `product_url`: `https://www.tiktok.com/shop/pdp/123`

Reply:

```text
request_id: <request_id>
```

User: 竞品表单行补全 https://www.tiktok.com/shop/pdp/123
Intent: `competitor_row_by_url`
Inputs:

- `product_url`: `https://www.tiktok.com/shop/pdp/123`

Reply:

```text
request_id: <request_id>
```

## Negative activation examples

User: FastMoss 是什么？
Reason: concept question, no task submission requested.

User: 帮我分析这个 skill 的问题
Reason: skill review request, not a TikTok/Feishu task submission.

User: TikTok竞品分析一般怎么做？
Reason: strategy discussion, no current Feishu competitor workflow requested.

User: TK选品表字段怎么设计？
Reason: table-design discussion, not selection-table ingest or keyword-selection search.

User: 搜索 FastMoss 商品并写入飞书
Reason: target table is ambiguous. Ask whether to write to `TK竞品收集` or `TK选品收集`.
