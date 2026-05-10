# 项目架构契约

日期: 2026-04-24

状态: 受控项目架构契约

## 1. 定位

本文定义 Mujitask 当前正式项目工程组织方式。它是当前和后续开发的架构事实来源，用于约束 workflow 开发、模块归属评审、代码定位和旧实现迁移。

本文回答两个问题:

- 系统中每类元素应该放在哪里: agent artifact、RPC service、daemon、workflow、job、handler、mapper/projection、input source、fact source、database、file storage、outbox、watchdog、reconciler、Execution Supervisor、配置文件。
- 一个 workflow 业务需求进入开发阶段时，如何把通用能力和独立业务逻辑拆分到工程目录，避免把逻辑继续堆进 flow、handler 或 agent script。

与其他契约的关系:

- 当前仓库真实结构见 [project-structure-contract.md](./project-structure-contract.md)。
- 运行控制面的当前入口见 [runtime-control-plane-contract.md](./runtime-control-plane-contract.md)。
- 本文是正式结构；新增代码、迁移旧代码和结构测试都必须与本文保持一致。

## 2. 项目目录结构

项目目录按“运行控制面 / 业务编排层 / 集成能力层 / 基础设施层 / 部署产物层”组织:

```text
src/{project}/
  apps/
    rpc_agent/              # RPC/HTTP agent service，只暴露 task request API
    cli/                    # 本地 submit/status/debug 命令
    daemons/                # executor/api-worker/browser-worker/outbox/watchdog/reconciler 进程入口

  control_plane/
    task_requests/          # agent task request 入口、submit/status/result/cancel
    executor/               # workflow 推进、stage/job 调度
    supervisor/             # Execution Supervisor，heartbeat/timeout/exception/result envelope
    reconciler/             # parent-child 汇总、终态判定、repair-ready 判断
    watchdog/               # stale lease、timeout、stuck parent、outbox timeout 扫描
    outbox/                 # outbox 调度模型和通道分发控制
    runtime_config/         # 配置解析、优先级、typed settings

  domains/
    {business_domain}/
      workflows/            # 业务 workflow 定义
      tasks/                # 顶层业务入口 task
      jobs/                 # job contract，绑定 handler capability
      policies/             # 业务级 retry/idempotency/selection/filter/finalize policy
      mappers/              # 业务数据映射，不接触外部 transport
      projections/          # 写回视图/表格/消息的投影
      flows/                # 业务组合逻辑；top-level workflow 用同名 package + stages/context/policies/summary

  capabilities/
    input_sources/
      feishu/
      dingding_sheet/
    fact_sources/
      tiktok/
      fastmoss/
      aws/
    persistence/
      database/
      object_storage/
    channels/
      feishu/
      dingding/
      discord/
    browser/
    media/

  infrastructure/
    clients/                # Feishu/FastMoss/TikTok/AWS/MinIO/Postgres client
    runtime/                # RuntimeStore façade、repositories、queries、schema availability
    stores/                 # FactStore/ObjectStore 等技术存储实现
    schemas/                # DB schema/migration contract
    observability/          # logging/metrics/tracing

  contracts/
    runtime/
    workflow/
    handler/
    config/
    outbox/

config/
  deployment/               # launchd/systemd/docker/k8s 配置
  browser_profiles.example.json

skills/
  {skill_code}/             # 最终复制到用户 agent workspace 的 skill/script/config bundle

scripts/
  deploy/
  dev/
  ops/
```

## 3. 分层职责

| 层 | 放什么 | 不放什么 |
| --- | --- | --- |
| `apps/**` | 进程入口、参数解析、配置加载、HTTP/RPC 适配、CLI 命令、daemon main | 业务 mapper/projection、外部系统字段解释、workflow stage 细节 |
| `control_plane/**` | task request 生命周期、executor、worker claim、Execution Supervisor、Reconciler、Watchdog、outbox 调度、runtime config | 飞书表字段、FastMoss 商品规则、TikTok 页面业务策略、业务专用 daemon fork |
| `domains/{business_domain}/**` | 业务 task、workflow、job、policy、mapper、projection、flow | Feishu/FastMoss/TikTok/AWS 底层 client、Runtime DB store 实现、daemon main |
| `capabilities/**` | 可复用 handler capability，按输入源、事实源、存储、通道、浏览器、媒体分类 | 单个业务域的筛选规则、字段命名、写回投影 |
| `infrastructure/**` | 外部系统 client、数据库 store、object store、schema/migration、日志监控实现 | task/workflow/job 命名、业务终态判断、表级字段映射 |
| `contracts/**` | runtime/workflow/handler/config/outbox 的稳定协议、schema、envelope | 参考资料、临时 debug 脚本、客户需求原文 |
| `config/**` | deployment 配置、浏览器 profile 示例和项目配置模板 | Python 业务实现、secret 真值、运行时生成产物 |
| `skills/**` | OpenClaw / Hermes / 用户 agent workspace 可安装的 skill/script/config bundle 源 | worker 消费循环、数据库 schema、workflow 主编排 |
| `scripts/**` | deploy/dev/ops 操作脚本 | handler 业务逻辑、mapper/projection、长期运行进程代码 |

