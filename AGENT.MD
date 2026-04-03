# automation-business-scaffold agent rules

这个仓库是业务模板仓库，默认给业务侧 Codex 使用。

## Start Here

按这个顺序读取：

1. `.platform/platform-manifest.yaml`
2. `.platform/model-rules.yaml`
3. `AGENT.MD`
4. `docs/framework_contract/0.2.1/public-capability-status.md`
5. `docs/framework_contract/0.2.1/business-consumption-contract.md`

## Repo Role

- 这个仓库是业务模板，不是 framework 源码仓库
- 业务侧不应把 framework 源码当作事实 contract
- 新业务应从这个仓库初始化为独立业务仓库后再长期开发
- 标准安装模式下，不应假设本机存在 `../automation-framework`
- 同级目录本地 framework checkout 只属于平台联调覆盖模式

## Default Working Mode

默认工作模式：`business_feature`

这意味着：

- 只在业务可编辑区工作
- 不修改 platform-managed 区域
- 不修改 vendored framework contract docs
- 不导入非白名单 framework 模块

如果任务明确涉及 framework 升级或 scaffold 基线升级，才进入 `platform_upgrade` 模式。

默认假设：

- 只存在当前 scaffold 仓库
- framework 通过 `pyproject.toml` 中 pin 的 git 依赖安装

## Protected Paths

普通业务开发不要修改：

- `.platform/**`
- `AGENT.MD`
- `docs/framework_contract/**`
- `src/automation_business_scaffold/agent.py`
- `src/automation_business_scaffold/registry.py`
- `tests/test_agent.py`
- `tests/test_contract_pack.py`

## Business Editable Paths

默认只在这些区域工作：

- `src/automation_business_scaffold/config.py`
- `src/automation_business_scaffold/tasks/**`
- `src/automation_business_scaffold/workflows/**`
- `src/automation_business_scaffold/flows/**`
- `src/automation_business_scaffold/models/**`
- `src/automation_business_scaffold/mappers/**`
- `src/automation_business_scaffold/validators/**`
- `docs/business/**`
- `tests/test_registry.py`
- `tests/test_workflow_demo.py`

## Allowed Framework Imports

只允许业务代码直接依赖这些模块：

- `automation_framework.agent.server`
- `automation_framework.core`
- `automation_framework.runtime`

默认使用这些符号：

- `create_app`
- `TaskRegistry`
- `BaseWorkflowTask`
- `FrameworkResult`
- `WorkflowSpec`
- `StepDefinition`
- `StepAction`

不要导入：

- `automation_framework.browser.*`
- `automation_framework.clients.*`
- `automation_framework.runtime.engine`
- `automation_framework.runtime.validators`
- `automation_framework.selftest.*`

## Workflow Rules

- 新业务默认走 `BaseWorkflowTask`
- `workflow_draft.review-only.yaml` 只是审核样例，不是可执行 workflow
- `run_mode` / `effects` 的解释以 vendored contract docs 为准
- 业务逻辑应放在 `tasks/`、`workflows/`、`mappers/`、`validators/`、`flows/`

## Escalation

如果需求必须修改 platform-managed 区域，先停止直接实现，再把任务明确标记为 `platform_upgrade`。

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
