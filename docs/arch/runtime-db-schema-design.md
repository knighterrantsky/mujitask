# Runtime DB Schema 设计

日期: 2026-04-23

## 1. 定位

Runtime DB 是系统的执行控制面，负责保存任务、队列、worker claim、lease、heartbeat、retry、outbox、artifact 索引等运行状态。

它不保存最终业务事实，不承担 TikTok / FastMoss / 飞书主体数据的主档职责。事实沉淀应进入 Fact DB，文件内容应进入 MinIO 或本地对象存储。

核心判断:

> Runtime DB 回答“任务怎么跑、跑到哪、谁在跑、是否可重试、是否需要兜底”；Fact DB 回答“采集到了什么事实”。

### 1.1 Schema 变更治理

Runtime DB 是生产执行控制面，schema 不能作为普通业务代码的内部实现细节随意变更。

生产约束:

- daemon / worker / dispatcher / watchdog 只能使用运行账号连接 Runtime DB。
- 运行账号只允许 `SELECT / INSERT / UPDATE / DELETE`，不允许 `CREATE TABLE`、`ALTER TABLE`、`DROP TABLE`、`CREATE INDEX`。
- Runtime schema 变更必须通过 migration 流程执行，并由 migration 账号完成。
- 生产进程启动时只做 schema version / migration version 校验；版本不匹配时应 fail fast，不继续 claim job。
- 本地开发或首次 bootstrap 可以保留自动建表能力，但该能力不能成为生产任务消费路径的一部分。

任何 Runtime schema 变更都必须说明:

| 变更项 | 必须说明 |
| --- | --- |
| 新增字段 | 默认值、旧数据回填方式、旧 worker 是否兼容 |
| 删除字段 | 下游代码和文档引用是否清理、是否经过 deprecation 周期 |
| 字段类型变更 | 数据迁移方式、失败回滚方式、索引影响 |
| 状态枚举变更 | Reconciler、Watchdog、Supervisor、summary 逻辑影响 |
| 索引/唯一键变更 | claim 性能、dedupe/idempotency 影响 |
| retry/lease 字段变更 | 卡死兜底、重试次数、死信策略影响 |

推荐账号模型:

| 账号 | 使用者 | 权限 |
| --- | --- | --- |
| `mujitask_runtime_user` | `executor_daemon`、`api_worker`、`browser_worker`、`outbox_dispatcher`、`watchdog` | Runtime 表读写，不含 DDL |
| `mujitask_migration_user` | CI/CD migration 或人工发布 | Runtime schema DDL |
| `mujitask_readonly_user` | 排障、报表、只读分析 | Runtime 表只读 |

## 2. Runtime DB 总体关系

```mermaid
erDiagram
    task_request ||--o{ task_execution : owns_browser_jobs
    task_request ||--o{ api_worker_job : owns_api_jobs
    task_execution ||--o| resource_lease : holds_browser_resource
    task_request ||--o{ notification_outbox : emits_notification
    task_request ||--o{ artifact_object : produces_artifact
    task_execution ||--o{ artifact_object : produces_artifact
```

目标 Runtime DB 使用“顶层 Task + 通用执行队列 + Outbox + Artifact”的结构。新 workflow 不新增业务专用 job 表:

- `task_request`: 顶层 Task。
- `api_worker_job`: API/IO 类型通用 job 队列。
- `task_execution`: browser/CDP 类型执行队列。
- `notification_outbox`: 结果通知分发队列。
- `resource_lease`: 浏览器资源租约。
- `artifact_object`: 运行产物索引。
- `fastmoss_session_cookie_cache`: FastMoss cookie/session 运行缓存。

当前代码中仍存在达人同步专用历史 job 表。它们只作为迁移来源和兼容事实记录，不作为目标 workflow contract，也不允许新业务流程继续扩展同类表。

## 3. 表设计

### 3.1 `task_request`

