# Git 分支管理规范

更新时间: 2026-04-29

本文定义日常开发分支、提交和合并规则。正式发布流程见 [release-flow.md](../ops/release-flow.md)。

## 1. 默认分支

- 默认主分支: `main`
- `main` 保持可运行、可部署
- 不直接在 `main` 上提交功能代码

## 2. 分支命名

日常开发统一按类型命名:

```text
feature/<topic>
fix/<topic>
docs/<topic>
refactor/<topic>
chore/<topic>
```

示例:

```text
feature/tiktok-product-slider-verification
fix/feishu-sync-retry
docs/add-local-development-guide
refactor/workflow-common-helper
chore/update-dependencies
```

## 3. 提交流程

1. 从最新 `main` 创建功能分支: `git switch -c feature/<topic>`
2. 在功能分支完成代码和文档修改
3. 运行必要测试: `uv run --extra dev pytest`
4. 提交 commit
5. 根据当前 origin 远端平台创建 PR / MR:
   - GitHub 远端: 创建 Pull Request (PR)
   - GitLab 远端: 创建 Merge Request (MR)
6. 合并回 `main`

## 4. Commit Message 建议

使用 `<type>: <summary>` 格式。

常见类型:

| 类型 | 说明 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修复问题 |
| `docs` | 文档更新 |
| `refactor` | 重构 |
| `test` | 测试 |
| `chore` | 构建、依赖、配置等维护任务 |

示例:

```text
docs: add local development guide
fix: handle feishu table write retry
refactor: align tiktok workflow job definitions
```

## 5. 发布规则

正式发布不在本文展开，统一遵守 [release-flow.md](../ops/release-flow.md)。

核心原则:

- 不在 `main` 直接提交功能代码
- 先创建功能分支
- 通过 PR / MR 合并
- release tag 只能打在合并后的 `main` 最新提交上