## 4. 系统元素归属

| 元素 | 正式归属 | 命名建议 | 边界 |
| --- | --- | --- | --- |
| Agent skill / script | `skills/{skill_code}/` | `{business_entry}` | 只提交 `task_request`，不消费 runtime job |
| RPC Agent Service | `apps/rpc_agent/` | `server.py`、`routes.py` | 只暴露 task registry、submit/status/result API |
| CLI | `apps/cli/` | `main.py`、`commands.py` | 本地 submit/status/debug，不承载业务映射 |
| Daemon | `apps/daemons/{daemon_code}/` | `main.py` | executor/worker/outbox/watchdog/reconciler 入口门面 |
| Task Request | `control_plane/task_requests/` | `submit.py`、`status.py`、`result.py` | 统一 request envelope、idempotency key、cancel/status/result |
| Executor | `control_plane/executor/` | `runner.py`、`scheduler.py` | workflow 推进、stage/job 调度 |
| Execution Supervisor | `control_plane/supervisor/` | `execution_supervisor.py` | heartbeat、timeout、异常归一化、result envelope |
| Reconciler | `control_plane/reconciler/` | `reconciler.py` | parent-child 汇总、终态判定、repair-ready 判断 |
| Watchdog | `control_plane/watchdog/` | `scanner.py`、`rules.py` | stale lease、timeout、stuck parent、outbox timeout 扫描 |
| Outbox 调度 | `control_plane/outbox/` | `dispatcher.py`、`models.py` | 分发控制、retry、状态流转 |
| Project Configuration | `control_plane/runtime_config/`、`config/**`、`scripts/execution_control/*.env.example`、`skills/{skill_code}/skill.local.env.example` | `settings.py`、`*.env.example` | 配置优先级、typed settings、部署模板 |
| Workflow | `domains/{domain}/workflows/` | `{workflow_code}.py` | stage DAG、依赖、summary contract |
| Task | `domains/{domain}/tasks/` | `{task_code}.py` | 顶层业务入口参数和校验 |
| Job | `domains/{domain}/jobs/` | `{job_code}.py` | 可执行单元 contract，绑定 capability handler；业务复合 job 的 runtime adapter 归 domain job |
| Policy | `domains/{domain}/policies/` | `{policy_code}.py` | selection/filter/retry/idempotency/finalize 业务决策 |
| Mapper | `domains/{domain}/mappers/` | `{source}_{business_object}_mapper.py` | 输入行、事实记录到业务对象的映射 |
| Projection | `domains/{domain}/projections/` | `{destination}_{view}_projection.py` | 写回表格、消息、视图字段 |
| Top-level Flow Package | `domains/{domain}/flows/{workflow_code}/` | `orchestrator.py`、`stages/{stage_code}.py`、`context/**`、`policies/**`、`summary.py` | workflow runtime 推进实现；不能成为 handler registry key |
| Row-level Leaf Flow Package | `domains/{domain}/flows/{row_flow_code}/` | `orchestrator.py`、内部 step/context/policy 模块 | 单个行级主 job 内部串行 pipeline；不表达 top-level workflow stage DAG |
| Input Source Handler | `capabilities/input_sources/{source}/` | `{capability}_handler.py` | Feishu / Dingding 表格等业务输入读取 |
| Fact Source Handler | `capabilities/fact_sources/{source}/` | `{entity}_fetch_handler.py` | TikTok / FastMoss / AWS 等事实采集 |
| Persistence Handler | `capabilities/persistence/{store}/` | `{operation}_handler.py` | database/object storage 能力，不写业务规则 |
| Channel Handler | `capabilities/channels/{channel}/` | `{message_type}_handler.py` | Feishu / Dingding / Discord 等出站通道 |
| Browser / Media Capability | `capabilities/browser/`、`capabilities/media/` | `{capability}_handler.py` | profile/CDP、截图、下载、转存 |
| Runtime Repository / Query | `infrastructure/runtime/repositories/`、`infrastructure/runtime/queries/` | `{table_or_read_model}_repo.py`、`{read_model}_query.py` | Runtime table persistence 和只读视图；不知道 workflow 业务语义 |
| Client / Store | `infrastructure/clients/`、`infrastructure/stores/`、`infrastructure/facts/`、`infrastructure/artifacts/` | `{system}_client.py`、`{store}_store.py` | 底层技术实现，不知道 workflow |

