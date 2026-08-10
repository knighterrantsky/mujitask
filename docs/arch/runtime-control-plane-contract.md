# Runtime 控制面契约

日期: 2026-07-23

状态: 受控架构契约

## 1. 定位

本文定义 Mujitask 运行控制面的代码归属、文件命名、配置入口和测试护栏。

Runtime 控制面负责把 agent / CLI / RPC 入口提交的业务请求变成 Runtime DB 中可追踪、可重试、可恢复的执行过程。它包括 RPC Agent Service、Task Request Entry、Daemon Entry、Execution Supervisor、Reconciler、Watchdog、Outbox Dispatcher 和 Project Configuration。

Runtime 控制面不负责外部业务系统的字段映射，也不负责 TikTok / FastMoss / Feishu / Dingding / Discord 等具体业务语义。外部输入源、事实数据源、消息通道和业务映射逻辑应继续落在 capability handler、domain mapper、domain projection、domain policy 或 domain flow 中。

相关事实来源:

- 工程结构定位: [project-structure-contract.md](./project-structure-contract.md)
- 当前系统链路: [system-architecture-design.md](./system-architecture-design.md)
- Runtime DB 状态机: [runtime-db-schema-design.md](./runtime-db-schema-design.md)
- 长期业务对象边界: [storage-architecture-design.md](./storage-architecture-design.md)
- 项目配置加载: [../dev/project-configuration.md](../dev/project-configuration.md)

## 2. 控制面组件

| 组件 | 代码归属 | 入口命令 | 职责 | 不应放入 |
| --- | --- | --- | --- | --- |
| Agent Skill Artifact | `skills/{skill_code}/`，部署目标 `MUJITASK_SKILLS_DIR/{skill_code}` | skill 内 `run_*_step.sh` / `lightweight_submit.py` | agent workspace 可读取的业务入口产物、固定输入、首条回执 | workflow 编排、worker retry、数据库 schema |
| RPC Agent Service | `src/automation_business_scaffold/apps/rpc_agent/server.py` | `automation-business-scaffold-agent` | 暴露 platform/framework 兼容的 HTTP/RPC task registry 和提交入口 | daemon loop、业务字段 mapper、外部 API transport |
| CLI / Task Request Entry | `src/automation_business_scaffold/apps/cli/main.py`、`control_plane/task_requests/`、`control_plane/executor/runner.py`、`control_plane/runtime_config/settings.py` | `automation-business-scaffold-run` | 本地/manual 运行、submit/status/result/control action 适配、构造顶层 `task_request` | worker 具体执行、浏览器 profile 操作、通知发送 |
| Executor Daemon Entry | `src/automation_business_scaffold/apps/daemons/executor/main.py` | `automation-business-scaffold-executor` | 解析 daemon 参数并调用 `execute_executor_once` / `run_executor_daemon` 推进 workflow | stage/job 业务实现、handler registry 修改 |
| API Worker Daemon Entry | `src/automation_business_scaffold/apps/daemons/api_worker/main.py` | `automation-business-scaffold-api-worker` | 解析 worker 参数并调用 `execute_api_worker_once` / `run_api_worker_daemon` 消费 API lane job | Feishu 表级 mapper、FastMoss 业务策略 |
| Browser Runloop Entry | `src/automation_business_scaffold/apps/daemons/browser_worker/main.py` | `automation-business-scaffold-browser-runloop` | 串行消费 browser lane job，通过 `child_process` supervisor 隔离 Sync Playwright，保护 browser profile / CDP 执行边界 | API worker 逻辑、事实库 schema |
| Outbox Dispatcher Entry | `src/automation_business_scaffold/apps/daemons/outbox/main.py` | `automation-business-scaffold-outbox-dispatcher` | 消费 `notification_outbox` 并分发最终消息 | workflow summary 生成、业务数据采集 |
| Watchdog Entry | `src/automation_business_scaffold/apps/daemons/watchdog/main.py`、`control_plane/watchdog/scanner.py` | `automation-business-scaffold-watchdog` | 扫描 stuck runtime 状态，执行 retry/fail/repair | 正常 workflow 推进、业务字段映射 |
| Execution Supervisor | `control_plane/supervisor/execution_supervisor.py` | 由 worker control path 调用 | 包装 handler dispatch，负责 heartbeat、进度、超时、异常归一化和 child process 结果落库 | 业务规则判断、外部数据源字段解释 |
| Reconciler | `control_plane/reconciler/views.py`、`control_plane/reconciler/reconciler.py` | 由 executor / status / result 路径调用 | 汇总 child task/job 状态，推进 parent request 终态和可观测视图 | 外部副作用、通知发送、handler 执行 |
| Project Configuration | `src/automation_business_scaffold/project_env.py`、`src/automation_business_scaffold/config.py`、`scripts/execution_control/executor.local.env`、`skills/{skill_code}/skill.local.env`、`.env` | 各 CLI / daemon / pytest / Alembic 启动时加载 | 统一配置加载顺序、默认值、typed settings；过滤 skill env 中的运行资源残留 | 将业务映射散落到 daemon 或 handler wrapper |
| Launchd Deployment | `config/deployment/launchd/*.plist.template`、`scripts/execution_control/install_launch_agents.sh`、`scripts/execution_control/run_launchd_agent.sh` | launchd | 安装、刷新、拉起常驻进程 | 业务逻辑、Runtime DB schema 变更 |

