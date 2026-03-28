# Public Migration Guide

这份文档只面向业务仓库升级 framework 时使用。

## 1. 当前基线

当前业务接入基线：

- framework version: `0.2.1`
- reference commit: `55e8223a92f562f4053006c55e66fe5491c9be61`

业务仓库当前推荐的依赖方式：

- 使用 git tag/commit pin framework
- 用公开 contract 文档和 scaffold 接入
- 不把 framework 源码实现细节当成升级依据

重要约束：

- 业务仓库和 scaffold 的正常安装不能依赖本机存在 `../automation-framework`
- 同级目录下的本地 framework checkout 只属于平台联调覆盖模式，不属于业务默认安装前提

## 2. 当前版本的升级建议

如果你是第一次接入业务仓库：

1. 先从 `automation-business-scaffold` 初始化新仓库
2. 保留 scaffold 自带的 `AGENT.MD`、`.platform/*` 和 vendored contract docs
3. 再在业务可编辑区内开始开发

如果你已经有业务仓库，想升级到当前基线：

1. 对比新的 `docs/framework_contract/<framework_version>/...`
2. 重点检查 `public-capability-status.md`
3. 再检查 `business-consumption-contract.md`
4. 如果 workflow 接入方式变化，再检查 `workflow-runtime-contract.md`

## 3. 标准模式与本地覆盖模式

### 标准模式

这是业务仓库与 scaffold 的默认工作模式：

- 只 clone 当前业务仓库或 scaffold 仓库
- 通过 `pyproject.toml` 中 pin 的 git 依赖安装 framework
- 不要求机器上有任何固定相对路径的 framework 源码目录

### 本地覆盖模式

这是平台维护者联调时的可选模式：

```bash
pip install -e ../automation-framework
```

用途：

- 用本地未发布的 framework 代码覆盖当前 pin 的远程依赖
- 验证 scaffold 是否已经兼容新的 framework 改动

这不是业务默认前提，也不应写成“必须存在的目录结构”。

## 4. 当前没有要求的迁移

在当前基线下，业务仓库不需要做下面这些迁移：

- 不需要从 `BaseWorkflowTask` 迁移到 `workflow.yaml` loader
- 不需要接 replay API
- 不需要适配 LLM repair

原因：

- 这些能力当前还没有进入业务公开 contract
- 它们在成熟前不会被要求作为业务仓库的默认依赖

## 5. 升级时必须检查的边界

每次升级 framework 或 scaffold 时，业务仓库都要检查：

- 公开 import 面是否变化
- capability 状态是否从 `beta` 变为 `ga` 或从 `ga` 进入 deprecation
- `run_mode` / `effects` 约束是否变化
- `workflow_draft` 的 review-only 定位是否变化
- scaffold 的平台受保护区是否需要同步

## 6. 受控升级顺序

推荐顺序固定为：

1. 平台升级 framework
2. 平台更新 scaffold 与 contract doc pack
3. 业务仓库再升级自己的 pinned 依赖与平台受保护区

不要跳过 scaffold 直接让业务仓库自行猜测 framework 变化。

## 7. 升级后验收

业务仓库升级完成后，至少验证：

- agent 可以正常启动
- `/tasks` 能列出业务 task
- 一个 demo 或 smoke workflow 能通过 `/runs` 成功执行
- `draft` 模式下的 `submit` effect 仍会被 runtime 阻止
- vendored contract docs 与业务仓库记录的 framework 版本一致
