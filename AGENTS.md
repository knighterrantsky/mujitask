# Mujitask agent rules

这个仓库是 Mujitask 当前业务项目仓库，用于运行 TikTok / FastMoss / 飞书自动化任务。

## Start Here

按这个顺序读取：

1. `.platform/platform-manifest.yaml`
2. `.platform/model-rules.yaml`
3. `AGENTS.md`
4. `README.md`
5. `docs/README.md`

## Repo Role

- 当前仓库是业务项目，不是 `automation-framework` 源码仓库。
- framework public API、contract 和迁移说明以已安装的 `automation-framework` package 或 framework 仓库自身文档为准。
- 本仓库不维护 framework contract 本地副本。
- 标准安装模式下，不假设本机存在 `../automation-framework`；同级目录本地 framework checkout 只属于平台联调覆盖模式。
- 当前业务入口、workflow、Runtime/Fact/Storage 设计以 `README.md` 和 `docs/README.md` 下的文档索引为准。

## Root Conversational Mode

用户通常会在 Codex app 的仓库根目录直接输入自然语言需求。不要要求用户记住文档路径、contract 文件名或当前重构阶段。

每次任务先自动判断任务类型:

- 普通业务实现
- 重构治理
- 架构契约变更
- 字段 / 状态 / workflow contract 变更
- legacy reference 查询
- 部署 / 运维任务

Codex 必须根据仓库内的 contracts 和 tests 自动选择最小上下文。默认先读取 `contracts/harness/code-roadmap.yaml`、相关 contract、当前源码和当前测试；只有任务触发条件需要时，才展开长 business / arch 文档。

## Architecture Owners

当前项目采用分层架构，代码归属如下：

- `src/automation_business_scaffold/domains/**` 是新业务实现 owner。
- `src/automation_business_scaffold/capabilities/**` 是通用 handler 能力 owner。
- `src/automation_business_scaffold/control_plane/**` 是 runtime 控制面 owner。
- `contracts/**` 是字段、状态、workflow 和 Codex 路由的机器契约 owner。

## Change Boundaries

普通业务实现可以修改业务代码、测试和项目文档，但要遵守事实来源边界：

- 系统架构、workflow、Runtime DB、Fact DB、Storage、handler contract: `docs/arch/**`
- 客户需求、业务规则、飞书字段口径、验收口径: `docs/business/**`
- 开发、调试、代码维护、文档治理: `docs/dev/**`
- 部署、验收、回退、runbook: `docs/ops/**`
- 外部接口研究、页面分析、字段样例: `docs/reference/**`
- 领域阅读路线: `docs/domains/**`
- 字段、状态、workflow 机器契约: `contracts/**`

普通业务实现不要直接修改：

- `.platform/**`
- `src/automation_business_scaffold/agent.py`
- `src/automation_business_scaffold/registry.py`
- framework 依赖内部代码

`AGENTS.md` 是仓库级协作规则。只有用户明确要求仓库治理变更时才修改，并且不要和普通业务实现混在一起提交。

`docs/business/requirements-backlog.md` 只是需求候选池，不能作为实现事实来源。只有 backlog item 被提升到 `docs/business/requirements/*.md` 后，才允许按正式需求实现。

## Completion Claim Gate

实现类、重构类、治理类任务都不能仅凭“已修改代码”声明完成。

声明 complete 前，必须识别 `feature_code`，并通过 `contracts/harness/code-roadmap.yaml` 中对应的 `done_gate`。本地静态 gate 入口是:

```bash
python scripts/harness/claim_done.py <feature_code>
```

如果 `done_gate` 不存在、未运行或失败，只能声明 `not complete` 或 `blocked`。禁止使用“已完成 / 实现完成 / 符合设计”这类结论，除非 gate 明确通过。

## Architecture Drift Gate

架构边界、统一能力和事实沉淀相关需求必须先收敛到设计事实来源，避免用局部 helper 把系统继续扩张。

Design-first rule:

- 当用户需求涉及“统一能力 / 所有流程 / 事实数据 / Fact DB / MinIO / mapper / projection / 架构边界 / 保持简单 / 不要新增 helper”时，默认先更新 docs / contracts / tests。
- 除非用户明确要求实现代码，否则不要先改业务代码。
- 若缺少对应 contract，不允许声明 `complete`。

No Helper Sprawl rule:

- 默认禁止为了解决局部问题新增 `helper` / `service` / `manager` / `coordinator` / `collection` / `collector` / `orchestrator` 等抽象模块。
- 如果确实需要新增抽象，必须先在 `contracts/harness/architecture-ownership.yaml` 或相关 contract 中声明 owner、职责边界、允许调用方、禁止调用方、为什么现有 owner 不能承接。
- 没有 contract 的新抽象不能通过 completion gate。