顶层任务表，一条记录对应用户提交的一次 Task。

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `request_id` | 顶层任务 ID，主键 |
| 业务路由 | `project_code`, `skill_code`, `task_name`, `task_code`, `resource_code` | executor 根据这些字段选择 workflow |
| 来源 | `trigger_mode`, `source_channel_code`, `source_session_id`, `reply_target`, `requested_by` | 任务来源与回复目标 |
| 输入 | `payload_json`, `idempotency_key` | 任务输入与顶层幂等键 |
| 状态 | `status`, `current_stage`, `stage_cursor_json` | workflow 状态机和阶段游标 |
| 汇总 | `summary_json`, `result_json`, `error_text` | 最终摘要、结果和错误 |
| 子任务计数 | `child_total_count`, `child_terminal_count`, `child_success_count`, `child_failed_count`, `child_skipped_count` | Reconciler 判断父任务是否可收敛 |
| claim | `worker_id`, `lease_until`, `heartbeat_at` | executor 领取顶层任务时的租约 |
| 时间 | `created_at`, `updated_at`, `started_at`, `finished_at` | 生命周期时间 |

关键索引:

- `idx_task_request_status_created_at(status, created_at)`
- `idx_task_request_task_code_created_at(task_code, created_at)`
- `idx_task_request_status_lease_until(status, lease_until)`

### 3.2 `task_execution`

Browser worker 消费的执行队列。当前用于需要浏览器/CDP/Profile 资源的任务，例如 TikTok browser fallback、登录态页面采集或其他 request/API 无法完成的页面动作。

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `execution_id` | 执行 ID，主键 |
| 归属 | `request_id` | 所属顶层 Task |
| 路由 | `task_name`, `item_code`, `workflow_code`, `business_key`, `dedupe_key`, `resource_code` | worker 和 handler 路由字段 |
| 队列 | `status`, `queue_seq`, `available_at` | 可领取状态、顺序和重试可用时间 |
| 执行 | `worker_id`, `attempt_count`, `max_attempts`, `run_id` | 当前执行者、重试次数、执行实例 |
| 数据 | `payload_json`, `summary_json`, `result_json`, `error_text` | 输入、摘要、结果、错误 |
| 时间 | `created_at`, `updated_at`, `started_at`, `finished_at`, `heartbeat_at` | 生命周期时间 |

关键索引:

- `idx_task_execution_request_created_at(request_id, created_at)`
- `idx_task_execution_status_available_queue_seq(status, available_at, queue_seq)`
- `idx_task_execution_resource_status_available_queue_seq(resource_code, status, available_at, queue_seq)`

### 3.3 `api_worker_job`

API worker 消费的通用 job 队列。适合飞书 API 读取/写回、FastMoss API/HTTP、事实库写入、对象上传、fan-out/finalizer 等不依赖浏览器 profile 的任务。

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `job_id` | job ID，主键 |
| 归属 | `request_id`, `task_code` | 所属 Task 和业务类型 |
| 路由 | `job_code`, `business_key`, `dedupe_key`, `stage` | handler 路由、业务键、幂等键、阶段 |
| 队列 | `status`, `available_at` | 可领取状态和重试时间 |
| 执行 | `attempt_count`, `max_attempts`, `worker_id`, `lease_until`, `run_id` | 执行生命周期 |
| 数据 | `payload_json`, `summary_json`, `result_json`, `error_text` | 输入、摘要、结果、错误 |
| 时间 | `created_at`, `updated_at`, `started_at`, `finished_at`, `heartbeat_at` | 生命周期时间 |

关键索引:

- `idx_api_worker_job_status_available_created(status, available_at, created_at)`
- `idx_api_worker_job_request_created(request_id, created_at)`
- `idx_api_worker_job_job_code_status_available(job_code, status, available_at)`
- `idx_api_worker_job_dedupe_key(dedupe_key)`，仅 `dedupe_key <> ''` 时唯一。

### 3.4 `api_worker_job` 父子与实体关联字段

为了支撑达人同步这类“商品发现 -> 达人详情 -> 飞书投影”的 fan-out / fan-in 流程，目标做法是在通用 job 表中补齐通用关联字段，而不是新增业务专用 job 表。

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 父子关系 | `parent_job_id`, `root_job_id`, `job_group` | 表达 product discovery job 与 creator detail jobs 的收敛关系 |
| 业务实体 | `entity_type`, `entity_key` | 表达当前 job 绑定的商品、达人、视频、飞书记录等实体 |
| 幂等辅助 | `dedupe_key` | 推荐格式为 `request_id:stage:entity_type:entity_key` |
| 进度 | `progress_stage`, `last_progress_at` | Watchdog 和 Reconciler 判断 job 是否卡死 |
| 失败分类 | `error_type`, `error_code`, `error_path`, `dead_letter_reason` | 统一错误归因和死信处理 |