## 3. 控制链路

```text
Agent Skill / RPC Agent Service / CLI
  -> Task Request Entry
  -> Runtime DB task_request
  -> Executor Daemon Entry
  -> domains/{domain}/workflows stage/job binding
  -> API Worker Daemon Entry 或 Browser Runloop Entry
  -> Execution Supervisor
  -> capabilities/{role}/{system}/{handler_code}_handler.py
  -> contracts/handler/domain_mapping.py 选择受控 domain Runtime result projection（如有）
  -> domains/{domain}/projections/*runtime_result_projection.py 校验并压缩业务结果（如有）
  -> Runtime DB job/result/progress
  -> Reconciler aggregate/finalize
  -> notification_outbox
  -> Outbox Dispatcher Entry
```

Watchdog 不在主推进链路上。Watchdog 只观察 Runtime DB 中的异常状态，并按契约执行 retry、fail 或 parent repair。

### 3.1 Executor / Worker / Reconciler 调度语义

Runtime 控制面只调度和收敛，不解释 TikTok、FastMoss、飞书字段语义。

`executor_daemon` 的职责:

- 只 claim `task_request.status=pending` 的顶层请求。
- 根据 `task_code/current_stage/stage_cursor_json` 选择 workflow stage，并创建或推进对应 `api_worker_job` / `task_execution`。
- 当 stage 需要等待 child job、browser fallback 或外部可观测事件时，把 `task_request` 或触发的行级 `api_worker_job` 标记为 `status=waiting`。对 row-serial browser fallback，waiting row job 是唯一 fallback 待处理事实，`task_execution` 是浏览器任务事实；任何 wait 引用只能作为观测冗余，不能成为第二套流程事实源。
- 对 browser fallback，executor 负责调度和数据交换: 读取 handler 返回的业务化 browser 请求，创建对应 `task_execution`，在 browser task 终态后把 browser result 引用、FastMoss cookie cache metadata 或失败证据写回原 row job，再把原 row job 改回 `pending` 或收敛为终态。
- 当 Reconciler 判断等待条件解除时，把顶层请求改回 `status=pending` 并保留或推进 `current_stage`，例如 `current_stage=ready_for_summary`。
- 只汇总终态结果、写 summary/outbox，不直接执行 handler、不解析 browser 页面数据、不写业务字段 mapper；summary gate 只看终态 row job / task 事实，不看 `after_browser_candidates` 这类派生 candidate 数量。