Existing owner first rule:

- 优先修改现有 handler / mapper / projection / ingestion / media sync / fact bundle upsert。
- 不允许通过新增旁路 helper 绕开现有架构 owner。

Stop rule:

- 如果用户明确指出“先文档约束，不要实现”，Codex 必须停止在 docs / contracts / tests 层，不得继续实现业务代码。

## Schema And Contract Rules

Runtime DB schema、Fact DB schema、workflow contract、handler contract、入口/输出 contract 是受控契约。字段、状态和 workflow 的可检查事实优先落在 `contracts/**`，Markdown 负责解释背景和边界。

必须遵守：

- 生产 daemon / worker / dispatcher / watchdog 只能使用 runtime DB user，不能拥有 DDL 权限。
- `CREATE TABLE`、`ALTER TABLE`、`DROP TABLE`、`CREATE INDEX` 只能由 migration 流程使用 migration user 执行。
- 生产应用启动时可以检查 schema / migration version；版本不匹配时应 fail fast，不继续 claim job。
- 本地开发或首次 bootstrap 可以保留建表便利能力，但不能成为生产任务消费路径。
- 新增 schema 字段必须说明默认值、旧数据兼容、回滚方式和旧 worker 影响。
- 修改 upsert key、dedupe key、状态枚举、handler payload/result/error 外壳必须同步更新 contract 文档并说明兼容策略。
- 破坏性 contract 变更要使用 `contract_revision`、migration、adapter 或新语义 handler，不要把 `v1` / `v2` 写进稳定 code 名称，也不要直接让旧 job 无法执行。

相关文档：

- `docs/dev/documentation-change-policy.md`
- `docs/arch/runtime-db-schema-design.md`
- `docs/arch/fact-db-schema-design.md`
- `docs/dev/workflow-design-guidelines.md`
- `docs/arch/handler-contract-design.md`
- `docs/arch/entry-output-contract-design.md`
- `contracts/README.md`

## Framework Boundary

业务代码默认只依赖 framework 公开入口：

- `automation_framework.agent.server`
- `automation_framework.core`
- `automation_framework.runtime`

不要在业务代码中直接依赖 framework 内部实现模块，例如：

- `automation_framework.browser.*`
- `automation_framework.clients.*`
- `automation_framework.runtime.engine`
- `automation_framework.runtime.validators`
- `automation_framework.selftest.*`

## Workflow Rules

- 顶层业务入口是 Task。
- Workflow 负责编排 Stage。
- Job 是 Runtime DB 中 worker 可 claim、retry、timeout、审计的运行时执行单元。
- Handler 是 job 的代码入口。
- Flow 是 handler 内部可复用的业务实现过程。
- `api_worker` / `browser_worker` 是业务无关执行层，不应直接理解完整业务流程。
- `run_mode` / `effects` 的解释以 `automation-framework` package 或 framework 仓库自身文档为准。
- 业务逻辑优先放在 `domains/{domain}/tasks/`、`workflows/`、`jobs/`、`flows/`、`mappers/`、`projections/`、`policies/` 或对应 capability adapter 中。

## Stop Protocol

默认所有实现、修复、重构和治理任务都是 bounded task。

实现类或重构类任务完成后只输出:

- `Status: complete / not complete / blocked`
- `Files changed`
- `Checks run`
- `Failed gates or blockers`

禁止输出:

- 下一步建议
- 后续可以继续
- 建议再做某某优化
- 顺手扩展范围
- 与本任务无关的 roadmap

只有当用户明确要求“规划、路线图、下一步、还有什么要优化”时，才可以输出后续建议。

## Escalation

如果需求必须修改 `.platform/**`、framework 接入边界、生产数据库权限模型或受控 contract，先明确这是仓库治理、platform upgrade 或 schema/contract 变更，再单独处理。

## Release Rules

当用户说“提交代码并发布”时，按 `docs/ops/release-flow.md` 执行完整流程。

核心约束:

- 先识别 `origin` 是 GitLab 还是 GitHub，再选择 MR / PR 与 release 流程。
- 不在 `main` 直接提交功能代码；默认按类型命名分支：`feature/<topic>`、`fix/<topic>`、`docs/<topic>`、`refactor/<topic>`、`chore/<topic>`。
- 正式 release tag 必须在 MR / PR 合并后的 `main` 最新提交上创建。
- release notes 必须使用 Markdown 文件，不能用带字面量 `\n` 的单行字符串拼接。
- 缺少平台 token 时先提示用户提供，不能把“只提交代码”当成“提交代码并发布”完成。