## 5. 外部系统分类

外部系统必须先按角色分类，再决定代码归属。

| 外部系统角色 | 例子 | 项目目录 | 业务定制放哪里 |
| --- | --- | --- | --- |
| 输入数据源 | 飞书表格、钉钉表格、表单、人工上传 CSV | `capabilities/input_sources/{source}/` | `domains/{domain}/mappers/`、`policies/` |
| 事实数据源 | TikTok、FastMoss、AWS、第三方 API、爬虫观测源 | `capabilities/fact_sources/{source}/` | `domains/{domain}/policies/`、`mappers/` |
| 数据库 | Runtime DB、Fact DB、业务索引库 | `capabilities/persistence/database/`、`infrastructure/stores/` | `domains/{domain}/policies/` 定义 key 和幂等语义 |
| 文件存储 | MinIO、S3、本地 object store | `capabilities/persistence/object_storage/`、`infrastructure/stores/` | `domains/{domain}/projections/` 只引用 artifact，不直连 store |
| 消息通道 | 飞书、钉钉、Discord、邮件、Webhook | `capabilities/channels/{channel}/` | `domains/{domain}/projections/` 定义消息内容 |
| 外部 API 节流 | FastMoss、Feishu、TikTok request、Webhook 的 request pacing | `infrastructure/rate_limit/` + provider client | `domains/{domain}/policies/` 只声明业务允许的覆盖范围 |
| Agent runtime | OpenClaw、Hermes、用户 agent workspace | `skills/{skill_code}/` | skill 只保留入口说明和固定输入配置 |

外部 API 节流是基础设施能力，不是 workflow 局部 helper。系统默认对同一业务 job 内同一 provider/resource key 的连续 request 应用可配置随机 pacing，默认区间为 `0.5s` 到 `1.0s`。默认值必须能通过 runtime config / env 覆盖: 全局键使用 `api_request_delay_min_seconds`、`api_request_delay_max_seconds` 或 `MUJITASK_API_REQUEST_MIN_DELAY_SECONDS`、`MUJITASK_API_REQUEST_MAX_DELAY_SECONDS`；provider 级覆盖使用 `fastmoss_api_request_delay_min_seconds`、`feishu_api_request_delay_min_seconds`、`tiktok_api_request_delay_min_seconds` 等同名 max 配置。业务 payload 可以显式收紧或放宽，但必须保留在 job result / runtime evidence 中。

## 6. Workflow 开发契约

一个 workflow 业务需求进入开发阶段时，必须按下面顺序拆分。

1. Agent 配置
   在 `skills/{skill_code}` 定义触发说明、入口脚本、固定输入配置模板。这里调用 task request submit，只返回 `request_id`、首条状态和用户可读摘要。

2. Task Request 入口
   在 `domains/{domain}/tasks/{task_code}` 定义顶层业务入口参数、校验和业务可见名称；由 `control_plane/task_requests` 负责 submit/status/result/cancel envelope。

3. Workflow 编排
   在 `domains/{domain}/workflows/{workflow_code}` 定义 stage、job DAG、依赖、终态规则、summary contract 和 outbox 触发点。

   复杂 workflow 的运行推进实现放入同名 `domains/{domain}/flows/{workflow_code}/` package。当前已完成包化的 TikTok top-level workflow 包括 `search_keyword_selection_products`、`search_keyword_competitor_products`、`refresh_current_competitor_table`、`sync_tk_influencer_pool` 和 `tiktok_fastmoss_product_ingest`。包内 `orchestrator.py` 只负责 stage dispatch，`stages/{stage_code}.py` 负责单 stage 推进，`context/**` 只提供 runtime views / stage inputs / decision models / summary inputs，`summary.py` 负责最终 summary/result/outbox payload。

4. Job Contract
   在 `domains/{domain}/jobs/{job_code}` 定义可执行单元，绑定 capability handler，例如 `feishu_table_read`、`fastmoss_product_fetch`、`fact_bundle_upsert`。业务复合 job 的 runtime adapter 也放在 domain job 文件中，并进入 domain flow；Job 可以引用 mapper/projection/policy code，但不能实现 transport。

5. 输入数据源
   飞书、钉钉表格这类业务输入源放 `capabilities/input_sources/{source}`；表级筛选、字段解释、行到业务对象转换放 `domains/{domain}/mappers`。