达人同步示例:

- 商品发现 job: `job_code=fastmoss_product_fetch`, `stage=discover_related_creators`, `entity_type=product`, `entity_key=product_id`。
- 达人详情 job: `job_code=fastmoss_creator_fetch`, `stage=collect_creator_detail`, `entity_type=creator`, `entity_key=influencer_id`, `parent_job_id=<product discovery job>`。
- 达人池写回 job: `job_code=feishu_table_write`, `stage=write_influencer_pool`, `entity_type=creator_projection`, `entity_key=source_record_id:influencer_id`。

### 3.5 `resource_lease`

浏览器资源租约表。它保护 browser profile / CDP 资源，避免多个 browser worker 同时使用同一资源。

| 字段 | 说明 |
| --- | --- |
| `resource_code` | 资源编码，主键 |
| `execution_id` | 当前持有该资源的 browser execution |
| `request_id` | 所属 Task |
| `worker_id` | 持有者 |
| `status` | 租约状态 |
| `lease_until`, `heartbeat_at` | 过期和心跳时间 |
| `created_at`, `updated_at` | 时间戳 |

关键索引:

- `idx_resource_lease_lease_until(lease_until)`

### 3.6 `notification_outbox`

结果通知分发队列。业务完成和通知发送解耦，避免通知失败反向污染主流程完成状态。

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `outbox_id` | outbox ID |
| 路由 | `channel_code`, `event_type`, `ref_type`, `ref_id`, `reply_target` | 通知渠道、事件和引用对象 |
| 幂等 | `dedupe_key` | 同一通知事件去重 |
| 队列 | `status`, `retry_count`, `max_retry_count`, `next_retry_at` | 分发状态和重试计划 |
| 执行 | `worker_id`, `lease_until`, `heartbeat_at` | dispatcher claim 信息 |
| 数据 | `payload_json`, `last_error_text` | 通知内容和错误 |
| 时间 | `sent_at`, `created_at`, `updated_at` | 发送和更新时间 |

关键索引:

- `idx_notification_outbox_dedupe_key(dedupe_key)`，仅 `dedupe_key <> ''` 时唯一。
- `idx_notification_outbox_status_next_retry_at(status, next_retry_at)`
- `idx_notification_outbox_ref_type_ref_id(ref_type, ref_id)`
- `idx_notification_outbox_status_lease_until(status, lease_until)`

#### 3.6.1 Outbox Channel 配置模型

`notification_outbox` 只保存路由事实和消息内容，不保存飞书应用密钥。

| 字段 / 参数 | 说明 |
| --- | --- |
| `channel_code` | 发送通道。准入值包括 `noop`、`disabled`、`stdout`、`console`、`webhook`、`feishu_bot_api`、`feishu_direct_api`、`openclaw_message`、`feishu_openclaw`。 |
| `reply_target` | 接收目标。可以是 JSON object、Python dict repr 或简写字符串。Feishu 目标支持 `user:ou_xxx`、`open_id:ou_xxx`、`chat:oc_xxx`、`group:oc_xxx`。 |
| `accountId` / `account_id` | `reply_target` 中的账号选择字段。缺省使用通道配置的默认账号。 |
| `payload_json.message_text` | 最终发送文本。必须由 workflow/domain projection 默认生成人类可读文本；压缩 JSON 只能在显式 `message_format=json` 时使用。 |
| `payload_json.message_format` / task `outbox_message_format` | 可选消息格式。支持 `plain_text_detail`、`plain_text_summary`、`json`、`template`。 |
| task `outbox_message_template` | 可选模板。存在时优先于 message format。 |
| `payload_json.dry_run` | 仅用于显式本地演练。生产 outbox 不应依赖 dry-run。未知 channel 不能因为 dry-run 被标记为成功。 |

Feishu `reply_target.to` 归一化规则:

| 输入形式 | 飞书 `receive_id_type` | 来源 |
| --- | --- | --- |
| `user:ou_xxx` / `open_id:ou_xxx` | `open_id` | 飞书用户 open id。 |
| `chat:oc_xxx` / `group:oc_xxx` / `channel:oc_xxx` | `chat_id` | 飞书群 chat id。 |
| 裸 `oc_xxx` | `chat_id` | 飞书群 chat id 的简写形式。 |
| 其他裸字符串 | `open_id` | 兼容旧的用户 open id 简写形式。 |

Feishu 账号配置按以下优先级解析:

1. 环境变量 `MUJITASK_FEISHU_ACCOUNTS_JSON`，格式为 `{"default":{"appId":"...","appSecret":"...","domain":"feishu"}}`。
2. 环境变量 `MUJITASK_FEISHU_ACCOUNTS_FILE` 指向的部署配置文件，文件内容同上。
3. OpenClaw 本机兼容配置 `OPENCLAW_CONFIG_PATH` 或 `~/.openclaw/openclaw.json` 下的 `channels.feishu`。

`appSecret` 等密钥不得写入 outbox payload、result、日志或错误详情。发送 result 只允许记录 `channel_code`、`reply_target`、`account_id`、`receive_id_type`、HTTP status 和飞书返回 code。

#### 3.6.2 Outbox 失败语义

`outbox_dispatcher` 只有在 handler 返回 success 时才能把 outbox 标记为 `sent`。真实通道发送失败必须进入 `retry_wait` 或 `failed`，不能把 dry-run、unsupported channel 或配置缺失伪装成 sent。

| 场景 | 语义 |
| --- | --- |
| `noop` / `disabled` | 明确跳过，标记 sent，`delivery_state=skipped`。 |
| `stdout` / `console` | 本地输出。`dry_run=true` 时为 `simulated`，否则为 `sent`。 |
| `webhook` / Feishu / OpenClaw 网络超时或 5xx | retryable infra failure，进入 `retry_wait`。 |
| 缺少 webhook URL、Feishu 账号密钥、接收目标、OpenClaw CLI 或 unsupported channel | terminal configuration failure，进入 `failed`。 |
| 飞书 token 或消息接口返回非 0 code | 不标 sent；按 handler 分类进入 `retry_wait` 或 `failed`，错误中保留 code/msg，不保留 secret。 |

### 3.7 `artifact_object`

运行产物索引表。大文件内容不放数据库，放 MinIO 或本地对象存储。

| 字段 | 说明 |
| --- | --- |
| `artifact_id` | artifact ID |
| `request_id`, `execution_id`, `run_id`, `step_id` | 归属和执行上下文 |
| `kind` | artifact 类型，例如 screenshot、stdout、state、media |
| `bucket`, `object_key`, `etag`, `size`, `content_type` | 对象存储定位和元信息 |
| `source_path` | 本地来源路径 |
| `metadata_json` | 扩展元数据 |
| `created_at` | 创建时间 |

关键索引:

- `idx_artifact_object_run_id(run_id)`

### 3.8 `fastmoss_session_cookie_cache`

FastMoss 登录态运行缓存。它是可再生缓存，不属于事实库。该缓存可由 API 登录刷新流程写入，也可由 FastMoss browser security resolve 流程在真实浏览器完成风控解除后刷新；例如 `fastmoss_security_browser_resolve` 成功验证原始 FastMoss API 请求不再返回 `MSG_SAFE_0001` 后，应将浏览器导出的 FastMoss cookies 写入本表。原始请求可以是搜索 `/api/goods/V2/search`，也可以是商品详情 `/api/goods/v3/base`、达人、店铺或视频接口。

Runtime DB 只记录 cookie cache 元数据审计，不在 summary/log 输出 cookie value。可出现在 result、summary、日志中的字段只限于 `cookie_count`、`has_fd_tk`、`fd_tk_digest`、`expires_at`、`updated_at`、`verified_path` 等脱敏信息；完整 cookie value 只能保存在 `cookies_json`，并按运行缓存处理。

