# Release Flow

更新时间: 2026-04-24

状态: Ops 执行规则

## 1. 定位

本文是仓库发布流程的事实来源。当用户说“提交代码并发布”时，按本文执行完整链路。

`AGENTS.md` 只保留发布流程的高优先级摘要；详细步骤、token 规则、脚本调用和禁止事项维护在本文中。

## 2. 标准流程

当用户说“提交代码并发布”时，固定按下面顺序执行：

0. 先读取 `origin` 远端地址，识别当前仓库是 GitLab 还是 GitHub
1. 如果当前分支是 `main`，先创建并切换到新的功能分支
2. 在功能分支提交代码
3. 推送分支
4. 创建 MR / PR
5. 合并到 `main`
6. `git switch main`
7. `git pull --ff-only origin main`
8. 只在 `main` 最新提交上打 tag
9. 推送 tag
10. 使用 Markdown 文件创建或更新 release

## 3. Git 托管识别规则

- 优先读取 `git remote get-url origin`。
- 如果远端是 GitLab，本仓库发布流程默认走 GitLab MR + GitLab release。
- 如果远端是 GitHub，才走 GitHub PR + GitHub release。
- 不要在未识别远端托管类型前跳过 MR / PR 创建。

## 4. 功能分支规则

- 不要直接在 `main` 上提交功能代码。
- 默认使用新分支承载当前改动。
- 分支命名按类型：`feature/<topic>`、`fix/<topic>`、`docs/<topic>`、`refactor/<topic>`、`chore/<topic>`。

## 5. Token 规则

GitLab 流程优先读取：

- `GITLAB_TOKEN`
- `GITLAB_API_TOKEN`

在 Windows 上，如果当前 shell 没有这些变量，发布脚本会继续尝试读取用户级和机器级环境变量中的同名 token。

GitHub 流程优先读取：

- `GITHUB_TOKEN`
- `GH_TOKEN`

如果自动读取不到所需 token，不能直接停止。必须明确提示用户输入对应平台 token，或说明当前缺少哪个 token 才无法继续。

只完成“提交代码”但没有继续创建 MR / PR 的情况，不算“提交代码并发布”完成。

## 6. 禁止做法

- 不要在功能分支上打正式 release tag。
- 不要在 MR / PR 未合并前先发正式 release。
- 不要用带字面量 `\n` 的单行字符串拼 release notes。
- 不要在 GitLab 仓库中用本地 `git merge` 或 `git pull` 合并功能分支来代替服务器 MR 合并。

## 7. Release Notes 规则

- 必须使用 Markdown 文件作为输入。
- 至少包含：
  - `## Summary`
  - `## Source`
  - `## Testing`
- release 页面必须正常渲染 Markdown 标题、列表和代码样式。
- release 页面中不能出现字面量 `\n`。

## 8. GitLab 流程专用脚本

以下脚本仅用于 GitLab 远端:

- `scripts/release/publish-gitlab-flow.ps1`
- `scripts/release/publish-gitlab-release.ps1`
- `scripts/release/release-notes.template.md`

## 9. GitHub 流程说明

GitHub 远端使用 GitHub PR + GitHub Release:

- 创建 PR 并合并到 `main`。
- 使用 GitHub UI 或 `gh` CLI 创建 release。
- 当前仓库未提供 GitHub release 自动化脚本时，应使用 GitHub UI 或 `gh release create` 创建 release。

## 10. GitLab 仓库执行要求

- 创建 MR 是必做步骤。
- 必须在 GitLab 服务器上的 MR 页面或 GitLab 服务端 API 完成合并。
- MR 合并到 `main` 后才能打正式 tag。
- Release 必须在 `main` 最新提交上创建。
- 如果当前会话缺少 GitLab token，先提示用户输入，再继续后续步骤。
- 只要用户要求”提交代码并发布”，优先使用 `scripts/release/publish-gitlab-flow.ps1` 走完整链路，而不是只做到提交代码。

## 11. GitLab 脚本调用示例

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