6. 事实数据源
   TikTok、FastMoss、AWS 等事实来源放 `capabilities/fact_sources/{source}`；事实标准化、去重、可信度、落库 key 规则放 `domains/{domain}/policies` 或 `domains/{domain}/mappers`。

7. 数据库与文件存储
   DB / Object Store capability 放 `capabilities/persistence`。Runtime DB public 入口是 `infrastructure/runtime/runtime_store.py` 的 `RuntimeStore` façade，具体 table owner 放 `infrastructure/runtime/repositories/**`，read model 放 `infrastructure/runtime/queries/**`；Fact DB / MinIO / S3 技术实现放 `infrastructure/facts`、`infrastructure/artifacts` 或对应 store/client。业务不得直接绕过 store 写底层 client。

8. 输出通道
   飞书写回字段放 `domains/{domain}/projections`；飞书、钉钉、Discord 通知通道放 `capabilities/channels/{channel}`。所有最终通知必须通过 outbox，workflow 不直接调用通道 API。

9. 运行控制
   daemon、Watchdog、Reconciler、Execution Supervisor 统一归 `control_plane`，不能引入业务专用 fork。业务差异只能通过 workflow、job、policy、mapper、projection 注入。

每个 workflow 必须同时维护一份 Workflow Architecture Manifest:

```text
contracts/workflow/{workflow_code}.yaml
```

Manifest 是开发阶段的结构契约，必须声明 `workflow_origin`、agent artifact、task、workflow、job、handler capability、mapper、projection、policy、outbox 的真实 module 和 export。`workflow_origin` 为 `new_workflow` 时不允许存在未落文件的 mapper / projection / policy；迁移旧 workflow 时可以暂时使用 `migrated_existing`，但必须把未拆出的项写入 `known_architecture_gaps`，作为后续真实迁移清单。

开发完成前必须能回答:

- 这个需求新增了哪个 `skill_code`、`task_code`、`workflow_code`、`job_code`。
- 哪些能力是通用 capability，哪些逻辑是 domain mapper / projection / policy。
- 哪些配置属于 runtime，哪些属于 agent skill，哪些属于 deployment。
- 哪些输出走 outbox，哪些数据沉淀到 database 或 object storage。
- Watchdog、Reconciler、Execution Supervisor 是否需要新增通用规则；如果只是业务差异，不得改控制面。

## 7. 业务示例

需求: 从飞书竞品表读取商品，采集 TikTok / FastMoss 事实，写回飞书并通知 Discord。

正式落位:

```text
skills/competitor-refresh/
  SKILL.md
  run_refresh_step.sh
  skill.env.example

src/{project}/domains/tiktok/
  tasks/refresh_competitor_table.py
  workflows/refresh_competitor_table.py
  jobs/read_competitor_rows.py
  jobs/fetch_product_facts.py
  jobs/upsert_product_facts.py
  jobs/write_competitor_projection.py
  jobs/send_completion_notice.py
  mappers/feishu_competitor_row_mapper.py
  projections/feishu_competitor_projection.py
  policies/product_selection_policy.py
  policies/refresh_idempotency_policy.py

src/{project}/capabilities/input_sources/feishu/
  table_read_handler.py

src/{project}/capabilities/fact_sources/tiktok/
  product_fetch_handler.py

src/{project}/capabilities/fact_sources/fastmoss/
  product_fetch_handler.py

src/{project}/capabilities/persistence/database/
  fact_bundle_upsert_handler.py

src/{project}/capabilities/channels/feishu/
  table_write_handler.py

src/{project}/capabilities/channels/discord/
  message_dispatch_handler.py
```

拆分规则:

- 读取飞书表是通用能力，属于 Feishu input source handler。
- 哪些行要处理、字段如何变成商品请求，属于业务 mapper。
- TikTok / FastMoss 获取商品事实，属于 fact source capability。
- 事实如何合并、去重、落库，属于业务 policy + persistence capability。
- 写回飞书哪些字段，属于 projection。
- 发 Discord / 飞书 / 钉钉通知，必须走 outbox channel，不由 workflow 直接调用外部通知 API。
- 超时、heartbeat、重试、父子任务汇总、卡住修复，全部由 control plane 处理。

## 8. 禁止规则