FastMoss session/cookie 恢复是 `infrastructure/fastmoss` 的平台策略，不属于 `product_search`、`product_fetch`、`creator_fetch`、`shop_fetch`、`video_fetch` 等业务 handler。缓存复用必须同时检查 `expires_at` 和 `last_auth_failed_at`；`last_auth_failed_at` 已标记的 cookie 不得继续加载复用。任意 FastMoss API 遇到明确 auth 失效时，平台层应在账号级 lock 内刷新登录并保存新 cookie，保存成功后清空 `last_auth_failed_at`。如果刷新后原请求仍 auth 失败，应归类为 `fastmoss_session_conflict_or_external_login`，表示可能存在单点登录或外部登录冲突。

| 字段 | 说明 |
| --- | --- |
| `cache_key` | cache 主键 |
| `namespace`, `account_key`, `base_url`, `region` | 账号和站点维度 |
| `cookies_json`, `cookie_count`, `has_fd_tk`, `fd_tk_digest` | cookie 内容和摘要 |
| `expires_at`, `last_auth_failed_at`, `last_login_at` | 过期、认证失败、登录时间 |
| `created_at`, `updated_at` | 时间戳 |

关键索引:

- `idx_fastmoss_session_cookie_cache_account(namespace, account_key, region)`
- `idx_fastmoss_session_cookie_cache_expires(expires_at)`

## 4. 统一生命周期字段

所有可执行 job 表应尽量统一这些字段。

| 字段 | 当前情况 | 作用 |
| --- | --- | --- |
| `status` | 已有 | 当前状态 |
| `attempt_count` | 已有 | 已尝试次数 |
| `max_attempts` | 已有 | 最大尝试次数 |
| `worker_id` | 已有 | 当前领取者 |
| `lease_until` | 多数已有 | worker 崩溃或失联后的回收依据 |
| `heartbeat_at` | 已有 | worker/supervisor 活跃时间 |
| `started_at` | 已有 | 本次执行开始 |
| `finished_at` | 已有 | 本次执行结束 |
| `available_at` / `next_retry_at` | 已有 | 重试延迟和下次可执行时间 |
| `run_id` | 多数已有 | 单次执行实例 |
| `error_text` / `last_error_text` | 已有 | 错误文本 |
| `last_error_type`, `last_error_code`, `last_error_path` | 建议统一补齐 | 标准化错误分类 |
| `last_progress_at` | 建议补齐 | 业务真实进展时间 |
| `progress_stage` | 建议补齐 | 业务真实进展阶段 |
| `max_execution_seconds` | 建议补齐 | 单次执行硬超时 |
| `dead_letter_reason` | 建议补齐 | 最终不可恢复原因 |

其中 `heartbeat_at` 和 `last_progress_at` 必须分开理解:

- `heartbeat_at`: worker 或 supervisor 还活着。
- `last_progress_at`: 业务动作有推进。

一个任务可以持续 heartbeat，但业务一直没有 progress，这种情况应由 Watchdog Scanner 兜底。

## 5. 状态机设计

### 5.1 `task_request` 状态机

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: executor claim
    running --> waiting_children: fan-out child jobs
    waiting_children --> ready_for_summary: reconciler detects no active children
    ready_for_summary --> running: executor claim summary
    running --> success: summary completed
    running --> failed: unrecoverable error
    waiting_children --> failed: child failures exceed policy
    running --> pending: executor lease expired
    running --> ready_for_summary: summary lease expired
    success --> [*]
    failed --> [*]
    cancelled --> [*]
```

当前主要状态:

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待 executor 推进 |
| `running` | executor 正在推进 |
| `waiting_children` | 已派生子 job，等待子任务完成 |
| `ready_for_summary` | 子任务已收敛，等待 executor 生成总结 |
| `success` | 顶层任务成功 |
| `failed` | 顶层任务失败 |
| `cancelled` | 顶层任务取消，当前需要补齐完整取消链路 |

### 5.2 `task_execution` 状态机

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: browser_worker claim
    retry_wait --> running: available_at reached
    running --> success: handler success
    running --> skipped: handler says no-op
    running --> retry_wait: retryable failure
    running --> failed: attempts exhausted
    running --> pending: resource lease expired and reclaimed
    success --> [*]
    skipped --> [*]
    failed --> [*]
    cancelled --> [*]
```

当前 `task_execution` 适合承载 browser worker 最小执行单元。失败重试时应保证外部副作用具备幂等保护。