`api_worker` 的职责:

- 只 claim `api_worker_job.status=pending` 且 `available_at <= now` 的 job。
- 通过 Execution Supervisor 调用对应 API/IO handler；若 handler 在受控 registry 中声明 domain Runtime result projection，则先由该纯投影校验身份、字段 allowlist 和终态策略，再由通用 worker 写入 `result_json`、`summary_json`、`error_text`、progress 和 `result_status`。
- domain Runtime result projection 只能返回紧凑、脱敏、可持久化的结果和失败策略；不得访问 `RuntimeStore`、数据库或执行任何外部副作用。未声明 projection 的既有 handler 保持通用 Runtime envelope 行为。
- 遇到需要 browser fallback 的业务场景时，只能返回“需要某个 browser handler 处理这件事”的业务化请求；不得内联执行 browser handler，不得写 `fallback_source_job_id` 这类 runtime 关联字段，也不得把 `fallback_required` 写成 Runtime DB lifecycle status。

`browser_worker` 的职责:

- 只 claim `task_execution.status=pending` 且 `available_at <= now` 的 browser job。
- 持有 `resource_lease` 后执行 browser handler。截图、HTML、raw response、日志和 state 等
  Runtime 诊断文件只保留在本地 `artifact_root`，不得由通用 browser worker 自动上传 MinIO。
  只有业务 contract 白名单明确准入的长期业务对象，才能由既有业务 owner 通过对象存储 transport
  写入；当前包括 Amazon normalized capture 与 blocked/captcha 受控截图，普通 Runtime artifact
  不在白名单中。
- 小型结构化结果在首次写入 `task_execution.result_json` 前，先经过已声明的 domain Runtime result projection 校验和压缩；artifact index record 也只能由该投影返回受控字段，再由通用 worker 写入。FastMoss cookie value 直接写入 `fastmoss_session_cookie_cache`，result/log/summary 只允许脱敏 metadata。

`Reconciler` 的职责:

- 从 Runtime DB 读取 child `api_worker_job` / `task_execution` 的 `status/result_status`，不依赖进程内 callback。
- 对 `status=waiting` 的 parent 或 row job，检查 Runtime DB 中对应 child `api_worker_job` / `task_execution` 是否终态。row-serial fallback 下，同一请求的 waiting row job 与未终态 browser `task_execution` 构成最小关联事实；不依赖派生 candidate 列表判断是否继续。
- child 仍有 `pending/running/waiting` 时保持 parent `waiting`。
- child 全部终态时，按 workflow contract 推进: TikTok browser fetch 可由 executor/runtime 把 browser result 引用写回原 row job，作为当前 stage 输出；FastMoss security resolve 只写回脱敏 cache metadata 并让原 FastMoss handler/stage 重试一次。
- summary 只汇总 `status=finished` 且带 `result_status` 的业务结果；browser child success、seed write success 或 fallback wait 信号都不能直接计入详情采集成功。

`Watchdog` 的职责:

- 扫描 lease 过期、heartbeat 失联、progress 卡住、`waiting` 引用已终态但 parent 未推进、`pending` 重试等待超时和 outbox sending 卡住。
- 对可恢复记录回到 `pending` 并设置 `available_at/next_retry_at`；对不可恢复记录写入 `status=finished,result_status=failed` 或 `status=cancelled`。
- 不做正常 workflow 推进，不生成业务 summary，不补写 TikTok / FastMoss / Feishu 字段。

### 3.2 Browser execution 健康、stall 与诊断契约

Browser execution 的机器事实来源是
[`contracts/runtime/browser-execution-recovery.yaml`](../../contracts/runtime/browser-execution-recovery.yaml)。
当前生产约束是同一部署只运行一个 browser runloop，不并发手工执行 `browser_once`；在这个前提下，
本阶段不新增物理 lane、profile 状态表或独立 browser recovery daemon。

