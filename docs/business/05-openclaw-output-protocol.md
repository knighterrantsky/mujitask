# OpenClaw 输出协议

更新时间：`2026-04-03`

本文件用于固化 OpenClaw skill 的输出协议，重点解决一个问题：

- 长任务已经被成功拉起并持续运行
- 但 OpenClaw 调用端不适合直接消费底层 CLI 的超大最终 JSON
- 需要一个稳定、短小、可机读的同步返回格式

## 1. 适用范围

当前协议先面向：

- OpenClaw `gateway` 与实际执行宿主机是同一台 macOS
- OpenClaw 通过主入口脚本同步执行 skill
- 业务配置仍然只从 `skill.local.env` 读取

当前不在本协议首期范围内的内容：

- 远程节点回传
- webhook callback
- 独立的异步 `start/result` 双入口

这些内容保留为后续增强方向，但不是本次落地目标。

## 2. 设计目标

同步方案必须同时满足：

1. 保留运行中可观察性  
   主入口和内部包装脚本仍然要持续输出阶段日志、进度日志、心跳日志。

2. 避免直接把底层超大 JSON 暴露给 OpenClaw  
   原始 CLI 输出改为落盘保存，不再直接透传到调用端。

3. 提供单行、稳定、可机读的最终结果  
   主入口脚本完成后，最后输出固定前缀结果行，供 OpenClaw 解析。

4. 保留完整排障路径  
   一旦失败，仍然可以通过 `run_id` 和 `runtime/cli_runs/*` 文件回放。

## 3. 同步输出协议

### 3.1 运行中输出

运行过程中继续输出：

- 阶段日志
- `run_id`
- `run_file`
- `steps_file`
- 心跳日志

例如：

```text
[feishu-tiktok-sync] Step 1/3: normalizing and deduplicating TikTok links in Feishu
[cleanup] Running tiktok_product_link_cleanup with run_mode=canary run_id=openclaw-cleanup-...
[cleanup] Progress files: run_file=... steps_file=...
[feishu-tiktok-sync] Step 2/3: scanning pending competitor rows in Feishu
[pending-rows] Running feishu_pending_rows_scan with run_mode=canary run_id=openclaw-pending-...
[pending-rows] Progress files: run_file=... steps_file=...
[feishu-tiktok-sync] Step 3/N: updating pending competitor rows one by one
[single-row-update] Running feishu_single_row_update for record_id=recXXXX run_mode=canary run_id=openclaw-update-...
[single-row-update] Progress files: run_file=... steps_file=...
[single-row-update] Heartbeat: run is still active; waiting for the next workflow update
```

### 3.2 原始 CLI 输出

底层 CLI 的完整 stdout/stderr 不再直接打印到 OpenClaw 调用端，而是落盘保存到：

- `runtime/cli_runs/stdout/<run_id>.log`

这样做的目的：

- 避免 OpenClaw 被超大最终 JSON 淹没
- 保留排障原始信息
- 让同步调用端只消费短摘要

### 3.3 最终结果行

主入口脚本结束前，必须输出一行固定前缀：

```text
__OPENCLAW_RESULT__ <json>
```

说明：

- 前缀固定为 `__OPENCLAW_RESULT__`
- `<json>` 必须是一行 JSON
- 这行输出是主入口脚本的最终机器结果
- 进程退出本身就是 EOF，不额外设计单独 EOF 文件

### 3.4 结果 JSON 最小字段

主入口返回的 JSON 至少包含：

- `status`
- `task_name`
- `message`
- `summary`
- `cleanup`
- `pending_rows`
- `updates`

推荐字段：

- `run_id`
- `summary_text`
- `failed_item_count`
- `error`

其中：

- `cleanup` 为前置链接清理阶段的短摘要
- `pending_rows` 为待更新行扫描阶段的短摘要
- `updates` 为逐条更新阶段的短摘要
- `summary` 默认复用 `updates` 阶段的 `summary`

## 4. 错误与排障

如果同步调用失败，优先检查：

- `runtime/cli_runs/<run_id>.json`
- `runtime/cli_runs/steps/<run_id>.json`
- `runtime/cli_runs/signals/<run_id>.json`
- `runtime/cli_runs/stdout/<run_id>.log`

如果主入口在 cleanup 阶段失败：

- 仍然输出 `__OPENCLAW_RESULT__ <json>`
- 其中 `status = failed`
- `pending_rows` 和 `updates` 可能为空

如果主入口在 pending rows 阶段失败：

- 仍然输出 `__OPENCLAW_RESULT__ <json>`
- `cleanup` 会保留已完成阶段的摘要
- `updates` 可能为空

如果主入口在逐条更新阶段失败：

- 仍然输出 `__OPENCLAW_RESULT__ <json>`
- `cleanup` 和 `pending_rows` 会保留已完成阶段的摘要
- `updates.error` 或顶层 `error` 会给出失败说明

## 5. 与后续异步方案的关系

当前同步协议不是异步方案的替代品，而是后续异步能力的兼容前置层。

后续如果补异步双入口，推荐演进为：

1. `start` 立即返回 `run_id`
2. `result` 或 `status` 基于 `run_id` 读取 `runtime/cli_runs/*.json`

同步协议中已经保留了这些兼容点：

- 每个关键阶段都有 `run_id`
- 所有运行结果都已经落盘
- 最终输出 JSON 中保留了 `run_file / steps_file / signals_file / stdout_file` 的引用能力