### 5.3 `api_worker_job` 状态机

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> running: api_worker claim
    retry_wait --> running: available_at reached
    running --> success: handler success
    running --> retry_wait: retryable failure or expired claim
    running --> failed: attempts exhausted
    success --> [*]
    failed --> [*]
    skipped --> [*]
    cancelled --> [*]
```

当前 claim 过期后会通过 requeue 逻辑进入 `retry_wait` 或 `failed`。后续建议把 `error_type` 记录为 `lease_expired`。

### 5.4 逻辑 fan-out job 状态

商品发现、达人详情、飞书写回这类逻辑 job 不拥有独立 Runtime 表。它们共享 `api_worker_job` 状态机，并通过 `stage`、`progress_stage`、`parent_job_id`、`job_group`、`entity_type`、`entity_key` 表达业务进度和父子收敛。

达人同步示例状态含义:

| 逻辑阶段 | Runtime 表达 |
| --- | --- |
| 商品待发现 | `api_worker_job.status=pending`, `stage=discover_related_creators` |
| 商品发现中 | `status=running`, `progress_stage=fetch_related_creators` |
| 商品发现完成并派生达人详情 | 商品发现 job `success`，creator detail jobs 已写入 `api_worker_job` |
| 达人详情待采集 | `api_worker_job.status=pending`, `stage=collect_creator_detail` |
| 达人详情采集中 | `status=running`, `progress_stage=fetch_creator_detail` |
| 达人详情完成/跳过/失败 | `status=success/skipped/failed`，父级由 Reconciler 聚合 |

### 5.5 `notification_outbox` 状态机

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> sending: dispatcher claim
    retry_wait --> sending: next_retry_at reached
    sending --> sent: send success
    sending --> retry_wait: retryable send failure
    sending --> failed: attempts exhausted
    sending --> retry_wait: lease expired
    sent --> [*]
    failed --> [*]
```

Outbox 的终态不应反向修改业务 Task 的成功/失败状态。业务完成和消息分发应保持解耦。

## 6. Claim / Lease / Retry 规则

### 6.1 Claim

worker claim job 时必须满足:

- `status` 在可执行集合内。
- `available_at <= now` 或 `next_retry_at <= now`。
- 如果涉及资源，必须拿到 `resource_lease`。
- 更新 `status = running` 或 `sending`。
- 写入 `worker_id`、`lease_until`、`started_at`、`heartbeat_at`、`run_id`。

### 6.2 Heartbeat

worker 或 supervisor 执行期间需要续约:

- 更新 `heartbeat_at`。
- 延长 `lease_until`。
- 只对当前 `worker_id` 且 `status = running/sending` 的记录生效。

### 6.3 Retry

handler 抛出可重试异常或外部调用失败时:

- `attempt_count += 1` 或 `retry_count += 1`。
- 如果未超过最大次数，进入 `retry_wait` / `failed_retry`。
- 设置 `available_at` / `next_retry_at`。
- 清理 `worker_id`、`lease_until`。
- 写入标准化错误。

如果次数耗尽:

- 通用 job 进入 `failed`。
- outbox 进入 `failed`。

### 6.4 Lease 过期

当前代码已有对部分 running claim 的回收:

- `task_request`: executor claim 过期后回到 `pending` 或 `ready_for_summary`。
- `api_worker_job`: running 过期后进入 `retry_wait` 或 `failed`。
- `task_execution` + `resource_lease`: 浏览器资源过期后释放租约，execution 回到可执行状态。
- `notification_outbox`: sending 过期后进入 `retry_wait` 或 `failed`。

推荐补齐:

- 所有 job 表统一 `error_type = lease_expired`。
- lease 过期是否消耗 attempt 要有统一策略。
- 对 browser job，资源 lease 释放和 job retry 状态应在同一个事务中完成。

## 7. 父子任务收敛

父任务进入 `waiting_children` 后，不能依赖进程内 callback 等子任务结束。Reconciler 必须从 Runtime DB 聚合子任务状态。

当前 `task_request` 已有子任务计数字段:

- `child_total_count`
- `child_terminal_count`
- `child_success_count`
- `child_failed_count`
- `child_skipped_count`

推荐收敛规则:

```text
active_count = pending + running + retry_wait

if active_count > 0:
  task_request.status = waiting_children
else:
  task_request.status = ready_for_summary
```