浏览器错误只是诊断触发器，不能直接作为 browser 是否健康的判据。共享 browser profile 的健康结论必须来自有硬超时的功能探针，顺序固定为：

```text
connect CDP
  -> create a new page
  -> navigate about:blank
  -> evaluate document.readyState
  -> close the probe page
  -> detach/disconnect the probe session
```

共享 CDP 探针禁止调用 `session.close()`，因为该动作可能关闭共享 browser instance。探针必须只关闭自己创建的 page，再使用 provider/session 的 `detach` 或 `disconnect` 语义结束 Playwright 连接；如果当前 provider 没有安全 detach 能力，本次探针记为 `inconclusive/cleanup_failure`，不能通过关闭共享 browser 来制造“清理成功”，也不能仅凭 cleanup failure 触发 restart。

`/json/version` 只说明 HTTP 端点有响应，不属于完整健康判定。Security Check、captcha、`403`、`429`、`503`、商品不存在、selector/parse 失败、artifact/Fact DB/飞书失败也不属于 browser 健康异常；页面仍可执行 DOM、截图或简单 JS 时，不得因此重启 Chrome。

判定前探针必须覆盖 profile 的 `auto_start=false` 与 `session_recovery.enabled=false`，否则探针可能先自行拉起或重启 Chrome，再把被污染的现场误报为 healthy。只有 exact-profile stop 已获 execution supervisor 授权后，restart 后探针才允许使用该 profile 的 `auto_start` 启动新实例；普通 handler session 同样关闭 provider 内建 restart，避免两层 restart budget 相乘。

本阶段覆盖的 `amazon_product_browser_fetch` 和 `tiktok_product_browser_fetch` 中，每个纳入 allowlist 的阻塞 browser operation 必须在调用前先发出 `started`，返回后发出 `completed`，异常时发出 `failed`；在同一 operation 边界明确吞掉异常时发出 `suppressed_error`。日志和 Runtime progress 不得包含 URL、HTML、DOM snapshot、cookie、header、原始异常正文或 traceback。普通 progress 只保存最新安全阶段，完整而有界的 operation 事件留在本地 stdout；终态 `summary_json/result_json` 保存紧凑诊断证据，不新增 Runtime 表或字段。`fastmoss_security_browser_resolve` 仍可得到通用 child stall 和功能探针证据，但本阶段不承诺其 operation 级 page/session 细分，不能把 active operation 缺失误报为确定的单页结论。

ChildRunner 使用 monotonic clock 判断 inactivity，并跟踪尚未收到同一 `operation_id` 终态事件的外层 active operation；短暂的嵌套 response callback 完成后不能覆盖仍卡住的 `page_ready_wait`，也不能用被动 `network_response_capture` 事件持续刷新外层操作的 inactivity deadline。检测到 stall 后必须先刷新 Runtime progress，并严格按下面顺序处理：

```text
probe_before_kill
  -> terminate/kill child
  -> confirm child exit
  -> probe_after_kill without browser restart
  -> restart the exact profile only when both before/after probes are unhealthy
  -> probe_after_restart
  -> persist diagnosis
```