- 禁止在 `apps/**` 中导入 domain mapper、projection、policy 或外部系统业务字段常量。
- 禁止在 `control_plane/**` 中写 Feishu 表字段、FastMoss 商品筛选、TikTok 页面业务策略。
- 禁止为单个业务新增专用 daemon、专用 Watchdog、专用 Reconciler 或专用 Execution Supervisor。
- 禁止让 workflow 直接调用 Dingding / Discord / Feishu 通知 API 绕过 outbox。
- 禁止让 agent skill 直接消费 `api_worker_job`、`task_execution` 或 `notification_outbox`。
- 禁止把 input source adapter、fact source mapper、projection、policy 注册成 runtime handler code。
- 禁止在 capability handler 中写业务域专属筛选、字段投影或终态判定。
- 禁止在 infrastructure client / store 中引用 task_code、workflow_code、job_code。
- 禁止把 secret 真值提交进 `config/**` 或 `skills/**`。

## 9. 真实迁移验收口径

迁移正式结构时，必须先声明迁移模式。

| 模式 | 允许行为 | 禁止行为 | 完成标准 |
| --- | --- | --- | --- |
| `scaffold` | 建目录、建空模块、写文档、写迁移计划 | 宣称完成真实迁移 | 只证明正式落位已预留 |
| `real_migration` | 把实现所有权移到项目目录，用旧实现作阅读参考和功能对照 | facade、shim、re-export、`sys.modules` alias、旧路径继续承载主实现 | runtime import 主路径只从项目目录加载真实实现 |

当用户要求“真实代码迁移”“不要兼容旧逻辑”“旧逻辑只作为参考”时，必须按 `real_migration` 执行。

`real_migration` 的硬规则:

- `capabilities/**/{capability}_handler.py` 必须拥有该 capability 的 handler 实现，文件内应出现主要 `def` / `class` / helper 逻辑；不能只 `from .implementations import xxx_handler`。
- 禁止新增或保留 `capabilities/_implementations/*.py` 作为大杂烩实现归属；大文件只能作为迁移前参考，不允许成为 runtime import 主路径。
- 完成迁移后旧路径应删除，或仅保留不被 runtime registry 引用的迁移说明。
- `domains/{domain}/**` 必须拥有业务 task、workflow、job、mapper、projection、policy 的实现。
- 根包 daemon alias 只能作为单次提交内的临时过渡；完成 `real_migration` 时 console script 和根包入口应直接指向 `apps/**`。
- Monkeypatch、旧 import、旧测试不作为保留兼容层的理由；旧测试应迁移到正式 import，旧代码只作为功能验证参考。
- “跑通旧测试”不是 `real_migration` 完成标准；完成标准是静态归属、runtime import 主路径和行为对照同时满足。

真实迁移期间的验证策略:

- 可以先不跑全量测试，避免为了旧测试继续保留兼容层。
- 优先使用静态结构检查、import ownership 检查和新旧行为对照 fixture。
- 如果需要测试，测试必须改成正式路径导入；不得为了测试 monkeypatch 旧路径而保留 alias。

## 10. 测试护栏

项目架构契约由以下测试守住:

| 测试 | 守护内容 |
| --- | --- |
| `test_project_architecture_contract` | 本文存在，并包含 `apps`、`control_plane`、`domains`、`capabilities`、`infrastructure`、`config`、`skills` 的边界 |
| `test_workflow_development_contract` | 新 workflow 开发必须拆到 task、workflow、job、mapper、projection、policy、capability、outbox、runtime control |
| `test_control_plane_boundary` | RPC/CLI/daemon/control_plane 只管 request lifecycle、supervisor、reconciler、watchdog、outbox，不承载业务 mapper/projection |
| `test_capability_boundary` | Feishu / Dingding / TikTok / FastMoss / AWS / database / object storage / channel 等能力按角色分类 |
| `test_real_migration_contract` | 真实迁移禁止 facade、shim、re-export、`sys.modules` alias 和 `_implementations` 大杂烩 |
| `test_workflow_architecture_manifests` | 每个 workflow 都有 `contracts/workflow/{workflow_code}.yaml`，并能对上真实 task/workflow/job/capability/custom logic 导出 |
| `test_agent_artifact_boundary` | skill/script 是部署给 OpenClaw / Hermes / 用户 agent workspace 的入口产物，只提交 task request |

## 11. 默认假设

- 一个业务域可以有多个 workflow 和多个 agent skill。
- handler 按能力命名，不按具体业务命名。
- mapper、projection、policy 按业务域命名。
- Reconciler 是 control plane 的稳定组件；未来可以独立 daemon 化，但不能业务专用 fork。
- Agent script / skills 是部署产物源，部署时复制到用户 agent workspace 并生成配置文件。
- 当前仓库迁移到正式结构可以分阶段进行，但每个阶段必须明确是 `scaffold` 还是 `real_migration`。只有 `real_migration` 才能被描述为“迁移完成”。
