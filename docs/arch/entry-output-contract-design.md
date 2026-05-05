# 入口与输出契约设计

日期: 2026-04-23

## 1. 定位

本文描述 OpenClaw / CLI / Skill 入口与系统输出契约。它承接旧 OpenClaw 输出协议文档中的技术协议部分；客户需求和业务验收仍以 `docs/business` 为准。

核心原则:

> 入口层只负责识别意图、提取参数、提交顶层 Task，并返回可追踪的 `request_id`；长流程执行、结果汇总和通知分发由 Runtime DB、executor、worker 和 outbox 完成。

## 2. 入口层职责

入口层包括:

- OpenClaw skill 脚本
- CLI 调用
- 定时任务触发器
- 未来可能的 webhook/API submitter

入口层只应该做:

- 参数提取和最小校验。
- 选择 `task_code`。
- 构造 `payload_json`。
- 写入或提交 `task_request`。
- 同步返回 `request_id`、当前状态和简短说明。

入口层不应该做:

- 陪跑完整长任务。
- 直接执行浏览器或批量 API 采集。
- 直接判断父子任务完成。
- 直接发送最终通知。
- 持有只能存在于进程内存中的任务状态。

### 2.1 Skill / CLI submit payload 边界

正式 Skill submit 的 payload 只承载业务输入和可追踪回执信息。业务输入包括商品 URL、关键词、飞书表 ref / table URL、字段筛选、采集范围、FastMoss / 飞书账号 env ref、通知目标等。

正式 Skill submit 不承载项目运行配置:

- 不传 `run_mode`。`run_mode` 只属于本地调试或测试 submit override，不是正式 Skill 契约。
- 不传 Runtime DB、Fact DB、MinIO/S3 endpoint、access key、secret key、bucket 等真实运行配置。
- 不传 `fact_db_url`、`db_url`、`execution_control_db_url`、`execution_control_fact_db_url`、`minio_secret_key`、`s3_secret_key` 这类连接或密钥字段。
- 不从 `skill.local.env` 读取浏览器固定资源配置；`BROWSER_PROFILE_REF`、`BROWSER_PROVIDER_NAME`、`BROWSER_PROFILE_ID`、`BROWSER_WORKSPACE_ID`、`BROWSER_PROFILES_FILE`、`DEFAULT_PROFILE_REF` 这类默认值属于项目运行配置。正式 Skill 只允许用户通过 CLI 参数显式覆盖本次业务使用的 `profile_ref`。

正式 submit 由 Runtime 控制面从项目运行配置解析 Runtime DB、Fact DB 和对象存储，并在创建 `task_request` 前做 preflight。缺 Runtime DB、Fact DB、artifact provider、bucket 或 MinIO/S3 必填配置时，submit 必须被拒绝，不能让后续 handler 退化成 `dry_run` 或本地 `local` 成功。

测试可以直接调用 submit 入口并携带显式 test-only override，用于隔离测试数据库、MinIO/S3 或本地 fixture；这类 override 不能由正式 Skill 自动注入，也不能成为长期业务 payload 字段。

## 3. 同步返回契约

入口层同步返回必须短小、可机读、可追踪。

推荐最小字段:

| 字段 | 说明 |
| --- | --- |
| `ok` | submit 是否成功 |
| `request_id` | Runtime DB 顶层任务 ID |
| `task_code` | 顶层任务类型 |
| `status` | 当前任务状态，通常为 `pending` 或 `waiting_children` |
| `message` | 面向调用方的简短说明 |
| `result_url` | 可选，后续查询或回执入口 |
| `reply_target` | 可选，最终通知目标 |

CLI / OpenClaw 兼容输出可继续使用 `__OPENCLAW_RESULT__` 作为最终一行机器可读 JSON，但该 JSON 不应塞入完整运行细节。

## 4. 异步结果契约

长流程最终结果由 executor 汇总并写入:

- `task_request.summary_json`
- `task_request.result_json`
- `notification_outbox.payload_json`

最终通知由 `outbox_dispatcher` 发送。通知失败不反向污染主业务成功状态。

### 4.1 Outbox 消息格式契约

`notification_outbox.payload_json` 必须同时保留可机读结果和面向人的通知文本:

