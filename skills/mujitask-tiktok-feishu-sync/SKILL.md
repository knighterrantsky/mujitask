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
- 如果用户表达的是“更新当前表 / 同步当前竞品表 / 定时抓取竞品数据”，走定时更新入口。
- 如果用户表达的是“搜索 / 查询 / 收集 FastMoss 商品并写入飞书”，走关键词搜索入口。

## 输入提取规则

- 只从用户输入中提取：
  - `关键词`
  - `7日销量阈值`
- 如果用户没有明确提供 `7日销量阈值`，默认使用 `200`。
- 如果用户明确提供了阈值，例如 `300`，则使用用户提供的值。

## 固定配置

- 从 `skill.local.env` 读取：
  - `INSTALL_DIR`
  - `TABLE_URL`
  - `FEISHU_ACCESS_TOKEN`
  - `BROWSER_PROFILE_REF`
  - `FASTMOSS_PHONE`
  - `FASTMOSS_PASSWORD`
- 不要在对话中向用户索取这些固定配置。

## 执行约束

- 不要输出设计意图、版本目标、历史重构信息。
- 用户如果要求“写入当前飞书表”，则默认自动执行完整后续流程，不要停在中间步骤等待用户追加指令。
- 默认在一次用户请求内自动推进到详情补全结束；只有遇到真实错误、安全验证未解除或明确停止条件时，才允许中断。
- 用户如果要求“写入当前飞书表”，则不能在只完成 `keyword-candidates` 或 `insert-seed-row` 后回复“已完成”。
- 定时更新入口必须按这个顺序执行：
  1. `run_fastmoss_login_check_step.sh`
  2. `run_cleanup_step.sh`
  3. `run_pending_rows_step.sh`
  4. 对 `target_rows` 执行 `run_single_row_update_step.sh`
- 关键词搜索入口必须按这个顺序执行：
  1. `run_fastmoss_login_check_step.sh`
  2. `run_keyword_candidate_step.sh`
  3. 对每个 `candidate_new` 执行 `run_insert_seed_row_step.sh`
  4. 对新写入的 `record_id` 执行 `run_single_row_update_step.sh`
- 如果 `run_insert_seed_row_step.sh` 没有返回新的 `record_id`，不要把该商品计入“详情已补全”。

## 输出约束

- 默认只向用户输出最终结果摘要，不展开内部 step 编排细节。
- 不要把候选发现、种子写入、内部续跑这些过程当成主要输出内容。
- 除非用户主动要求详细过程，否则不要把候选数、步骤名、内部子任务分段作为常规输出。
- 如果流程已经完整执行到详情补全结束，可以回复“已完成”。
- 如果流程因为真实错误或安全验证等原因未能自动完成，才说明未完成原因。
- 如果内部为了稳定性拆成多个子任务，继续自动衔接执行；除非确实未完成，否则不要把中间分段细节暴露给用户。

## 定时更新入口

用户语义示例：

- 更新当前飞书竞品表
- 执行每日竞品数据同步
- 对当前飞书表做定时刷新

推荐步骤：

1. `bash run_fastmoss_login_check_step.sh`
2. `bash run_cleanup_step.sh`
3. `bash run_pending_rows_step.sh`
4. 从 pending 结果中读取 `target_rows`
5. 对 `target_rows` 继续执行：
   - `bash run_single_row_update_step.sh --record-id <record_id> --skip-fastmoss-login-validation`

固定规则：

- 必须先执行链接标准化与去重。
- 商品详情更新时，必须先抓 TikTok 信息，再进入 FastMoss 详情页。
- 打开 FastMoss 商品详情页后必须先截图，再抓取销量信息。

## 关键词搜索入口

用户语义示例：

- 帮我查询关键字为 `east egg` 的 7 日内销量大于 200 的 TK 商品数据
- 帮我搜一下 `easter egg` 的 TK 竞品并写入当前飞书表
- 搜索 FastMoss 中关键词为 `easter egg` 的商品并写入飞书
- 收集关键词为 `graduation gifts` 的 TK 竞品

推荐步骤：

1. `bash run_fastmoss_login_check_step.sh`
2. `bash run_keyword_candidate_step.sh --search-keyword "<keyword>" --sales-7d-threshold <number> --skip-fastmoss-login-validation`
3. 读取关键词候选结果中的 `target_items`
4. 对每个 `candidate_new` 逐条执行：
   - `bash run_insert_seed_row_step.sh --sku-id <sku_id> --search-keyword "<keyword>"`
5. 从每次 seed insert 结果中读取 `item.record_id`
6. 对新插入的 `record_id` 继续执行：
   - `bash run_single_row_update_step.sh --record-id <record_id> --skip-fastmoss-login-validation`

如果用户没有指定 `7日销量阈值`，用：

- `bash run_keyword_candidate_step.sh --search-keyword "<keyword>" --skip-fastmoss-login-validation`

此时默认阈值为 `200`。

固定规则：

- 数据源固定为 FastMoss。
- 搜索结果固定按 `近7天销量` 排序并翻页穷举。
- 候选商品必须先按 `SKU-ID` 判重，再按标准化后的 `产品链接` 判重。
- 只要命中任一判重条件，就视为已存在商品并直接跳过。
- 新商品先写入 `SKU-ID`、`产品链接`、`备注`，再进入详情补全。
- `备注` 写入格式固定为：`通过搜索关键字：{关键词}`。

## 运行时处理规则

- 如果同一批次里已经做过 FastMoss 登录校验，后续步骤复用 `--skip-fastmoss-login-validation`。
- 如果为了稳定性需要拆成多个内部子任务，默认自动继续，不要等待用户追加同类指令。
- 如果 TikTok 遇到安全验证，先给人工介入窗口；窗口结束后仍未解除，再按跳过处理。
- 结束前依然输出 `__OPENCLAW_RESULT__ <json>`。
- 优先让 OpenClaw 根据结果 JSON 继续后续步骤，而不是依赖自然语言猜测。