需要注意:

- `api_worker_job` 和 `task_execution` 的终态命名需要统一映射。
- `skipped` 对父任务通常计入成功完成，但需要在 summary 中单独展示。
- `failed` / dead letter 应进入 failed count，并影响最终策略。
- 父任务从 `waiting_children` 到 `ready_for_summary` 的更新应幂等。

## 8. 幂等与去重

Runtime DB 的幂等分两层:

| 层 | 字段 | 规则 |
| --- | --- | --- |
| 顶层 Task | `idempotency_key` | 防止同一来源重复创建同一顶层请求 |
| API job | `dedupe_key` | 防止重复派生同一个 API/IO job |
| Browser job | `dedupe_key`, `business_key`, `resource_code` | 防止重复派生同一个浏览器执行单元 |
| Product discovery job | `dedupe_key=request_id:discover_related_creators:product_id` | 同一任务下同一竞品/商品只生成一个商品发现 API job |
| Creator detail job | `dedupe_key=request_id:collect_creator_detail:product_id:influencer_id` | 同一任务下同一商品的同一达人只生成一个达人详情 API job |
| Outbox | `dedupe_key` | 同一通知事件只发送一次 |

幂等不是只靠 Runtime DB。凡是 job 内部有外部副作用，还需要外部系统写入幂等:

- 飞书写回需要基于 `record_id`、业务唯一键或目标表去重。
- Fact DB 写入需要使用业务唯一键和 upsert。
- MinIO/object store 写入需要稳定 `object_key` 或写入后可重复覆盖。

## 9. Watchdog Scanner 应补齐的 Runtime 能力

当前 Runtime DB 已经有 lease、heartbeat、retry 的基础字段，但应用层兜底还需要 Watchdog Scanner 将“不可恢复或无响应”的状态显式处理掉。

Watchdog 每轮扫描:

```text
1. running/sending 且 lease_until < now
2. running 且 started_at + max_execution_seconds < now
3. running 且 last_progress_at 长时间不更新
4. waiting_children 但所有子任务已终态
5. retry_wait/failed_retry 超过最大等待策略
6. outbox sending 卡住
```

处理动作:

| 场景 | 动作 |
| --- | --- |
| worker 崩溃，lease 过期 | 标记 `lease_expired`，进入 retry 或 failed |
| handler 卡死但进程还活着 | 标记 `stale_progress`，必要时 kill child process |
| 单次执行超过硬超时 | 标记 `timeout`，进入 retry 或 failed |
| 子任务已终态但父任务未收敛 | 幂等推进父任务到 `ready_for_summary` |
| outbox sending 卡住 | 进入 `retry_wait` 或 `failed` |
| attempts 耗尽 | 进入 dead letter / hard_failed，并记录原因 |

推荐新增字段:

| 表 | 字段 |
| --- | --- |
| `task_execution`, `api_worker_job`, `notification_outbox` | `last_progress_at`, `progress_stage`, `max_execution_seconds`, `dead_letter_reason` |
| 通用 job 表 | `error_type`, `error_code`, `error_path` |
| `task_request` | `last_progress_at`, `progress_stage`, `cancel_requested_at` |

## 10. 演进建议

第一阶段:

- 冻结业务专用 job 表扩展，新 workflow 只能使用 `api_worker_job` / `task_execution`。
- 文档和代码口径统一: product discovery / creator detail 是逻辑 job 粒度，不是独立 Runtime 表。
- 将所有状态枚举集中定义，避免散落字符串。

第二阶段:

- 实现 Watchdog Scanner。
- 为 `api_worker_job` 补齐父子关联、实体关联、progress、dead letter 和错误分类字段。
- 将达人同步历史专用 job 表迁移到通用 `api_worker_job` 表达。

第三阶段:

- 引入标准 Execution Supervisor。
- handler 放入 child process 或可取消 runner。
- supervisor 负责 heartbeat、progress、hard timeout、kill、retry 分类。

第四阶段:

- 根据运行数据决定是否进一步抽象统一 `runtime_job` 表。
- 若确有高频查询需求，优先增加只读投影、索引或事实库关系表；不得把业务流程重新拆成专用 Runtime job 表。
