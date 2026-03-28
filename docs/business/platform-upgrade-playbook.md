# Platform Upgrade Playbook

这份文档说明两件事：

1. framework 更新后，平台如何更新 scaffold
2. 业务仓库如何从新的 scaffold 基线吸收升级

## 1. 目录结构不是升级前提

先固定一个最重要的规则：

- `automation-business-scaffold` 必须能独立工作
- 标准模式下，不应依赖本机存在 `../automation-framework`
- 同级目录的 framework checkout 只是平台联调便利

因此以后任何升级流程都应以“版本化依赖 + vendored contract docs”为准，而不是以本地路径为准。

## 2. 平台如何从 framework 更新到 scaffold

推荐顺序固定为：

1. 在 `automation-framework` 完成代码与 contract 文档更新
2. 确认新的 framework version、tag 或 commit
3. 在 `automation-business-scaffold` 开升级分支
4. 更新 `pyproject.toml` 中的 pinned framework 依赖
5. 更新 `.platform/platform-manifest.yaml`：
   - `framework_version`
   - `framework_commit`
   - `public_contract_pack_version`
6. 刷新 `docs/framework_contract/<framework_version>/...`
7. 如有需要，更新：
   - `README.md`
   - `AGENT.MD`
   - `.platform/model-rules.yaml`
   - demo `tasks/`
   - demo `workflows/`
   - `tests/`
8. 运行 scaffold 自己的验证
9. 发布新的 scaffold 版本/tag

## 3. 平台联调时怎么用本地 framework

如果平台维护者在同一个 workspace 下同时有：

- `automation-framework`
- `automation-business-scaffold`

可以临时这样覆盖远程依赖：

```bash
pip install -e ../automation-framework
```

这个模式只适用于：

- 验证 scaffold 是否兼容新的 framework 改动
- 在 framework 尚未发布时做本地联调

注意：

- 它不会改变标准发布模型
- 不能要求业务方也采用同样的目录结构

## 4. 业务仓库如何吸收 scaffold 升级

业务仓库是从 scaffold 初始化出来的独立仓库，所以升级不走“强绑定自动同步”。

推荐顺序：

1. 先确认当前业务仓库记录的基线
   - 当前 `framework_version`
   - 当前 `framework_commit`
   - 当前 `scaffold_version`
2. 查看新 scaffold 的：
   - `.platform/platform-manifest.yaml`
   - `docs/framework_contract/<framework_version>/...`
   - `README.md`
   - `AGENT.MD`
3. 只同步 platform-managed 区域
4. 如有必要，再更新业务仓库自己的 pinned framework 依赖
5. 保留业务自维护目录不被覆盖：
   - `tasks/`
   - `workflows/`
   - `mappers/`
   - `validators/`
   - `flows/`
6. 运行业务仓库自己的 smoke test

## 5. 业务仓库升级时的检查清单

至少检查：

- framework pinned 依赖是否更新
- vendored contract docs 是否对应新版本
- `public-import-surface` 是否变化
- `public-capability-status` 是否变化
- `run_mode` / `effects` 约束是否变化
- platform-managed 目录是否需要同步
- demo 壳层是否需要迁移到自己的业务壳层

## 6. 推荐记录方式

每次业务仓库升级后，建议在自己的 `docs/business/upgrade-notes.md` 里记录：

- 旧版本基线
- 新版本基线
- 同步了哪些 platform-managed 文件
- 哪些业务目录刻意未同步
- 验证结果
