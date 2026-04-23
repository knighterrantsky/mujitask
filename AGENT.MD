# Mujitask agent rules

这个仓库是 Mujitask 当前业务项目仓库，用于运行 TikTok / FastMoss / 飞书自动化任务。

## Start Here

按这个顺序读取：

1. `.platform/platform-manifest.yaml`
2. `.platform/model-rules.yaml`
3. `AGENT.MD`
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

普通业务实现不要直接修改：

- `.platform/**`
- `src/automation_business_scaffold/agent.py`
- `src/automation_business_scaffold/registry.py`
- framework 依赖内部代码

`AGENT.MD` 是仓库级协作规则。只有用户明确要求仓库治理变更时才修改，并且不要和普通业务实现混在一起提交。

## Schema And Contract Rules

Runtime DB schema、Fact DB schema、workflow contract、handler contract、入口/输出 contract 是受控契约。

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

当用户说“提交代码并发布”时，固定按下面顺序执行：

0. 先读取 `origin` 远端地址，识别当前仓库是 GitLab 还是 GitHub
1. 如果当前分支是 `main`，先创建并切换到新的功能分支
2. 在功能分支提交代码
3. 推送分支
4. 创建 MR
5. 合并到 `main`
6. `git switch main`
7. `git pull --ff-only origin main`
8. 只在 `main` 最新提交上打 tag
9. 推送 tag
10. 使用 Markdown 文件创建或更新 release

Git 托管识别规则：

- 优先读取 `git remote get-url origin`
- 如果远端是 GitLab，本仓库发布流程默认走 GitLab MR + GitLab release
- 如果远端是 GitHub，才走 GitHub PR / GitHub release
- 不要在未识别远端托管类型前就跳过 MR / PR 创建

功能分支规则：

- 不要直接在 `main` 上提交功能代码
- 默认使用新分支承载当前改动
- 分支命名优先使用 `codex/<topic>`

Token 规则：

- GitLab 流程优先读取：
  - `GITLAB_TOKEN`
  - `GITLAB_API_TOKEN`
- 在 Windows 上，如果当前 shell 没有这些变量，发布脚本会继续尝试读取用户级和机器级环境变量中的同名 token
- GitHub 流程优先读取：
  - `GITHUB_TOKEN`
  - `GH_TOKEN`
- 如果自动读取不到所需 token，不能直接停止
- 必须明确提示用户输入对应平台 token，或说明当前缺少哪个 token 才无法继续
- 只完成“提交代码”但没有继续创建 MR / PR 的情况，不算“提交代码并发布”完成

禁止做法：

- 不要在功能分支上打正式 release tag
- 不要在 MR 未合并前先发正式 release
- 不要用带字面量 `\n` 的单行字符串拼 release notes

release notes 规则：

- 必须使用 Markdown 文件作为输入
- 至少包含：
  - `## Summary`
  - `## Source`
  - `## Testing`
- release 页面必须正常渲染 Markdown 标题、列表和代码样式
- release 页面中不能出现字面量 `\n`

仓库内发布脚本：

- `scripts/release/publish-gitlab-flow.ps1`
- `scripts/release/publish-gitlab-release.ps1`
- `scripts/release/release-notes.template.md`

GitLab 仓库的执行要求：

- 创建 MR 是必做步骤
- 不允许在本地通过 `git merge`、`git pull` 合并功能分支来代替服务器 MR 合并
- 必须在 GitLab 服务器上的 MR 页面或 GitLab 服务端 API 完成合并
- MR 合并到 `main` 后才能打正式 tag
- release 必须在 `main` 最新提交上创建
- 如果当前会话缺少 GitLab token，先提示用户输入，再继续后续步骤
- 只要用户要求“提交代码并发布”，优先使用 `scripts/release/publish-gitlab-flow.ps1` 走完整链路，而不是只做到提交代码

标准调用方式：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release\publish-gitlab-release.ps1 -TagName vX.Y.Z -ReleaseNotesFile .\scripts\release\release-notes.template.md
```

GitLab 全流程调用方式：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release\publish-gitlab-flow.ps1 -TagName vX.Y.Z
```

更新已有 release 时：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\release\publish-gitlab-release.ps1 -TagName vX.Y.Z -ReleaseNotesFile .\scripts\release\release-notes.template.md -UpdateExisting
```