`probe_before_kill` 不能挪到 child 退出后，因为退出 child 本身可能释放 session 或 CDP contention，从而污染故障现场。probe 的总预算从调用入口开始计时，OS process start 的耗时也会消耗该预算；process start 本身不可被本进程硬中断，因此 start 失败或返回时预算已经耗尽只能记为 `inconclusive`，不得归因 Chrome 或触发重启。process 成功启动后只能使用剩余预算，并运行在可硬终止的 deadline 内。deadline 先用 SIGTERM 让 probe 执行 page close/session detach 的 `finally`；connect 阶段还没有 probe-owned page/session 时，成功退出记为 `cleanup_status=not_required`。只有清理完成或确实无须清理时才可保留 `unhealthy` 判定，必须 SIGKILL 时记为 `inconclusive`。任一 probe process 或业务 child 未确认退出时，必须停止后续 probe/restart 链；browser restart 只有在 kill 前和 kill 后两个探针都明确为 `unhealthy` 时才允许执行。诊断、kill 和 recovery 每个转换都要更新 progress，使 supervisor 的 stall 处理早于 300 秒无进度 Watchdog。默认 inactivity 上限是 150 秒。Runtime 总预算必须由 handler wall-clock budget 加 240 秒诊断恢复保留组成；Amazon 因此使用 300 秒 handler budget 和 540 秒 execution 总预算。即使 handler 持续上报 progress 直到 300 秒 wall timeout，也会进入同一 stall probe 链，仍保留完整恢复窗口，而不是直接退化成无诊断的通用 child timeout。

如果 TERM/KILL 后仍无法确认业务 child 或功能探针 child 退出，父进程必须先以 exclusive create、0600 权限写入并 fsync `runtime/daemons/browser_runloop.quarantine.json`，再持久化 `child_termination_failed` 或 `browser_probe_termination_failed`。marker 的 `child_pids` 保存本次所有未确认退出的 probe/business PID，`child_pid` 仅作为首个 PID 的兼容别名。常驻 runloop 和手工 `browser_once` 的 claim 边界都必须在 marker 存在时停止 claim；空文件、损坏 JSON 或无法读取的 marker 也必须 fail closed。因此 launchd `KeepAlive` 重启父进程也不会与可能存活的孤儿 child 并发操作共享 Chrome。

若 marker 首次写入失败，当前 runloop 必须先把 `progress_stage=browser_runloop_quarantine_write_failed` 持久化到该 `task_execution`，再按 heartbeat 周期持续重试同一个 marker。Watchdog 对带有该 progress stage 的 execution 必须跳过全部规则，不能 automatic terminal，也不能 TERM/KILL 当前 worker；marker 重试成功后才允许继续终态持久化。这个闭环依赖“本地 marker”或“Runtime hold stage”至少一个能够持久化：如果两者同时持续失败，当前进程仍会保持重试，但在父进程被外部强制终止并由 launchd 重启后无法保证继续隔离，契约不得把这一双重持久化失败描述为绝对安全。marker 不自动清除；只有操作人确认 child 已退出，或确认 host 已重启后，才可删除。

Stall diagnosis 的 recovery budget 只有一个 owner：browser execution control path。一次 stall 最多执行一次 exact-profile restart，探针最多出现在 kill 前、kill 后和 restart 后三个时间点；普通 handler 不拥有 stop/restart 权限。获得恢复授权后，ownership check、stop command 与 post-restart probe 共享同一个 120 秒 monotonic deadline，stop 阶段不能再给后续探针发放完整的新预算；post-restart probe 即使返回健康，只要返回时已经越过该 deadline，也必须记为 `browser_recovery_failed`。probe process 的 OS `start` 不可被本进程硬中断，但其耗时从总预算中扣除；start 失败或超时返回只能得到 `inconclusive`，这一本地进程创建边界不被伪称为可硬终止。restart 或 post-restart probe 失败时，`browser_recovery_failed` 是当前诊断的 terminal failure。

本阶段完成门禁只覆盖“原子 operation 证据 + stall 检测 + 三时点探针 + 最多一次 restart + 紧凑诊断”。所有 browser task 的 post-claim preflight、功能探针健康后的自动新 page 重试，以及 browser-runloop cooldown 仍是后续独立能力，不能由本阶段 completion claim 暗示已经实现。

诊断结论描述的是证据支持的 failure scope，不等于厂商级最终根因：

