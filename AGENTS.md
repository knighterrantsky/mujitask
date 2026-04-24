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
- `docs/arch/workflow-design-guidelines.md`
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
- 业务逻辑优先放在 `tasks/`、`workflows/`、`flows/`、`mappers/`、`validators/` 或对应 infrastructure adapter 中。

## Escalation

如果需求必须修改 `.platform/**`、framework 接入边界、生产数据库权限模型或受控 contract，先明确这是仓库治理、platform upgrade 或 schema/contract 变更，再单独处理。

## Release Rules

当用户说“提交代码并发布”时，按 `docs/ops/release-flow.md` 执行完整流程。

核心约束:

- 先识别 `origin` 是 GitLab 还是 GitHub，再选择 MR / PR 与 release 流程。
- 不在 `main` 直接提交功能代码；默认使用 `codex/<topic>` 功能分支。
- 正式 release tag 必须在 MR / PR 合并后的 `main` 最新提交上创建。
- release notes 必须使用 Markdown 文件，不能用带字面量 `\n` 的单行字符串拼接。
- 缺少平台 token 时先提示用户提供，不能把“只提交代码”当成“提交代码并发布”完成。