| 字段 | 说明 |
| --- | --- |
| `summary_payload` / `summary` | 机器可读摘要，供排障、审计和二次消费使用 |
| `result` | 机器可读结果，包含行级、商品组或候选写入明细 |
| `message_text` | 默认发送给飞书/OpenClaw/console 的人类可读文本 |

默认 `message_text` 必须使用人类可读格式，不能把一整段压缩 JSON 作为默认飞书消息。JSON 格式只能作为显式调试格式使用。

支持的 message format:

| format | 行为 |
| --- | --- |
| `plain_text_detail` | 默认。摘要 + 关键明细，例如 SKU、record、status、失败原因。 |
| `plain_text_summary` | 只输出摘要，不展开明细。适合大量数据或群聊。 |
| `json` | 原始 JSON 文本，仅用于调试或机器消费。 |
| `template` | 使用调用方提供的模板渲染文本。 |

配置优先级:

1. Task payload 中的 `outbox_message_template`。存在时强制使用 `template`。
2. Task payload 中的 `outbox_message_format`。
3. 环境变量 `MUJITASK_OUTBOX_MESSAGE_FORMAT`。
4. 默认 `plain_text_detail`。

各 workflow 的 `ready_for_summary` 必须通过领域 projection 生成 `message_text`，不能在 flow 中手写压缩 JSON，也不能让 outbox channel handler 反向理解业务 result 结构。

当前 TikTok 三个主流程的默认文案约束:

| task_code | 默认标题 | 必须包含 |
| --- | --- | --- |
| `refresh_current_competitor_table` | `TK竞品表刷新完成` | request、final_status、总数、更新/成功/失败数、每条 SKU/record/status/失败原因 |
| `search_keyword_competitor_products` | `关键词竞品入库完成` | request、final_status、关键词、候选数、种子写入/跳过/失败数、详情成功/失败数、每条 SKU/record/status/失败原因 |
| `sync_tk_influencer_pool` | `TK达人池同步完成` | request、final_status、商品组数、商品组状态计数、子任务成功数、每个商品组 SKU/record/status/creator/pool write 摘要 |

推荐最终 result 包含:

| 字段 | 说明 |
| --- | --- |
| `request_id` | 顶层任务 |
| `task_code` | 任务类型 |
| `final_status` | `success / failed / partial_success` |
| `summary` | 业务摘要 |
| `counts` | 成功、失败、跳过、去重等计数 |
| `failed_items` | 失败明细摘要，不放超大 payload |
| `artifact_uri_prefix` | 可选，运行产物入口 |
| `run_object_key` / `steps_object_key` / `stdout_object_key` | 可选，关键 artifact 索引 |

## 5. 查询契约

入口提交后，状态查询应以 Runtime DB 为准:

```text
request_id -> task_request -> child jobs -> artifacts -> outbox
```

建议保留三类动作:

| 动作 | 说明 |
| --- | --- |
| `submit` | 创建或提交顶层 Task |
| `status` | 查询当前任务状态和进度摘要 |
| `result` | 查询终态结果和 artifact 索引 |

这些动作可以由 CLI、Skill 或未来 API 承载，但语义必须一致。

## 6. 错误契约

同步 submit 失败只表示任务没有成功进入 Runtime DB，常见原因:

- 参数缺失。
- `task_code` 不支持。
- Runtime DB 不可用。
- 幂等键冲突且无法返回已有任务。

异步执行失败表示任务已进入 Runtime DB，但 workflow 或 job 执行失败。异步失败应写入:

- `task_request.error_text`
- job `error_text` / `last_error_text`
- `error_type`
- `error_code`
- `artifact_object` 中的排障产物

## 7. 与其他架构文档关系

- Task / Workflow / Job 拆分规则见 [workflow-design-guidelines.md](./workflow-design-guidelines.md)。
- Runtime 状态和队列表见 [runtime-db-schema-design.md](./runtime-db-schema-design.md)。
- Outbox 和整体进程关系见 [system-architecture-design.md](./system-architecture-design.md)。
- Artifact 输出和 MinIO 规则见 [storage-architecture-design.md](./storage-architecture-design.md)。