| 证据 | 允许的 failure scope | 不允许的结论 |
| --- | --- | --- |
| 原操作卡住，kill 前新页功能探针健康 | `page_or_site` 或 `target_or_session` | Chrome/GCP 故障 |
| artifact upload/verify 或纯解析卡住，kill 前功能探针健康 | `non_browser_handler` | page、Chrome 或 GCP 故障 |
| kill 前探针失败，child 退出后探针恢复 | `target_or_session` | 已证明 Chrome 进程或 VM 故障 |
| kill 前后均失败，重启 exact profile 后恢复 | `browser_instance` | 已证明 Chrome bug、profile 损坏或 GCP VM 根因 |
| 重启后仍失败，缺少实例外证据 | `host_runtime_suspected` | `gcp_instance_unreachable` |
| 独立主机 heartbeat、GCP instance status API 或实例外网络探针确认不可达 | `gcp_instance_unreachable` | 仅凭 VM 内本地日志得出该结论 |

这里的 `browser_instance` 指同一个 `profile_ref` 背后的共享 Chrome/CDP 实例，不等于 GCP VM。运行在 VM 内的 worker 无法在 VM 停止或完全不可达时写出最终日志；因此本地实现只能落到 `host_runtime_suspected`。`gcp_instance_unreachable` 必须携带实例外证据，本次轻量实现不新增该外部监控组件，也不得伪造该结论。

Browser bridge 只负责 profile-scoped probe 和 exact-profile lifecycle transport，不能决定 Runtime retry、business status 或 GCP VM 归因。Execution Supervisor/worker 负责 stall、证据时序和单次 restart budget。TikTok/Amazon handler 只报告原子 operation，不得分别调用 stop 脚本。

## 4. 配置契约

Project Configuration 的优先级为:

```text
CLI 参数 > 环境变量 > executor.local.env > skill.local.env > .env
```

具体约束:

- `project_env.py` 只负责按顺序读取项目配置文件；默认不覆盖已经存在的进程环境变量。
- `config.py` 负责把环境变量解析成 typed defaults，例如 `BusinessDefaults`、`ExecutionControlDefaults` 和 `get_execution_control_defaults()`。
- `scripts/execution_control/executor.local.env` 是 Runtime DB、Fact DB、Object Store、lease、heartbeat、worker poll、daemon stop idle 等运行配置的主入口。
- `skills/{skill_code}/skill.local.env` 是 agent skill 固定输入和部署到 agent workspace 后的 skill 配置入口。
- TikTok 与 Amazon 使用不同的 skill env 和 OpenClaw workspace；Amazon 固定为 `amazon-ops` / `workspace-amazon`，并通过部署配置的飞书 account ID 与精确群聊 peer binding 隔离。account ID 只要求与当前 session 一致，不固定本地别名；该入口隔离不复制 Runtime DB 或 daemon。
- `skills/{skill_code}/skill.local.env` 不得提供 Runtime DB、Fact DB、Object Store 或 browser profile 配置；加载器必须忽略其中残留的 `EXECUTION_CONTROL_*`、`BUSINESS_EXECUTION_CONTROL_*`、`TK_FACT_DB_URL`、`BROWSER_*`、`DEFAULT_PROFILE_REF` 等运行资源键。
- `.env` 是本地默认配置入口，适合浏览器 profile、agent host/port、通用本地变量。
- `*.env.example` 必须跟实际读取文件保持同名示例关系；新增必填配置时要同步示例、部署脚本、配置文档和测试。
- 正式 Skill submit 只提交业务 payload；Runtime DB、Fact DB、Object Storage 必须由 Project Configuration 解析，不能由 Skill payload 透传真实连接串或密钥。
- browser profile 属于项目运行资源；正式 Skill 可以传显式业务级 `profile_ref` override，但固定默认值、provider、profile_id、workspace_id 必须来自 Project Configuration 或 `config/browser_profiles.json`，不能来自 `skill.local.env`。
- Formal workflow submit 必须在 `Task Request Entry` 阶段按 workflow 声明完成持久化 preflight：Runtime DB 始终必需；仅当 `requires_fact_db=true` 时要求 Fact DB；仅当 `requires_object_storage=true` 且流程会持久化允许的长期业务对象时要求 MinIO provider、bucket 和凭据。缺少当次流程实际需要的依赖时拒绝创建正式任务；只产生本地 Runtime 诊断文件的流程不要求 MinIO。
- `task_request.payload_json` 和 child job payload 可以保存非敏感运行摘要，例如 `requires_fact_db`、`requires_object_storage`、`artifact_store.provider`、`artifact_store.bucket`、`artifact_store.object_prefix` 和配置来源标记；不得保存完整 DB URL、access key、secret key。
- `run_mode` 只允许作为本地测试或 debug submit override，不属于正式 Skill submit 契约。

禁止:

- 在 daemon wrapper 中硬编码业务默认值。
- 在 handler 中私自读取部署脚本专属配置。
- 把用户 agent workspace 的 `skill.local.env` 当成 Runtime DB 或 Object Store 的唯一事实来源。
- 把用户 agent workspace 的 `skill.local.env` 当成 browser profile / provider / profile_id / workspace_id 的事实来源。
- 让不同入口用不同配置优先级。
- 将 Amazon Skill 安装到 TikTok workspace、把 TikTok Skill 安装到 Amazon workspace，或让 Amazon reply target 回落到非 `amazon` 飞书账号。
- 在正式 Skill payload 中传入 `run_mode`、Runtime DB、Fact DB 或 MinIO/S3 连接配置。
- 为了让正式任务“先跑起来”而把 `fact_bundle_upsert` 退化成 `dry_run`，或把 `media_asset_sync` 退化成 `local` / `linked_local` 成功。

## 4.1 Runtime DB 连接契约

Runtime DB 连接属于 Runtime 控制面基础能力，不属于业务 flow 局部实现。所有 daemon、worker、dispatcher、watchdog 和 task submit / status / result 路径必须遵守以下约束:

- Runtime DB 访问默认通过 `RuntimeStore` 或 `control_plane/runtime_config/create_runtime_store()` 创建。
- `RuntimeStore` 的连接策略必须适合用户电脑长期运行，不能长期占用无界连接池。
- 禁止在 domain flow、capability handler 或脚本中随手 `create_engine()` 访问 Runtime DB。
- 如果 Fact DB 与 Runtime DB 物理共用同一个 Postgres，事实写入路径应优先复用传入的 `RuntimeStore`。
- 如果确实需要独立 SQLAlchemy engine，必须显式配置有界连接策略，例如 `pool_size`、`max_overflow`、`pool_timeout`、`pool_recycle` 和 `pool_pre_ping`，或明确使用 `NullPool`。
- `too many clients already`、连接超时、连接被关闭等 DB 连接错误属于基础设施错误，Supervisor / worker 不应把它归因成业务字段失败。
- Task submit / skill submit 在创建大量 child job 前，应允许接入 DB health preflight。连接数接近上限时，应拒绝大任务提交并返回运行环境异常。
- Watchdog 应能够扩展 DB connection health check，至少观测总连接数、idle 连接数、idle in transaction 和主要 `application_name` 来源。

运行时连接稳定性的部署与排障动作见 [../ops/runtime-db-connection-stability.md](../ops/runtime-db-connection-stability.md)。本文只约束代码边界和控制面职责。

## 5. 文件命名契约

应用入口文件归 `apps/**`:

- `apps/rpc_agent/server.py` 表示 RPC Agent Service。
- `apps/cli/main.py` 表示本地/manual 命令入口。
- `apps/daemons/{daemon_code}/main.py` 表示常驻 daemon 或可 `--once` 执行的后台 worker 入口。

这些入口只做参数解析、配置加载、日志上下文和调用 `control_plane/**` 中的控制面函数。新增业务能力不得直接塞进 app 入口文件。

控制面函数归属:

- 提交、状态查询、result 查询、executor/api/browser/outbox control action: `control_plane/executor/runner.py` 和 `control_plane/task_requests/`。
- runtime settings、formal task code、request payload 构造: `control_plane/runtime_config/settings.py`。
- workflow runtime module 解析: `control_plane/executor/workflow_registry.py`。
- child request / child job 汇总视图和 Reconciler 辅助: `control_plane/reconciler/views.py`。
- handler 执行监督、heartbeat、timeout、异常归一化: `control_plane/supervisor/execution_supervisor.py`。
- stuck 状态扫描、候选生成、retry/fail/repair: `control_plane/watchdog/scanner.py`。

如果未来 Reconciler 需要独立常驻进程，必须新增:

```text
src/automation_business_scaffold/apps/daemons/reconciler/main.py
control_plane/reconciler/reconciler.py
automation-business-scaffold-reconciler
```

同时更新本文、`project-structure-contract.md`、`pyproject.toml` console script 和结构契约测试。

## 6. 扩展规则

新增 RPC 服务:

1. 先确认是 platform/framework 入口还是业务 channel。
2. platform/framework 入口放 `apps/rpc_agent` 或 `apps/cli`，业务逻辑仍落到 domain task/workflow 和 `control_plane/**`。
3. 新增 console script 时，必须同步 `pyproject.toml`、部署模板、README/ops 文档和测试。

新增 daemon:

1. 入口新增在 `apps/daemons/{daemon_code}/main.py`，console script 直接指向该入口。
2. 循环、claim、lease、retry、heartbeat 等控制逻辑放 `control_plane/**`。
3. 外部系统 transport 放 `infrastructure/**` 或 capability handler。
4. 业务字段 mapper 放 domain mapper / projection，不放 daemon。

新增消息通道:

1. 通道配置可以进入 Project Configuration。
2. 出站消息通过 `notification_outbox` 和 `capabilities/channels/{channel_code}/` 扩展。
3. 不允许 workflow stage 直接调用 Dingding / Discord / Feishu 通知 API 绕过 outbox。

新增外部输入源或事实数据源:

1. 运行控制面只负责调度和可观测性。
2. 输入源读取能力进入 `capabilities/input_sources/**` 或 `capabilities/browser/**`。
3. 表级或业务定制映射进入 source adapter / projection mapper / policy。
4. 事实数据写入进入对应 fact/object-store handler 或 flow，不改变 daemon contract。

## 7. 测试护栏

结构契约由以下测试守住:

| 测试 | 守护内容 |
| --- | --- |
| `tests/test_runtime_control_plane_contract.py` | 本文存在、核心组件命名、入口文件、console script、配置优先级和 launchd 模板存在 |
| `tests/test_project_structure_contract.py` | 工程结构契约中必须引用 Runtime 控制面契约 |
| `tests/test_config.py` | 配置默认值和环境变量解析 |
| `tests/test_runtime_lifecycle.py` | task request 生命周期、executor 推进和状态转换 |
| `tests/test_execution_supervisor.py`、`tests/test_execution_supervisor_runtime.py` | Execution Supervisor 的 heartbeat、timeout、结果/错误归一化 |
| `tests/test_watchdog_scanner.py`、`tests/test_watchdog_apply_integration.py` | Watchdog 候选扫描和修复动作 |
| `tests/test_outbox_dispatcher_integration.py` | Outbox dispatcher 消费和重试 |

任何变更 RPC Agent Service、Task Request Entry、Daemon Entry、Project Configuration、Execution Supervisor、Reconciler、Watchdog 或 Runtime DB 连接策略的实现，都必须同步本文和对应测试。

## 8. 最终规则

一句话规则:

> Agent/RPC/CLI 只提交请求，Daemon 只推进运行态，Supervisor 只监督 handler 执行，Reconciler 只汇总终态，Watchdog 只修复异常态，Outbox 只发消息，Project Configuration 只定义配置来源；业务字段映射和外部系统语义不得漂移进运行控制面。
